"""手工设定「未分配利润」科目。

**为什么要有这一组。** 未分配利润是**年度结转**的目的科目（本年利润的余额
冲到它上面，变成留存收益），而它原先**只能靠编码猜**
（``RETAINED_SUBJECT_CODES = ("310415", "3104", "4104")``）。科目表是各人
自己的：改过编码的、把「未分配利润」挂在别的大类码底下的都有。

识别不出来时**年度结转就结不了**（后端 fail-closed：缺这个科目就停下，
本年利润的余额会原样滚到下一年，而资产负债表的未分配利润恒为 0），
而界面上**没有任何地方能改** —— 用户只能看着一句报错，无路可走。

这一组钉住新加的那条人工设定通道：

* **手工设定优先于自动识别**（``resolve_retained_subject``），
* **设定值失效时回落自动识别**，且**不替用户清掉设定**，
* **年度结转真的冲到手工指定的那个科目**上（不是只存了个值），
* 手工设定**带到新年度**（照抄本年度科目表，编码多半也在），
* 接口层的三道闸：编码不存在 / 不是末级 / 已停用，一律拒收。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.accounting.periods import (RETAINED_SOURCE_AUTO, RETAINED_SOURCE_MISSING,
                                    RETAINED_SOURCE_SETTING, carry_forward_profit,
                                    resolve_retained_subject, retained_subject_setting,
                                    set_retained_subject_setting, year_end_carry)
from app.models import Entry, Period, Subject, Voucher
from tests.accounting_helpers import close_any


# --------------------------------------------------------------------- 助手


def _add_subject(db, book, code: str, name: str, category: str,
                 *, active: bool = True, is_leaf: bool = True) -> Subject:
    """往**启用年度**的科目表里加一个科目。"""
    s = Subject(book_id=book.id, year=book.start_year, code=code, name=name,
                category=category, direction="CREDIT" if category == "EQUITY" else "DEBIT",
                level=1, is_leaf=is_leaf, carry_forward=False, active=active)
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


def _close_year(db, book, periods, admin_user):
    """把 2026 年 12 个月结账（每月先结转损益再结账）。"""
    for m in range(1, 13):
        period = db.get(Period, periods[m].id)
        if period.locked:
            continue
        try:
            carry_forward_profit(db, book.id, period, admin_user)
        except ValueError:
            pass          # 该月没损益，跳过
        close_any(db, book, period, admin_user)


def _carry_voucher(db, book, year: int) -> Voucher:
    """年度结转生成的那张「结转本年利润」凭证。"""
    v = db.scalar(select(Voucher).where(
        Voucher.book_id == book.id,
        Voucher.remark.like(f"%{year}年 结转本年利润%")
    ).order_by(Voucher.id.desc()))
    assert v is not None, "没有生成结转本年利润的凭证"
    return v


# --------------------------------------------------------------------- 解析口径


def test_没设定时按候选编码自动识别(db, book, subjects):
    """没设过 = 走自动识别。**不是**「不识别」—— 写死一个非空默认值
    反而会盖掉那些本来就识别得出来的账套。"""
    subject, source = resolve_retained_subject(db, book.id, book.start_year)
    assert subject is not None
    assert subject.code == "310415"
    assert source == RETAINED_SOURCE_AUTO


def test_手工设定优先于自动识别(db, book):
    """用户指定了就用用户指定的 —— 这正是「自动识别不出来时的出路」。"""
    mine = _add_subject(db, book, "3999", "未分配利润", "EQUITY")
    set_retained_subject_setting(db, book.id, book.start_year, "3999")
    subject, source = resolve_retained_subject(db, book.id, book.start_year)
    assert subject.id == mine.id
    assert source == RETAINED_SOURCE_SETTING


def test_设定值指向不存在的科目时回落自动识别(db, book, subjects):
    """那张科目表被整表换过是常事（重新导入）。

    这时**不能拿一个不存在的科目去记账**，也不能**替用户把设定清掉** ——
    设定还在，要不要改由用户决定（他可能马上要把那张表导回来）。
    """
    set_retained_subject_setting(db, book.id, book.start_year, "3999")
    subject, source = resolve_retained_subject(db, book.id, book.start_year)
    assert subject is not None
    assert subject.code == "310415", "设定值不存在时应回落到自动识别"
    assert source == RETAINED_SOURCE_AUTO
    assert retained_subject_setting(db, book.id, book.start_year) == "3999", \
        "回落不该把用户的设定清掉"


def test_两级都没有时报告缺失(db, book, subjects):
    """既没设定、候选编码也没有 —— 报「缺失」。"""
    from sqlalchemy import update
    # 三条候选逐个改成**互不相同**的空闲编码：改成同一个会撞唯一约束
    # （(book_id, year, code) 唯一）。
    for old_code, new_code in (("310415", "3999"), ("3104", "3998"), ("4104", "3997")):
        db.execute(update(Subject)
                   .where(Subject.book_id == book.id, Subject.year == book.start_year,
                          Subject.code == old_code)
                   .values(code=new_code))
    db.commit()
    subject, source = resolve_retained_subject(db, book.id, book.start_year)
    assert subject is None
    assert source == RETAINED_SOURCE_MISSING


# --------------------------------------------------------------------- 年度结转真的用它


def test_年度结转真的冲到手工指定的科目上(db, book, subjects, periods,
                                          admin_user, make_voucher):
    """不是只存了个值 —— **年度结转出来的凭证要真的记在它上面**。

    否则「设了」与「用得上」是两回事：用户看到「已设为 3999」，
    账却还是冲到了 310415，而报表上的未分配利润还是 0。
    """
    mine = _add_subject(db, book, "3999", "未分配利润", "EQUITY")
    set_retained_subject_setting(db, book.id, book.start_year, "3999")
    make_voucher("2026-01-10", dr=[("1002", 1000, "x")],
                 cr=[("5001", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user)
    db.expire_all()

    v = _carry_voucher(db, book, 2026)
    ids = {sid for sid, _d, _a in _entries_of(db, v.id)}
    assert mine.id in ids, "年度结转没有挂在手工指定的未分配利润科目上"
    assert subjects["310415"].id not in ids, "年度结转仍然冲到了自动识别出来的科目"


def test_没设定时年度结转照样冲自动识别出来的科目(db, book, subjects, periods,
                                                  admin_user, make_voucher):
    """空值 = 走自动识别，不是「不识别」—— 没设过的账套行为不能变。"""
    make_voucher("2026-01-10", dr=[("1002", 1000, "x")],
                 cr=[("5001", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user)
    db.expire_all()

    v = _carry_voucher(db, book, 2026)
    ids = {sid for sid, _d, _a in _entries_of(db, v.id)}
    assert subjects["310415"].id in ids


# --------------------------------------------------------------------- 跨年


def test_年度结转把未分配利润设定带到新年度(db, book, subjects, periods, admin_user):
    """新年度的科目表是照抄本年度的，**那个设定也得跟着走**。

    不带过去的话，新年度会退回自动识别 —— 而这本账的科目表压根不认
    310415/3104/4104，于是**新年度一开账就结不了转**，用户得把同一件事再设一遍。
    """
    _add_subject(db, book, "3999", "未分配利润", "EQUITY")
    set_retained_subject_setting(db, book.id, book.start_year, "3999")
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user, 2026, 2027)
    db.expire_all()
    assert retained_subject_setting(db, book.id, 2027) == "3999", \
        "未分配利润科目的手工设定没有带到新年度"


def test_新年度的科目表里没有那个编码时不带过去(db, book, subjects, periods,
                                               admin_user):
    """带过去的是**一个指向不存在科目的设定** —— 那不如不带。

    新年度若是用户自己导入的另一张表，本年度的编码可能压根不在里面。
    """
    # 这个编码在 2026 的科目表里就不存在（所以也不会被照抄进 2027）
    set_retained_subject_setting(db, book.id, book.start_year, "3999")
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user, 2026, 2027)
    db.expire_all()
    assert retained_subject_setting(db, book.id, 2027) is None, \
        "指向不存在科目的设定不该被带到新年度"


# --------------------------------------------------------------------- 接口


def test_接口_设定与清空(client, auth, book, subjects):
    """设定 → 状态变成 setting；清空 → 回到自动识别。"""
    year = book.start_year
    r = client.get(f"/api/books/{book.id}/years/{year}/retained-subject", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == RETAINED_SOURCE_AUTO
    assert r.json()["subject"]["code"] == "310415"

    r = client.put(f"/api/books/{book.id}/years/{year}/retained-subject",
                   headers=auth, json={"code": "310415"})
    assert r.status_code == 200, r.text
    assert r.json()["setting_code"] == "310415"
    assert r.json()["source"] == RETAINED_SOURCE_SETTING

    r = client.put(f"/api/books/{book.id}/years/{year}/retained-subject",
                   headers=auth, json={"code": None})
    assert r.status_code == 200, r.text
    assert r.json()["setting_code"] is None
    assert r.json()["source"] == RETAINED_SOURCE_AUTO


def test_接口_编码不存在要被拒(client, auth, book):
    """设一个这一年的科目表里没有的编码 → 拒收。

    收下的话，年度结转时找不到它、回落到自动识别，而用户以为设上了 ——
    账冲到的却是另一个科目。
    """
    r = client.put(f"/api/books/{book.id}/years/{book.start_year}/retained-subject",
                   headers=auth, json={"code": "3999"})
    assert r.status_code == 400, r.text
    assert "3999" in r.json()["detail"]


def test_接口_非末级科目要被拒(client, auth, book, db):
    """年度结转那张凭证只记在末级科目上 —— 非末级**现在**就该被拒。"""
    _add_subject(db, book, "3900", "利润分配", "EQUITY", is_leaf=False)
    r = client.put(f"/api/books/{book.id}/years/{book.start_year}/retained-subject",
                   headers=auth, json={"code": "3900"})
    assert r.status_code == 400, r.text
    assert "末级" in r.json()["detail"]


def test_接口_停用科目要被拒(client, auth, book, db):
    """停用的语义是「不让再挂新的分录」—— 结转凭证挂不上去。"""
    _add_subject(db, book, "3999", "未分配利润", "EQUITY", active=False)
    r = client.put(f"/api/books/{book.id}/years/{book.start_year}/retained-subject",
                   headers=auth, json={"code": "3999"})
    assert r.status_code == 400, r.text
    assert "停用" in r.json()["detail"]


def test_接口_账套没有这一年被拒(client, auth, book):
    """给一个不存在的年度建设定 —— 那是给空气写了个规则。"""
    r = client.get(f"/api/books/{book.id}/years/2030/retained-subject", headers=auth)
    assert r.status_code == 400, r.text
    r = client.put(f"/api/books/{book.id}/years/2030/retained-subject",
                   headers=auth, json={"code": "310415"})
    assert r.status_code == 400, r.text


def test_设定要留下审计痕迹(client, auth, book, db):
    """改了「年度结转的留存收益冲到哪儿」是账务口径的改动 —— 必须留痕。"""
    from app.models import AuditLog

    client.put(f"/api/books/{book.id}/years/{book.start_year}/retained-subject",
               headers=auth, json={"code": "310415"})
    rows = db.scalars(select(AuditLog).where(
        AuditLog.book_id == book.id, AuditLog.entity == "book_year")).all()
    assert rows, "设定未分配利润科目没有写审计日志"
    assert "未分配利润" in rows[-1].detail
