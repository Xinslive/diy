"""审计日志。因为凭证号可复用，删除/修改必须记录完整快照。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog, Entry, Voucher


def _jsonable(v: Any) -> Any:
    """把快照里的值变成 JSON 能存、且**不丢精度**的形态。

    金额一律存**十进制文本**，不转 float（审计 P1-23）。

    原来是 ``float(v)``，而 float 只有 15~17 位有效数字：

        float(Decimal('9999999999999999.99')) == 1e16

    ``Numeric(20,2)`` 的上界（999999999999999999.99，20 位）远在 float 能无损
    表示的范围之外。也就是说：金额一旦越过千万级，审计快照里记下来的
    就不是那个数了。而这张表的立论（models/audit.py 开篇）恰恰是
    「因为凭证号可复用，日志必须保存完整快照，否则同一个号码事后无法对应」——
    快照存的不是原数，那句话就不成立。

    存字符串还有第二层理由：JSON 里的数字在 JavaScript 侧同样会在
    2^53 之后失真，审计日志页是用前端渲染的。文本是十进制的唯一无损载体，
    ``set_openings`` 的期初快照早就是按 ``str(amount)`` 存的（``before`` /
    ``after`` 两张表都是），这里与它保持同一口径。
    """
    if isinstance(v, Decimal):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def voucher_snapshot(voucher: Voucher) -> dict:
    """凭证的完整快照。金额是十进制文本 —— 见 :func:`_jsonable` 的理由。"""
    return {
        "id": voucher.id,
        "voucher_no": voucher.voucher_no,
        "voucher_date": _jsonable(voucher.voucher_date),
        "status": voucher.status,
        "source": voucher.source,
        "remark": voucher.remark,
        "attachments": voucher.attachments,
        "debit": _jsonable(sum(e.amount for e in voucher.entries
                               if e.direction == "DEBIT")),
        "credit": _jsonable(sum(e.amount for e in voucher.entries
                                if e.direction == "CREDIT")),
        "entries": [
            {
                "line_no": e.line_no,
                "subject_id": e.subject_id,
                "direction": e.direction,
                "amount": _jsonable(e.amount),
                "summary": e.summary,
            }
            for e in sorted(voucher.entries, key=lambda x: x.line_no)
        ],
    }


def log(
    db: Session,
    *,
    action: str,
    entity: str,
    entity_id: str | int,
    user,
    book_id: int | None = None,
    detail: str = "",
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            book_id=book_id,
            user_id=getattr(user, "id", None),
            username=getattr(user, "username", "system"),
            action=action,
            entity=entity,
            entity_id=str(entity_id),
            detail=detail,
            before=before,
            after=after,
        )
    )
