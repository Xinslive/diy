"""给日志类表补「判龄列」索引。

Revision ID: 0037_log_retention_indexes
Revises: 0036_invoice_fallback_subject
Create Date: 2026-10-25

**这一条改的是什么。**

只加索引，不改数据、不改列、不改约束。

``app/maintenance.py`` 负责按保留期删日志行，而删除的判据是**时间列**
（``created_at`` / ``finished_at``）。这几张表原本的索引都**带不了这个判据**：

* ``audit_log`` 只有 ``ix_audit_book_time (book_id, created_at)`` ——
  清理是「``created_at`` < 某时刻」，不带 ``book_id``，用不上这个索引，
  于是每次清理都是一次**全表扫**。
* ``sys_backup_run`` 只有 ``ix_backup_run_scope_tier_time (scope, book_id, tier, created_at)`` ——
  同理，前缀对不上，也是全表扫。

而 ``audit_log`` 是这套系统里**增长最快**的一张表（一笔一行的操作全进它）。
「每天清一次」配上「每次全表扫」，就是「清理本身变成了负载」—— 那正是「加了
自动清理反而更慢」这种最劝退的结局。

所以补的索引就是给清理用的那一列：

* ``audit_log (created_at)``
* ``sys_backup_run (created_at)``
* ``admin_mfa_challenge (created_at)``
* ``plugin_install_attempt (created_at)``

**为什么``sys_scheduled_task_run`` 不在其中**：它已经有
``ix_scheduled_task_run_finished (finished_at)``（0028 建表时就加了），
而清理正是按 ``finished_at`` 判的 —— 那一处早就是对的。

**为什么不用「先删后建」**：``CREATE INDEX`` 在 PostgreSQL 里不阻塞并发写
（读会等一会儿），而这套表上可能有正在进行的备份与审计写入。加索引是安全的那一侧。

**降级**：删掉索引。数据一行不动 —— 少了索引只是清理变慢（全表扫），
不会让任何记录凭空消失。
"""
from __future__ import annotations

from alembic import op

revision = "0037_log_retention_indexes"
down_revision = "0036_invoice_fallback_subject"
branch_labels = None
depends_on = None

#: (表, 索引名, 列)
INDEXES = (
    ("audit_log", "ix_audit_created_at", "created_at"),
    ("sys_backup_run", "ix_backup_run_created_at", "created_at"),
    ("admin_mfa_challenge", "ix_admin_mfa_challenge_created_at", "created_at"),
    ("plugin_install_attempt", "ix_plugin_install_attempt_created_at", "created_at"),
)


def upgrade() -> None:
    for table, index, column in INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({column})")


def downgrade() -> None:
    for table, index, column in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index}")
