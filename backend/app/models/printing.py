"""打印模板。模板内容存库，前端只负责渲染，因此财务可自助调整版式。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text
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
    这也意味着**门禁不能靠「它没有主人」来放宽**：第一批做的 OWNER 级全局门禁
    是收紧，不是隔离。

    **user_id 之后是三层，而不是两层（迁移 0023）。** 一张模板恰好处在其中一层：

    ==================  ==========================================  =========
    层                   判定                                      谁看得见
    ==================  ==========================================  =========
    系统基线             ``book_id IS NULL AND user_id IS NULL``    所有人
    账套模板             ``book_id = X AND user_id IS NULL``        账套 X
    个人私有副本         ``book_id IS NULL AND user_id = 我``       只有我
    ==================  ==========================================  =========

    私有副本**不占 book_id**：它是「我这个人打印凭证的习惯」，换一本账也得跟着
    我走，不是某本账的私产。所以两层不许同时占着（表上的 CHECK 约束），归属含混
    的行宁可插入失败 —— 那种行在可见性判定里会落进两个分支，取默认模板时就会
    随机挑中一份。

    **为什么私有这一层落到「人」而不是「账套」**：账套里不止一个人，凭证版式
    恰恰是每个人的事。按账套 fork 意味着张三改版式、李四明天打印出来的凭证也
    跟着变，而李四从没同意过。
    """

    __tablename__ = "print_template"
    __table_args__ = (
        Index("ix_print_template_book_scope", "book_id", "scope"),
        Index("ix_print_template_user_scope", "user_id", "scope"),
        # 兜底基线仍按 (scope, name, book_id IS NULL) 查存在性（0022 的 upgrade），
        # 这条索引让它别退化成顺序扫。
        #
        # **这一条以前是「库里有、模型里没有」的漂移**（审查报告 P2-4）：
        # Alembic 的 autogenerate **不比较索引**，但它会拿库里的实际结构与
        # 模型声明求差 —— 模型没声明这条索引，于是下一次 autogenerate 会生成
        # 一条 ``DROP INDEX ix_print_template_scope_name``。
        # 迁移 0023 特意建的索引就这样在一次「例行的 autogenerate」里被悄悄
        # 丢掉，而丢掉之后基线查找退化成顺序扫 —— 表现是「模板一多就变慢」，
        # 没有任何报错。
        Index("ix_print_template_scope_name", "scope", "name"),
        # 同一个人对同一份基线只许有一份副本（NULL 不参与，故基线与账套模板
        # 不受影响）。应用里已先查后插，这条是并发的背书。
        Index("uq_print_template_user_fork", "user_id", "forked_from_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("book.id", ondelete="CASCADE"), nullable=True)
    # **私有副本归属谁**（NULL = 不是私有，是基线或账套模板）。
    # 账号被删时副本跟着删：它是那个人的版式，留着没有意义，
    # 而且留着就等于把一个已不存在的账号的 id 当成归属判据。
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=True)
    # 「这份是从哪份模板 fork 出来的」。列表去重与 fork 幂等都靠它，
    # **不能靠 name**：名字是用户自己可改的，改完就对不上了。
    # 基线被删时置 NULL —— 这份副本降级成一份普通的个人版式，仍然只有本人可见。
    forked_from_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("print_template.id", ondelete="SET NULL"), nullable=True)
    # VOUCHER 凭证 / LEDGER 总账 / DETAIL_LEDGER 明细账 / BALANCE 科目余额表 / REPORT 报表
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # A4 / A5 / LANDSCAPE / CONTINUOUS 连续打印纸
    paper: Mapped[str] = mapped_column(String(24), default="A4", nullable=False)
    # 模板 JSON：字段顺序、列宽、字号、表头、合计行等
    content: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    remark: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = __table_args__ + (
        # 「两层不许同时占着」。这是 fail-closed 的最后一层：归属含混的行如果
        # 混进库里，可见性判定会落进两个分支，取默认模板时随机挑中一份 ——
        # 表现是「同一天里同一本账打印出的版式不一样」，极难查。
        CheckConstraint("NOT (book_id IS NOT NULL AND user_id IS NOT NULL)",
                        name="ck_print_template_one_owner"),
    )
