"""登录令牌的**单枚吊销名单**。

**为什么要有这张表。**

登录令牌不设时间上限（见 ``app/core/config.py`` 的 ``access_token_minutes``
与 ``app/core/security.py`` 的 ``token_minutes_for``）—— 载荷里压根没有
``exp``。永不过期的令牌，它的终点只有三个：

  1. **用户主动登出** —— 这一次必须真的作废，否则「退出登录」只是一句安慰；
  2. **改口令 / 停用账号** —— 账号级撤销，靠 ``sys_user.token_version``
     （``tv``），不需要这张表；
  3. **密钥轮换** —— 签名对不上，不需要这张表。

第 2 条是**账号级**的：改一次口令就把这个人所有设备的会话一起踢掉。
这正是「登出」不能用的手段 —— 「点一下退出登录」不该把手机上也一起踢了，
那是「退出全部设备」的语义。所以这里记的是**粒度更细的一层**：
一枚令牌一个 ``jti``。

**和 ``mcp_token`` 的关系。** MCP 令牌也永不过期，它的安全阀是
``mcp_token.revoked_at``（自己那张表）。本表是**登录令牌**这一侧的对位物，
两者不共用一张表 —— 一张表管两种凭据，就会有「这个字段对 MCP 是什么意思」
的歧义，而歧义在安全相关的表上是最贵的。

**不做什么。**

* **不存令牌本体**，只存 ``jti``（uuid4 的 32 位十六进制）—— 拿到这张表
  的人也无法反推出任何一枚令牌。
* **不存 ``expires_at``**：令牌永不过期，没有到期时间可记；留一个恒为空的
  列只会让日后的人以为「令牌会过期」，从而放松吊销管理
  （这条与 ``mcp_token`` 的取舍完全一致）。
* **不做跨请求缓存**：吊销必须**对下一次请求立刻生效**。库里查一次
  （主键命中，代价约零点几毫秒），换掉的是「登出之后还能用」这条硬伤。

**行数不会长**：一行 = 一次登出。一天登出十次，十年也就三万多行 ——
它记的还是「谁在什么时候登出」的审计线索，不是无限增长的日志。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (DateTime, ForeignKey, Index, Integer, String,
                        UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SysTokenRevocation(Base):
    """一枚**已被单独吊销**的登录令牌。

    一行 = 「这枚令牌（``jti``）从这一刻起不再有效」。

    **主键之外的唯一键就是 ``jti``**：一枚令牌最多一行，重复吊销是幂等的
    （登出接口可能被重放 —— 网络重试、用户手抖点两下）。
    """

    __tablename__ = "sys_token_revocation"
    __table_args__ = (
        # **具名**唯一约束（不是 ``unique=True``）：模型与库必须**同名同义**，
        # 否则 ``alembic autogenerate`` 会把它当成「库里有、模型里没有」
        # 而生成一条改约束的迁移（审计 P3-2 的那类静默失配）。
        UniqueConstraint("jti", name="uq_sys_token_revocation_jti"),
        # 按人查是这个表最常见的运维动作（「这个人的令牌都吊销了吗」
        # 「他换设备了，把旧的都踢掉」）。鉴权路径不走它 —— 那边按
        # ``jti`` 唯一约束点查。
        Index("ix_sys_token_revocation_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 被吊销的那枚令牌的 ``jti``（载荷里那个 uuid4 的十六进制串）。
    jti: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 这枚令牌属于谁。**记下来是为了能回答「谁在什么时候登出了哪台设备」**
    #: —— 出事之后的第一个问题往往就是它。
    #: ``ondelete="CASCADE"``：账号没了，这张名单对它也就没有意义了。
    user_id: Mapped[int] = mapped_column(
        ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False)
    #: 吊销原因。目前只有一种取值：**用户主动登出**。
    #: 写成列而不是自由文本，是为了让日后新增取值时有地方可枚举。
    reason: Mapped[str] = mapped_column(String(32), nullable=False, default="LOGOUT")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
