"""宿主插件注册、安装日志、显式用户授权及逐 worker 租约。"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class PluginRegistry(Base, TimestampMixin):
    __tablename__ = "plugin_registry"
    plugin_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    version: Mapped[str] = mapped_column(String(64))
    plugin_digest: Mapped[str] = mapped_column(String(64))
    manifest_version: Mapped[int] = mapped_column(Integer, default=1)
    desired_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    desired_revision: Mapped[str] = mapped_column(String(32))
    access_default: Mapped[str] = mapped_column(String(16), default="none")
    runtime_status: Mapped[str] = mapped_column(String(32), default="PENDING_RESTART")
    generation: Mapped[str] = mapped_column(String(64), default="")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(String(256), default="")
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    installed_by: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id", ondelete="SET NULL"))
    migration_head: Mapped[str] = mapped_column(String(64), default="")
    tables: Mapped[list] = mapped_column(JSONB, default=list)
    manifest_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    archive_digest: Mapped[str] = mapped_column(String(64), default="")
    pending_attempt_id: Mapped[str | None] = mapped_column(String(32))
    backup_path: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PluginInstallAttempt(Base, TimestampMixin):
    __tablename__ = "plugin_install_attempt"
    attempt_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plugin_id: Mapped[str] = mapped_column(String(96), index=True)
    operation: Mapped[str] = mapped_column(String(16))
    old_digest: Mapped[str] = mapped_column(String(64), default="")
    new_digest: Mapped[str] = mapped_column(String(64))
    staging_path: Mapped[str] = mapped_column(Text)
    active_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="PREPARED")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    migration_head: Mapped[str] = mapped_column(String(64), default="")
    manifest_metadata: Mapped[dict] = mapped_column(JSONB)
    tables: Mapped[list] = mapped_column(JSONB, default=list)
    archive_digest: Mapped[str] = mapped_column(String(64), default="")
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id", ondelete="SET NULL"))
    backup_path: Mapped[str] = mapped_column(Text, default="")


class PluginUserAccess(Base):
    __tablename__ = "plugin_user_access"
    plugin_id: Mapped[str] = mapped_column(ForeignKey("plugin_registry.plugin_id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(32), unique=True)
    granted_by: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id", ondelete="SET NULL"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_plugin_access_user", "user_id", "plugin_id", "revoked_at"),)


class PluginCohort(Base):
    __tablename__ = "plugin_cohort"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[int] = mapped_column(BigInteger)
    expected_workers: Mapped[int] = mapped_column(Integer)


class PluginWorkerRuntime(Base):
    __tablename__ = "plugin_worker_runtime"
    generation: Mapped[str] = mapped_column(String(64), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 空 plugin_id 是该 worker 的宿主注册，其他行是该 worker 的插件加载结果。
    plugin_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    plugin_digest: Mapped[str] = mapped_column(String(64), default="")
    desired_revision: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str] = mapped_column(String(64), default="")
    lease_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (Index("ix_plugin_worker_lease", "plugin_id", "lease_at"),)
