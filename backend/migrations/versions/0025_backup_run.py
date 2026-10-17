"""数据安全：备份运行记录 sys_backup_run（2026-10）

Revision ID: 0025_backup_run
Revises: 0024_paper_a4_only
Create Date: 2026-10-17

为什么要有这张表
----------------
本系统的备份此前只有两样东西：``scripts/db_backup.py`` 与它那份 systemd
timer。两样都**不回答**「备份到底成功了没有」—— timer 永远显示 active，
而 dump 成功与否只写进 ``/opt/cwq/backups/backup.log``，那个文件没人翻。

搬到 OSS 之后这个盲区更糟：产物写在一台**别的机器**上，两个失败点
（本地生成失败、上传失败）都不在本地日志的视野里，而页面照旧显示一切正常。
于是「有备份」退化成一句没有人验证过的话。

这张表就是那句验证。一行 = 一次尝试，**失败也留痕**，字段各答一个问题：

* ``status`` / ``message``        —— 这次成没成，失败原因全文（不是摘要）
* ``oss_key``                     —— 产物在哪；失败时为空串
* ``size_bytes`` / ``row_count``  —— 占了多少钱 / 到底备份到了东西没有
* ``checksum``                    —— 产物的 SHA256；OSS 不会告诉你它悄悄损坏了什么
* ``scope`` + ``book_id`` + ``tier`` —— 分级判重（这个账套这月/这周成功过没有）

两个刻意的取舍
--------------
**book_id 不建外键。** 账套可以被删，而备份记录必须活过它的账套 —— 出了事
正要靠这条记录找回数据，那时账套没了是常态。建了外键，删账套就会连带删掉
「那次备份是在删之前做的」这唯一的时间证据，那才是把退路砍掉。
CASCADE 在这里是反的。

**``tier`` 加 CHECK 白名单。** 保留策略是按 tier 前缀删对象的
（``retention.py`` 只认 daily/weekly/monthly 三个目录）。一个手滑写进去的
``"Monthly"``／``"month"`` 会让清理**静默跳过**这个前缀 —— 于是「周备份保存
一年」悄悄变成「永久保留」，白烧存储费；而反过来一个拼错的 tier 又可能被
当成月度从而**永不删除**。白名单把这个失败模式堵在写入口。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025_backup_run"
down_revision = "0024_paper_a4_only"
branch_labels = None
depends_on = None


def upgrade():
    # **必须可重入。** 本仓库的迁移一律写成幂等（``IF NOT EXISTS`` / 先问再动），
    # 不是洁癖：``tests/test_migration_guardrails.py`` 会把一个**已经升到 head**
    # 的 schema 打回 0004 再重放整条链 —— 那条用例存在的理由是
    # 「护栏能不能到达存量库」，而它重放时表已经在了。这里写裸 CREATE TABLE
    # 就是 DuplicateTable，那条钉子当场变红，而且红得与它想测的东西毫无关系。
    # 0023 / 0024 用 `op.execute` 而不是 `op.create_table`，为的是同一件事。
    op.execute("""
        CREATE TABLE IF NOT EXISTS sys_backup_run (
            id SERIAL NOT NULL,
            -- 故意无外键 —— 见文件头「两个刻意的取舍」。
            book_id INTEGER,
            scope VARCHAR(16) NOT NULL DEFAULT 'BOOK',
            book_label VARCHAR(256) NOT NULL DEFAULT '',
            tier VARCHAR(16) NOT NULL,
            status VARCHAR(16) NOT NULL,
            -- 失败时为空串：失败不该留下一个指向对象的键，
            -- 否则「有键就说明有对象」这条推断会骗人。
            oss_key VARCHAR(512) NOT NULL DEFAULT '',
            size_bytes BIGINT NOT NULL DEFAULT 0,
            row_count INTEGER NOT NULL DEFAULT 0,
            checksum VARCHAR(64) NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT sys_backup_run_pkey PRIMARY KEY (id),
            -- 约束名与 models/backup.py 的声明**逐字一致**：两边分成两套名字的话，
            -- 下次 autogenerate 会把一次改名报成「库里有、模型没有」+「模型有、
            -- 库里没有」两条变更，把一件事伪装成两件。
            CONSTRAINT ck_sys_backup_run_scope CHECK (scope IN ('BOOK', 'SYSTEM')),
            CONSTRAINT ck_sys_backup_run_tier CHECK (tier IN ('daily', 'weekly', 'monthly')),
            CONSTRAINT ck_sys_backup_run_status CHECK (status IN ('success', 'failed'))
        )
    """)
    # 与 models 里那份声明同名同义。分级判重每次备份都要问
    # 「某账套某 tier 成功过没有」，没有它就是每次全表扫。
    op.execute("CREATE INDEX IF NOT EXISTS ix_backup_run_scope_tier_time "
               "ON sys_backup_run (scope, book_id, tier, created_at)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_backup_run_scope_tier_time")
    op.execute("DROP TABLE IF EXISTS sys_backup_run")
