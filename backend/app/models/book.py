"""账套、会计期间、期初快照。

设计要点：
- 账套是唯一的顶层实体：公司名只是账套的一个文本属性（company_name），
  没有独立的「公司」表。报表页眉、凭证打印、Excel 导出直接读它即可，
  省掉一次查询，也不会出现「先建公司才能建账套」的额外步骤。
  同一家公司开多个账套（租金 / 管理费 / 代收）时，company_name 相同，
  靠 (company_name, name) 二元组区分。
- **年度是账套的属性，一个账套有很多个年度。** 账上并没有「2026 的账套」
  和「2027 的账套」两个东西，只有「启明物业这套账，目前记到哪一年」。
  科目表、权限、期初录入都跟着账套走；年度结转做的事是给同一个账套
  追加下一年的 12 个会计期间，并把本年期末余额写成下一年 1 月的期初。
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
    # 这是账套自己的科目体系属性，所以挂在账套上而不是写死在前端。
    subject_code_rule: Mapped[str] = mapped_column(String(32), default="4-2-2-2", nullable=False)
    # 保留字段：ACTIVE / CLOSED。年度结转不再新建账套，这里不再由结转改写，
    # 只留给「停用账套」这类运营语义使用。
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)

    periods: Mapped[list["Period"]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="Period.ord"
    )


class Period(Base, TimestampMixin):
    __tablename__ = "period"
    __table_args__ = (UniqueConstraint("book_id", "year", "month", name="uq_period"),)

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
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id", ondelete="CASCADE"), nullable=False)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subject.id", ondelete="CASCADE"), nullable=False)
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
