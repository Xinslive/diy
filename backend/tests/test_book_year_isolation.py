"""年度隔离：**(账套, 年度)** 这一对才是真正的记账单位。

**这份文件钉的是用户要的那件事本身。**

用户报的问题：往一个 2027 年**一张凭证都没有**的账套里导入科目表，
被提示「账套已有凭证或期初余额，不能覆盖科目表」—— 原因只是**上一个
年度（2026）有记账**。

旧模型里科目表是**账套级**的，闸门也只能按账套统计。这不是作者忘了加年，
而是**当时只能这么写**：覆盖导入执行的是「删掉账套的全部科目再重建」，
而那些科目正是 2026 在用的。只把闸门改成按年、放行 2027 的导入，当场就会
删掉 2026 的科目表 —— 2026 的凭证全部指向被删的科目（``entry.subject_id``
外键），账直接烂掉。

科目表下沉到年度（``book_year`` + ``subject.year``）之后，
「删本年度的科目、只数本年度的业务数据」才**同时成立**：
别的年度的科目一行都不动。

下面每一条都在钉这个「同时成立」：

1. 导入闸门**按年度**：2026 有凭证 → 2027 照样能导入（用户报的那件事）
2. 导入**只动本年度**：2027 导入之后，2026 的科目表一行都不变
3. 两个年度的科目表是**两份独立的行**：同编码、两个 id
4. 凭证按年度：2027 的凭证不能挂 2026 的科目（**库层**也挡，裸 SQL 一样挡）
5. 期初按年度：期初的科目必须与期间同年度
6. 科目树按年度：父节点必须与子节点同年度
7. 年度结转：科目表**复制**到新年度，期初**按编码**承接
8. 结转对不上就**拒绝**：宁可报错，也不让钱凭空消失
"""
from __future__ import annotations

import io
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.accounting.periods import (carry_forward_profit, ensure_periods,
                                    year_end_carry)
from app.ledger.engine import LedgerEngine
from app.models import BookYear, Period, PeriodOpening, Subject
from tests.accounting_helpers import close_any

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --------------------------------------------------------------------- 夹具


