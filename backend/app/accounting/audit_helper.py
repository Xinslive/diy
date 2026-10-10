"""审计日志。因为凭证号可复用，删除/修改必须记录完整快照。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog, Entry, Voucher


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def voucher_snapshot(voucher: Voucher) -> dict:
    return {
        "id": voucher.id,
        "voucher_no": voucher.voucher_no,
        "voucher_date": _jsonable(voucher.voucher_date),
        "status": voucher.status,
        "source": voucher.source,
        "remark": voucher.remark,
        "attachments": voucher.attachments,
        "debit": float(sum(e.amount for e in voucher.entries if e.direction == "DEBIT")),
        "credit": float(sum(e.amount for e in voucher.entries if e.direction == "CREDIT")),
        "entries": [
            {
                "line_no": e.line_no,
                "subject_id": e.subject_id,
                "direction": e.direction,
                "amount": float(e.amount),
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
