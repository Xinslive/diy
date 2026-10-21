"""新建 ``mcp_token`` 表：MCP 只读服务令牌。

Revision ID: 0034_mcp_token
Revises: 0033_book_year_guardrails
Create Date: 2026-10-21

**这一条改的是什么。**

给主应用加一个对外的 MCP（Model Context Protocol）只读数据服务，
外部 AI agent 用一个**绑定真实用户**的令牌接入。令牌表是这件事唯一的
新持久状态。

**表里刻意没有的两列**（理由写在 ``app/models/mcp.py`` 的模块 docstring 里，
这里只留一句，免得日后有人「顺手补上」）：

* **没有 ``expires_at``** —— 产品决定令牌永不过期。留一个恒为空的到期列，
  只会让人误以为令牌会过期，从而放松吊销管理。失效开关只有
  ``revoked_at`` / ``grace_until`` 与用户侧状态。
* **没有 ``book_scope``** —— 账套可见性 100% 来自 ``sys_user_book``。
  再加一列「令牌自己的账套范围」就是同一口径两份实现，迟早对不上。

**降级。** 删表即回到「MCP 面不存在」这个状态，没有中间语义 ——
``mcp_token`` 里没有业务数据，全是令牌生命周期。所以 ``downgrade`` 只
``DROP TABLE``，不试图保留什么。

**关于 ``trg_updated_at``。**
``db_init.sql`` 第 7 段给所有带 ``TimestampMixin`` 的表挂了
``BEFORE UPDATE`` 触发器，语义与 ORM 的 ``onupdate`` 一致：库自己改写的
行（比如运维直接跑 SQL）也要刷新 ``updated_at``。本表同样是
``TimestampMixin``，所以挂同一条触发器。

但**不能靠重放 db_init.sql 来装它**：那份文件的触发器清单是一个固定数组，
在 0034 之前重放它时 ``mcp_token`` 这张表还不存在（0005/0010/0031/0033
都在这条链前面），``CREATE TRIGGER ... ON mcp_token`` 会当场报「表不存在」。
所以这里的做法是：

1. 本迁移自己建这条触发器；
2. 同时把 ``db_init.sql`` 的清单加上 ``mcp_token``，并给那段 DO 块加上
   ``to_regclass`` 守卫 —— 守卫让「表还不存在」这件事不再是错误。

第 2 条是顺带修掉的一个真实脆弱点：那份清单此前假设「数组里的表一定都
已经建好了」，任何一条在它之前重放 db_init.sql 的迁移都会踩到同一个坑。
加守卫之后，清单里可以有「后建的表」，而重放顺序不再是个坑。
"""
from __future__ import annotations

from alembic import op

revision = "0034_mcp_token"
down_revision = "0033_book_year_guardrails"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # **建表用裸 SQL + IF NOT EXISTS，不用 ``op.create_table``。**
    #
    # 理由与 0026 / 0030 一致：迁移必须**幂等**。
    # ``tests/test_migration_guardrails.py`` 里有一条「把结构打回旧形态、
    # 再把版本号 stamp 回 0004、然后升级到 head」的用例 —— 那条路径会让
    # 0005..head 这段迁移**跑第二遍**。用 ``op.create_table`` 的话，第二遍
    # 会撞「relation already exists」而整条用例红。
    #
    # 这不是假设出来的场景：真实运维也会「手工重跑某一段迁移」。
    op.execute("""
        CREATE TABLE IF NOT EXISTS mcp_token (
            id SERIAL PRIMARY KEY,
            name VARCHAR(64) NOT NULL,
            token_hash VARCHAR(64) NOT NULL,
            user_id INTEGER NOT NULL REFERENCES sys_user(id) ON DELETE CASCADE,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            grace_until TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            last_used_at TIMESTAMPTZ,
            created_by VARCHAR(64) NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_mcp_token_hash UNIQUE (token_hash)
        )
    """)
    # 按用户查是这个表最常见的访问路径（管理面列某人的全部令牌、
    # 批量吊销某人的全部令牌）；token_hash 的唯一约束自带索引，
    # 不需要为「按摘要查」再单独建一个。
    op.execute("CREATE INDEX IF NOT EXISTS ix_mcp_token_user_id "
               "ON mcp_token (user_id)")
    # 与 ORM 的 onupdate 同一语义：库自己改写的行也要刷新 updated_at。
    # 函数体在 db_init.sql（唯一事实来源），这里只挂触发器。
    # DROP 先于 CREATE —— 触发器没有 IF NOT EXISTS。
    op.execute("DROP TRIGGER IF EXISTS trg_updated_at ON mcp_token")
    op.execute(
        "CREATE TRIGGER trg_updated_at BEFORE UPDATE ON mcp_token "
        "FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_updated_at ON mcp_token")
    op.execute("DROP INDEX IF EXISTS ix_mcp_token_user_id")
    op.execute("DROP TABLE IF EXISTS mcp_token")
