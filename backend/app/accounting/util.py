"""业务层通用工具。"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, DecimalException, ROUND_HALF_UP

TWO = Decimal("0.01")
ZERO = Decimal("0")

# 金额量级上限：与 ``Numeric(20, 2)`` 的上界对齐（20 位里 2 位给小数）。
#
# 这道闸不是洁癖。超出这个值的金额有**两条**撞墙路径，而且都不是我们想看到的：
#   · 1e30 这类超大值在 ``quantize`` 就抛 InvalidOperation（Decimal 默认上下文
#     只有 28 位有效数字）—— 而 InvalidOperation 的祖先是 ArithmeticError，
#     **不是 ValueError**，于是全链路那些 ``except ValueError`` 一律接不住，
#     一张凭证直接 500（审计 P1-21）。
#   · 1e20 这类「quantize 能过、但列存不下」的值会一路走到 INSERT，
#     PostgreSQL 抛 NumericValueOutOfRange（sqlstate 22003），
#     而全局 DBAPIError 处理器只把 235xx 翻成 400，22003 一样落成 500。
# 量化之前先按上界拒掉，两条路都不会走到。
MONEY_LIMIT = Decimal("999999999999999999.99")


def money(value) -> Decimal:
    """统一金额精度：两位小数，四舍五入。**超量级、非法值一律抛 ValueError。**

    异常类型是这里最要紧的约定：调用链上有十几处 ``except ValueError`` 把
    金额问题翻成 400，而 Decimal 抛的 ``InvalidOperation`` 走的是
    ``DecimalException → ArithmeticError`` 这条线，不进 ValueError。
    所以这里**把 Decimal 异常翻译成 ValueError 再抛**（而不是让它逃出去），
    让「金额非法」在全系统只有一种表现。
    """
    if value is None:
        return ZERO
    try:
        d = Decimal(str(value))
    except (DecimalException, ArithmeticError, TypeError) as exc:
        raise ValueError(f"{value!r} 不是合法金额") from exc
    # NaN / Infinity 必须单独挡：``Decimal('NaN').quantize(TWO)`` 不抛异常，
    # 返回的还是 NaN。它会一路进 sum()，而 NaN 的比较恒为 False ——
    # 借贷平衡那道 ``dr != cr`` 将永远为假，一张「NaN 凭证」就这么存进去了。
    if not d.is_finite():
        raise ValueError(f"{value!r} 不是合法金额（NaN / 无穷大）")
    if d > MONEY_LIMIT or d < -MONEY_LIMIT:
        raise ValueError(
            f"金额 {value} 超出可表示范围（最大 ±{MONEY_LIMIT}，"
            "与库里 Numeric(20,2) 的上界一致）")
    try:
        return d.quantize(TWO, rounding=ROUND_HALF_UP)
    except (DecimalException, ArithmeticError) as exc:
        # 到这里只剩「位数太多量化不动」这一种可能（上面已经挡掉量级）。
        raise ValueError(f"金额 {value} 无法按两位小数量化") from exc


def period_label(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


#: 汇总行标签（审计 P2-13）。
#:
#: 放在 util 而不是各自留在导入器里，是因为**两个导入器都需要它**：
#: 期初余额导入早就做了标签过滤，凭证导入却只有结构性判据，于是同一份
#: 用友表，期初能剔干净、凭证会漏 —— 漏掉的那一行会把
#: 「找不到科目「合计」」挂到**前一张真实凭证**头上，而那张凭证因此被判
#: 不通过、静默地不进库。各写一份迟早会再次对不上。
SUMMARY_LABELS = frozenset({
    "资产小计", "负债小计", "权益小计", "损益小计",
    "合计", "总计", "本页合计", "累计", "本年累计",
})


def is_summary_label(*texts: str | None) -> bool:
    """这一行是不是「资产小计 / 合计 / 本年累计」这类汇总行。

    两列都判一遍：真实文件里标签常写在「科目编码」列、名称列留空，
    但别的版本会写进名称列。

    **必须整行剔除、不能当普通数据行**：这些行是**已经加总过的数**
    （合计行的借贷 = 明细行加总），当成科目会重复计入，合计直接翻倍。
    **也不能报成错误**：它不是用户的错，报错等于让人去改一张没错的表。

    匹配前先去空白 —— 表里写「本页 合计」也是同一行。
    """
    for text in texts:
        compact = re.sub(r"\s+", "", text or "")
        if compact and compact in SUMMARY_LABELS:
            return True
    return False


def constraint_name(exc: Exception) -> str:
    """从数据库异常里取出**被违反的约束名**。

    撞号要翻成人话，但翻之前必须先认得出是哪一条约束：
    别的唯一约束（外键指向不存在、字段非空）撞上时报「凭证号重复」是**假话**，
    用户会照着去改一个根本没问题的字段。所以「凡 IntegrityError 都当撞号」
    这种写法在这里是错的。
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    return str(getattr(diag, "constraint_name", "") or "")


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
