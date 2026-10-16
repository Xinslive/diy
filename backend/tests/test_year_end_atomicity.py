"""年度结转的原子性（审计 P1-04）。

``year_end_carry`` 是多步骤操作（解锁 12 月 → 删旧结转凭证 → 建结转凭证
→ 记账 → 结账 → 追加次年期间 → 刷新期初），原来却在中间 commit 了四次。
路由层捕获 ValueError 后 ``db.rollback()`` **只能撤销最后一次 commit 之后
的部分**，之前已提交的留在库里。

审计实测：构造「次年已有凭证」让它在刷新期初那步失败 ——
接口明确返回 400「2027 年已经有 1 张凭证，不能再刷新期初余额」，
但本年利润已被冲平（-100 → 0）、未分配利润变成 -100、结转凭证还留着。

这比报错本身危险得多：失败响应让用户以为「什么都没发生」而重试或改数，
而库里已经有半成品。

这里的断言口径是**失败后数据库与调用前完全一致**，不是「没报错」。
逐项检查：结转凭证、本年利润、未分配利润、期间锁状态、期初记录数。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.accounting.periods import (carry_forward_profit, close_period,
                                    year_end_carry)
from app.ledger.engine import LedgerEngine
from app.models import Entry, Period, PeriodOpening, Subject, Voucher


def sid(subjects, code):
    return subjects[code].id


def _seed_2027_placeholder(db, book, subjects, admin_user):
    """预先铺好 2027 并塞一张凭证，让「刷新期初」那一步必然失败。

    这是审计报告里复现 P1-04 的构造。不能把凭证落在 2026-12：
    那个月此时已结账，create_voucher 会先拒绝（这本身是对的）。
    """
    from app.accounting.periods import ensure_periods
    from app.accounting.vouchers import create_voucher

    new_periods = ensure_periods(db, book, 2027)
    jan27 = new_periods[0]
    create_voucher(db, book_id=book.id, user=admin_user, voucher_date=date(2027, 1, 15),
                   entries=[
                       {"subject_id": sid(subjects, "1002"), "direction": "DEBIT",
                        "amount": Decimal("5"), "summary": "占位"},
                       {"subject_id": sid(subjects, "5001"), "direction": "CREDIT",
                        "amount": Decimal("5"), "summary": "占位"},
                   ], period_id=jan27.id, commit=True)
    return jan27


def _close_year(db, book, periods, admin_user):
    for m in range(1, 13):
        p = db.get(Period, periods[m].id)
        if p.locked:
            continue
        try:
            carry_forward_profit(db, book.id, p, admin_user)
        except ValueError:
            pass
        close_period(db, p, admin_user)


def _snapshot(db, book, subjects):
    """把「与结转有关的全部状态」拍成一张可比对的快照。"""
    eng = LedgerEngine(db, book.id)
    dec_ord = db.scalar(select(Period.ord).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 12))
    carry = sorted(
        v.id for v in db.scalars(select(Voucher).where(
            Voucher.book_id == book.id,
            Voucher.remark == "2026年 结转本年利润")).all())
    return {
        "profit": eng.closing(sid(subjects, "3103"), dec_ord),
        "retained": eng.closing(sid(subjects, "3104"), dec_ord),
        "carry_ids": carry,
        "dec_locked": bool(db.scalar(select(Period.locked).where(
            Period.book_id == book.id, Period.year == 2026, Period.month == 12))),
        "openings_2027": db.scalar(
            select(PeriodOpening.id).where(PeriodOpening.book_id == book.id)
            .join(Period, Period.id == PeriodOpening.period_id)
            .where(Period.year == 2027).limit(1)) is not None,
        "years": sorted(set(db.scalars(
            select(Period.year).where(Period.book_id == book.id)).all())),
    }


def test_结转中途失败时数据库与调用前一致(
    db, book, periods, subjects, admin_user, make_voucher
):
    """审计 P1-04 的核心场景：刷新期初那一步失败，前面几步必须一起回滚。"""
    # 亏损年度：本年利润落在借方，结转会把它冲平
    make_voucher("2026-01-10", dr=[("560206", 100, "x")],
                 cr=[("1002", 100, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    # 先手工把 2027 年铺出来并塞一张凭证，让「刷新期初」那步必然失败
    _seed_2027_placeholder(db, book, subjects, admin_user)

    before = _snapshot(db, book, subjects)
    # 2027 是上面刻意预铺的：它已有期间、也已有凭证，所以「刷新期初」必然被挡
    assert before["years"] == [2026, 2027], "前置条件没搭对"
    assert before["profit"] != Decimal("0"), "前置条件：2026 应当是亏损年度，本年利润在借方"

    # 必须显式指定 2026 → 2027：默认口径会挑「最后一个有活动的年度」，
    # 也就是预铺出来的 2027，那样会在「2027 必须全部结账」那一步就失败 ——
    # 而那一步在任何写操作之前，测的就不是「中途失败」了。
    with pytest.raises(ValueError, match="已经有.*张凭证"):
        year_end_carry(db, book, admin_user, 2026, 2027)
    db.rollback()

    after = _snapshot(db, book, subjects)
    assert after == before, (
        "年度结转失败后数据库没有回到调用前状态：\n"
        f"  调用前：{before}\n  回滚后：{after}")


def test_结转失败不留残留凭证(db, book, periods, subjects, admin_user, make_voucher):
    """失败后不能有「结转凭证已经生成却没记完」的半成品。"""
    make_voucher("2026-01-10", dr=[("560206", 100, "x")],
                 cr=[("1002", 100, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    _seed_2027_placeholder(db, book, subjects, admin_user)

    n_before = db.scalar(select(Voucher).where(Voucher.book_id == book.id).with_only_columns(
        __import__("sqlalchemy").func.count()))

    with pytest.raises(ValueError, match="已经有.*张凭证"):
        year_end_carry(db, book, admin_user, 2026, 2027)
    db.rollback()
    db.expire_all()

    n_after = db.scalar(select(Voucher).where(Voucher.book_id == book.id).with_only_columns(
        __import__("sqlalchemy").func.count()))
    assert n_after == n_before, f"失败后凭证数变了：{n_before} → {n_after}"

    leftover = db.scalars(select(Voucher).where(
        Voucher.book_id == book.id,
        Voucher.remark == "2026年 结转本年利润")).all()
    assert leftover == [], f"残留了结转凭证：{[v.voucher_no for v in leftover]}"


def test_结转失败不动本年利润(db, book, periods, subjects, admin_user, make_voucher):
    """本年利润不能被冲平一半 —— 这是审计里最刺眼的那条。"""
    make_voucher("2026-01-10", dr=[("560206", 100, "x")],
                 cr=[("1002", 100, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    eng = LedgerEngine(db, book.id)
    dec_ord = db.scalar(select(Period.ord).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 12))
    before = eng.closing(sid(subjects, "3103"), dec_ord)
    retained_before = eng.closing(sid(subjects, "3104"), dec_ord)

    _seed_2027_placeholder(db, book, subjects, admin_user)

    with pytest.raises(ValueError, match="已经有.*张凭证"):
        year_end_carry(db, book, admin_user, 2026, 2027)
    db.rollback()
    db.expire_all()

    eng = LedgerEngine(db, book.id)
    assert eng.closing(sid(subjects, "3103"), dec_ord) == before, \
        f"本年利润被改了：{before} → {eng.closing(sid(subjects, '3103'), dec_ord)}"
    assert eng.closing(sid(subjects, "3104"), dec_ord) == retained_before, \
        "未分配利润被改了"


def test_成功的结转一次提交(db, book, periods, subjects, admin_user, make_voucher):
    """修好原子性不能把正常路径搞坏：成功的结转照常完成全部步骤。"""
    make_voucher("2026-01-10", dr=[("560206", 100, "x")],
                 cr=[("1002", 100, "x")], posted=True)
    _close_year(db, book, periods, admin_user)

    r = year_end_carry(db, book, admin_user)

    assert r["new_year"] == 2027
    jan27 = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2027, Period.month == 1))
    assert jan27 is not None, "2027-01 没有建出来"
    # 本年利润冲平、留存转过去
    eng = LedgerEngine(db, book.id)
    dec_ord = db.scalar(select(Period.ord).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == 12))
    assert eng.closing(sid(subjects, "3103"), dec_ord) == Decimal("0")
    bank_opening = db.scalar(select(PeriodOpening).where(
        PeriodOpening.period_id == jan27.id,
        PeriodOpening.subject_id == sid(subjects, "1002")))
    # 银行存款期初：dr 6602 / cr 1002，现金实际是减少的，导出/期初存的是实际方位
    assert bank_opening is not None, "2027-01 缺少 1002 的期初"
    assert bank_opening.amount == Decimal("-100")


def test_HTTP层失败也是原子的(client, db, book, periods, subjects, admin_user,
                             make_voucher, auth):
    """接口层：返回 400 之后，库里必须什么都没变。"""
    make_voucher("2026-01-10", dr=[("560206", 100, "x")],
                 cr=[("1002", 100, "x")], posted=True)
    _close_year(db, book, periods, admin_user)
    _seed_2027_placeholder(db, book, subjects, admin_user)
    before = _snapshot(db, book, subjects)

    r = client.post(f"/api/books/{book.id}/year-end", headers=auth,
                    json={"from_year": 2026})
    assert r.status_code == 400, f"应该失败，实际 {r.status_code}：{r.text}"

    db.expire_all()
    assert _snapshot(db, book, subjects) == before, "接口返回失败但库里已经变了"
