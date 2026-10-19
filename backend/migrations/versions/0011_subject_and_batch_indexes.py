"""两条被审计点名、却一直没有索引的访问路径（审计 2026-10-14）

Revision ID: 0011_subject_and_batch_indexes
Revises: 0010_subject_year_code_subtree
Create Date: 2026-10-14

**要修的现象。** ``entry.subject_id`` 上**一条以它为首列的索引都没有**。

``accounting/vouchers.py::list_vouchers`` 里「按科目筛选凭证」是这么写的::

    Voucher.id.in_(select(Entry.voucher_id).where(Entry.subject_id == subject_id))

只按 ``subject_id`` 过滤，**没有 book_id 约束**。而 ``entry`` 上现有的三条索引
全部用不上：

* ``ix_entry_subject_ord (book_id, subject_id, period_ord)`` —— subject_id 打在
  第二列，前面少了 ``book_id = ?``，PG 拿不到可用的 Index Cond；
* ``ix_entry_book_period (book_id, period_ord) INCLUDE (...)`` —— 同理，
  首列 book_id 没约束；它是 0006 为账簿聚合建的覆盖索引，不该拿来当过滤索引；
* ``ix_entry_voucher (voucher_id)`` —— 反方向。

实测（开发库，PG 16.15）::

    ->  Seq Scan on entry e
          Filter: (subject_id = '3109'::smallint)
          Rows Removed by Filter: 438

580 行里读了 580 行、丢掉 438 行，命中 142 行。行数一上来就是全表扫。

**为什么是 ``(subject_id, voucher_id)`` 而不是单列 ``(subject_id)``。**
这个子查询的输出列就是 ``voucher_id``，把它放进索引里，那条子查询就能
Index Only —— 一次都不回堆。单列版本则要多一次 heap fetch 换 voucher_id。
第二列是 int4，索引仍然很小（每条分录一个条目 + 两个 int4）。

**索引建好之后，在 580 行上 PG 仍然选 Seq Scan —— 这是对的，不是回归。**
15 个页的全表扫比任何索引都便宜，所以这个量级下新索引是**备而不用**。
本条的收益要到行数上来、Seq Scan 的代价超过索引往返时才兑现。
能在这里验的只有一件事：「这条索引**可用**」，所以对应的用例在 EXPLAIN 里
关掉 seqscan 再断言（见 ``tests/test_perf_data_layer.py``）。

**已知取舍：这条索引也会成为明细账的候选（这里只说明，不在这里解决）。**
明细账取数（``ledger/queries.py``）是
``book_id = ? AND subject_id IN (...) AND period_ord BETWEEN ? AND ?``。
审计已实测出：单科目时它走 ``ix_entry_subject_ord``，**3 个科目及以上时改走**
覆盖索引 ``ix_entry_book_period`` 并 ``Filter: subject_id = ANY(...)``、丢弃约
71% 的行，且 ``SET enable_seqscan=off`` 压不住。审计结论是「不必动索引，
先在生产量级跑 EXPLAIN 确认交叉点」，本条**不去碰那个交叉点**。

但必须说清楚本条的副作用：多一条以 subject_id 打头的索引，明细账就多一个
候选。在能测到的量级（book 59，580 行 / 16 个科目）上实测，**新索引前后明细账
的计划形状没有变化** —— 都是 ``Bitmap Index Scan`` + ``Bitmap Heap Scan``，
读到的 heap block 数与走 ``ix_entry_subject_ord`` 时相同（7 blocks），
差别只在索引层 2 个 buffer vs 3 个。生产量级上它会不会挤掉
``ix_entry_subject_ord``，本条**没有测过**，留给后续在生产量级复测。

**如果哪天能改到 ``vouchers.py``，更省的做法是不加这条索引。**
那句子查询补上 ``Entry.book_id == book_id`` 就能复用现成的
``ix_entry_subject_ord`` 的前导列，连一条新索引都不用建。该文件属于并行工作线，
本条不碰；是否补由文件属主定。

--------------------------------------------------------------------------
**上面那条「更省的做法」实测是错的，别照着改。**（2026-10-15 压测基线顺带实测）

真在 ``accounting/vouchers.py::query_vouchers`` 上试了（账套 60,000 分录 /
15,000 凭证 / 317 末级科目），两个版本都取 ``EXPLAIN (ANALYZE, BUFFERS)``：

| 版本 | 内层节点 | 实测 |
|---|---|---|
| 原样（只有 ``subject_id``） | ``Index Only Scan using ix_entry_subject_voucher`` | **17.3 ms** |
| 补上 ``book_id`` | ``Index Scan using ix_entry_subject_ord`` (``loops=15000``) | **395.4 ms（慢 23 倍）** |

原因是**半连接的探测次数**，不是索引覆盖度：外层是「账套下全部凭证」，
内层子查询要被探测上万次。``ix_entry_subject_voucher`` 的第二列就是
``voucher_id``，探测时 Index Only，一次命中；而 ``ix_entry_subject_ord`` 的
第二列是 ``period_ord``，输出 ``voucher_id`` 不在其中，每次探测都要再比一次索引项。

**教训：判断「换个索引会不会更快」不能只看计划里出现的是哪条索引，
要看它在探测循环里被跑了多少次。** 本条索引当初就是为了让这个子查询
Index Only —— 它的存在是对的，别删，也别用上面那段已被实测推翻的说法去删。

结果集本身两种写法完全一致（一张凭证只属于一个账套），所以这纯粹是性能取舍。
实测数据与结论见 ``压测基线报告_2026-10-15.md``。

**第二条：``import_batch`` 只有主键。**
这张表**没有 ``__table_args__``**，线上只有 ``import_batch_pkey``。三条访问路径
全裸奔：

1. ``importer.py::purge_stale_batches``（审计 P2-02）：
   ``WHERE status = 'PENDING' AND created_at < ?``，可选再带 ``book_id``；
2. 导入历史列表（``api/vouchers.py``）：
   ``WHERE book_id = ? ORDER BY id DESC LIMIT 100``；
3. 按主键取单条（``db.get(ImportBatch, id)``）—— 主键已覆盖。

**为什么是这两条、列序为什么这样。**

* ``(book_id, id)``：``book_id`` 等值在前、``id`` 是排序键也在前，
  ``ORDER BY id DESC LIMIT 100`` 可以顺着索引取满 100 行就停。
* ``(status, created_at)``：**等值列在前、范围列在后**，这是 B-tree 的规矩 ——
  等值列把区间掐成一个点，范围列在这个点里排好序。反过来
  ``(created_at, status)`` 就只剩「扫时间再滤状态」，那张表单调增长
  （COMMITTED / DISCARDED 永不清理，是 ``importer.py`` 刻意的，为了留追溯），
  清理入口每次都要在整张表上找陈年 PENDING。

两条都很小：每行一个 int4/int8 指针级别的东西，且 import_batch 本身增长很慢。

**幂等。** 三条都是 ``IF NOT EXISTS``，重跑不报错也不重建。ORM 里
``models/accounting.py`` / ``models/importing.py`` 已声明同名同列的
``Index``，所以 autogenerate 不会生成 DROP/CREATE 来「修」差异。

**没有重放 db_init.sql。** 0005 立的规矩是「改了它就得让存量库重放」，
本条**没有改那份文件**，所以不重放。
"""
from __future__ import annotations

from alembic import op

revision = "0011_subject_and_batch_indexes"
down_revision = "0010_subject_year_code_subtree"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_entry_subject_voucher", "entry", ["subject_id", "voucher_id"],
        unique=False, if_not_exists=True,
    )
    op.create_index(
        "ix_import_batch_book_id", "import_batch", ["book_id", "id"],
        unique=False, if_not_exists=True,
    )
    op.create_index(
        "ix_import_batch_status_created_at", "import_batch", ["status", "created_at"],
        unique=False, if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_import_batch_status_created_at", table_name="import_batch",
                  if_exists=True)
    op.drop_index("ix_import_batch_book_id", table_name="import_batch", if_exists=True)
    op.drop_index("ix_entry_subject_voucher", table_name="entry", if_exists=True)