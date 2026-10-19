"""凭证号补零位数的一次性对齐（幂等）。

位数跟着 `organize.NO_WIDTH` 走（2026-10-16 由三位改回四位）。改位数时把
**还没结账的期间**里已有的号一起对齐，不然同一个月的列表里上半截 记-0057、
下半截 记-058，两种宽度混排比全是四位还难看。

两条硬规矩：

1. **只补零，不改数字**：`记007 → 记0007`、`记-058 → 记-0058`。
   号码的大小关系、期间的先后顺序都不变，补零在整数上是一一对应的，
   所以不会出现两张凭证撞成同一个号。

2. **已结账的期间一律不动**。`fn_voucher_guard` 那个触发器本来就禁止改已结账
   期间的凭证 —— 那道护栏是对的：结账意味着这段账簿已经定稿，凭证号是账的一部分，
   不是排版。所以这里只在开着的期间里改，改不了的那些老实留着。
   （早先的写法是全表改，被触发器当场顶回来，整个事务回滚，一张都没改成 ——
   而异常被 `except Exception` 吞成一行日志，页面上看着「什么都没发生」。）
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Period, Voucher

log = logging.getLogger("cwq.init")

from app.accounting.organize import NO_WIDTH

# 缺省宽度跟着发号口径走（organize.NO_WIDTH）：这里干的事是「把已经在库里的号
# 补到同一个宽度」，不是另立一套宽度 —— 两处各写一个数字，改一处就又混排了。
WIDTH = NO_WIDTH
NO_RE = re.compile(r"^(?P<prefix>[^\d]{1,4})(?P<seq>\d+)$")


def repad_voucher_numbers(db: Session, width: int = WIDTH,
                          book_id: int | None = None) -> tuple[int, int]:
    """把**开着的**期间里位数不足的凭证号补到 ``width`` 位。

    返回 `(改了多少张, 因为已结账而跳过了多少张)`。

    ``book_id`` 给了就只动那一个账套，不给就是全库 —— 全库是维护脚本的口径
    （「上线时把历史数据一次性对齐」），但它同时意味着这个函数的返回值**依赖
    库里此刻有什么**。测试库里各用例的账套是逐个累积的（见 conftest 里
    ``db`` 夹具为什么没有 savepoint），于是「本次改了几张」这种断言会被别的
    用例留下的凭证污染 —— 单跑绿、全量红。加一个作用域参数既符合直觉
    （修一个账套的数据凭什么要动全库），也让这个函数的返回值变成可确定断言的。
    """
    def _scope(stmt):
        return stmt.where(Voucher.book_id == book_id) if book_id is not None else stmt

    changed = 0
    for v in db.scalars(
        _scope(
            select(Voucher).join(Period, Voucher.period_id == Period.id)
            .where(Period.locked.is_(False))
        )
    ).all():
        m = NO_RE.match(v.voucher_no or "")
        if not m:
            continue                       # 非「前缀 + 数字」的手工号，不动
        digits = m.group("seq")
        n = int(digits)
        # 按**数字本身该有的宽度**判，而不是按现在的字符串长度 ——
        # 现在的长度恰恰是要改的那件事（记0007 的长度是 4，正是要收窄成 3）。
        if len(digits) == max(width, len(str(n))):
            continue                       # 已经是该有的样子，不动
        v.voucher_no = m.group("prefix") + str(n).rjust(width, "0")
        changed += 1

    skipped = db.scalar(
        _scope(
            select(func.count()).select_from(Voucher)
            .join(Period, Voucher.period_id == Period.id)
            .where(Period.locked.is_(True))
        )
    ) or 0
    if changed:
        db.commit()
    if changed or skipped:
        log.info("凭证号补零到 %d 位：改了 %d 张；%d 张在已结账期间里，按规矩不动",
                 width, changed, skipped)
    return changed, skipped
