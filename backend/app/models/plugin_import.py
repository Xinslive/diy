"""宿主管理的插件导入证据；插件的差异配置仍放在各自的 book-scoped 表。"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class PluginImportBatch(Base, TimestampMixin):
    __tablename__ = "plugin_import_batch"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(ForeignKey("plugin_registry.plugin_id", ondelete="CASCADE"))
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id", ondelete="SET NULL"))
    username: Mapped[str] = mapped_column(String(128))
    plugin_version: Mapped[str] = mapped_column(String(64))
    plugin_digest: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="UPLOADED")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    original_preview: Mapped[list | None] = mapped_column(JSONB)
    preview: Mapped[list | None] = mapped_column(JSONB)
    preview_digest: Mapped[str] = mapped_column(String(64), default="")
    configuration: Mapped[dict] = mapped_column(JSONB, default=dict)
    configuration_digest: Mapped[str] = mapped_column(String(64), default="")
    validation_errors: Mapped[list] = mapped_column(JSONB, default=list)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[list] = mapped_column(JSONB, default=list)
    request_id: Mapped[str] = mapped_column(String(128), default="")
    __table_args__ = (
        CheckConstraint("status IN ('UPLOADED','PREVIEW_READY','EDITING','CONFIRMED','SUBMITTING','SUBMITTED','FAILED','CANCELLED')", name="ck_plugin_import_status"),
        CheckConstraint("revision > 0", name="ck_plugin_import_revision"),
        Index("uq_plugin_import_fingerprint", "plugin_id", "book_id", "fingerprint", unique=True, postgresql_where=text("status <> 'CANCELLED'")),
        Index("ix_plugin_import_owner", "plugin_id", "user_id", "book_id", "created_at"),
    )


class PluginImportFile(Base):
    __tablename__ = "plugin_import_file"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("plugin_import_batch.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(256))
    content_type: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    # 列表、预览和提交不加载完整文件。BYTEA 与批次一起备份/恢复，无外部路径。
    content: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("size > 0", name="ck_plugin_import_file_size"),)


class PluginVoucherOrigin(Base):
    __tablename__ = "plugin_voucher_origin"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("plugin_import_batch.id", ondelete="CASCADE"))
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"))
    voucher_id: Mapped[int | None] = mapped_column(ForeignKey("voucher.id", ondelete="SET NULL"), unique=True)
    # 删除凭证后仍能对应历史提交结果，避免可复用的凭证号造成歧义。
    original_voucher_id: Mapped[int] = mapped_column(Integer)
    voucher_no: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("batch_id", "position", name="uq_plugin_origin_position"),)
