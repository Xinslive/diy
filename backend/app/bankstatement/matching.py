"""发票 ↔ 流水 的结算科目匹配。

**这张发票的钱是从哪个账户收/付的？** 发票上不写 —— 发票只写「买了什么、
多少钱」，不写「从哪个银行账户付的」。而对账单知道。所以流程是：

    发票（金额 + 方向） → 流水（金额 + 方向） → 流水所属的现金科目
                          → 这张发票的结算科目

**匹配口径是「金额相等 + 收支方向相反对」。** 不用日期窗口：发票与流水
之间本来就会差几天（票开了几天才付款、货款到了才开票），而**金额相等且
方向一致**这件事本身已经足够稀有。

不唯一时的处理（**一律回落，绝不猜**）：

- **多个账户同额** → 回落配置科目，附一句「多账户同额，需人工指定」；
- **流水已被别的发票占用** → 回落配置科目（那一行只能被一张发票占）；
- **没有命中** → 回落配置科目。

**一行流水只被一张发票占用**，同批次内按页序先到先得 —— 同一笔钱
不可能既是 A 发票的结算又是 B 发票的结算。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import BankStatement, BankStatementRow
from app.models.bankstatement import (
    DIRECTION_IN,
    DIRECTION_OUT,
    STATUS_IGNORED,
)

#: 发票类型 → 流水方向。销项是「收钱」，找**收入**行；进项是「付钱」，
#: 找**支出**行。
DIRECTION_FOR_INVOICE = {
    "OUTPUT": DIRECTION_IN,
    "INPUT": DIRECTION_OUT,
}


def direction_for_invoice(invoice_type: str) -> Optional[str]:
    return DIRECTION_FOR_INVOICE.get(invoice_type)


def candidates_for(db: Session, book_id: int, amount: Decimal,
                   direction: str) -> list[dict[str, Any]]:
    """金额与方向都对得上的流水行。

    已经被别的发票占用的行**不在这里** —— 一行流水只被一张发票占用。
    """
    if direction not in (DIRECTION_IN, DIRECTION_OUT):
        return []
    rows = db.execute(
        select(BankStatementRow, BankStatement)
        .join(BankStatement, BankStatement.id == BankStatementRow.statement_id)
        .where(
            BankStatementRow.book_id == book_id,
            BankStatementRow.direction == direction,
            BankStatementRow.amount == amount,
            BankStatementRow.status != STATUS_IGNORED,
            BankStatementRow.matched_invoice_record_id.is_(None),
        )
        .order_by(BankStatementRow.tx_date, BankStatementRow.id)
    ).all()
    return [{
        "row_id": row.id,
        "seq": row.seq,
        "tx_date": row.tx_date.isoformat() if row.tx_date else "",
        "summary": row.summary,
        "counterparty_name": row.counterparty_name,
        "statement_id": statement.id,
        "filename": statement.filename,
        "account_subject_id": statement.account_subject_id,
        "account_subject_code": statement.account_subject_code,
        "account_name": statement.account_name or statement.account_no,
        "bank_name": statement.bank_name,
    } for row, statement in rows]


def pick(candidates: Iterable[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], str]:
    """从候选里挑一个，**挑不出来就说清楚为什么**。

    返回 ``(选中的那条, 提示语)``。提示语为空表示干净命中。

    判据是**账户**而不是行：同一个账户下有两条同额流水时，结算科目
    （哪个账户）是确定的，具体记哪一条不影响科目 —— 所以这不算歧义。
    真正的歧义是「两个不同账户各有一笔同额的钱」，那时候必须人来定。
    """
    items = list(candidates)
    if not items:
        return None, ""
    accounts = {item["account_subject_id"] for item in items}
    if len(accounts) > 1:
        names = "、".join(sorted({
            item.get("account_subject_code") or item.get("account_name") or ""
            for item in items
        } - {""}))
        return None, f"多账户同额（{names}），需人工指定"
    return items[0], ""


def release_claims(db: Session, record_ids: Iterable[int]) -> None:
    """把某些发票记录占用的流水**放掉**。

    什么时候要放：重算预览之前。用户在预览里改了发票的金额或类型之后，
    原来占着的那条流水应当重新变成「没人用」——
    否则改完之后那笔流水会一直显示「已被发票占用」，而真正该占它的
    发票（金额改过的那张）反而匹配不上。

    **已提交的发票不放**：它们的凭证已经在账上，放掉会让同一条流水
    再被一张新发票认领一次。
    """
    ids = [int(i) for i in record_ids if i]
    if not ids:
        return
    db.execute(
        update(BankStatementRow)
        .where(BankStatementRow.matched_invoice_record_id.in_(ids))
        .values(matched_invoice_record_id=None)
    )


def claim(db: Session, row_id: int, record_id: int) -> None:
    """把一条流水标记为「被这张发票占用」。"""
    db.execute(
        update(BankStatementRow)
        .where(BankStatementRow.id == row_id)
        .values(matched_invoice_record_id=int(record_id))
    )
