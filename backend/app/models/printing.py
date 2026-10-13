"""打印模板。模板内容存库，前端只负责渲染，因此财务可自助调整版式。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PrintTemplate(Base, TimestampMixin):
    """打印模板。模板内容存库，前端只负责渲染，因此财务可自助调整版式。

    **book_id 是模板的归属账套**（审计 P1-05 的根治项）。在它存在之前，
    模板是**全局**的，而 ``api/printing.py::_template`` 在没有 template_id 时
    会去取「该 scope 下的默认模板」—— 于是一张全局模板会流进每个账套的打印页，
    包括别人的账套、包括只读账号的浏览器。P0-2（模板内容进 ``<style>``）的
    爆炸半径因此是全站，而不是一个账套。

    **NULL 的语义是「全局兜底」，不是「没有归属」。** 迁移之前建的模板一律是
    全局的，回填到任意一个账套等于替人断言归属；让它们留 NULL，
    由 ``_template`` 在取默认模板时兜底，既不丢数据也不假装隔离。
    这也意味着**门禁暂时不能放宽**：第一批做的 OWNER 级全局门禁是收紧，
    不是隔离，而 book_id 落地到 ``api/printing.py`` 之前它仍是唯一的一道。
    """

    __tablename__ = "print_template"
    __table_args__ = (Index("ix_print_template_book_scope", "book_id", "scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), nullable=True)
    # VOUCHER 凭证 / LEDGER 总账 / DETAIL_LEDGER 明细账 / BALANCE 科目余额表 / REPORT 报表
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # A4 / A5 / LANDSCAPE / CONTINUOUS 连续打印纸
    paper: Mapped[str] = mapped_column(String(24), default="A4", nullable=False)
    # 模板 JSON：字段顺序、列宽、字号、表头、合计行等
    content: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    remark: Mapped[str] = mapped_column(Text, default="", nullable=False)
