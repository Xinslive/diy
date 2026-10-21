"""凭证拆分：按分录边界拆成两张。

用户 2026-10-21 定的四条口径在这里落地：

    · 两张**各自**借贷平衡 —— 有一张不平就拆不成，且要报出差多少
    · **原凭证继承它自己的号**，新凭证往后拿一个（= 原号 +1），后面同期的号整体顺延 +1
    · 入口在凭证编辑页（后端是 split-preview / split 两个接口）
    · 新凭证沿用原凭证的日期与摘要；附单据数留在原凭证

这里测的每一条都是**账**上的性质，不是接口的形状：拆完之后两张凭证在
总账、余额表、结账体检里都必须站得住（各有各的号、各平各的）。
"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.accounting.organize import NO_WIDTH, check_period, split_no
from app.accounting.vouchers import (
    create_voucher,
    side_totals,
    split_preview,
    split_voucher,
)
from app.models import AuditLog, Entry, Period, Subject, Voucher


# --------------------------------------------------------------------- 夹具


def _entries(subjects: dict, rows: list[tuple]) -> list[dict]:
    """rows: [(科目编码, 方向, 金额, 摘要), ...] —— **顺序就是行号顺序**。

    这里不能用 ``make_voucher`` 夹具：它把借方全部排在贷方前面，
    而拆分要测的正是「借 5000 / 贷 5000 / 借 3000 / 贷 3000」这种交错排法。
    """
    return [{"subject_id": subjects[code].id, "direction": direction,
             "amount": str(amount), "summary": summary}
            for code, direction, amount, summary in rows]


def _make(db, book, user, subjects: dict, rows: list[tuple], *,
          date_: str = "2026-01-05", source: str = "MANUAL",
          remark: str = "拆分测试", attachments: int = 3):
    v = create_voucher(
        db, book_id=book.id, user=user,
        voucher_date=date.fromisoformat(date_),
        entries=_entries(subjects, rows), source=source,
        remark=remark, attachments=attachments, commit=False,
    )
    db.commit()
    db.refresh(v)
    return v


def _balanced_rows(db, voucher) -> tuple[Decimal, Decimal]:
    rows = [{"direction": e.direction, "amount": e.amount} for e in voucher.entries]
    return side_totals(rows)


def _next_no(no: str) -> str:
    """号 +1（补零宽度照抄 organize 那一处，不另写一份）。"""
    prefix, seq = split_no(no)
    return f"{prefix}{seq + 1:0{NO_WIDTH}d}"


# 两笔「各记各的」业务：拆在中间那道缝上，两半各自都平。
PAIR_A = [("1002", "DEBIT", 5000, "收款"), ("5001", "CREDIT", 5000, "收款")]
PAIR_B = [("1122", "DEBIT", 3000, "挂账"), ("2203", "CREDIT", 3000, "挂账")]
# 一笔费用归集：借方两行、贷方一行 —— 在任何位置切开都不平。
# 560101 / 560102 是 5601 底下的**末级**科目（挂到 5601 上会被拒）。
LUMP = [("560101", "DEBIT", 500, "办公"), ("560102", "DEBIT", 800, "差旅"),
        ("1002", "CREDIT", 1300, "付款")]


# --------------------------------------------------------------------- 主路径


def test_按分界拆成两张且各自平(db, book, admin_user, subjects):
    """在第 2 行后拆：前半张留在原凭证，后半张另起一张。"""
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)
    old_no = v.voucher_no

    result = split_voucher(db, v, admin_user, 2)
    original, created = result["original"], result["created"]

    # 两张都在，各自平
    assert _balanced_rows(db, original) == (Decimal("5000.00"), Decimal("5000.00"))
    assert _balanced_rows(db, created) == (Decimal("3000.00"), Decimal("3000.00"))
    # 摘要是原样搬过去的，不是重写的
    assert [e.summary for e in created.entries] == ["挂账", "挂账"]


def test_原凭证继承原号_新凭证往后拿一个_后面的号顺延(db, book, admin_user, subjects):
    """用户 2026-10-21 定的编号口径。

    **原凭证继承它自己的号**（它可能已经打印、已经被引用过，一个字都不动），
    新凭证往后拿一个（= 原号 +1），后面同期的号整体顺延 +1 ——
    与「增加凭证」同一套语义。
    """
    v1 = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)
    v2 = _make(db, book, admin_user, subjects, PAIR_A)
    old1, old2 = v1.voucher_no, v2.voucher_no

    result = split_voucher(db, v1, admin_user, 2)
    original, created = result["original"], result["created"]

    assert original.voucher_no == old1             # 原凭证的号不动
    assert created.voucher_no == _next_no(old1)    # 新凭证往后拿一个
    db.refresh(v2)
    assert v2.voucher_no == _next_no(old2)         # 后面那张顺延 +1


def test_原凭证的id与号都不变_只是行数少了(db, book, admin_user, subjects):
    """原凭证留在原地（id 不变、号不变），页面上还是那一张 —— 变的只有行数。"""
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)
    v_id = v.id
    old_no = v.voucher_no

    result = split_voucher(db, v, admin_user, 2)
    original = result["original"]

    assert original.id == v_id
    assert original.voucher_no == old_no
    assert [e.line_no for e in original.entries] == [1, 2]
    assert [e.line_no for e in result["created"].entries] == [1, 2]


def test_新凭证沿用日期与摘要_附单据数留在原凭证(db, book, admin_user, subjects):
    """同一笔业务拆出来的两张，日期摘要一致才好查；附单据不重复计数。"""
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B,
              date_="2026-03-08", remark="三月的业务", attachments=5)

    result = split_voucher(db, v, admin_user, 2)
    original, created = result["original"], result["created"]

    assert created.voucher_date == date(2026, 3, 8) == original.voucher_date
    assert created.remark == "三月的业务" == original.remark
    assert created.attachments == 0
    assert original.attachments == 5
    assert created.status == "UNPOSTED"
    assert created.period_id == original.period_id


def test_拆分后号段仍连续无断号(db, book, admin_user, subjects):
    """顺延是「整体 +1」而不是「删一个补一个」—— 期间里既不缺号也不重号。"""
    v1 = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)
    _make(db, book, admin_user, subjects, PAIR_A)
    _make(db, book, admin_user, subjects, PAIR_B)

    split_voucher(db, v1, admin_user, 2)

    period = db.get(Period, v1.period_id)
    rep = check_period(db, period)
    assert rep["gaps"] == []
    assert rep["duplicates"] == []
    assert rep["clean"] is True


def test_拆分留下三条审计(db, book, admin_user, subjects):
    """凭证号可回收复用，所以「拆之前那张长什么样」必须完整留在日志里。"""
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)
    # 后面再有一张：拆的时候它要顺位 —— 顺位这件事也必须留痕
    _make(db, book, admin_user, subjects, PAIR_A)
    result = split_voucher(db, v, admin_user, 2)

    rows = db.scalars(select(AuditLog).where(AuditLog.book_id == book.id)).all()
    actions = [r.action for r in rows]
    assert "SPLIT" in actions and "CREATE" in actions and "RENAME" in actions

    split_log = next(r for r in rows if r.action == "SPLIT")
    assert split_log.entity_id == str(v.id)
    assert len(split_log.before["entries"]) == 4      # 拆之前的完整 4 行
    assert len(split_log.after["entries"]) == 2       # 拆之后原凭证只剩 2 行

    create_log = next(r for r in rows
                      if r.action == "CREATE" and r.entity_id == str(result["created"].id))
    assert len(create_log.after["entries"]) == 2

    rename_log = next(r for r in rows if r.action == "RENAME")
    assert rename_log.before["moved"]


def test_预览一次列出全部候选并标出能不能拆(db, book, admin_user, subjects):
    """只算不改：一次请求拿到所有分界处，号码口径与落库时同源。"""
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)

    p = split_preview(db, v)

    assert p["entry_count"] == 4
    assert len(p["candidates"]) == 3
    assert [c["at_line"] for c in p["candidates"]] == [1, 2, 3]
    assert p["candidates"][1]["valid"] is True        # 在第 2 行后切，两半都平
    assert p["candidates"][0]["valid"] is False       # 第 1 行后切，前半张不平
    assert "不成立" in p["candidates"][0]["message"]
    assert p["original_no"] == v.voucher_no           # 原凭证的号不动
    assert p["created_no"] == _next_no(v.voucher_no)  # 新凭证往后拿一个


def test_合计的写法与界面一致_零也要写成两位小数(db, book, admin_user, subjects):
    """一半没有贷方时是「贷 0.00」而不是「贷 0」。

    同一个数两种写法，看着像两个数 —— 而页面上摆的永远是两位小数。
    """
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)

    p = split_preview(db, v)

    assert p["candidates"][0]["first"]["credit"] == "0.00"
    assert p["candidates"][0]["first"]["debit"] == "5000.00"
    assert p["candidates"][0]["second"]["debit"] == "3000.00"


# --------------------------------------------------------------------- 闸门


def test_拆不平被拒并报出差额(db, book, admin_user, subjects):
    """不平就拆不成，而且要把两边的差额和方向说清楚。"""
    v = _make(db, book, admin_user, subjects, LUMP)

    with pytest.raises(ValueError) as ei:
        split_voucher(db, v, admin_user, 1)

    msg = str(ei.value)
    assert "不成立" in msg
    assert "借方多 500" in msg      # 前半张只有借方 500
    assert "贷方多 500" in msg      # 后半张只有贷方 1300
    db.rollback()
    # 账上什么都没变：还是那一张
    assert db.scalar(select(Voucher).where(Voucher.id == v.id)) is not None


def test_分界越界被拒(db, book, admin_user, subjects):
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)

    for bad in (0, 4, 5):
        with pytest.raises(ValueError, match="分界处必须在第 1 行到第 3 行之间"):
            split_voucher(db, v, admin_user, bad)
        db.rollback()


def test_只有一条分录不能拆(db, book, admin_user, subjects):
    """一条分录的凭证在正常路径下压根存不进来（借贷必平），这里是兜底闸门。

    直接删掉一条**但不提交**来造这个状态：提交时那条延迟到提交才判平的
    触发器会拦下来，所以这里只 flush，测完就回滚。
    """
    v = _make(db, book, admin_user, subjects, PAIR_A)
    victim = sorted(v.entries, key=lambda e: e.line_no)[-1]
    db.delete(victim)
    db.flush()

    with pytest.raises(ValueError, match="只有 1 条分录，拆不开"):
        split_voucher(db, v, admin_user, 1)
    db.rollback()


def test_已记账不能拆(db, book, admin_user, subjects):
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)
    db.execute(update(Voucher).where(Voucher.id == v.id).values(status="POSTED"))
    db.commit()
    db.refresh(v)

    with pytest.raises(ValueError) as ei:
        split_voucher(db, v, admin_user, 2)

    assert "已记账" in str(ei.value)
    # 提示必须指名那个**现在还存在的**入口 —— 凭证列表上的「反记账」早撤了
    assert "期末结账" in str(ei.value)
    db.rollback()


def test_已结账期间不能拆(db, book, admin_user, subjects):
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)
    period = db.get(Period, v.period_id)
    db.execute(update(Period).where(Period.id == period.id).values(locked=True))
    db.commit()

    with pytest.raises(ValueError, match="已结账"):
        split_voucher(db, v, admin_user, 2)
    db.rollback()


def test_结转凭证不能拆(db, book, admin_user, subjects):
    """结转是系统生成的：拆出来的半张签名对不上，重新结转时它既不会被删，
    也不再是系统凭证 —— 账上会留一张孤儿。"""
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B, source="CARRY_FORWARD")

    with pytest.raises(ValueError, match="结转损益凭证由系统生成"):
        split_voucher(db, v, admin_user, 2)
    db.rollback()


def test_科目已停用则拆不了(db, book, admin_user, subjects):
    """与「改凭证」同口径：科目停用后不仅改不了，拆也不了。"""
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)
    db.execute(update(Subject).where(Subject.id == subjects["5001"].id)
               .values(active=False))
    db.commit()

    with pytest.raises(ValueError, match="已停用"):
        split_voucher(db, v, admin_user, 2)
    db.rollback()


def test_历史非末级分录仍能拆(db, book, admin_user, subjects):
    """祖父条款（审计 P2-21）：科目后来被加了子节点，老凭证上的分录原样保留。

    拆分的两半**各自**带自己的豁免计数 —— 拿整张的计数去套两半，
    会让另一半凭空多出一条豁免。
    """
    v = _make(db, book, admin_user, subjects, [
        ("1001", "DEBIT", 100, "老凭证"), ("5001", "CREDIT", 100, "老凭证"),
        ("1002", "DEBIT", 50, "老凭证"), ("2203", "CREDIT", 50, "老凭证"),
    ])
    parent = subjects["1001"]
    db.add(Subject(book_id=book.id, year=parent.year, code="100199",
                   name="后来加的子科目",
                   parent_id=parent.id, level=2, category="ASSET",
                   direction="DEBIT", is_leaf=True))
    db.flush()
    db.execute(update(Subject).where(Subject.id == parent.id).values(is_leaf=False))
    db.commit()
    db.refresh(v)

    # 拆在第 2 行后：前半张带着那条非末级分录（要靠祖父条款才放行），
    # 后半张干干净净 —— 两半各自的豁免计数必须各算各的。
    result = split_voucher(db, v, admin_user, 2)

    assert len(result["original"].entries) == 2
    assert len(result["created"].entries) == 2
    assert result["original"].entries[0].subject_id == parent.id


def test_红字凭证拆开后各自仍判平(db, book, admin_user, subjects):
    """红字冲销（借 −300 / 贷 −300）是平的，拆开之后两半也各自是平的。"""
    v = _make(db, book, admin_user, subjects, [
        ("1002", "DEBIT", -300, "冲销"), ("5001", "CREDIT", -300, "冲销"),
        ("1002", "DEBIT", -200, "冲销"), ("5001", "CREDIT", -200, "冲销"),
    ])

    result = split_voucher(db, v, admin_user, 2)

    for key in ("original", "created"):
        dr, cr = _balanced_rows(db, result[key])
        assert dr == cr


# --------------------------------------------------------------------- API 层


def test_拆分接口返回两张凭证(client, auth, db, book, admin_user, subjects):
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)
    old_no = v.voucher_no

    r = client.post(f"/api/books/{book.id}/vouchers/{v.id}/split",
                    headers=auth, json={"at_line": 2})

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["original"]["voucher_no"] == old_no             # 原凭证继承原号
    assert data["created"]["voucher_no"] == _next_no(old_no)    # 新凭证往后拿一个
    assert len(data["original"]["entries"]) == 2
    assert len(data["created"]["entries"]) == 2


def test_拆分接口_不平给400并说清差额(client, auth, db, book, admin_user, subjects):
    v = _make(db, book, admin_user, subjects, LUMP)

    r = client.post(f"/api/books/{book.id}/vouchers/{v.id}/split",
                    headers=auth, json={"at_line": 1})

    assert r.status_code == 400
    assert "不成立" in r.text
    assert "借方多 500" in r.text


def test_拆分预览接口只算不改(client, auth, db, book, admin_user, subjects):
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)

    r = client.get(f"/api/books/{book.id}/vouchers/{v.id}/split-preview", headers=auth)

    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["candidates"]) == 3
    assert data["original_no"] == v.voucher_no
    assert data["created_no"] == _next_no(v.voucher_no)
    # 只算不改：库里还是那一张
    assert db.scalar(select(Voucher).where(Voucher.period_id == v.period_id)) is not None


def test_拆分接口_别人的凭证是404(client, auth, db, book, admin_user, subjects):
    """跨账套不许碰：拿着别家账套的接口来拆这张凭证，404 而不是「拆了」。"""
    from app.accounting.periods import create_periods
    from app.models import Book, UserBook
    from app.seed.chart_of_accounts import install_chart

    other = Book(company_name="别家", name="别家账套", start_year=2026, start_month=1)
    db.add(other)
    db.flush()
    create_periods(db, other)
    install_chart(db, other.id)
    db.add(UserBook(user_id=admin_user.id, book_id=other.id, role="OWNER"))
    db.commit()

    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)

    r = client.post(f"/api/books/{other.id}/vouchers/{v.id}/split",
                    headers=auth, json={"at_line": 2})

    assert r.status_code == 404
    assert "凭证不存在" in r.text


def test_拆分接口_分界必须是正整数(client, auth, db, book, admin_user, subjects):
    v = _make(db, book, admin_user, subjects, PAIR_A + PAIR_B)

    r = client.post(f"/api/books/{book.id}/vouchers/{v.id}/split",
                    headers=auth, json={"at_line": 0})

    assert r.status_code == 422
