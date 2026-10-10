"""审计查询（便于在业务层直接调用）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog


def recent(db: Session, book_id: int, limit: int = 50) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(AuditLog.book_id == book_id)
            .order_by(AuditLog.id.desc()).limit(limit)
        ).all()
    )
