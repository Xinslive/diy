"""科目、凭证、分录。

这是系统的唯一事实来源。任何余额、账簿、报表、打印数据都由本模块派生。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
    """会计科目。**每个年度账套有一份独立的科目表。**

    2026 年和 2027 年各有各的科目表 —— 两套行、两套 id、同一套编码。
    这就是「挂着同一个账套名下的两个账套」在数据上的样子。
    """

    __tablename__ = "subject"
    __table_args__ = (
        # 唯一键带上年度：同一套编码在两个年度各有一份，互不冲突。
        # 少了 year 这一维，2027 年导入一张编码相同的科目表就会撞 2026 年的行。
        UniqueConstraint("book_id", "year", "code", name="uq_subject_book_year_code"),
        Index("ix_subject_book_year_tree", "book_id", "year", "parent_id", "sort_no"),
        # 每个科目必须挂在一个已登记的年度账套上（迁移 0032）。
        # 与 ``period`` 的那条组合外键同源：期间与科目都只能落在登记过的年度上。
        ForeignKeyConstraint(
            ["book_id", "year"], ["book_year.book_id", "book_year.year"],
            name="fk_subject_book_year", ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    # 所属会计年度。**科目表的下沉维度** —— 同一个 (账套, 编码) 在不同年度
    # 是两行不同的记录。引用它的分录/期初快照必须与它同年度，
    # 库层由 ``fn_entry_subject_year_guard`` / ``fn_opening_subject_year_guard`` 把关。
    year: Mapped[int] = mapped_column(Integer, nullable=False)
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
    # 三个核算开关（对应柠檬云的「数量核算 / 辅助核算 / 外币核算」）：
    # 勾了以后这张科目的分录要填数量、辅助项、外币汇率。先只存标记，
    # 分录上的三种明细由 entry.aux_ref 承载。
    qty_calc: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    aux_calc: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fx_calc: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 预留：个性化属性，避免后期改表
    ext_attrs: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class Voucher(Base, TimestampMixin):
    __tablename__ = "voucher"
    __table_args__ = (
        UniqueConstraint("period_id", "voucher_no", name="uq_voucher_period_no"),
        # **库级 CHECK 约束，模型侧必须同名同义**（审计 P3-2）。
        #
        # 这条约束由 ``db_init.sql`` 交付（迁移 0005/0007/0009/0010 重放），
        # 库里是活的：服务层传 ``status="FOO"`` 会被它挡下
        # （sqlstate 23514 check_violation）。
        #
        # 以前模型侧没有声明它，于是「模型 == 库」这条判据（``models/book.py``
        # 里专门写了一段说明为什么必须一致）对 CHECK 约束**结构性失效**：
        # Alembic 1.20 的 ``compare_metadata`` 根本不比较 CHECK 约束，
        # 缺了不会报、补了也不会报 —— 两条路都看不见。
        # 现在按「同名同义」补齐，换用会比较 CHECK 约束的工具时也不会
        # 把这条护栏当成「库里没有、模型里有」而删掉。
        #
        # 约束名与表达式**必须与库里逐字一致**，否则 autogenerate 会误报。
        CheckConstraint("status IN ('UNPOSTED','POSTED')", name="ck_voucher_status"),
        # ``ck_voucher_source`` 同上（审计 P3-2 / 报告 §3.14、§3.22）。
        #
        # 库里这条约束是活的：``db_init.sql:23`` 把它装上，服务层传
        # ``source="FOO"`` 会被它挡下（sqlstate 23514 check_violation）。
        # 以前模型侧只有 ``ck_voucher_status``，于是这一条对「模型 == 库」
        # 这条判据**结构性失效**（Alembic 不比较 CHECK 约束，缺了不会报、
        # 补了也不会报 —— 两条路都看不见）。
        #
        # 名字与表达式**与库里逐字一致**（PG 会把 IN 归一化成 = ANY(ARRAY[...])，
        # 语义等价即可）：少一条不会让服务崩，但会让所有以模型为准的心智模型、
        # 静态检查、基于模型的测试工具对它完全失明。
        CheckConstraint(
            "source IN ('MANUAL','IMPORT','INVOICE','CARRY_FORWARD')",
            name="ck_voucher_source",
        ),
        Index("ix_voucher_book_date", "book_id", "voucher_date"),
        Index("ix_voucher_book_status", "book_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id"), nullable=False)
    # 凭证字 + 编号，例如 记-001
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
        # 冗余列不许自己漂：组合外键把 (book_id, period_ord) 钉死在
        # 「某个账套的某个真实期间」上。它同时**传递地**保证了 book_id
        # 指向一个真实账套（period.book_id 本身就是 book 的外键），
        # 所以不再单列 entry.book_id -> book 的外键 —— 两条外键各自挂一个
        # 触发器，每次写分录都要多走一遍，白花开销换不来第二层保障。
        #
        # ON DELETE CASCADE 而不是 RESTRICT：period 本身是 book 的级联下级，
        # 用 RESTRICT 的话「删账套」会被这条外键卡住（级联删除的中间态里
        # period 已经没了而 entry 还在）。而 CASCADE 在一致库上是**不可达**的
        # —— 有分录就必然有凭证，有凭证就必然被 voucher.period_id 那条
        # NO ACTION 外键先挡住。所以它不会造成 P1-05 那种静默数据损失，
        # 只会让账套删除能一路走通。
        ForeignKeyConstraint(
            ["book_id", "period_ord"], ["period.book_id", "period.ord"],
            name="fk_entry_book_period", ondelete="CASCADE",
        ),
        # **库级 CHECK 约束，模型侧必须同名同义**（审计 P3-2）。
        #
        # 两条都是真的在拦东西的活约束（实测过）：
        #   · ck_entry_direction      —— direction 只能是 DEBIT / CREDIT
        #   · ck_entry_amount_nonzero —— 金额不能是 0（0 金额是「没填」，
        #     不是「记了 0 块」；放进去报表上会凭空多一张凭证）
        #
        # 名字与表达式逐字对齐 ``db_init.sql``（迁移 0005/0007/0009/0010 交付），
        # 理由同 ``Voucher.ck_voucher_status`` 那条注释：Alembic 不比较 CHECK
        # 约束，模型侧缺不缺它都看不出来，所以只能靠「同名同义」这条人工约定。
        CheckConstraint("direction IN ('DEBIT','CREDIT')", name="ck_entry_direction"),
        CheckConstraint("amount <> 0", name="ck_entry_amount_nonzero"),
        # **同一张凭证里，行号不能重复**（审计报告 §3.15，迁移 0038）。
        #
        # 以前这张表**没有**这条约束，于是「同一张凭证挂两条 line_no=1」
        # 能静默入库。并发改同一张凭证时就会真的发生：B 会话按自己那份
        # 陈旧的分录集合「删旧插新」，而 A 会话已经把那几行删掉了 ——
        # DELETE 命中 0 行，B 的新行照样插进去，于是这张凭证有 4 条分录、
        # 两组重复的 line_no，借贷还平（3000 = 3000），**所有自检都查不出来**：
        # 借贷平、试算平、科目余额、总账、「整理就绪」体检全过 —— 数字是错的，
        # 但没有任何一处会报错。
        #
        # 有这条约束之后，同样的交错会在库层炸成 23505（可见的失败），
        # 而不是留下一张「金额记了两遍」的凭证。
        #
        # 名字与库里那条逐字一致（迁移 0038 建的就是它）——
        # 理由同 ``Voucher.ck_voucher_status`` 那条注释：Alembic 不比较
        # CHECK/UNIQUE 约束的语义，模型侧与库侧只能靠「同名同义」这条人工约定。
        UniqueConstraint("voucher_id", "line_no", name="uq_entry_voucher_line_no"),
        # 明细账逐科目取数靠它；保留，不能被下面的宽索引取代。
        Index("ix_entry_subject_ord", "book_id", "subject_id", "period_ord"),
        # 账簿最热的聚合（ledger/engine._query_movements：WHERE book_id = ?
        # AND period_ord <= ? GROUP BY subject_id, period_ord）专用。
        # INCLUDE 把聚合要的三列塞进索引里，让它变成 Index Only Scan ——
        # 实测把 5.7 万行表上的这次聚合从「扫堆 + 读索引」变成纯索引遍历。
        # 注意它是 CONCURRENTLY 建的（见 0006），所以这里的声明只是让
        # autogenerate 认得它，别让它再生成一条 DROP INDEX。
        Index("ix_entry_book_period", "book_id", "period_ord",
              postgresql_include=["subject_id", "direction", "amount"]),
        Index("ix_entry_voucher", "voucher_id"),
        # **以 subject_id 打头**的索引（0011）。下面两条都打不成：
        #   ix_entry_subject_ord 的首列是 book_id —— 没有 book_id 约束就用不上；
        #   ix_entry_book_period 是账簿聚合的覆盖索引（0006），拿来做
        #   「这个科目出现在哪些凭证里」只会退化成扫索引再 Filter。
        # 凭证列表的「按科目筛选」（accounting/vouchers.py::list_vouchers
        # 里的 `Voucher.id.in_(select(Entry.voucher_id).where(
        # Entry.subject_id == ?))`）只按 subject_id 过滤，是全系统唯一一处
        # 以科目为入口、却没带 book_id 的查询。
        # 第二列带上 voucher_id 是为了让这个子查询 Index Only：
        # 它要的输出就是 voucher_id，带上就不用回堆。
        Index("ix_entry_subject_voucher", "subject_id", "voucher_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("voucher.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # int 而不是 smallint（审计 P2-08）：来源 period.ord 早已是 integer，
    # 这里再窄一档就等于在一份**派生**列上留了个永远走不到的溢出点。
    # 派生列与来源列类型不一致，是「模型改了类型、库没跟上」那类化石的同款。
    period_ord: Mapped[int] = mapped_column(Integer, nullable=False)
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
