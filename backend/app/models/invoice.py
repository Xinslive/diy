"""发票导入、科目规则与凭证追溯模型。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class InvoiceConfig(Base, TimestampMixin):
    """每个账套一份发票生成凭证配置。"""

    __tablename__ = "invoice_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False, unique=True)
    taxpayer_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    # 只有销项需要税额科目：进项税额按用户约定不单独列示（并入第一条明细行），
    # 所以这里没有 input_tax_subject_id。
    output_tax_subject_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subject.id", ondelete="RESTRICT"))
    # 进项发票贷方（付款账户）与销项发票借方（收款账户）。**一律不预填** ——
    # 预填的已选项不会提醒任何人去核对，而生成出来的凭证科目错了要等总账
    # 对不上才发现。留空就是「还没配」，预览时明确报「未配置结算科目」。
    input_credit_subject_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subject.id", ondelete="RESTRICT"))
    output_debit_subject_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subject.id", ondelete="RESTRICT"))


class InvoiceRule(Base, TimestampMixin):
    """供应商/客户或项目关键词到末级科目的映射规则。"""

    __tablename__ = "invoice_rule"
    __table_args__ = (
        Index("ix_invoice_rule_book_active", "book_id", "active", "priority"),
        Index("ix_invoice_rule_book_type", "book_id", "match_type", "invoice_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    # SELLER 供应商/客户名称；ITEM 发票项目名称。
    match_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # INPUT / OUTPUT / BOTH。
    invoice_type: Mapped[str] = mapped_column(String(8), default="BOTH", nullable=False)
    keyword: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subject.id", ondelete="RESTRICT"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(64))


class InvoiceBatch(Base, TimestampMixin):
    """一份上传文件对应的发票处理批次。"""

    __tablename__ = "invoice_batch"
    __table_args__ = (
        Index("ix_invoice_batch_book_id", "book_id", "id"),
        Index("ix_invoice_batch_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    # PENDING 待确认 / COMMITTED 已生成凭证 / DISCARDED 已作废。
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ready_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    committed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    committed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    committed_by: Mapped[Optional[str]] = mapped_column(String(64))

    invoices: Mapped[list["InvoiceRecord"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="InvoiceRecord.page_index"
    )


class InvoiceRecord(Base, TimestampMixin):
    """批次内的一张发票，保留解析快照和生成凭证的关联。"""

    __tablename__ = "invoice_record"
    __table_args__ = (
        # 空发票号允许多张记录存在；业务层对非空号码执行重复检查。
        UniqueConstraint("book_id", "invoice_no", name="uq_invoice_record_book_no"),
        Index("ix_invoice_record_batch_id", "batch_id", "page_index"),
        Index("ix_invoice_record_voucher_id", "voucher_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("invoice_batch.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    invoice_no: Mapped[Optional[str]] = mapped_column(String(64))
    issue_date: Mapped[Optional[date]] = mapped_column(Date)
    # INPUT / OUTPUT / UNKNOWN。
    invoice_type: Mapped[str] = mapped_column(String(8), default="UNKNOWN", nullable=False)
    # PENDING / READY / COMMITTED / REJECTED / DISCARDED。
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    parse_warning: Mapped[str] = mapped_column(Text, default="", nullable=False)
    errors: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # meta、lines、resolution、overrides 的完整结构化快照。
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    voucher_id: Mapped[Optional[int]] = mapped_column(ForeignKey("voucher.id", ondelete="SET NULL"))

    batch: Mapped[InvoiceBatch] = relationship(back_populates="invoices")