def _chart_xlsx(rows: list[tuple[str, int, str, str, str]]) -> bytes:
    """一份外部科目表：类型、级次、科目编码、科目名称、余额方向。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["类型", "级次", "科目编码", "科目名称", "余额方向"])
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _chart_2027() -> bytes:
    """一张与标准科目表**完全不同**的 2027 科目表。

    故意用另一套编码：这样「导进去的是 2027 自己的表」一眼就能看出来，
    不会与「标准表被复制了一份」混淆。
    """
    return _chart_xlsx([
        ("资产", 1, "1101", "2027库存现金", "借"),
        ("资产", 2, "110101", "2027库存现金-基本户", "借"),
        ("负债", 1, "2101", "2027短期借款", "贷"),
    ])


def _close_year(db, book, periods, admin_user):
    for m in range(1, 13):
        p = db.get(Period, periods[m].id)
        if p.locked:
            continue
        try:
            carry_forward_profit(db, book.id, p, admin_user)
        except ValueError:
            pass          # 该月没损益，跳过
        close_any(db, book, p, admin_user)


def _import_chart(client, auth, book_id: int, year: int, data: bytes):
    return client.post(
        f"/api/books/{book_id}/subjects/import?year={year}",
        files={"file": ("chart.xlsx", data, XLSX_MIME)},
        headers=auth,
    )


def _subjects_of(db, book_id: int, year: int) -> dict[str, Subject]:
    return {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book_id, Subject.year == year)
        .order_by(Subject.code)).all()}


# ------------------------------------------------- 1. 导入闸门按年度（用户报的那件事）


def test_上一年有凭证也不挡下一年的科目表导入(
        client, auth, db, book, make_voucher):
    """**用户报的那件事，一字不差地复现。**

    2026 年记了账；2027 年一张凭证都没有。往 2027 年导入科目表**必须放行**。
    旧模型里这条会被拒（闸门数的是整个账套），而那正是用户说「不符合我的
    要求」的地方。
    """
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    db.commit()

    r = _import_chart(client, auth, book.id, 2027, _chart_2027())
    assert r.status_code == 200, f"2027 年一张凭证都没有，导入却被拒了：{r.text}"
    assert r.json()["created"] == 3, r.json()


def test_有业务数据的那一年照样挡住导入(client, auth, db, book, make_voucher):
    """闸门按年度，不是「一律放行」：2026 自己有凭证，2026 就不能覆盖导入。

    少了这一条，上面那条就可能是因为闸门被拆了才通过的。
    """
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    db.commit()

    r = _import_chart(client, auth, book.id, 2026, _chart_2027())
    assert r.status_code == 409, r.text
    assert "2026" in r.text, r.text


def test_导入只动本年度_另一年的科目表一行不变(client, auth, db, book, make_voucher):
    """**这次改造最关键的一条**：覆盖导入只删本年度的科目。

    旧模型的「覆盖导入」删的是**整个账套**的科目 —— 放行 2027 的导入，
    2026 正在用的科目会被删光，挂在它们上面的凭证（``entry.subject_id``
    外键）全部指向不存在的行，账当场烂掉。
    """
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    before = _subjects_of(db, book.id, 2026)
    assert before, "前置条件不成立：2026 年应当有标准科目表"

    r = _import_chart(client, auth, book.id, 2027, _chart_2027())
    assert r.status_code == 200, r.text
    db.expire_all()

    after = _subjects_of(db, book.id, 2026)
    assert set(after) == set(before), "导入 2027 把 2026 的科目表改了"
    for code, s in before.items():
        assert after[code].id == s.id, f"2026 年的 {code} 被换成了另一行"
        assert after[code].name == s.name


def test_两个年度的科目表是两份独立的行(db, book):
    """同一个编码在两个年度是**两行不同的记录、两个 id**。

    这是「每个年度有自己的科目表」在数据上的样子：2026 的 1001 与
    2027 的 1001 之间没有任何关系，改一个动不到另一个。
    """
    from app.accounting.years import copy_chart

    copy_chart(db, book.id, 2026, 2027)
    db.commit()

    y26 = _subjects_of(db, book.id, 2026)
    y27 = _subjects_of(db, book.id, 2027)
    assert y26 and y27
    assert set(y26) == set(y27), "复制出来的科目表编码应当一致"
    for code in y26:
        assert y26[code].id != y27[code].id, f"{code} 在两个年度是同一行"
        assert y26[code].year == 2026 and y27[code].year == 2027


# ------------------------------------------------- 2. 库层护栏：年度一致性


def test_凭证不能挂别的年度的科目(db, book, make_voucher):
    """**裸 SQL 也挡得住** —— 护栏写在库里，不写在应用层（铁律 3）。

    应用层会漏：psql、脚本、未来的运维命令都绕得过去，数据库不会。
    """
    from app.accounting.years import ensure_book_year
    from app.models import Voucher

    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)
    db.commit()

    jan26 = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 1))
    # 2027 年要先登记成年度账套，科目才有地方挂（组合外键钉着这件事）
    ensure_book_year(db, book, 2027)
    db.commit()

    # 一张 2026 年的凭证，挂 2026 年的科目 —— 合法，先建起来
    db.execute(text(
        "INSERT INTO voucher (book_id, period_id, voucher_no, voucher_date, "
        "created_at, updated_at) VALUES (:b, :p, '记-900', DATE '2026-01-15', "
        "now(), now())"), {"b": book.id, "p": jan26.id})
    vid = db.scalar(select(Voucher.id).where(
        Voucher.book_id == book.id, Voucher.voucher_no == "记-900"))

    with pytest.raises(DBAPIError) as e:
        # 科目换成 **2027 年**的那一行（先建出来），凭证仍在 2026 年
        db.execute(text(
            "INSERT INTO subject (book_id, year, code, name, level, category, "
            "direction, is_leaf, ext_attrs, created_at, updated_at) "
            "VALUES (:b, 2027, '1999', '错挂的科目', 1, 'ASSET', 'DEBIT', true, "
            "'{}'::jsonb, now(), now())"), {"b": book.id})
        db.flush()
        s27 = db.scalar(select(Subject.id).where(
            Subject.book_id == book.id, Subject.year == 2027,
            Subject.code == "1999"))
        db.execute(text(
            "INSERT INTO entry (voucher_id, book_id, period_ord, line_no, "
            "subject_id, direction, amount, summary, aux_ref) "
            "VALUES (:v, :b, :ord, 1, :s, 'DEBIT', 10, 'x', '{}'::jsonb)"),
            {"v": vid, "b": book.id, "ord": jan26.ord, "s": s27})
        db.commit()
    assert "年度" in str(e.value), str(e.value)
    db.rollback()


def test_期初不能挂别的年度的科目(db, book):
    """期初快照的科目必须与它所在期间**同年度**。

    次年的期初挂在上一年的科目上，「下一年 1 月的期初 = 上一年 12 月的期末」
    这条不变量在数据层就断了，报表的年初数与总账对不齐。
    """
    from app.accounting.years import ensure_book_year

    jan26 = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 1))
    ensure_book_year(db, book, 2027)
    db.commit()
    db.execute(text(
        "INSERT INTO subject (book_id, year, code, name, level, category, "
        "direction, is_leaf, ext_attrs, created_at, updated_at) "
        "VALUES (:b, 2027, '1998', '2027 的科目', 1, 'ASSET', 'DEBIT', true, "
        "'{}'::jsonb, now(), now())"), {"b": book.id})
    db.flush()
    s27 = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2027, Subject.code == "1998"))

    with pytest.raises(DBAPIError) as e:
        db.execute(text(
            "INSERT INTO period_opening (book_id, period_id, subject_id, amount) "
            "VALUES (:b, :p, :s, 100)"), {"b": book.id, "p": jan26.id, "s": s27})
        db.commit()
    assert "年度" in str(e.value), str(e.value)
    db.rollback()


def test_子科目的父节点必须同年度(db, book):
    """科目树不能跨两张表：父节点在 2026、子节点在 2027，级次与上卷全乱。"""
    parent26 = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2026, Subject.code == "1001"))

    with pytest.raises(DBAPIError) as e:
        db.execute(text(
            "INSERT INTO subject (book_id, year, code, name, parent_id, level, "
            "category, direction, is_leaf, ext_attrs, created_at, updated_at) "
            "VALUES (:b, 2027, '100102', '跨年度挂的子科目', :p, 2, 'ASSET', "
            "'DEBIT', true, '{}'::jsonb, now(), now())"),
            {"b": book.id, "p": parent26})
        db.commit()
    assert "年度" in str(e.value), str(e.value)
    db.rollback()


def test_换年度的科目行要跟着换_不能就地改(db, book):
    """把一个科目从 2026 挪到 2027（它有下级）必须被拒。

    下级还留在旧年度，科目树就劈成两半 —— 而且这种错在界面上看不出来：
    树还能画出来，只是对不上。
    """
    child = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2026, Subject.code == "110101"))
    assert child, "前置条件不成立：标准科目表里应当有 110101（1101 的下级）"

    with pytest.raises(DBAPIError) as e:
        db.execute(text(
            "UPDATE subject SET year = 2027 WHERE id = :s"), {"s": child})
        db.commit()
    assert "年度" in str(e.value), str(e.value)
    db.rollback()


# ------------------------------------------------- 3. 年度结转：复制 + 按编码承接


def test_年度结转把科目表复制到新年度(db, book, periods, subjects, admin_user):
    """结转给新年度的是**它自己的一份**科目表，不是共用上一年的。"""
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user, 2026, 2027)
    db.expire_all()

    y26 = _subjects_of(db, book.id, 2026)
    y27 = _subjects_of(db, book.id, 2027)
    assert y27, "新年度没有自己的科目表"
    assert set(y26) == set(y27), "复制出来的科目表编码应当一致"
    assert y26["1001"].id != y27["1001"].id, "两个年度共用了同一行科目"


def test_年度结转按编码把期初承接过去(db, book, periods, subjects,
                                     admin_user, make_voucher):
    """跨年结转是「搬过两张科目表」—— **编码**是唯一稳定的对应关系。

    id 跨年根本不是一回事（2026 的 1001 与 2027 的 1001 是两行），
    按 id 搬是搬不过去的。
    """
    make_voucher("2026-03-10", dr=[("1002", 1000, "存钱")],
                 cr=[("5001", 1000, "存钱")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user, 2026, 2027)
    db.expire_all()

    jan27 = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2027, Period.month == 1))
    s27 = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2027, Subject.code == "1002"))
    row = db.scalar(select(PeriodOpening).where(
        PeriodOpening.period_id == jan27.id, PeriodOpening.subject_id == s27))
    assert row is not None, "次年 1 月没有 1002 的期初"
    assert row.amount == Decimal("1000"), row.amount


def test_编码对不上时拒绝结转并列出差异(db, book, periods, subjects,
                                        admin_user, make_voucher):
    """宁可报错，也不让钱凭空消失。

    新年度的科目表里没有 1002，而 1002 上有 1000 的余额 —— 搬不过去。
    静默丢弃的后果是「钱没了，而借贷仍然平衡，系统自检查不出来」，
    那正是这套系统反复强调的头号风险。
    """
    make_voucher("2026-03-10", dr=[("1002", 1000, "存钱")],
                 cr=[("5001", 1000, "存钱")], posted=True)
    _close_year(db, book, periods, admin_user)

    # 把新年度的科目表换成一张**没有 1002** 的表
    from app.accounting.years import copy_chart

    copy_chart(db, book.id, 2026, 2027)
    db.execute(text("DELETE FROM subject WHERE book_id = :b AND year = 2027 "
                    "AND code = '1002'"), {"b": book.id})
    db.commit()

    with pytest.raises(ValueError) as e:
        year_end_carry(db, book, admin_user, 2026, 2027)
    msg = str(e.value)
    assert "1002" in msg, msg
    assert "搬不过去" in msg or "找不到" in msg, msg
    db.rollback()


def test_两个年度的账簿各算各的(db, book, periods, subjects, admin_user,
                                make_voucher):
    """2027 年的账簿不能把 2026 年的数算进来，反之亦然。

    科目表是两张、分录挂在各自的科目上 —— 引擎按 id 聚合，
    两年的分录天然分得开。这条钉的是「分得开」这件事没被改坏。
    """
    make_voucher("2026-03-10", dr=[("1002", 1000, "存钱")],
                 cr=[("5001", 1000, "存钱")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user, 2026, 2027)
    db.expire_all()

    s27 = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2027, Subject.code == "1002"))
    s26 = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2026, Subject.code == "1002"))
    dec26 = db.scalar(select(Period.ord).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 12))
    jan27 = db.scalar(select(Period.ord).where(
        Period.book_id == book.id, Period.year == 2027, Period.month == 1))

    eng = LedgerEngine(db, book.id)
    # 2027 年 1 月的期初 = 2026 年 12 月的期末（承接过来的那一份）
    assert eng.figures(s27, jan27).opening == Decimal("1000")
    # 2026 那张表上的同一编码，12 月底也是 1000（同一笔钱，不是两份）
    assert eng.figures(s26, dec26).closing == Decimal("1000")


# ------------------------------------------------- 4. 初始化状态按年度


def test_初始化状态按年度_上一年做完不代表下一年做完(db, book, admin_user):
    """2026 年做完了，2027 年的第 1 步仍然是「没做」。

    账套级的状态会把 2027 一起标成「已完成」，于是 2027 年可以在
    **一张科目都没有**的情况下录期初、记账 —— 期初整批挂空。
    """
    from app.accounting.initialization import init_state, mark_subjects_ready

    y26 = db.scalar(select(BookYear).where(
        BookYear.book_id == book.id, BookYear.year == 2026))
    mark_subjects_ready(db, y26, "CHART")
    db.commit()

    assert init_state(db, book, 2026)["subjects_ready"] is True
    assert init_state(db, book, 2027)["subjects_ready"] is False, (
        "2026 年做完了就把 2027 也标成做完了 —— 状态必须是年度级的")


def test_两处闸门必须是同一个口径(db, book, admin_user):
    """初始化向导说「不能导入」，科目表页却说「能导」—— 这种不一致迟早出现。

    初始化向导用的是 ``init_state`` 里的 ``can_import_subjects``，
    科目表页用的是 ``subject_import_state`` 里的 ``can_overwrite``。
    两边各算一遍的话，用户点哪边得到的答案不一样。
    """
    from app.accounting.initialization import (init_state, subject_import_state)

    for year in (2026, 2027):
        assert init_state(db, book, year)["can_import_subjects"] == (
            subject_import_state(db, book.id, year)["can_overwrite"]
        ), f"{year} 年两处闸门口径不一致"


# ------------------------------------- 5. 结转之后再导入新科目表：期初按编码搬家


_TYPE_NAME = {"ASSET": "资产", "LIABILITY": "负债", "EQUITY": "权益",
              "COST": "成本", "PROFIT": "损益"}


def _openings_of(db, book_id: int, year: int) -> dict[str, Decimal]:
    """某个年度的期初：``{科目编码: 金额}``。"""
    rows = db.execute(
        select(Subject.code, PeriodOpening.amount)
        .join(Subject, Subject.id == PeriodOpening.subject_id)
        .join(Period, Period.id == PeriodOpening.period_id)
        .where(Period.book_id == book_id, Period.year == year)
    ).all()
    return {r.code: r.amount for r in rows}


def _chart_same_codes(db, book_id: int, year: int) -> bytes:
    """按该年度现有的科目表生成一张**编码相同、名称全换掉**的科目表。

    用来证明「导入之后那批期初是按编码搬到新科目上的」，而不是碰巧没动。
    """
    rows = []
    for s in sorted(_subjects_of(db, book_id, year).values(),
                    key=lambda x: (x.level, x.code)):
        rows.append((_TYPE_NAME[s.category], s.level, s.code,
                     f"新{s.name}", "借" if s.direction == "DEBIT" else "贷"))
    return _chart_xlsx(rows)


def test_结转后导入新科目表_期初按编码搬到新科目上(
        db, book, periods, subjects, admin_user, make_voucher, client, auth):
    """**结转之后再换科目表** —— 这是「每个年度有自己的科目表」最常见的用法。

    年度结转给 2027 承接了一份期初快照（1002 有 1000 元）。用户随后想给
    2027 导入**另一张**自己的科目表 —— 旧模型下这一年被「已有期初余额」
    挡住，而那正是用户报的那类「不符合我的要求」。

    现在期初**按编码搬家**：金额一分不变，只是从老科目行挪到新科目行。
    """
    make_voucher("2026-03-10", dr=[("1002", 1000, "存钱")],
                 cr=[("5001", 1000, "存钱")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user, 2026, 2027)
    db.commit()

    before_amounts = _openings_of(db, book.id, 2027)
    assert before_amounts, "前置条件不成立：结转后 2027 应当有承接过来的期初"
    old_subject_ids = {s.code: s.id for s in _subjects_of(db, book.id, 2027).values()}

    r = _import_chart(client, auth, book.id, 2027,
                      _chart_same_codes(db, book.id, 2027))
    assert r.status_code == 200, r.text
    db.expire_all()

    after_amounts = _openings_of(db, book.id, 2027)
    assert after_amounts == before_amounts, (
        f"换科目表把期初金额改了：{before_amounts} → {after_amounts}")

    new_subjects = _subjects_of(db, book.id, 2027)
    for code, amount in after_amounts.items():
        assert new_subjects[code].id != old_subject_ids[code], (
            f"{code} 的期初还指着被删掉的老科目行")
        row = db.scalar(select(PeriodOpening).where(
            PeriodOpening.subject_id == new_subjects[code].id))
        assert row is not None and row.amount == amount


def test_新科目表缺编码且余额非零_拒绝导入并列出差异(
        db, book, periods, subjects, admin_user, make_voucher, client, auth):
    """搬不动就**拒绝**，而且要把搬不动的编码**列出来**。

    静默丢弃的后果是「钱没了，而借贷仍然平衡，系统自检不会报错」——
    这套系统反复强调的头号风险。所以宁可报错，也不让钱凭空消失。
    """
    make_voucher("2026-03-10", dr=[("1002", 1000, "存钱")],
                 cr=[("5001", 1000, "存钱")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user, 2026, 2027)
    db.commit()

    before = _openings_of(db, book.id, 2027)
    busy = [code for code, amt in before.items() if amt != 0]
    assert busy, "前置条件不成立：2027 应当有非零的期初余额"

    # 一张**没有**那些编码的新表
    keep = [c for c in _subjects_of(db, book.id, 2027) if c not in busy][:2]
    # 「类型」必须跟**编码首位**（大类码）对得上 —— 导入路径上有一道
    # **独立于「类型」列**的校验（编码首位的大类码与类别必须一致，见
    # ``accounting/subject_importer``）。类型与编码对不上时，那道闸会
    # **先**挡下来（400），这条用例就测不到「搬不动就拒绝」那条路了。
    def _type_of(code: str) -> str:
        return {"1": "资产", "2": "负债", "3": "所有者权益",
                "4": "成本", "5": "损益"}.get(code[0], "资产")

    chart = _chart_xlsx([
        (_type_of(keep[0]), 1, keep[0], "保留的科目", "借"),
        (_type_of(keep[1]), 1, keep[1], "保留的科目", "贷"),
    ])

    r = _import_chart(client, auth, book.id, 2027, chart)
    assert r.status_code == 409, r.text
    for code in busy:
        assert code in r.text, f"差异里没有列出 {code}：{r.text}"
    assert "搬不过去" in r.text, r.text

    db.expire_all()
    # 拒绝必须发生在**零破坏**的状态下：老科目表与老期初都还在
    assert _openings_of(db, book.id, 2027) == before, "拒绝之后期初被动了"
    old = _subjects_of(db, book.id, 2027)
    assert busy[0] in old, "拒绝之后老科目表被动了"


def test_换本年度的科目表不动另一年的期初(
        db, book, periods, subjects, admin_user, make_voucher, client, auth):
    """搬期初只搬**本年度**的。2026 的期初快照一根汗毛都不许动。"""
    make_voucher("2026-03-10", dr=[("1002", 1000, "存钱")],
                 cr=[("5001", 1000, "存钱")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user, 2026, 2027)
    db.commit()

    before26 = _openings_of(db, book.id, 2026)
    ids26 = {s.code: s.id for s in _subjects_of(db, book.id, 2026).values()}

    r = _import_chart(client, auth, book.id, 2027,
                      _chart_same_codes(db, book.id, 2027))
    assert r.status_code == 200, r.text
    db.expire_all()

    assert _openings_of(db, book.id, 2026) == before26, "2026 的期初被改了"
    after26 = _subjects_of(db, book.id, 2026)
    for code, sid in ids26.items():
        assert after26[code].id == sid, f"2026 的 {code} 被换成了另一行"
