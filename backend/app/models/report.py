"""可配置报表：模板 / 行 / 列 / 单元格公式 / 计算结果。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ReportTemplate(Base, TimestampMixin):
    """报表模板。模板本身与账套无关，科目用「编码」引用，因此可跨账套复用。"""

    __tablename__ = "report_template"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # BALANCE_SHEET 资产负债表 / INCOME_SHEET 利润表 / CUSTOM 自定义
    type: Mapped[str] = mapped_column(String(24), default="CUSTOM", nullable=False)
    remark: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ReportRow(Base):
    __tablename__ = "report_row"
    __table_args__ = (UniqueConstraint("template_id", "row_no", name="uq_report_row"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("report_template.id", ondelete="CASCADE"), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    indent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bold: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 小计行：对本行指定范围内的行求和（配合合计公式）
    style: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class ReportCol(Base):
    __tablename__ = "report_col"
    __table_args__ = (UniqueConstraint("template_id", "col_no", name="uq_report_col"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("report_template.id", ondelete="CASCADE"), nullable=False)
    col_no: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # STATIC 固定列 / PERIOD 期间列（按 kind 绑定列类型）
    kind: Mapped[str] = mapped_column(String(32), default="STATIC", nullable=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=120, nullable=False)


class ReportCell(Base):
    """单元格公式。空字符串表示留空。"""

    __tablename__ = "report_cell"
    __table_args__ = (UniqueConstraint("template_id", "row_no", "col_no", name="uq_report_cell"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("report_template.id", ondelete="CASCADE"), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    col_no: Mapped[int] = mapped_column(Integer, nullable=False)
    formula: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ReportResult(Base, TimestampMixin):
    """一次求值的结果快照。保留公式与报错，便于财务自助排查。"""

    __tablename__ = "report_result"
    __table_args__ = (
        UniqueConstraint("template_id", "book_id", "period_id", "row_no", "col_no", name="uq_report_result"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("report_template.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id"), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    col_no: Mapped[int] = mapped_column(Integer, nullable=False)
    formula: Mapped[str] = mapped_column(Text, default="", nullable=False)
    value: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2))
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
