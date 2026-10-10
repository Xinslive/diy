"""业务层通用工具。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

TWO = Decimal("0.01")
ZERO = Decimal("0")


def money(value) -> Decimal:
    """统一金额精度：两位小数，四舍五入。"""
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(TWO, rounding=ROUND_HALF_UP)


def period_label(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def find_period(db, book_id: int, d: date):
    """按凭证日期定位所属会计期间。"""
    from app.models import Period
    from sqlalchemy import select

    period = db.scalar(
        select(Period).where(
            Period.book_id == book_id,
            Period.year == d.year,
            Period.month == d.month,
        )
    )
    if period is None:
        raise ValueError(f"{d.year}-{d.month:02d} 不在本账套的会计期间范围内")
    return period


def previous_period(db, period):
    """账套内紧邻的上一期。跨年度靠 ord 连续，所以 1 月的上一期就是上年 12 月。

    没有上一期（账套的第一个期间）返回 None。
    """
    from app.models import Period
    from sqlalchemy import select

    return db.scalar(
        select(Period).where(
            Period.book_id == period.book_id,
            Period.ord == period.ord - 1,
        )
    )
