"""新建 ``sys_token_revocation`` 表：登录令牌的**单枚吊销名单**。

Revision ID: 0048_login_token_revocation
Revises: 0047_invoice_voucher_release
Create Date: 2026-10-30

**这一条改的是什么。**

登录令牌**不再有时间上限**（载荷里压根没有 ``exp``）—— 这是产品决定：
用户要的是「登录之后别老是被踢下线」，而不是「把 12 小时改成 30 天」。

而令牌一旦没有到期时间，它的终点就只剩两个来源：**账号级撤销**
（``sys_user.token_version``，改口令 / 停用时全量作废）和**这张表**
（按 ``jti`` 吊销**一枚**）。

没有这张表，「退出登录」就只是一句安慰 —— 用户点了退出，令牌照样能用，
而且能用到天荒地老。这条迁移就是那个「退出」的真实终点。

**为什么是「单枚」而不是复用 ``token_version``。**
``token_version`` 是账号级的：加一就把这个人**所有设备**的会话一起踢掉。
那是「退出全部设备」的语义，不该由「点一下退出登录」触发（共享电脑、
手机上还在用同一个账号的场景都会被误伤）。所以这里记的是更细的一层：
一枚令牌一个 ``jti``。

**表里刻意没有的两列**（理由写在 ``app/models/token_revocation.py`` 里）：

* **没有 ``expires_at``** —— 令牌永不过期，没有到期时间可记。留一个恒为空的
  到期列只会让日后的人以为「令牌会过期」，从而放松吊销管理
  （``mcp_token`` 当初也是这个取舍）。
* **不存令牌本体** —— 只存 ``jti``（uuid4 的 32 位十六进制）。拿到这张表的
  人反推不出任何一枚可用的令牌。

**降级。** 删表即回到「登出不吊销」这个状态（也就是本次改动的**前**状态），
没有中间语义 —— 这张表里没有业务数据，全是令牌生命周期。
注意降级**不会**让已签发的令牌重新生效：它们本来就没有 ``exp``，
降级后只是「登出不再作废」而已。

**幂等。** 建表用裸 SQL + ``IF NOT EXISTS``（理由与 0026 / 0030 / 0034 一致）：
``tests/test_migration_guardrails.py`` 里有一条「把结构打回旧形态、再把版本号
stamp 回 0004、然后升级到 head」的用例，那条路径会让本迁移**跑第二遍**。
"""
from __future__ import annotations

from alembic import op

revision = "0048_login_token_revocation"
down_revision = "0047_invoice_voucher_release"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # **建表用裸 SQL + IF NOT EXISTS，不用 ``op.create_table``。**
    #
    # 理由与 0026 / 0030 / 0034 一致：迁移必须**幂等** ——
    # ``tests/test_migration_guardrails.py`` 里那条「打回旧形态再升级到 head」
    # 的用例会本迁移跑第二遍，用 ``op.create_table`` 的话第二遍会撞
    # 「relation already exists」而整条用例红。
    #
    # 这不是假设出来的场景：真实运维也会「手工重跑某一段迁移」。
    op.execute("""
        CREATE TABLE IF NOT EXISTS sys_token_revocation (
            id SERIAL PRIMARY KEY,
            jti VARCHAR(32) NOT NULL,
            user_id INTEGER NOT NULL REFERENCES sys_user(id) ON DELETE CASCADE,
            reason VARCHAR(32) NOT NULL DEFAULT 'LOGOUT',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_sys_token_revocation_jti UNIQUE (jti)
        )
    """)
    # 按人查是这个表最常见的运维动作（「这个人的令牌都吊销了吗」）。
    # 鉴权路径不走它 —— 那边按 jti 的唯一约束点查，那条约束自带索引。
    op.execute("CREATE INDEX IF NOT EXISTS ix_sys_token_revocation_user_id "
               "ON sys_token_revocation (user_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sys_token_revocation_user_id")
    op.execute("DROP TABLE IF EXISTS sys_token_revocation")
