"""**数据覆盖度**：本次统计实际覆盖了多少张凭证。

**为什么要有这一层。**

这个 MCP 最危险的不是「报错」，是**在数据缺失时给出一份结构完整的
「零值」或「看起来合理」的数字**。三个真实的例子：

* ``income_expense_trend(2026)`` 返回 12 期全 ``0.00``，还给出
  ``yoy = -100.00`` —— 而 2026 年实际有 171 张凭证，**只是没记账**。
  读到这个结果的人（包括 agent）会得出「今年收入归零」这个结论。
* ``render_report`` 在记账前给出资产总计 ``506,409.08``、平衡校验
  ``0.00``，看着一切正常；同一期科目余额表是 ``-2,021,759.90``。
* ``ledger_balance`` 区间查询返回期初 ``0.00``，真实值是 ``9,731,811.54``。

这三处的共同点是：**「0」既可能是「没有这笔业务」，也可能是「没记账」，
而返回值分不出来。** 所以凡是涉及凭证的工具，都必须带上这次的覆盖度 ——
让「0」这件事变成「0（原因见 coverage）」。

**口径。**

* ``posting`` —— 本次取数的记账状态口径：
  ``POSTED_ONLY``（只含已记账）/ ``POSTED_AND_UNPOSTED``（两者都含）/
  ``UNPOSTED_ONLY``（只含未记账）。
* ``posted_vouchers`` / ``unposted_vouchers`` —— **实际纳入统计**的张数。
* ``excluded_unposted_vouchers`` —— **因口径被排除**的未记账张数。
  **这一个字段才是「静默零值」的解药**：它是 0 时，零值就是真的零值；
  它不是 0 时，看到零值的人立刻知道「账还没记账」。
* ``periods_with_data`` / ``periods_total`` —— 期间覆盖度。
* ``completeness`` —— ``FULL`` / ``PARTIAL`` / ``NONE``。

**口径是「期间级」，不是「过滤后级」。** 统计的是**这些期间里**有多少张凭证
在记账、没记账，不跟着调用方的科目/摘要/金额过滤走。这样做有两个理由：

* 报告里那句最要紧的话是「2026 年实际有 171 张凭证，只是没记账」——
  **期间级**才是这个事实本身，缩到某个科目的过滤结果上反而说不清；
* 跟着过滤走就要把每一层过滤（科目、摘要、金额、结转）都复制一遍到统计里，
  少复制一层就是一次静默的漂移 —— 而这个块的全部价值就在于「它说的是真的」。

**为什么不算「金额覆盖率」。** 金额口径随科目而变（同一张凭证对不同科目
的意义不同），算出来的比例没有稳定含义；张数与期数是**与查询无关**的
事实，可以直接比。
"""
from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Period, Voucher

#: 只含已记账。
POSTING_POSTED_ONLY = "POSTED_ONLY"
#: 已记账 + 未记账。
POSTING_BOTH = "POSTED_AND_UNPOSTED"
#: 只含未记账（``search_vouchers(status="UNPOSTED")``）。
POSTING_UNPOSTED_ONLY = "UNPOSTED_ONLY"

#: 覆盖完整。
COMPLETENESS_FULL = "FULL"
#: 有东西没算进来（未记账凭证被口径排除）。
COMPLETENESS_PARTIAL = "PARTIAL"
#: 一张凭证都没有（连未记账的也没有）。
COMPLETENESS_NONE = "NONE"


def voucher_coverage(db: Session, book_id: int,
                     periods: Sequence[Period], *,
                     include_unposted: bool = False,
                     include_posted: bool = True,
                     ) -> dict:
    """这几个期间里，本次统计实际覆盖了多少张凭证。

    ``periods`` 是本次查询涉及的期间（可以跨年）。空列表 ⇒ 什么都没有，
    覆盖度是 ``NONE``。

    ``include_posted`` / ``include_unposted`` 说的是**本次取数纳入了哪一边**
    —— 与工具真正用的状态过滤保持一致（``search_vouchers`` 的
    ``status`` 可以是 ``POSTED`` / ``UNPOSTED`` / ``ALL``，标签不能
    说错）。
    """
    if not periods:
        return {
            "posting": _posting_label(include_posted, include_unposted),
            "posted_vouchers": 0,
            "unposted_vouchers": 0,
            "excluded_unposted_vouchers": 0,
            "periods_with_data": 0,
            "periods_total": 0,
            "completeness": COMPLETENESS_NONE,
        }

    period_ids = [p.id for p in periods]
    stmt = (
        select(Voucher.status, func.count(Voucher.id))
        .where(Voucher.book_id == book_id, Voucher.period_id.in_(period_ids))
        .group_by(Voucher.status)
    )
    posted = unposted = 0
    for status, count in db.execute(stmt).all():
        if status == "POSTED":
            posted = count
        else:
            unposted = count

    # 「有几期真的有凭证」：按期间分组数一遍。
    periods_with_data = (
        db.scalar(
            select(func.count(func.distinct(Voucher.period_id)))
            .where(Voucher.book_id == book_id, Voucher.period_id.in_(period_ids))
        ) or 0
    )

    in_scope_posted = posted if include_posted else 0
    in_scope_unposted = unposted if include_unposted else 0
    excluded = 0 if include_unposted else unposted

    # **判定的顺序有讲究。**
    #
    # ``NONE`` 说的是「这个区间里一张凭证都没有」——所以它看的是**不论记账
    # 状态**的总数。真要是先看「在范围内的张数 == 0」，一张未记账的凭证就会
    # 被算成「什么都没有」，而真相是「有 1 张，只是没记账」—— 那是 ``PARTIAL``，
    # 恰恰是最容易被误读成「没有业务」的那种情况。
    if posted + unposted == 0:
        completeness = COMPLETENESS_NONE
    elif excluded > 0:
        completeness = COMPLETENESS_PARTIAL
    else:
        completeness = COMPLETENESS_FULL

    return {
        "posting": _posting_label(include_posted, include_unposted),
        "posted_vouchers": in_scope_posted,
        "unposted_vouchers": in_scope_unposted,
        "excluded_unposted_vouchers": excluded,
        "periods_with_data": periods_with_data,
        "periods_total": len(periods),
        "completeness": completeness,
    }


def _posting_label(include_posted: bool, include_unposted: bool) -> str:
    """本次取数纳入了哪一边的记账状态。"""
    if include_posted and include_unposted:
        return POSTING_BOTH
    if include_unposted:
        return POSTING_UNPOSTED_ONLY
    return POSTING_POSTED_ONLY


def coverage_note(coverage: dict) -> Optional[str]:
    """把覆盖度写成人话（一句话）。

    **只在有问题的时候说话。** 覆盖度完整时多一句「一切正常」只会稀释
    真正需要注意的那几句。
    """
    excluded = coverage.get("excluded_unposted_vouchers") or 0
    if excluded:
        return (f"本次取数只含已记账凭证，这个区间里还有 {excluded} 张未记账凭证"
                f"没有算进来 —— 看到的「0」很可能是「还没记账」而不是「没有这笔业务」。"
                f"需要看全量请传 include_unposted=true。")
    if coverage.get("completeness") == COMPLETENESS_NONE:
        return ("本次统计的区间里一张凭证都没有（连未记账的也没有）—— 这里的 0 是"
                "「没有业务」，不是「算错了」。")
    return None


__all__ = [
    "COMPLETENESS_FULL", "COMPLETENESS_NONE", "COMPLETENESS_PARTIAL",
    "POSTING_BOTH", "POSTING_POSTED_ONLY", "POSTING_UNPOSTED_ONLY",
    "coverage_note", "voucher_coverage",
]
