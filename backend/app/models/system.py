"""系统域：用户、账套授权。

没有「公司」这一层：公司名是账套的一个文本属性，见 ``app.models.book.Book``。
"""
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
    # 管理员标记。原来靠「用户名恰好叫 admin」来判断，测试里造不出管理员，
    # 而且用户名是能改的 —— 权限判据必须存在字段里。
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class UserBook(Base, TimestampMixin):
    """账套授权。role 决定该用户在此账套内的权限范围。

    授权直接挂在账套上，没有公司这一层：同一家公司开多个账套（租金 / 管理费）时，
    每个账套各授权一次即可。
    """

    __tablename__ = "sys_user_book"
    __table_args__ = (UniqueConstraint("user_id", "book_id", name="uq_user_book"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    # OWNER / ACCOUNTANT(可制单记账) / VIEWER(只读)
    role: Mapped[str] = mapped_column(String(16), default="ACCOUNTANT", nullable=False)

    user: Mapped["SysUser"] = relationship()
    book: Mapped["Book"] = relationship()
