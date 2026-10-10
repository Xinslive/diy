"""科目、凭证、分录。

这是系统的唯一事实来源。任何余额、账簿、报表、打印数据都由本模块派生。
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
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Subject(Base, TimestampMixin):
    """会计科目。每个账套拥有独立的科目表。"""

    __tablename__ = "subject"
    __table_args__ = (
        UniqueConstraint("book_id", "code", name="uq_subject_book_code"),
        Index("ix_subject_book_tree", "book_id", "parent_id", "sort_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subject.id"))
    level: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    # ASSET 资产 / LIABILITY 负债 / EQUITY 所有者权益 / COST 成本 / PROFIT 损益
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    # DEBIT 借 / CREDIT 贷 —— 决定期初与期末余额的符号方向
    direction: Mapped[str] = mapped_column(String(8), default="DEBIT", nullable=False)
    is_leaf: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 结转损益开关：损益类科目月末是否参与自动结转
    carry_forward: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 预留：个性化属性，避免后期改表
    ext_attrs: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class Voucher(Base, TimestampMixin):
    __tablename__ = "voucher"
    __table_args__ = (
        UniqueConstraint("period_id", "voucher_no", name="uq_voucher_period_no"),
        Index("ix_voucher_book_date", "book_id", "voucher_date"),
        Index("ix_voucher_book_status", "book_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id"), nullable=False)
    # 凭证字 + 编号，例如 记-0001
    voucher_no: Mapped[str] = mapped_column(String(32), nullable=False)
    voucher_date: Mapped[date] = mapped_column(Date, nullable=False)
    # UNPOSTED 未记账 / POSTED 已记账
    status: Mapped[str] = mapped_column(String(16), default="UNPOSTED", nullable=False)
    # MANUAL 手工 / IMPORT 导入 / CARRY_FORWARD 结转损益
    source: Mapped[str] = mapped_column(String(16), default="MANUAL", nullable=False)
    attachments: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    remark: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_by: Mapped[Optional[str]] = mapped_column(String(64))
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[Optional[str]] = mapped_column(String(64))
    ext_attrs: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    entries: Mapped[list["Entry"]] = relationship(
        back_populates="voucher", cascade="all, delete-orphan", order_by="Entry.line_no"
    )


class Entry(Base, TimestampMixin):
    """凭证分录。

    book_id / period_ord 是从凭证反规范化下来的冗余列，由触发器维护。
    这样账簿聚合只需要扫描单张表，报表和总账查询都走同一条路径。
    """

    __tablename__ = "entry"
    __table_args__ = (
        Index("ix_entry_subject_ord", "book_id", "subject_id", "period_ord"),
        Index("ix_entry_voucher", "voucher_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("voucher.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[int] = mapped_column(Integer, nullable=False)
    period_ord: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subject.id"), nullable=False)
    # DEBIT 借 / CREDIT 贷
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 预留：将来要做辅助核算时无需改表
    aux_ref: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    voucher: Mapped["Voucher"] = relationship(back_populates="entries")
    subject: Mapped["Subject"] = relationship()
