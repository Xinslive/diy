"""凭证被删掉时，把它生成出来的那几张发票**放回「可生成」**。

**为什么单独成文件。**

``app.invoice.service`` 在模块层 import 了 ``app.accounting.vouchers``
（它要建凭证），而 ``app.accounting.vouchers`` 删凭证时要回调这里 ——
两个模块互相 import 就成环了。所以回调方需要的这几个函数放在一个
**不 import 任何 accounting 模块**的独立文件里，两边都能安全地 import 它。

**为什么要有这个动作。**

不变式只有一条：

    ``invoice_record.status == 'COMMITTED'`` ⟺ ``invoice_record.voucher_id IS NOT NULL``

数据库的外键是 ``ON DELETE SET NULL``：凭证一删，发票记录的 ``voucher_id``
自动变空 —— 但 ``status`` 还停在「已生成」。两边就对不上了。

对不上的后果不是「显示难看」，是**这张发票再也变不出凭证**：

* 服务端对「已生成」的记录**跳过重算**（``_build_preview`` 里那条规矩），
  它不会变回「可生成」；
* 「确认生成」只认「可生成」的那些，所以点下去是 0 张。

于是用户的处境是：账上那张凭证他亲手删了，界面上这张发票还标着「已生成」，
却再也没有任何按钮能让他用这张票重新生成一张凭证。

删凭证时顺手把它放回「可生成」，不变式就由删除路径自己维持住了。
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import InvoiceBatch, InvoiceRecord


def release_records_for_vouchers(db: Session, voucher_ids: Iterable[int]) -> int:
    """把「由这些凭证生成出来的发票记录」放回可生成，返回放掉了几张。

    放掉的动作有三件，缺一不可：

    1. ``status`` 回到 ``READY`` —— 它重新变成「可生成」，下一次「确认生成」
       会带上它。
    2. ``voucher_id`` 清空 —— 凭证已经没了，这个指向必须是空的
       （数据库的 ``ON DELETE SET NULL`` 也会做，但这里先做掉，
       ORM 与库里不会出现「一半旧一半新」的中间态）。
    3. **把它占着的流水放掉** —— 结算科目是从对账单流水上认出来的，
       发票被放回可生成时若还占着那条流水，重算时反而认不回自己。

    批次的计数也要跟着走：``committed_count`` 是「这一批现在有几张已入账」，
    少了一张就该少一；一张都不剩时批次回到「待确认」—— 那一栏写着「已生成」，
    而实际上一个凭证都没了。
    """
    ids = [int(v) for v in voucher_ids if v]
    if not ids:
        return 0
    records = db.scalars(
        select(InvoiceRecord).where(InvoiceRecord.voucher_id.in_(ids))
    ).all()
    if not records:
        return 0

    released_per_batch: dict[int, int] = {}
    for record in records:
        record.status = "READY"
        record.voucher_id = None
        released_per_batch[record.batch_id] = released_per_batch.get(record.batch_id, 0) + 1
    # **必须显式 flush。** 这个会话关掉了自动 flush
    # （``app/core/db.py`` 里是 ``autoflush=False``）—— 不 flush 的话，
    # 下面那个「还剩几张已入账」的查询读到的是**旧值**：刚放掉的那张还躺在
    # 库里说「已生成、有凭证」，于是批次永远回不到「待确认」。
    db.flush()

    # 放掉它占着的流水 —— 结算科目是从对账单流水上认出来的，发票被放回
    # 可生成时若还占着那条流水，重算时反而认不回自己。
    from app.bankstatement import matching

    matching.release_claims(db, [record.id for record in records])

    for batch_id, released in released_per_batch.items():
        batch = db.get(InvoiceBatch, batch_id)
        if batch is None:
            continue
        batch.committed_count = max(0, (batch.committed_count or 0) - released)
        # 这个批次里还剩几张是「已入账」的？（有凭证的那种才算）
        remaining = db.scalar(
            select(func.count(InvoiceRecord.id)).where(
                InvoiceRecord.batch_id == batch.id,
                InvoiceRecord.status == "COMMITTED",
                InvoiceRecord.voucher_id.is_not(None),
            )
        ) or 0
        if not remaining and batch.status == "COMMITTED":
            batch.status = "PENDING"
            batch.message = "凭证已删除，可以重新生成"
    return len(records)
