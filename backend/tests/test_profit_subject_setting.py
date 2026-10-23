"""手工设定「本年利润」科目。

**为什么要有这一组。** 本年利润原先**只能靠编码猜**
（``PROFIT_SUBJECT_CODES = ("3103", "4103")``），而科目表是各人自己的：
编码改成 3111 的有、干脆叫「本年损益」的也有。识别不到时账就结不了转，
而界面上**没有任何地方能改** —— 用户只能看着一句报错，无路可走。

这一组钉住新加的那条人工设定通道：

* **手工设定优先于自动识别**（``resolve_profit_subject``），
* **设定值失效时回落自动识别**，且**不替用户清掉设定**，
* **结转损益真的冲到手工指定的那个科目**上（不是只存了个值），
* **手工指定的本年利润不会被当成结转对象**（不结转给自己），
* 接口层的三道闸：编码不存在 / 不是末级 / 已停用，一律拒收。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.accounting.periods import (PROFIT_SOURCE_AUTO, PROFIT_SOURCE_MISSING,
                                    PROFIT_SOURCE_SETTING, carry_forward_profit,
                                    carry_forward_subjects, resolve_profit_subject,
                                    set_profit_subject_setting)
from app.models import Entry, Period, Subject, Voucher


# --------------------------------------------------------------------- 助手


def _add_subject(db, book, code: str, name: str, category: str,
                 *, carry: bool = False, active: bool = True,
                 is_leaf: bool = True) -> Subject:
    """往**启用年度**的科目表里加一个科目。"""
    s = Subject(book_id=book.id, year=book.start_year, code=code, name=name,
                category=category, direction="DEBIT" if category in ("ASSET", "COST")
                else "CREDIT",
                level=1, is_leaf=is_leaf, carry_forward=carry, active=active)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _entries_of(db, voucher_id: int) -> list[tuple[int, str, str]]:
    """一张凭证的分录：(科目 id, 方向, 金额)。"""
    rows = db.execute(
        select(Entry.subject_id, Entry.direction, Entry.amount)
        .where(Entry.voucher_id == voucher_id).order_by(Entry.id)
    ).all()
    return [(r[0], r[1], str(r[2])) for r in rows]


# --------------------------------------------------------------------- 解析口径


def test_没设定时按候选编码自动识别(db, book, subjects):
    """没设过 = 走自动识别。**不是**「不识别」—— 写死一个非空默认值
    反而会盖掉那些本来就识别得出来的账套。"""
    subject, source = resolve_profit_subject(db, book.id, book.start_year)
    assert subject is not None
    assert subject.code == "3103"
    assert source == PROFIT_SOURCE_AUTO


def test_手工设定优先于自动识别(db, book):
    """用户指定了就用用户指定的 —— 这正是「自动识别不出来时的出路」。"""
    mine = _add_subject(db, book, "3999", "本年损益", "EQUITY")
    set_profit_subject_setting(db, book.id, book.start_year, "3999")
    subject, source = resolve_profit_subject(db, book.id, book.start_year)
    assert subject.id == mine.id
    assert source == PROFIT_SOURCE_SETTING


def test_设定值指向不存在的科目时回落自动识别(db, book, subjects):
    """那张科目表被整表换过是常事（重新导入）。

    这时**不能拿一个不存在的科目去记账**，也不能**替用户把设定清掉** ——
    设定还在，要不要改由用户决定（他可能马上要把那张表导回来）。
    """
    set_profit_subject_setting(db, book.id, book.start_year, "3999")
    subject, source = resolve_profit_subject(db, book.id, book.start_year)
    assert subject is not None
    assert subject.code == "3103", "设定值不存在时应回落到自动识别"
    assert source == PROFIT_SOURCE_AUTO
    from app.accounting.periods import profit_subject_setting
    assert profit_subject_setting(db, book.id, book.start_year) == "3999", \
        "回落不该把用户的设定清掉"


def test_两级都没有时报告缺失(db, book, subjects):
    """既没设定、候选编码也没有 —— 报「缺失」，且**说清去哪儿设**。"""
    from sqlalchemy import update
    db.execute(update(Subject)
               .where(Subject.book_id == book.id, Subject.year == book.start_year,
                      Subject.code.in_(("3103", "4103")))
               .values(code="3199"))
    db.commit()
    subject, source = resolve_profit_subject(db, book.id, book.start_year)
    assert subject is None
    assert source == PROFIT_SOURCE_MISSING


# --------------------------------------------------------------------- 结转真的用它


def test_结转损益冲到手工指定的科目(db, book, subjects, periods, admin_user,
                                   make_voucher):
    """不是只存了个值 —— **结转出来的凭证要真的挂在那个科目上**。

    断言的是凭证分录本身：目的科目的 id 必须是手工指定的那一个，
    而不是自动识别出来的 3103。
    """
    mine = _add_subject(db, book, "3999", "本年损益", "EQUITY")
    set_profit_subject_setting(db, book.id, book.start_year, "3999")
    make_voucher("2026-01-10", dr=[("1001", 1000, "收款")],
                 cr=[("5001", 1000, "收款")])

    result = carry_forward_profit(db, book.id, periods[1], admin_user)
    assert result["vouchers"], result

    v = db.scalar(select(Voucher).where(
        Voucher.id == db.scalar(select(Voucher.id)
                                .where(Voucher.remark.like("%结转损益%"))
                                .limit(1))))
    assert v is not None
    ids = {sid for sid, _d, _a in _entries_of(db, v.id)}
    assert mine.id in ids, "结转凭证没有挂在手工指定的本年利润科目上"
    assert subjects["3103"].id not in ids, "结转仍然冲到了自动识别出来的科目"


def test_手工指定的本年利润不会被结转给自己(db, book, subjects, periods,
                                            admin_user, make_voucher):
    """**结转集合里不能有本年利润** —— 它是结转的**目的科目**。

    这里的科目被归成了损益类（用户自己导的科目表就是这样），
    若不排除，它会**结转给自己**：自己冲自己，借贷仍然平衡，
    系统自检抓不到，账却错了。
    """
    mine = _add_subject(db, book, "3999", "本年损益", "PROFIT", carry=True)
    set_profit_subject_setting(db, book.id, book.start_year, "3999")
    codes = {s.code for s in carry_forward_subjects(db, book.id, book.start_year)}
    assert "3999" not in codes, "手工指定的本年利润进了结转集合"


def test_设定失效时结转集合仍排除自动识别的那一个(db, book, subjects, periods,
                                                   admin_user):
    """用户改了设定之后，**旧口径认的那个也不能回到结转集合里** ——
    那是行为回退：本该结走的损益会留在科目上。"""
    _add_subject(db, book, "3999", "本年损益", "PROFIT", carry=True)
    set_profit_subject_setting(db, book.id, book.start_year, "3999")
    codes = {s.code for s in carry_forward_subjects(db, book.id, book.start_year)}
    assert "3103" not in codes
    assert "3999" not in codes


# --------------------------------------------------------------------- 跨年


def test_年度结转把本年利润设定带到新年度(db, book, subjects, periods, admin_user):
    """新年度的科目表是照抄本年度的，**那个设定也得跟着走**。

    不带过去的话，新年度会退回自动识别 —— 而这本账的科目表压根不认
    3103/4103，于是**新年度一开账就结不了转**，用户得把同一件事再设一遍。
    """
    from app.accounting.periods import profit_subject_setting, year_end_carry
    from tests.accounting_helpers import close_any

    _add_subject(db, book, "3999", "本年损益", "EQUITY")
    set_profit_subject_setting(db, book.id, book.start_year, "3999")

    for m in range(1, 13):
        p = db.get(Period, periods[m].id)
        if p.locked:
            continue
        try:
            carry_forward_profit(db, book.id, p, admin_user)
        except ValueError:
            pass          # 该月没损益，跳过
        close_any(db, book, p, admin_user)

    year_end_carry(db, book, admin_user, 2026, 2027)
    db.expire_all()
    assert profit_subject_setting(db, book.id, 2027) == "3999", \
        "本年利润科目的手工设定没有带到新年度"


def test_新年度的科目表里没有那个编码时不带过去(db, book, subjects, periods,
                                             admin_user):
    """带过去的是**一个指向不存在科目的设定** —— 那不如不带。

    新年度若是用户自己导入的另一张表，本年度的编码可能压根不在里面。
    """
    from app.accounting.periods import profit_subject_setting, year_end_carry
    from tests.accounting_helpers import close_any

    set_profit_subject_setting(db, book.id, book.start_year, "3999")
    for m in range(1, 13):
        p = db.get(Period, periods[m].id)
        if p.locked:
            continue
        try:
            carry_forward_profit(db, book.id, p, admin_user)
        except ValueError:
            pass
        close_any(db, book, p, admin_user)

    year_end_carry(db, book, admin_user, 2026, 2027)
    db.expire_all()
    assert profit_subject_setting(db, book.id, 2027) is None, \
        "指向不存在科目的设定不该被带到新年度"


# --------------------------------------------------------------------- 接口


def test_接口_设定与清空(client, auth, book, subjects):
    """设定 → 状态变成 setting；清空 → 回到自动识别。"""
    year = book.start_year
    r = client.get(f"/api/books/{book.id}/years/{year}/profit-subject", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == PROFIT_SOURCE_AUTO
    assert r.json()["subject"]["code"] == "3103"

    r = client.put(f"/api/books/{book.id}/years/{year}/profit-subject",
                   headers=auth, json={"code": "3103"})
    assert r.status_code == 200, r.text
    assert r.json()["setting_code"] == "3103"
    assert r.json()["source"] == PROFIT_SOURCE_SETTING

    r = client.put(f"/api/books/{book.id}/years/{year}/profit-subject",
                   headers=auth, json={"code": None})
    assert r.status_code == 200, r.text
    assert r.json()["setting_code"] is None
    assert r.json()["source"] == PROFIT_SOURCE_AUTO


def test_接口_编码不存在要被拒(client, auth, book):
    """设一个这一年的科目表里没有的编码 → 拒收。

    收下的话，结转时找不到它、回落到自动识别，而用户以为设上了 ——
    账冲到的却是另一个科目。
    """
    r = client.put(f"/api/books/{book.id}/years/{book.start_year}/profit-subject",
                   headers=auth, json={"code": "3999"})
    assert r.status_code == 400, r.text
    assert "3999" in r.json()["detail"]


def test_接口_非末级科目要被拒(client, auth, book, db):
    """结转凭证只能记在末级科目上 —— 非末级**现在**就该被拒，
    而不是等结转时服务端抛一句「不是末级科目」。"""
    parent = _add_subject(db, book, "3900", "损益汇总", "EQUITY", is_leaf=False)
    child = _add_subject(db, book, "390001", "本年损益", "EQUITY")
    child.parent_id = parent.id
    db.commit()

    r = client.put(f"/api/books/{book.id}/years/{book.start_year}/profit-subject",
                   headers=auth, json={"code": "3900"})
    assert r.status_code == 400, r.text
    assert "末级" in r.json()["detail"]


def test_接口_停用科目要被拒(client, auth, book, db):
    """停用的语义是「不让再挂新的分录」—— 结转凭证挂不上去。"""
    _add_subject(db, book, "3999", "本年损益", "EQUITY", active=False)
    r = client.put(f"/api/books/{book.id}/years/{book.start_year}/profit-subject",
                   headers=auth, json={"code": "3999"})
    assert r.status_code == 400, r.text
    assert "停用" in r.json()["detail"]


def test_接口_账套没有这一年被拒(client, auth, book):
    """给一个不存在的年度建设定 —— 那是给空气写了个规则。"""
    r = client.get(f"/api/books/{book.id}/years/2030/profit-subject", headers=auth)
    assert r.status_code == 400, r.text
    r = client.put(f"/api/books/{book.id}/years/2030/profit-subject",
                   headers=auth, json={"code": "3103"})
    assert r.status_code == 400, r.text


def test_设定要留下审计痕迹(client, auth, book, db):
    """改了「结转冲到哪里」是账务口径的改动 —— 必须留痕。"""
    from app.models import AuditLog

    client.put(f"/api/books/{book.id}/years/{book.start_year}/profit-subject",
               headers=auth, json={"code": "3103"})
    rows = db.scalars(select(AuditLog).where(
        AuditLog.book_id == book.id, AuditLog.entity == "book_year")).all()
    assert rows, "设定本年利润科目没有写审计日志"
    assert "本年利润" in rows[-1].detail
