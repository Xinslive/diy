"""持久化的应用级计划任务与运行记录。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ScheduledTask(Base, TimestampMixin):
    __tablename__ = "sys_scheduled_task"
    __table_args__ = (
        CheckConstraint("owner_type IN ('SYSTEM', 'PLUGIN')", name="ck_scheduled_task_owner"),
        Index("ix_scheduled_task_due", "enabled", "next_run_at", "lease_until"),
    )

    task_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    plugin_id: Mapped[str | None] = mapped_column(String(96))
    task_name: Mapped[str] = mapped_column(String(64), nullable=False)
    plugin_digest: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    schedule: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_run_id: Mapped[str | None] = mapped_column(String(32))


class ScheduledTaskRun(Base):
    __tablename__ = "sys_scheduled_task_run"
    __table_args__ = (
        CheckConstraint("owner_type IN ('SYSTEM', 'PLUGIN')", name="ck_scheduled_task_run_owner"),
        CheckConstraint("status IN ('RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED')", name="ck_scheduled_task_run_status"),
        UniqueConstraint("task_key", "scheduled_for", name="uq_scheduled_task_run_occurrence"),
        Index("ix_scheduled_task_run_recent", "task_key", "scheduled_for"),
        Index("ix_scheduled_task_run_finished", "finished_at"),
    )

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_key: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    plugin_id: Mapped[str | None] = mapped_column(String(96))
    task_name: Mapped[str] = mapped_column(String(64), nullable=False)
    plugin_digest: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    user_results: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
