"""系统域：用户、账套授权。

没有「公司」这一层：公司名是账套的一个文本属性，见 ``app.models.book.Book``。
"""
from __future__ import annotations

from datetime import datetime

from typing import Optional

from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String,
                        UniqueConstraint, func)
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

    # 登录失败计数与锁定截止时间（审计 P3-04 建议 4：登录接口增加限流与失败次数锁定）。
    #
    # 放**账号**上而不是内存里：多进程部署时每个进程各有一份内存计数，
    # 攻击者打满 N 个进程就能把 N 次额度用满，等于没锁。放内存还有一个更麻烦的问题 ——
    # 重启即清零，于是「慢速爆破」（每秒一次，攒够就停）根本不会触发阈值。
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # 令牌版本号（审计 P2-01）。
    #
    # JWT 是自签名的，服务端不存会话，所以「重置口令」过去只能改
    # ``password_hash`` —— 已经发出去的 bearer token 一律照常有效，直到自然过期
    # （普通用户 720 分钟）。口令泄露之后管理员把口令重置了，攻击者手里的
    # 旧令牌还能继续读写账套，最长还能用半天。
    #
    # 解法就这一个整数：签发时写进载荷，鉴权时与库里比一次，不一致就拒。
    # 撤销 = ``token_version += 1``，于是该账号**所有**在用令牌一次性作废，
    # 代价是这个人要重新登录一次 —— 这正是「改口令」该有的效果。
    #
    # 放在账号上而不是内存集合里：内存集合在多进程部署下每个进程各有一份，
    # 撤销只撤销了其中一份；重启即清零，等于没实现。
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")


class UserBook(Base, TimestampMixin):
    """账套授权。role 决定该用户在此账套内的权限范围。

    授权直接挂在账套上，没有公司这一层：同一家公司开多个账套（租金 / 管理费）时，
    每个账套各授权一次即可。
    """

    __tablename__ = "sys_user_book"
    __table_args__ = (
        UniqueConstraint("user_id", "book_id", name="uq_user_book"),
        # 角色白名单下沉到数据库（审计 P2-12）。
        #
        # 应用层已经改成白名单语义（core/deps.py::is_read_only），但那只挡
        # 「走 API 的写入者」。CHECK 约束挡的是另一条路：绕过 API 直接改库的
        # 写入者 —— 那条路上根本没有应用层，原来 ``role == "VIEWER"`` 的
        # 字面量比较在那里形同虚设，写个 'viewer' 就拿到完整写权限。
        #
        # 约束名与表达式必须与迁移 `0005_db_guardrails`（经 db_init.sql 交付）
        # **逐字一致**：两边分成两套名字的话，Alembic 下次 autogenerate 会
        # 同时报「库里有库里没有的」和「模型有库里没有的」，把一次改名
        # 伪装成两条变更。存量脏数据由该迁移按 NOT VALID 挂上去，
        # 这里只声明结构、不重复处理那批数据。
        #
        # 大小写归一化交给应用层（deps.normalize_role）做：库里若出现
        # 'viewer' 这种值，判定侧一律按只读处理（fail-closed），
        # 所以不必在库这一层再叠一条大小写约束。
        CheckConstraint(
            "role IN ('OWNER', 'ACCOUNTANT', 'VIEWER')",
            name="ck_sys_user_book_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False)
    book_id: Mapped[int] = mapped_column(ForeignKey("book.id", ondelete="CASCADE"), nullable=False)
    # OWNER / ACCOUNTANT(可制单记账) / VIEWER(只读)
    role: Mapped[str] = mapped_column(String(16), default="ACCOUNTANT", nullable=False)

    user: Mapped["SysUser"] = relationship()
    book: Mapped["Book"] = relationship()
