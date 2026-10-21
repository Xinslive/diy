"""MCP 只读服务令牌。

这张表是整个 MCP 面的**唯一**持久状态。它回答两个问题：

1. 「这个 agent 是谁」—— ``user_id`` 指向一个**真实用户**。
2. 「它还能不能用」—— ``enabled`` / ``revoked_at`` / ``grace_until``。

**账套可见性不在这张表里。** 它 100% 来自 ``sys_user_book``：令牌绑定的那个
用户能访问哪些账套，用这个令牌的 agent 就只能访问哪些账套。用户侧改了角色、
撤了授权、停用了账号，agent 侧下一次调用立刻同步 —— 因为每次调用都重新查
``UserBook`` 与 ``sys_user``，**不做跨请求缓存**（与 ``core/deps.py`` 的
``_link_or_403`` 同一个原则：改密/停用/换角色必须立刻生效）。

刻意**没有**的两列，以及为什么
----------------------------

* **没有 ``expires_at``**：产品决定是「令牌永不过期」。留一个恒为空的到期列，
  只会让日后的人以为「令牌会过期」，从而放松吊销管理 —— 而吊销是这件事
  唯一的安全阀。失效开关只有两个：``revoked_at`` / ``grace_until``，
  以及**用户侧状态**（停用/删除）。
* **没有 ``book_scope``**：账套范围完全由 ``UserBook`` 决定。留一列空数组的
  「令牌账套范围」会造成两套可见性口径，迟早对不上 —— 这正是本仓审计第 7.3 节
  记过的「同一口径两份实现」的病根。

凭据处理
--------
明文形如 ``mcp_<token_urlsafe(32)>``，库里只存 ``sha256(明文)`` 的十六进制。
用 SHA-256 而不是 PBKDF2：口令是用户自选的、低熵，必须用慢 KDF 拖慢离线
爆破；令牌是 256 位随机生成的，没有字典可撞，快散列正是正确选择。
本仓对这两类凭据已经区分处理（``core/security.py`` vs
``scheduler/service.py``），MCP 令牌归入后者。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (Boolean, DateTime, ForeignKey, Index, Integer, String,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class McpToken(Base, TimestampMixin):
    """外部 AI agent 接入用的**只读**服务令牌。"""

    __tablename__ = "mcp_token"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_mcp_token_hash"),
        # 按用户查是这个表最常见的访问路径（管理面列某人的全部令牌、
        # 批量吊销某人的全部令牌），而 token_hash 的唯一约束自带索引，
        # 不需要为「按摘要查」再单独建一个。
        Index("ix_mcp_token_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    #: 用途标识（如「Claude Desktop · 财务问答」）。**必填** ——
    #: 一个用户可以有多个令牌（每个外部 agent 一个），名字是管理员唯一能
    #: 分辨「哪个 agent 在用」的线索，也是按 agent 单独吊销的前提。
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    #: ``sha256(明文)`` 的十六进制小写。**明文只在签发/轮换的响应里返回一次**，
    #: 库里永远不存。
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 令牌绑定的真实用户。**ON DELETE CASCADE**：用户被删 ⇒ 令牌一起消失，
    #: 不留一张指向空用户的「僵尸令牌」。
    user_id: Mapped[int] = mapped_column(
        ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False)

    #: 软开关。吊销会同时把它置 False，但反过来单独关掉它**不等于**吊销 ——
    #: 关掉只是「现在不能用」，记录与审计都还在。
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true")

    #: 轮换宽限期截止。轮换时旧令牌进入宽限：**宽限期内仍然可用**，
    #: 到期自动失效（由校验链比较 now 决定，不需要定时任务去清）。
    grace_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None)

    #: 吊销时刻。**与 ``grace_until`` 配合**：立即吊销 ⇒ ``revoked_at`` 有值
    #: 且 ``grace_until`` 为空；轮换 ⇒ ``revoked_at`` 有值且 ``grace_until``
    #: 在未来（此时仍可用）。
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None)

    #: 每次成功调用更新。管理面用它算 ``unused_days``，让管理员一眼看出
    #: 哪些令牌是僵尸、哪些还在被外部 agent 使用。
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None)

    #: 签发人（管理员用户名）。留痕用。
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    user: Mapped["SysUser"] = relationship()  # type: ignore[name-defined] # noqa: F821
