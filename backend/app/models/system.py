"""系统域：用户、公司、账套授权。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SysUser(Base, TimestampMixin):
    __tablename__ = "sys_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    real_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SysCompany(Base, TimestampMixin):
    """公司。一个公司可以同时存在多个账套（如租金账套、管理费账套）。"""

    __tablename__ = "sys_company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    books: Mapped[list["Book"]] = relationship(back_populates="company")


class UserBook(Base, TimestampMixin):
    """账套授权。role 决定该用户在此账套内的权限范围。"""

    __tablename__ = "sys_user_book"
    __table_args__ = (UniqueConstraint("user_id", "book_id", name="uq_user_book"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    # OWNER / ACCOUNTANT(可制单记账) / VIEWER(只读)
    role: Mapped[str] = mapped_column(String(16), default="ACCOUNTANT", nullable=False)

    user: Mapped["SysUser"] = relationship()
    book: Mapped["Book"] = relationship()
