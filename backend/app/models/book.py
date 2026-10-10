"""账套、会计期间、期初快照。

设计要点：
- 账套是唯一的顶层实体：公司名只是账套的一个文本属性（company_name），
  没有独立的「公司」表。报表页眉、凭证打印、Excel 导出直接读它即可，
  省掉一次查询，也不会出现「先建公司才能建账套」的额外步骤。
  同一家公司开多个账套（租金 / 管理费 / 代收）时，company_name 相同，
  靠 (company_name, name, year) 三元组区分。
- 期间是「结账锁」的载体。locked=True 时数据库触发器拒绝一切凭证写入。
- period_ord 是账套内的期间序号（1..12），账簿聚合直接按它做区间比较。
- period_opening 只在结账时写入，作为不可变的期初快照；未结账期间的期初
  由上一期期末实时推导，不需要物化。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Book(Base, TimestampMixin):
    __tablename__ = "book"
    __table_args__ = (
        UniqueConstraint("company_name", "name", "year", name="uq_book"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 单位名称：页眉 / 报表 / 打印用，允许与其他账套重复，不做外键
    company_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 账套名：业务用途，如「租金账套」「管理费账套」
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    # 账套启用月份（1..12），之前的月份不参与业务
    start_month: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    # 是否已执行年度结转（下一年账套已生成并结转期初）
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)

    periods: Mapped[list["Period"]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="Period.month"
    )


class Period(Base, TimestampMixin):
    __tablename__ = "period"
    __table_args__ = (UniqueConstraint("book_id", "year", "month", name="uq_period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # 账套内序号；用于聚合时的区间比较
    ord: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[Optional[str]] = mapped_column(String(64))
    # 结账版本号，每次重算/解锁递增，用于报表缓存失效
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    book: Mapped["Book"] = relationship(back_populates="periods")

    @property
    def code(self) -> str:
        return f"{self.year}-{self.month:02d}"


class PeriodOpening(Base, TimestampMixin):
    """期初快照：某期间期初时各科目的余额。仅在结账/初始化时写入。"""

    __tablename__ = "period_opening"
    __table_args__ = (
        UniqueConstraint("period_id", "subject_id", name="uq_period_opening"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id", ondelete="CASCADE"), nullable=False)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subject.id", ondelete="CASCADE"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal("0"), nullable=False)
