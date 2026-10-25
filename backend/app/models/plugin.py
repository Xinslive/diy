"""宿主插件注册、安装日志、显式用户授权及逐 worker 租约。"""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class PluginRegistry(Base, TimestampMixin):
    __tablename__ = "plugin_registry"
    __table_args__ = (
        # **库级 CHECK 约束，模型侧必须同名同义**（审计 P3-2 / P3-22）。
        #
        # 迁移 0026 里这条是**内联**写的（``access_default VARCHAR(16) NOT
        # NULL DEFAULT 'none' CHECK (access_default = 'none')``），PostgreSQL
        # 给内联校验自动取名 ``plugin_registry_access_default_check`` ——
        # 模型侧必须用**这个**名字，才算同名。
        #
        # 它是**安全护栏**：插件默认拿不到任何用户数据，只有显式授权之后才
        # 能碰到。写成别的名字，autogenerate（及任何以模型为准的心智模型）
        # 就看不见这条护栏；写成别的语义，这条护栏就名存实亡了。
        CheckConstraint("access_default = 'none'", name="plugin_registry_access_default_check"),
    )
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
    __table_args__ = (
        # **按时间判龄用的索引**（app/maintenance.py 的保留期清理）。
        # 清理按 created_at 过滤，没有索引就是每次清理一次全表扫。
        Index("ix_plugin_install_attempt_created_at", "created_at"),
    )
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
    __table_args__ = (
        # **库级 CHECK 约束，模型侧必须同名同义**（审计 P3-2 / P3-22）。
        #
        # 两条都由迁移 0026 内联交付，PostgreSQL 自动取名
        # ``plugin_cohort_id_check`` / ``plugin_cohort_expected_workers_check``。
        #
        # ``id = 1`` 是**单行守卫**：这张表全局只允许有一行（当前这一代
        # 插件 worker 的租约世代）。它是库层最后一道防线 —— 应用层万一
        # 插了第二行，租约就会分裂成两代，worker 的排他租约随之失效。
        #
        # ``expected_workers BETWEEN 1 AND 64`` 给的是**容量上限**：
        # 预计 worker 数是个小整数，写成 0 或 1000 都是配置事故，
        # 而 64 这个上限本身是排期系统的口径（见 app/plugins/workers）。
        CheckConstraint("id = 1", name="plugin_cohort_id_check"),
        CheckConstraint("expected_workers BETWEEN 1 AND 64",
                        name="plugin_cohort_expected_workers_check"),
    )
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
