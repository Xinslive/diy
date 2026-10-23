"""年度结转的盈亏方向（审计 P1-03）。

原来本年利润结转**不区分盈亏**，一律生成「借本年利润 / 贷未分配利润」，
注释还写着两种情况都成立 —— 这个判断是错的。

系统内部口径是「借正贷负」（closing = opening + period_dr - period_cr）：

    盈利：bal<0，本年利润在贷方 → 借方冲平 / 贷方增加留存
    亏损：bal>0，本年利润在借方 → 贷方冲平 / 借方减少留存

按老代码做亏损，本年利润不但没被冲平，反而**再记一笔借方**：
审计实测 100 的亏损被结成 200。而且因为分录借贷平衡，
系统自检不会报错 —— 错误静默沉淀，污染次年每一份报表。

四个场景都断言「本年利润结转后必须为 0」，而不是只断言未分配利润的符号：
只查符号的话，100 结成 200 这种「方向对、金额错」的情况照样能蒙混过关。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.accounting.periods import (carry_forward_profit, close_period,
                                    year_end_carry)
from app.ledger.engine import LedgerEngine
from app.models import Period, PeriodOpening, Subject, Voucher
from tests.accounting_helpers import close_any


def sid(subjects, code):
    return subjects[code].id


def _close_year(db, book, periods, admin_user):
    for m in range(1, 13):
        period = db.get(Period, periods[m].id)
        if period.locked:
            continue
        try:
            carry_forward_profit(db, book.id, period, admin_user)
        except ValueError:
            pass          # 该月没损益，跳过
        close_any(db, book, period, admin_user)


def _dec26_ord(db, book):
    return db.scalar(select(Period.ord).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 12))


def _closing(db, book, subject_id, ord_):
    return LedgerEngine(db, book.id).figures(subject_id, ord_).closing


def _retained_opening_2027(db, book, code):
    """次年 1 月期初里，这个**编码**的余额。

    **按编码找，不按 id**：科目表每个年度各有一份（迁移 0032），
    2026 的 3104 与 2027 的 3104 是**两行不同的记录、两个 id** ——
    拿 2026 的 id 去查 2027 年的期初，永远查不到（会误判成「没结转」）。
    """
    jan27 = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2027, Period.month == 1))
    subject_id = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.year == 2027, Subject.code == code))
    o = db.scalar(select(PeriodOpening).where(
        PeriodOpening.period_id == jan27.id, PeriodOpening.subject_id == subject_id))
    return None if o is None else o.amount


def test_盈利年度结转后本年利润归零(db, book, periods, subjects, admin_user, make_voucher):
    """收入 > 费用：留存收益增加。"""
    make_voucher("2026-01-10", dr=[("1002", 1000, "x")],
                 cr=[("5001", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user)

    profit, retained = sid(subjects, "3103"), sid(subjects, "3104")
    assert _closing(db, book, profit, _dec26_ord(db, book)) == Decimal("0"), "盈利年度本年利润没冲平"
    assert _retained_opening_2027(db, book, "3104") == Decimal("-1000")   # 贷方，留存增加


def test_亏损年度结转后本年利润归零(db, book, periods, subjects, admin_user, make_voucher):
    """审计 P1-03 的核心场景：费用 > 收入，留存收益应当**减少**。"""
    # 只有费用没有收入 → 本年利润落在借方
    make_voucher("2026-01-10", dr=[("560206", 500, "x")],
                 cr=[("1002", 500, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    before = _closing(db, book, sid(subjects, "3103"), _dec26_ord(db, book))
    assert before > 0, f"前置条件不成立：本年利润应当在借方（借正贷负），实际 {before}"

    year_end_carry(db, book, admin_user)

    profit, retained = sid(subjects, "3103"), sid(subjects, "3104")
    after = _closing(db, book, profit, _dec26_ord(db, book))
    assert after == Decimal("0"), (
        f"亏损年度本年利润应冲平为 0，实际 {after}（结转前 {before}）")
    # 亏损 500 → 未分配利润借方 500
    assert _retained_opening_2027(db, book, "3104") == Decimal("500")


def test_亏损不是翻倍(db, book, periods, subjects, admin_user, make_voucher):
    """回归老 bug 的具体形态：100 的亏损被结成 200。"""
    make_voucher("2026-01-10", dr=[("560206", 100, "x")],
                 cr=[("1002", 100, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user)

    assert _closing(db, book, sid(subjects, "3103"),
                    _dec26_ord(db, book)) == Decimal("0")


def test_零余额年度不生成结转凭证(db, book, periods, subjects, admin_user):
    """没赚没亏不该凭空造一张结转凭证。"""
    _close_year(db, book, periods, admin_user)
    n_before = db.scalar(select(Voucher).where(Voucher.book_id == book.id).with_only_columns(
        __import__("sqlalchemy").func.count()))

    r = year_end_carry(db, book, admin_user)

    n_after = db.scalar(select(Voucher).where(Voucher.book_id == book.id).with_only_columns(
        __import__("sqlalchemy").func.count()))
    assert n_after == n_before, "零余额年度不该新增结转凭证"
    assert r["carried_subjects"] >= 0


def test_重复年度结转是幂等的(db, book, periods, subjects, admin_user, make_voucher):
    """再点一次不能让未分配利润翻倍 —— 结转凭证会被先删后重建。

    第二次必须显式指定 2026：默认口径会取「最后一个有活动的年度」，
    那时已经是 2027 了，会去结转 2027（还没结账，直接报错），
    测的就不是幂等而是年度选择了。
    """
    make_voucher("2026-01-10", dr=[("560206", 100, "x")],
                 cr=[("1002", 100, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    year_end_carry(db, book, admin_user, 2026, 2027)
    first = _retained_opening_2027(db, book, "3104")

    year_end_carry(db, book, admin_user, 2026, 2027)
    second = _retained_opening_2027(db, book, "3104")

    assert second == first, f"重复结转后留存收益变了：{first} → {second}"
    # 本年利润也不能被结第二次结出新的差额
    assert _closing(db, book, sid(subjects, "3103"), _dec26_ord(db, book)) == Decimal("0")


def _carry_entry_directions(db, book, subjects):
    """取出年度结转凭证里，本年利润与未分配利润各自记在哪一侧。"""
    from app.models import Entry

    v = db.scalars(select(Voucher).where(
        Voucher.book_id == book.id,
        # remark 末尾带一段签名尾巴（见 periods._carry_forward_token），
        # 所以这里按前缀找，不能按全等找。
        Voucher.remark.startswith("2026年 结转本年利润")
    ).order_by(Voucher.id.desc())).first()
    assert v is not None, "没找到年度结转凭证"
    # 留存那条要取 310415 未分配利润（末级，记账接口只认末级），
    # 不是它的上级 3104 —— 分录挂不到非末级上。
    profit, retained = sid(subjects, "3103"), sid(subjects, "310415")
    out = {}
    for e in db.scalars(select(Entry).where(Entry.voucher_id == v.id)).all():
        if e.subject_id in (profit, retained):
            out["本年利润" if e.subject_id == profit else "未分配利润"] = e.direction
    return out


def test_亏损的结转凭证贷记本年利润(db, book, periods, subjects, admin_user, make_voucher):
    """直接看分录方向：亏损要在**贷方**冲平本年利润，借方冲减留存。"""
    make_voucher("2026-01-10", dr=[("560206", 300, "x")],
                 cr=[("1002", 300, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user)

    d = _carry_entry_directions(db, book, subjects)
    assert d["本年利润"] == "CREDIT", f"亏损年度本年利润方向错：{d}"
    assert d["未分配利润"] == "DEBIT", f"亏损年度留存方向错：{d}"


def test_盈利的结转凭证借记本年利润(db, book, periods, subjects, admin_user, make_voucher):
    """盈利反过来：借方冲平本年利润，贷方增加留存。"""
    make_voucher("2026-01-10", dr=[("1002", 1000, "x")],
                 cr=[("5001", 1000, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    year_end_carry(db, book, admin_user)

    d = _carry_entry_directions(db, book, subjects)
    assert d["本年利润"] == "DEBIT", f"盈利年度本年利润方向错：{d}"
    assert d["未分配利润"] == "CREDIT", f"盈利年度留存方向错：{d}"
