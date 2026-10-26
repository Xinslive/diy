"""账套、会计期间、期初快照。

设计要点：
- 账套是唯一的顶层实体：公司名只是账套的一个文本属性（company_name），
  没有独立的「公司」表。报表页眉、凭证打印、Excel 导出直接读它即可，
  省掉一次查询，也不会出现「先建公司才能建账套」的额外步骤。
  同一家公司开多个账套（租金 / 管理费 / 代收）时，company_name 相同，
  靠 (company_name, name) 二元组区分。
- **年度是账套的属性，一个账套有很多个年度。** 账上并没有「2026 的账套」
  和「2027 的账套」两个东西，只有「启明物业这套账，目前记到哪一年」。
  科目表、权限跟着账套走；年度结转做的事是给同一个账套
  追加下一年的 12 个会计期间，并把本年期末余额写成下一年 1 月的期初。
- **但「记账单位」是 (账套, 年度) 这一对，不只是账套。** 账套回答的是
  「这是谁家的哪本账」，年度回答的是「这一年这本账长什么样」。
  科目表、期初余额、初始化进度都是**年度**的属性 —— 2026 年和 2027 年
  各有各的科目表、各有各的期初，就像挂在同一个账套名下的两个独立账套
  （``book_year`` 就是这个「子账套」）。
  权限仍然是**账套级**的：一个人对这套账有写权限，对它的每个年度都有。
- 期间是「结账锁」的载体。locked=True 时数据库触发器拒绝一切凭证写入。
- period_ord 是**账套内跨年度连续**的期间序号：
      ord = (year - start_year) * 12 + month
  账簿聚合（累计、上下卷、区间比较）全部只认这个序号，所以跨年度天然连续；
  「本年累计」则在每个年度的 1 月归零（见 ledger/engine.py）。
- period_opening 只在两处写入：账套启用时的录入、年度结转时的结转。
  其余期间的期初由上一期期末实时推导。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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
        # 一个账套跨多个年度，所以年度不在唯一键里
        UniqueConstraint("company_name", "name", name="uq_book"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 单位名称：页眉 / 报表 / 打印用，允许与其他账套重复，不做外键
    company_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 账套名：业务用途，如「租金账套」「管理费账套」
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 账套的**起始**会计年度。年度结转不会新建账套，只是往后追加年度，
    # 所以这里记的是「这套账从哪一年开始」，而不是「当前是哪一年」。
    start_year: Mapped[int] = mapped_column(Integer, nullable=False)
    # 账套启用月份（1..12），只对 start_year 那一年生效；之后的年度从 1 月起
    start_month: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    # 科目编码规则：逐级几位，如「4-2-2-2」= 一级 4 位、二级 2 位……
    # 新增科目时按它自动接号（1002 银行存款 → 100204），并显示在新增科目的提示里。
    #
    # **账套级的是「默认」**（新年度建起来时继承它），**有效值在年度上**
    # （``BookYear.subject_code_rule``）：2027 年换了一张结构完全不同的科目表，
    # 它的接号规则就该跟着换，而 2026 年正在用的规则不该被带着一起变。
    subject_code_rule: Mapped[str] = mapped_column(String(32), default="4-2-2-2", nullable=False)
    # 保留字段：ACTIVE / CLOSED。年度结转不再新建账套，这里不再由结转改写，
    # 只留给「停用账套」这类运营语义使用。
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)

    # 数据版本号：**由触发器维护**，应用层只读不写（迁移 0015）。
    #
    # 任何影响聚合的写（凭证/分录/科目/期初/期间）都在同一个事务里把它 +1。
    # `LedgerEngine` 构造时先读它、再用它当跨请求缓存的键的一部分，
    # 于是「写提交之后开始的请求一定读到新数」成立，且多 worker 天然一致
    # （版本号在库里，不在各进程手里）。
    #
    # **不要在应用代码里写这个字段** —— 手写的赋值不会可靠地与写入同一事务，
    # 而漏 bump 一次的后果是「用户改完账看到的还是旧余额」。规则见
    # migrations/versions/0015_book_data_version.py。
    data_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    periods: Mapped[list["Period"]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="Period.ord"
    )
    years: Mapped[list["BookYear"]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="BookYear.year"
    )


class BookYear(Base, TimestampMixin):
    """年度账套（子账套）：一个账套下的一个会计年度。

    **它是真正的记账单位。** 账套（``Book``）回答「这是谁家的哪本账」，
    年度账套回答「这一年这本账长什么样」—— 科目表、期初余额、初始化进度
    都是**年度**的属性。2026 年和 2027 年各有各的科目表、各有各的期初，
    就像挂在同一个账套名下的两个独立账套。

    **为什么不是「年度 = 账套的一个整数列」。** 因为科目表是**一组行**
    （``subject``），它得挂在某个东西下面。挂账套上就只有一个年度能用它；
    挂年度上，每个年度各有一份，账套仍然只有一个（名字、单位、授权、
    账套列表里的一行）—— 这正是用户要的东西。

    **权限不在这里**，仍在账套上：一个人对这套账有写权限，对它的每个年度都有。

    ---- 初始化状态（从 ``Book`` 挪下来，迁移 0032）----------------------
    初始化被显式拆成**有先后的两步**：先定科目，再录期初余额。
    期初是挂在科目上的，所以顺序不是流程偏好而是数据依赖 —— 账上还没有
    科目时录进来的期初挂不到任何科目上，只能整批丢掉。用户看不出这个
    先后关系（科目表和期初余额是两个页面），所以状态记在**年度**上，由接口层拦。

    为什么必须是年度级而不是账套级：每个年度有**自己的**科目表，也就各有
    各的「第 1 步做没做」。2026 年导入了科目表、2027 年还没导 —— 账套级的
    状态会把 2027 一起标成「已完成」，于是 2027 年可以在**一张科目都没有**
    的情况下录期初、记账，期初整批挂空。

    「完成第 1 步」有两条路：从用友等外部系统导入科目表（IMPORT），
    或确认沿用建账时系统装好的标准科目表（CHART）。两条路都算数 ——
    强按第一条卡人，会把「就用标准科目表从零开始记账」的用户挡在门外。
    """

    __tablename__ = "book_year"
    __table_args__ = (
        UniqueConstraint("book_id", "year", name="uq_book_year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    # 该年度的科目编码规则（新增科目时怎么接号）。建年度时从账套继承。
    subject_code_rule: Mapped[str] = mapped_column(String(32), default="4-2-2-2", nullable=False)

    # ---- 本年利润科目（手工设定）----
    #
    # 结转损益的**目的科目**。系统按候选编码自动识别（3103 / 4103），
    # 但每本账的科目表是各人自己的：编码改成 3111 的有、干脆叫「本年损益」
    # 的也有。识别不到时账就结不了转，而**没有任何地方能改** ——
    # 所以这里留一个手工设定值（存的是**编码**，不是 id）。
    #
    # 为什么存编码而不是 id：科目表每个年度各有一份，同一编码在 2026 与 2027
    # 是两行不同的记录。存 id 就只能管一年；存编码，这一年设了，
    # 下一年的科目表若是同一套结构（年度结转就是照抄上一年的），照样能用。
    #
    # **空 = 走自动识别**，不是「不识别」。写死一个非空默认值反而会盖掉
    # 那些本来就识别得出来的账套。
    profit_subject_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # ---- 未分配利润科目（手工设定）----
    #
    # 年度结转的**目的科目**：本年利润的余额冲到这里（留存收益）。
    # 系统按候选编码自动识别（310415 / 3104 / 4104），但每本账的科目表
    # 是各人自己的：编码改成 310411 的有、把「未分配利润」挂在别的大类码
    # 底下的也有。识别不到时**年度结转就结不了**，而界面上没有地方能改。
    #
    # 口径跟 ``profit_subject_code`` 一模一样：年度级、存**编码**不存 id、
    # **空 = 走自动识别**（空值不是「不识别」，是「没覆盖」）。
    retained_subject_code: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # ---- 初始化状态 ----
    # 第 1 步（定科目）完成的时间与来源。IMPORT = 导入了外部科目表；
    # CHART = 确认沿用标准科目表。**存量为空 = 还没做第 1 步。**
    init_subjects_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    init_subjects_source: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )
    # 第一次真正写入期初余额的时间。第一步录了一堆期初不等于初始化做完了 ——
    # 还要等它存进库，才算第 2 步走完。
    init_openings_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 第 2 步的来源：IMPORT = 导入过余额表；NONE = 本期本来就没有期初余额
    # （新公司第一年、上一年度是空账）。**没存来源的话，「已导入 213 条」与
    # 「确认过没有期初」在库里长得一模一样**，而后者恰恰最需要留痕 ——
    # 出事要查「当时是不是本来就没有」，而界面上一个数字都没有。
    init_openings_source: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True
    )

    book: Mapped["Book"] = relationship(back_populates="years")


class Period(Base, TimestampMixin):
    __tablename__ = "period"
    __table_args__ = (
        UniqueConstraint("book_id", "year", "month", name="uq_period"),
        # (book_id, ord) 是「账套内连续期间序号」的自然主键：账簿聚合、
        # 分录冗余列（entry.book_id + entry.period_ord 的组合外键）都指着它。
        # 没有这条 UNIQUE，PG 不允许拿两列做外键目标，entry 的冗余列
        # 也就无从被库层钉住。ord 本来就由 (year - start_year) * 12 + month
        # 唯一决定，加约束不会与存量数据冲突。
        UniqueConstraint("book_id", "ord", name="uq_period_book_ord"),
        # **每个会计期间必须挂在一个已登记的年度账套上**（迁移 0032）。
        #
        # 这条组合外键把「期间属于哪个账套的哪一年」与「年度账套登记的是
        # 哪个账套的哪一年」钉成同一件事：库里不可能出现一个
        # 「账套 A 的 2027 年」却没有 A 的 2027 年度账套行。
        #
        # 为什么要它：科目表挂在年度上，而分录的科目必须与所在期间同年度
        # （库层触发器 ``fn_entry_subject_year_guard``）。那个触发器要拿
        # 「这个期间属于哪一年」当判据 —— 如果期间本身的年度可以是一句
        # 没有对应行的空话，触发器的判据就成了空中楼阁。
        ForeignKeyConstraint(
            ["book_id", "year"], ["book_year.book_id", "book_year.year"],
            name="fk_period_book_year", ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # 账套内序号（跨年度连续）；用于聚合时的区间比较
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
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
        # 期初汇总查询（ledger/engine._roll_up_openings：WHERE book_id = ?）
        # 原来**一个 book_id 索引都没有**，PG 只能扫全表再过滤。实测：见 0006。
        Index("ix_period_opening_book", "book_id", "period_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id", ondelete="CASCADE"), nullable=False)
    # **RESTRICT，不是 CASCADE**（审计 P1-05 / P1-10）。
    #
    # 这条外键上分歧过：库里是 RESTRICT（db_init.sql 的 DO 块 + 0003 都写死
    # 了它，0003 的 docstring 甚至说「这条外键正是本 revision 存在的理由」），
    # 而模型这里写的是 CASCADE。谁照着模型跑一次
    # ``alembic revision --autogenerate``，就会生成一条把 RESTRICT 改回
    # CASCADE 的 revision —— 那正是 10-11 报告实测过的静默数据损失：
    # 「只被期初引用、没有凭证引用」的科目一删，期初行跟着消失，
    # 2 条删成 1 条，余额 0 → -100，没有任何报错或审计线索。
    #
    # 模型侧必须和库一致，否则 autogenerate 就是一台「把护栏改回缺陷」的机器。
    # 防回归不靠自觉：tests/test_migration_guardrails.py 里有一条
    # compare_metadata 用例，断言模型与库的外键集合零差异。
    subject_id: Mapped[int] = mapped_column(ForeignKey("subject.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal("0"), nullable=False)


# --------------------------------------------------------------------- 期间序号换算
#
# 账簿引擎只认 Period.ord 一个序号，年度只是它的一个视图。所有换算都走这里，
# 免得各处自己写 (year - start_year) * 12 + month，写错一处余额就整体漂移。

FIRST_ORD = 1  # 第一个年度的 1 月对应的 ord


def period_ord(year: int, month: int, start_year: int) -> int:
    """年度 + 月份 -> 账套内连续序号。"""
    return (year - start_year) * 12 + month


def year_start_ord(start_year: int, year: int, start_month: int = 1) -> int:
    """某个年度第一个会计期间的 ord。

    账套第一年从 start_month 开始（启用月之前的月份不存在），
    之后每个年度都从 1 月开始。
    """
    if year == start_year:
        return start_month
    return (year - start_year) * 12 + 1


def ord_to_month(ord_: int) -> int:
    """ord -> 月份（1..12）。"""
    return (ord_ - 1) % 12 + 1


def book_first_ord(book: "Book") -> int:
    return book.start_month
