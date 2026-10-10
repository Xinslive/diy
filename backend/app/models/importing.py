"""批量导入暂存区。

导入走「暂存 → 预览 → 确认提交」三步，而不是解析完直接写库：
Excel 里的科目编码写错、日期落在已结账期间这类问题，必须先让人看见，
确认后再落库。解析结果整包存在 payload 里，避免重复解析。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    # PENDING 已暂存待确认 / COMMITTED 已入库 / DISCARDED 已作废
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ok_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    committed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 解析结果：{"vouchers": [...], "errors": [...], "subject_map": {...}}
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    committed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    committed_by: Mapped[Optional[str]] = mapped_column(String(64))
