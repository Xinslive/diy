"""资产负债表改成会小企01表的双栏版式（8 列）

Revision ID: 0012_balance_sheet_dual_sided
Revises: 0011_subject_and_batch_indexes
Create Date: 2026-10-14

**要改的现象。** 资产负债表原来是一张竖排的单栏表：3 列（项目 / 期末余额 /
年初余额）、39 行，资产从上往下排完再排负债和权益。这张表算得没错，但**不是
会计上要交的那张表** —— 财务要的是《小企业会计准则》印成左右两栏的
「会小企01表」：左栏资产、右栏负债和所有者权益，每栏各有「行次 / 期末余额 /
年初余额」，8 列并排，行次 1~53。

**为什么不靠改 db_init.sql。** 0005 立的规矩是「改了它就得让存量库重放」。
本条**没有动那份文件**：报表模板的内容在
``app/seed/builtin_reports.py`` 里，它随代码走，不需要 DDL 迁移。
存量库的报表模板是**数据**，不是 schema。

**为什么必须重跑 install()。** ``install()`` 全仓只有 0001_baseline 调过一次，
不在应用启动路径上。也就是说：**改了 BALANCE_ROWS，已有的库还是老的 39 行。**
测试库每次是全新 upgrade 到 head，所以测试永远跑的是新布局 ——
它抓不到这个「存量库没跟上」的问题，只能靠这条迁移把存量库补上。

**只补资产负债表。** ``install(db, codes=...)`` 只重播指定模板。
利润表的形状一个字没动，让它跟这条迁移无关 —— 万一有人把利润表模板在设计
模式里改成了自己要的形状，不该被一次版式升级冲掉（``install()`` 对已存在的
模板是先删后建，不重播就是保留用户的改动）。

**「先删后建」是有代价的。** 如果有人在设计模式里改过这张**内置**资产负债表
（改项目名、改公式、插行删列），本条会把它重置成标准版式。这是刻意的取舍：
内置模板跟着版本走，用户要的是标准表；真要自定义请用「另存为新模板」。
"""
from __future__ import annotations

from alembic import op

revision = "0012_balance_sheet_dual_sided"
down_revision = "0011_subject_and_batch_indexes"
branch_labels = None
depends_on = None

#: 只重播这一张。利润表不动。
REPLAY_TEMPLATES = ("BALANCE_SHEET",)


def upgrade() -> None:
    from app.core.db import SessionLocal
    from app.seed.builtin_reports import install

    db = SessionLocal(bind=op.get_bind())
    try:
        install(db, codes=REPLAY_TEMPLATES)
        db.commit()
    finally:
        db.close()


def downgrade() -> None:
    """没有「退回旧版式」的迁移。

    旧版式（3 列 / 39 行）在 git 里，需要退回就 checkout 当时的代码再重跑
    ``install``。真在这里写一段把行删回去的代码，等于把一个 2026-10 之前的
    布局当成需要长期维护的正式版本 —— 它不是。
    """
    raise NotImplementedError(
        "资产负债表版式回退请用 git 检出 0012 之前的 builtin_reports.py 再重跑 install()"
    )