"""管理员二次验证（邮箱链接）的挑战记录。

**它解决的问题。** 审查报告 P0-1：「管理员账号无任何二次验证」。管理员口令
一旦泄露（钓鱼、撞库、旧备份里的散列被离线爆破），持码者立刻获得
「开号 / 改口令 / 装插件 / 读 OSS 凭据 / 轮换调度密钥」的全套权限 ——
而这些权限恰好是普通会计永远拿不到的。二次验证把「拿到口令」与「拿到会话」
拆成两件事：前者泄露不再等于后者泄露。

**形态：邮箱链接。** 复用项目已有的 SMTP 通知通道（``app/notify/mailer.py``），
口令验过之后往通知邮箱发一封带一次性链接的邮件，点链接即完成登录。

**为什么这张表要存两份额外的东西。**

一次「邮箱链接式登录」实际有两个参与者：

1. **发信的浏览器**（发起登录的那台机器）—— 它拿到了口令，需要知道
   「有人点了那封邮件吗」；
2. **收信的邮箱**（可能是手机上点的）—— 它拿到了链接，点开即确认。

第 2 步的链接是「一串凭证」，它会被邮件网关扫描、被手机锁屏预览、被 IT
留档 —— 所以**它绝不能直接就是会话令牌**。本表的分工是：

* ``email_token_hash`` —— 邮件链接里那串东西的散列。点链接只把挑战标成
  ``VERIFIED``，**拿不到任何会话**；
* ``client_secret_hash`` —— 只发给**发起登录的那个客户端**的一串东西的散列。
  客户端拿着 ``challenge_id + client_secret`` 轮询状态，挑战被验证后才换到
  真正的访问令牌。

于是「邮件链接被第三方点开」的最坏后果，只是浪费掉一次挑战
（发起方再点一次「重新发送」即可），而**不是**第三方拿到了管理员会话。
这条分工是整个设计里最要紧的一处，请不要为了「少一个字段」把它合并掉。

**为什么不存明文。** 与 ``SysUser.password_hash`` 同一个口径：库里只存
散列。邮件链接被扫到时，攻击者拿到的是散列而不是可用的凭证。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (DateTime, ForeignKey, Index, Integer, String,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# 挑战状态机。**每次跃迁都由服务端按条件写完成**，不靠「先查后写」，
# 否则两个并发请求会各自以为自己拿到了这个挑战。
#
#   PENDING  ──(点邮件链接)──►  VERIFIED  ──(客户端换令牌)──►  CONSUMED
#      │                          │
#      └──────(超时/被顶替)───────┴──►  EXPIRED
#
# PENDING → EXPIRED：超过 admin_mfa_ttl_seconds 没点。
# PENDING → CONSUMED：同一账号发起新的挑战时，旧挑战作废。
STATUS_PENDING = "PENDING"
STATUS_VERIFIED = "VERIFIED"
STATUS_CONSUMED = "CONSUMED"
STATUS_EXPIRED = "EXPIRED"

MFA_STATUSES = (STATUS_PENDING, STATUS_VERIFIED, STATUS_CONSUMED, STATUS_EXPIRED)


class AdminMfaChallenge(Base, TimestampMixin):
    """一次「管理员登录 → 邮箱点链接」的挑战。

    **时间口径：aware UTC。** 全部时间列都是带时区的，Python 侧一律用
    ``datetime.now(timezone.utc)``。这与 ``app/api/auth.py`` 里那些
    **naive UTC** 的登录时间列不是同一套 —— 混用会在东八区部署时凭空差
    8 小时。这里刻意与 auth.py 分开：本表的过期判断要跟「现在」比，
    差 8 小时的表现是「链接永远不过期」或「链接一出生就过期」，
    两种都很难在事后看出来。
    """

    __tablename__ = "admin_mfa_challenge"
    __table_args__ = (
        # 邮件链接里的 token 是全局唯一的一次性凭证。唯一约束在这里是
        # **并发正确性的一部分**：两个请求同时用同一个 token 点链接时，
        # 靠这个约束让其中一个失败，而不是两个都成功。
        UniqueConstraint("email_token_hash", name="uq_admin_mfa_challenge_email_token"),
        # 清理入口按「未完成且已过期」扫。没有索引就是每次清理一次全表扫，
        # 而清理会挂在登录路径上顺带做。
        Index("ix_admin_mfa_challenge_pending", "status", "expires_at"),
        # 「这个账号当前有没有未完成的挑战」—— 发起新挑战时要把旧的作废，
        # 这条查询按 user_id 走。
        Index("ix_admin_mfa_challenge_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 挑战属于哪个管理员。用户被删时挑战一并消失 —— 留着它没有意义，
    # 而且留着就等于留了一份「可以拿它去换会话」的过期凭证。
    user_id: Mapped[int] = mapped_column(
        ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False)

    # 邮件链接里那串 token 的 SHA-256 散列。**不存明文**。
    email_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 客户端持有的那串 secret 的 SHA-256 散列。**不存明文**。
    client_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), default=STATUS_PENDING, nullable=False)
    # 发出时刻与过期时刻。过期判定在服务端做，客户端显示的倒计时只是提示。
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 邮件最后一次发出的时刻 —— 用于「重新发送」的最短间隔节流。
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # 链接被点开的时刻 / 挑战被换走令牌的时刻。
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)

    # 发起这次登录的来源 IP。审计用：管理员从哪台机器发起的登录。
    ip: Mapped[str] = mapped_column(String(45), default="", nullable=False)
    # 发起这次登录的 User-Agent。审计用。
    user_agent: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    # 这次挑战是因为「登录」发起的，还是因为「高危动作要求近期重新认证」
    # 发起的。后者由管理员**已登录**的会话触发。
    reason: Mapped[str] = mapped_column(String(32), default="login", nullable=False)
