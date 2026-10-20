"""重放 db_init.sql：把「本年」的库层口径改成账套会计年度

Revision ID: 0031_subject_guard_year
Revises: 0030_admin_email_mfa
Create Date: 2026-10-20

**这一条改的是什么。**

``fn_subject_guard``（库层的「科目不可变」护栏）里判断「本年度已有发生额」时，
用的是 ``p.year = EXTRACT(YEAR FROM CURRENT_DATE)::int`` —— **服务器日历年**，
而且受**连接会话时区**影响（本项目的连接池把时区钉死成 UTC）。

而应用层同一件事（``app/accounting/periods.py::current_accounting_year``，
审查报告 P2-5）用的是**账套的会计年度** = 最近一期有凭证的期间所在的年度，
一张凭证都没有时退回 ``book.start_year``。

**两层口径不同，后果是「用错年份拒绝一次合法的操作」**：给一本 2026 年建的账
补录 2025 年的分录时，应用层判的是 2026（账套在记的那一年），库层判的是服务器
今年 —— 同一笔分录，一层说「可以改编码」、另一层说「本年度已有发生额」。
用户看到的是那条**库层**的拒绝，而文案里的「本年度」指的是另一个年，
于是完全看不懂。

**为什么不新建一条迁移去 ``CREATE OR REPLACE FUNCTION``。**

那样做的话，函数体要在「迁移文件」与「db_init.sql」里**各存一份** ——
而「整份文件被复制成两份、分叉之后开始漂移」正是这轮审查反复点名的病根
（工程域 P3-2：本项目里 ``db_init.sql`` 曾经被复制过两遍）。

这里改用**重放整份文件**的方式交付，与迁移 ``0005_db_guardrails`` **同一个
位置、同一份文件、同一段执行代码**（``0005`` 就是 ``cur.execute(SQL_FILE.
read_text())``）。``db_init.sql`` 本身是**幂等**的（触发器与约束都用
``CREATE OR REPLACE`` / ``DROP ... IF EXISTS``），重放它只会把护栏刷新到
当前文件的内容，不会破坏任何已存在的数据。

**降级。** 护栏本身**不撤销** —— 它与 ``0001`` 装上去的是同一批，拆掉一半
会得到一个既不是旧版本、也不是新版本的库。而且这一条**没有可回退的语义**：
旧口径（服务器日历年）不是「旧版本」，它是**错的那一个** —— 回退等于把已知
错误装回去。

**revision id 的长度。** ``alembic_version.version_num`` 是 ``varchar(32)``，
而 alembic 会执行 ``UPDATE alembic_version SET version_num='<本 id>'`` ——
所以 **revision id 必须短于 32 个字符**，否则升级在最后一步失败（而那时
所有 DDL 都已执行完，事务回滚后又要重来一遍）。本 id 是 23 个字符。
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0031_subject_guard_year"
down_revision = "0030_admin_email_mfa"
branch_labels = None
depends_on = None

# 与 0001 / 0005 同一个位置、同一份文件。迁移读它而不是复制一份 ——
# 复制就会分叉，而分叉正是 P1-9/P2-33 的病根。
SQL_FILE = Path(__file__).resolve().parents[2] / "app" / "core" / "db_init.sql"


def upgrade() -> None:
    # db_init.sql 是一整段 psql 风格的 DDL（大量分号与 $$ 引用），走 SQLAlchemy 的
    # 参数化通道会被 psycopg 当占位符解析，所以用裸 DBAPI 游标整段执行。
    # 和 0001 / 0005 用的是同一条路径 —— 这正是「两条路径不留分叉」的含义。
    raw = op.get_bind().connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    # **不撤销**，理由见模块 docstring：护栏与 0001/0005 装上去的是同一批，
    # 拆掉一半会得到一个既不是旧版本、也不是新版本的库；而旧口径本身就是
    # 错的那一个，回退等于把已知错误装回去。
    pass
