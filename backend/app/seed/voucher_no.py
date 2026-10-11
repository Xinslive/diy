"""凭证号补零位数的一次性对齐（幂等）。

发号口径从四位（记0022）改成三位（记022），把**还没结账的期间**里已有的号一起收窄，
不然同一个月的列表里上半截 记0022、下半截 记023，两种宽度混排比全是四位还难看。

两条硬规矩：

1. **只补零，不改数字**：`记0007 → 记007`、`记0022 → 记022`。
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

WIDTH = 3
NO_RE = re.compile(r"^(?P<prefix>[^\d]{1,4})(?P<seq>\d+)$")


def repad_voucher_numbers(db: Session, width: int = WIDTH) -> tuple[int, int]:
    """把**开着的**期间里位数不足的凭证号补到 `width` 位。

    返回 `(改了多少张, 因为已结账而跳过了多少张)`。
    """
    changed = 0
    for v in db.scalars(
        select(Voucher).join(Period, Voucher.period_id == Period.id)
        .where(Period.locked.is_(False))
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
        select(func.count()).select_from(Voucher)
        .join(Period, Voucher.period_id == Period.id)
        .where(Period.locked.is_(True))
    ) or 0
    if changed:
        db.commit()
    if changed or skipped:
        log.info("凭证号补零到 %d 位：改了 %d 张；%d 张在已结账期间里，按规矩不动",
                 width, changed, skipped)
    return changed, skipped
