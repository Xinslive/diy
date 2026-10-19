"""可配置报表：模板 / 行 / 列 / 单元格公式 / 计算结果。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ReportTemplate(Base, TimestampMixin):
    """报表模板。

    **归属**由 ``book_id`` 决定，分两种：

    * ``NULL`` —— 官方内置基线（资产负债表 / 利润表）。所有账套共看同一份，
      而且**只读**：谁都不能直接改它。
    * 某个 ``book_id`` —— 归属该账套，账套 OWNER 可改。

    「被改了」和「共享」这两件事不能同时成立，所以走**写时复制**：某账套
    第一次进入设计模式要改内置表时（``POST /templates/{id}/fork``），先把
    内置基线整份复制一份归属该账套，之后的改动只落在自己那份上。没动过
    的账套继续看基线，不产生任何副本；动过的账套也影响不到别人。
    推导见 ``ARCHITECTURE.md``「报表按账套隔离」。

    **唯一约束分两条**，因为 NULL 在普通 UNIQUE 里可以重复（PG 默认
    NULLS DISTINCT），而内置基线恰恰要求 ``code`` 唯一：
    ``UNIQUE(code) WHERE book_id IS NULL`` 守住基线，
    ``UNIQUE(book_id, code) WHERE book_id IS NOT NULL`` 守住账套内不重名。
    顺带解决了一个老问题：导入模板的 code 是内容指纹，隔离前同一份文件
    整个系统只能导一次，现在每个账套可以各导各的。
    """

    __tablename__ = "report_template"
    __table_args__ = (
        Index("uq_report_template_builtin_code", "code", unique=True,
              postgresql_where=text("book_id IS NULL")),
        Index("uq_report_template_book_code", "book_id", "code", unique=True,
              postgresql_where=text("book_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: NULL = 官方内置基线（共享只读）；非空 = 归属该账套
    #: index=True：0020 建了 ix_report_template_book_id（列表按账套过滤 + 去重
    #: 都要走它）。不写在这里就是模型与库的漂移 —— compare_metadata 会报
    #: remove_index，谁按模型跑一次 autogenerate 就把它删了。
    book_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), nullable=True, index=True)
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
