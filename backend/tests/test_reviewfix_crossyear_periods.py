"""跨年结账与年度结转的两处会计正确性缺陷（审查修复）。

两条：

* **跨年链结账被架空**（``app/accounting/periods.py::close_period``）
  —— 级联结账的链是「ord <= 被结那一期的全部未结账期间」，而 ord 跨年度
  连续，链是可以跨年的。以前跨年链是被**默默放行**的，而损益结平校验
  只取 ``target.year`` 那一套科目表：链上属于**其他年度**的期间用的是
  **另一年的科目行** —— 那些行在那些期间既没有分录也没有期初快照，
  ``closing`` 恒为 0，于是**被判成「已结平」**。前几年挂在损益科目上的
  余额就这么被放过去了，而且期间还被锁上了。
  现在跨年链**当场拒绝**，并说清下一步该做什么。

* **关掉结转的损益科目被搬成下年期初**（``_refresh_opening``）
  —— ``carry_forward=False`` 的损益科目既不被结转损益结走、也不被结账的
  结平校验盯着（两侧共用 ``carry_forward_subjects``，自洽），而承接期初
  时**不看 category、不看 carry_forward** —— 于是那笔损益被写进**下一年
  1 月的期初快照**，挂在损益科目上：下一年利润表看不到它、资产负债表不
  平，而**全程无报错**。现在年度结转在这里 fail closed。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Period, PeriodOpening, Subject


def _period(db, book, year: int, month: int) -> Period:
    p = db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == year, Period.month == month))
    assert p is not None, f"账套 {book.id} 没有 {year}-{month:02d} 这个期间"
    return p


def _open_next_year(db, book, year: int) -> None:
    """给账套铺出下一年的期间与科目表（年度结转之前也可以先开新年）。"""
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart

    copy_chart(db, book.id, year - 1, year)
    ensure_periods(db, book, year)
    db.commit()


# --------------------------------------------------------------------- 1. 跨年链


def test_跨年结账被拒并要求先结完上一年度(db, book, admin_user, make_voucher):
    """结 2027 年的账，而 2026 年还有没结账的期间 → **拒绝**。

    修好之前：这条链被默默放行 —— 2026-12 与 2027-01 一起被锁上，
    而 2026-12 的损益余额（挂在 2026 的科目行上）因为校验用的是
    2027 的科目表而恒为 0，被判成「已结平」。
    """
    from app.accounting.periods import close_period

    _open_next_year(db, book, 2027)
    # 2026-12：一笔收入，**没有结转损益**（损益科目上还挂着余额）
    make_voucher("2026-12-10", dr=[("1002", 100, "收款")], cr=[("5001", 100, "收入")])
    # 2027-01：也有凭证（不然会先撞上「空月份不许结账」那道闸）
    make_voucher("2027-01-10", dr=[("1002", 50, "收款")], cr=[("5001", 50, "收入")])
    db.commit()

    target = _period(db, book, 2027, 1)
    with pytest.raises(ValueError) as exc:
        close_period(db, target, admin_user)
    message = str(exc.value)
    assert "先结完上一年度" in message, f"报错要说清下一步做什么：{message}"
    assert "2026" in message, f"报错要点名上一年度：{message}"


def test_跨年结账被拒时期间没有被锁上(db, book, admin_user, make_voucher):
    """拒绝必须是**什么都没做**：期间不能被锁，凭证不能被记账。"""
    from app.accounting.periods import close_period
    from app.models import Voucher

    _open_next_year(db, book, 2027)
    make_voucher("2026-12-10", dr=[("1002", 100, "收款")], cr=[("5001", 100, "收入")])
    make_voucher("2027-01-10", dr=[("1002", 50, "收款")], cr=[("5001", 50, "收入")])
    db.commit()

    target = _period(db, book, 2027, 1)
    with pytest.raises(ValueError, match="先结完上一年度"):
        close_period(db, target, admin_user)
    db.rollback()
    db.expire_all()

    for year, month in ((2026, 12), (2027, 1)):
        p = _period(db, book, year, month)
        assert p.locked is False, f"{p.code} 被锁上了 —— 拒绝就该什么都不做"
    statuses = set(db.scalars(select(Voucher.status).where(
        Voucher.book_id == book.id)).all())
    assert "POSTED" not in statuses, "拒绝之后不该有任何凭证被自动记账"


def test_同一条链上每一期都按自己的年度查损益(db, book, admin_user, make_voucher):
    """结平校验用的科目表必须来自**被校验期间自己的年度**。

    同一年度内的链（2026-01 ~ 2026-12）里，11 月的损益没结走时，
    结 12 月必须被拦下 —— 这一格报的是「11 月那一期」的余额。
    """
    from tests.accounting_helpers import close_any

    for month in range(1, 11):
        close_any(db, book, _period(db, book, 2026, month), admin_user)
    db.commit()
    # 11 月：一笔收入，不结转
    make_voucher("2026-11-10", dr=[("1002", 100, "收款")], cr=[("5001", 100, "收入")])
    db.commit()

    with pytest.raises(ValueError) as exc:
        close_any(db, book, _period(db, book, 2026, 12), admin_user)
    message = str(exc.value)
    assert "损益科目有余额" in message, message
    assert "2026-11" in message, f"报错要点名是哪一期没结平：{message}"
    assert "5001" in message, f"报错要点名是哪个科目没结平：{message}"


# --------------------------------------------------------------------- 2. 关掉结转的损益科目


def test_关掉结转的损益科目挡住年度结转(db, book, admin_user, make_voucher):
    """损益科目被关掉「结转」且有余额 → **年度结转必须被拒**。

    修好之前：它的余额被写进下一年度 1 月的期初快照（挂在损益科目上）——
    下一年利润表看不到它、资产负债表不平，而全程无报错。
    """
    from app.accounting.periods import year_end_carry
    from tests.accounting_helpers import close_any

    subject = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == 2026, Subject.code == "5001"))
    assert subject is not None, "标准科目表里没有 5001"
    subject.carry_forward = False       # 用户把「结转」关掉了
    db.commit()

    make_voucher("2026-12-10", dr=[("1002", 100, "收款")], cr=[("5001", 100, "收入")])
    db.commit()

    for month in range(1, 13):
        close_any(db, book, _period(db, book, 2026, month), admin_user)
    db.commit()

    with pytest.raises(ValueError) as exc:
        year_end_carry(db, book, admin_user, from_year=2026)
    message = str(exc.value)
    assert "5001" in message, f"报错要点名是哪个科目：{message}"
    assert "结转" in message, f"报错要说清为什么：{message}"


def test_年度结转被拒时没有写下年期初(db, book, admin_user, make_voucher):
    """拒绝必须是**什么都没做**：下一年度的期初不能被写出来。"""
    from app.accounting.periods import year_end_carry
    from tests.accounting_helpers import close_any

    subject = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == 2026, Subject.code == "5001"))
    subject.carry_forward = False
    db.commit()

    make_voucher("2026-12-10", dr=[("1002", 100, "收款")], cr=[("5001", 100, "收入")])
    db.commit()
    for month in range(1, 13):
        close_any(db, book, _period(db, book, 2026, month), admin_user)
    db.commit()

    with pytest.raises(ValueError, match="5001"):
        year_end_carry(db, book, admin_user, from_year=2026)
    db.rollback()
    db.expire_all()

    openings = db.scalars(select(PeriodOpening).where(
        PeriodOpening.book_id == book.id)).all()
    assert openings == [], "拒绝之后不该留下任何期初快照"
