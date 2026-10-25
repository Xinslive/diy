"""新建 ``voucher_idempotency`` 表：新建凭证请求的幂等占位。

Revision ID: 0039_voucher_idempotency
Revises: 0038_entry_voucher_line_unique
Create Date: 2026-10-25

**这一条改的是什么。**

给「新建凭证」这个 POST 装一把幂等键（审计报告 §3.28）。

没有它的时候，那条路的失败场景是：

    新凭证 POST 发出 → 服务端已入库 → 响应在网络上丢了（30s 超时）
    → 客户端以为自己没存上 → 再敲一个字 → 自动保存又发一次 POST
    → 库里两张内容几乎相同的凭证

**为什么不是「客户端自己查一下有没有存上」。** 查是可以查，但查和写之间
还是有时间窗：两次自动保存几乎同时到（用户连敲两下、或者网络抖动让
第一个请求慢到 30s 才超时），两边都查不到、都去写 —— 还是两张。
所以这件事必须在**服务端**做，而且要靠数据库当场分出胜负。

**这张表记的是「请求占位」，不是业务数据。** 一行 = 一个
(账套, 幂等键)：

  · 谁先 ``INSERT … ON CONFLICT DO NOTHING`` 抢到这一行，谁负责把这次创建做完；
  · 做完把 ``voucher_id`` 填上 —— 后来的同键请求拿这张凭证的响应回去。

**唯一约束就是去重的全部机制。** ``uq_voucher_idempotency_book_key``
让同一账套里一把键只能占一行；两个并发请求抢同一把键时，后到的会**等**
在先到的那次事务上 —— 先到的提交，它拿到 0 行（键已存在）；先到的回滚，
它抢到。两种结果都保证**库里只有一张凭证**。

**为什么 ``voucher_id`` 的外键是 ``ON DELETE CASCADE``。**
凭证被删了，这把键也就没有意义了 —— 留着它只会让「同一把键」永远指向
一张不存在的凭证，下一次同键请求连回放都回放不出来。

**降级。** 删表即回到「新建凭证没有幂等键」这个状态，没有中间语义 ——
这张表里没有业务数据，全是请求占位。

**幂等。** 用裸 SQL + IF NOT EXISTS，不用 ``op.create_table`` ——
理由与 0026 / 0030 / 0034 一致：迁移必须能重跑
（``tests/test_migration_guardrails.py`` 里有一条「把结构打回旧形态、
再把版本号 stamp 回旧版、然后升级到 head」的用例，那条路径会让这一段
迁移**跑第二遍**）。
"""
from __future__ import annotations

from alembic import op

revision = "0039_voucher_idempotency"
down_revision = "0038_entry_voucher_line_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS voucher_idempotency (
            id SERIAL PRIMARY KEY,
            book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
            idem_key VARCHAR(128) NOT NULL,
            request_hash VARCHAR(64) NOT NULL,
            voucher_id INTEGER REFERENCES voucher(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # 去重的全部机制就在这一条唯一索引上（见上面模块 docstring）。
    # **名字与 ORM 的声明一致**（app/models/idempotency.py）：
    # ``app/core/idempotency.py`` 的 ``ON CONFLICT (book_id, idem_key)``
    # 靠的正是它 —— 两边名字/列不一样的话，去重会静默失效
    # （ON CONFLICT 指定的列上没有唯一约束时，PostgreSQL 直接报错，
    #  但如果有人改了索引名，ORM 的 create_all 与迁移建的就会分叉）。
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_voucher_idempotency_book_key "
               "ON voucher_idempotency (book_id, idem_key)")
    # 清理残留用：占位行正常情况下跟着凭证走（CASCADE），
    # 但「凭证建好了、填 voucher_id 那一步之前事务回滚」这种情形下
    # 行会留下来 —— 按 created_at 能兜底清掉。
    op.execute("CREATE INDEX IF NOT EXISTS ix_voucher_idempotency_created_at "
               "ON voucher_idempotency (created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_voucher_idempotency_created_at")
    op.execute("DROP INDEX IF EXISTS uq_voucher_idempotency_book_key")
    op.execute("DROP TABLE IF EXISTS voucher_idempotency")
