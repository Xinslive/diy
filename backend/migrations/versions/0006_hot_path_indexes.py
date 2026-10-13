"""两条最热路径的索引（审计 P2-6）

Revision ID: 0006_hot_path_indexes
Revises: 0005_db_guardrails
Create Date: 2026-10-13

**为什么单独一条。** 索引是运维上最该能与「紧急护栏」分开部署的一类变更：
0005 里的 ``ADD CONSTRAINT ... FOREIGN KEY`` 会对整张表做校验并持
ACCESS EXCLUSIVE，而建索引只持 SHARE。把两者放同一条 revision，运维就没法
「先把护栏上线、索引约在维护窗口」。

**为什么没有用 ``CREATE INDEX CONCURRENTLY``（实测过的结论，不是省事）。**

试过，两条路都堵死：

1. ``op.get_context().autocommit_block()`` 要求 alembic 自己持有事务
   （``self._transaction is not None``）。命令行 ``alembic upgrade head``
   满足，而 ``app.core.schema_version.upgrade_to_head``（``conftest.py``
   建库、运维脚本走的都是它）的事务是调用方用 ``conn.begin()`` 开的，
   alembic 手里没有 → 进去就 ``AssertionError``。
   手工绕开（先 ``conn.commit()`` 再切 DBAPI autocommit）也不成立：
   ``upgrade_to_head`` 的 ``with conn.begin():`` 在退出时会对一个**已被提交**
   的事务再操作，直接抛
   ``InvalidRequestError: Can't operate on closed transaction inside context manager``。
   改 ``schema_version.py`` 或 ``migrations/env.py`` 能解决 —— 但那两个文件
   不属于这条工作线的归属范围。
2. 另开一条连接跑 CONCURRENTLY 也不行：``migrations/env.py`` 的
   ``context.begin_transaction()`` 把**整条 revision 链**包在同一个事务里，
   所以跑 0006 时 0005 刚加的约束仍持着 ``entry`` / ``period_opening`` 的
   ACCESS EXCLUSIVE 锁；另一条连接要等这把锁，而等锁的那个事务正是持锁的
   事务 —— 必然自锁。

真要用上 CONCURRENTLY，前置条件是在 ``env.py`` 打开
``transaction_per_migration = True``，让每条 revision 各自一个事务，
之后索引必须**独占**一条 revision（本条即为此预留）。已记在报告里。

代价对比：同一次 upgrade 里，0005 的外键全表校验已经持 ACCESS EXCLUSIVE，
相对之下建索引的 SHARE 锁窗口小到可以忽略。

**为什么是这两条。**

1. ``ix_entry_book_period ON entry (book_id, period_ord)
   INCLUDE (subject_id, direction, amount)``
   —— 账簿引擎唯一的全账扫描（``ledger/engine.py::_query_movements``）：
   ``WHERE entry.book_id = ? AND entry.period_ord <= ? GROUP BY subject_id, period_ord``。

   **修正审查报告的一处判断。** 报告称「现有索引只能用到最左前缀，PG 退化为
   扫该账套全部分录」。实测（PG 16.15，5.76 万行 entry、10 个账套）**不成立**：
   ``ix_entry_subject_ord`` 的 ``Index Cond`` 同时带上了 ``book_id = 1`` 与
   ``period_ord <= 12`` —— PG 16 把后者当成可用的 Index Cond（skip scan），
   并未退化成「只按 book_id 过滤、再扫该账套全部分录」。

   所以这条索引的收益要老实说：**主要不是「从扫变成索引」，而是去掉对
   ``subject_id`` 的枚举，再靠 INCLUDE 免掉回堆**（Bitmap Heap Scan →
   Index Only Scan）。

   ``ix_entry_subject_ord`` **保留**：明细账逐科目取数（``subject_id``
   打在第二列）靠它，宽索引替不了。

2. ``ix_period_opening_book ON period_opening (book_id, period_id)``
   —— ``_roll_up_openings``：``WHERE period_opening.book_id = ?``。
   这条**原报告没列**，是账簿工作包在本机实测时发现的更硬的一条：
   ``period_opening`` 上**一个 book_id 索引都没有**，执行计划是
   ``Seq Scan + Filter: (book_id=1)``。带上 ``period_id`` 是为了顺带覆盖
   与 ``period`` 的关联；索引很小（每个 (期间, 科目) 一行）。

**幂等。** 两条都是 ``IF NOT EXISTS``，重跑不报错也不重建。ORM 里
``models/accounting.py`` / ``models/book.py`` 已声明同名同列的 ``Index``，
所以 autogenerate 不会生成 DROP/CREATE 来「修」它们（``INCLUDE`` 是
postgresql_include，autogenerate 不比对它，也就不会误报差异）。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_hot_path_indexes"
down_revision = "0005_db_guardrails"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_entry_book_period", "entry", ["book_id", "period_ord"],
        unique=False, if_not_exists=True,
        postgresql_include=["subject_id", "direction", "amount"],
    )
    op.create_index(
        "ix_period_opening_book", "period_opening", ["book_id", "period_id"],
        unique=False, if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_period_opening_book", table_name="period_opening", if_exists=True)
    op.drop_index("ix_entry_book_period", table_name="entry", if_exists=True)
