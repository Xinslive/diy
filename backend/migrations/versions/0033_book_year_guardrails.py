"""重放 db_init.sql：把「年度一致性」三条护栏装到存量库上。

Revision ID: 0033_book_year_guardrails
Revises: 0032_book_year
Create Date: 2026-10-21

**这一条改的是什么。**

迁移 0032 把科目表下沉到年度，于是「这条分录的科目与它所在的期间是不是
同一个年度」成了新的不变量。应用层会拦（凭证校验按期间年度取科目），
但**库层也要拦** —— 铁律 3：护栏写在数据库触发器里，不写在应用层。
应用层会被 psql、脚本、未来的运维命令绕过去，数据库不会。

三条护栏（函数体在 ``app/core/db_init.sql``，这里是**唯一**的事实来源）：

* ``fn_entry_subject_year_guard`` —— 分录的科目必须与所在期间同年度。
  不对上，钱就落在错误的那一年，而借贷仍然平衡，系统自检查不出来。
* ``fn_opening_subject_year_guard`` —— 期初快照的科目必须与所在期间同年度。
  不对上，「下一年 1 月的期初 = 上一年 12 月的期末」这条不变量在数据层
  就对不上，报表的年初数与总账对不齐。
* ``fn_subject_parent_year_guard`` —— 科目的父节点必须同年度。
  不对上，科目树跨了两张表，级次与上卷全乱。

**为什么不新建一条迁移去 ``CREATE OR REPLACE FUNCTION``。**

那样做的话，函数体要在「迁移文件」与「db_init.sql」里**各存一份** ——
而「整份文件被复制成两份、分叉之后开始漂移」正是这轮审查反复点名的病根
（工程域 P3-2：本项目里 ``db_init.sql`` 曾经被复制过两遍）。

这里改用**重放整份文件**的方式交付，与迁移 ``0005_db_guardrails``、
``0010``、``0031`` **同一个位置、同一份文件、同一段执行代码**。
``db_init.sql`` 本身是**幂等**的（触发器与约束都用
``CREATE OR REPLACE`` / ``DROP ... IF EXISTS``），重放它只会把护栏刷新到
当前文件的内容，不会破坏任何已存在的数据。

那三条触发器在 ``db_init.sql`` 里是**条件创建**的（列 ``subject.year``
在才装）—— 0032 之前的那些重放会跳过它们，0032 之后的重放（含本条）
会装上。**顺序不会漏**：装上去的时机由「列在不在」决定，不由 revision 号决定。

**降级。** 护栏本身**不撤销** —— 它与 ``0001`` 装上去的是同一批，拆掉一半
会得到一个既不是旧版本、也不是新版本的库。而且这一条**没有可回退的语义**：
旧口径（科目表是账套级的）不是「旧版本」，它是**错的那一个** ——
回退等于把已知缺陷装回去。
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0033_book_year_guardrails"
down_revision = "0032_book_year"
branch_labels = None
depends_on = None

# 与 0001 / 0005 / 0010 / 0031 同一个位置、同一份文件。迁移读它而不是复制一份 ——
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
