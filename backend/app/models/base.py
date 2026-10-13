"""ORM 基类。Base 与 app.core.db.Base 是同一个对象，
保证 create_all 能看到所有已注册模型。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

__all__ = ["Base", "TimestampMixin"]


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # ``onupdate=func.now()`` 只覆盖**这一侧**发起的 UPDATE。凡是**数据库自己**
    # 改写行的路径 —— 触发器（fn_voucher_resync_entries 换期回填、
    # fn_touch_updated_at）—— 都绕过 ORM，updated_at 就会停在旧值上，
    # 而「按时间戳取证」正是审计与排障的常用手段（审计 P2-09）。
    # 所以库层另有一条同名同义触发器，见 db_init.sql 的第 7 段：
    # 两侧保持同一语义，ORM 写的和库写的刷新规则一致。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
