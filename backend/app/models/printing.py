"""打印模板。模板内容存库，前端只负责渲染，因此财务可自助调整版式。"""
from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PrintTemplate(Base, TimestampMixin):
    __tablename__ = "print_template"

    id: Mapped[int] = mapped_column(primary_key=True)
    # VOUCHER 凭证 / LEDGER 总账 / DETAIL_LEDGER 明细账 / BALANCE 科目余额表 / REPORT 报表
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # A4 / A5 / LANDSCAPE / CONTINUOUS 连续打印纸
    paper: Mapped[str] = mapped_column(String(24), default="A4", nullable=False)
    # 模板 JSON：字段顺序、列宽、字号、表头、合计行等
    content: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    remark: Mapped[str] = mapped_column(Text, default="", nullable=False)
