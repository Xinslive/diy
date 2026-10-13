"""把「插子科目却不动父节点」这条叶子护栏交付给存量库（审计 P2-04 的另一半）

Revision ID: 0007_subject_leaf_parent_guard
Revises: 0006_hot_path_indexes
Create Date: 2026-10-13

**为什么 0005 之后还要单开一条。**

``0005_db_guardrails`` 已经把整份 ``db_init.sql`` 在存量库上重放过一次，
但那是**在 0005 落地之前**发生的。``trg_subject_leaf_parent_guard`` 是
``0005`` 上线之后才写进 ``db_init.sql`` 的 —— 凡是当时已经 stamp 到
``0005``/``0006`` 的库，``alembic upgrade head`` 都不会再跑一遍 0005，
于是这条触发器和当初那个 ``trg_subject_leaf_guard`` 是**同一个命运**：
写在 ``db_init.sql`` 里、对任何真实库都不生效、而 CI 全绿。

这正是 0005 的 docstring 里写的那句话本身：「唯一能把这条路径钉死的办法，
是让『改 db_init.sql』和『存量库拿到新护栏』变成同一个动作。」新增护栏时，
那条 revision 必须是**加在这次改动之后的**，否则同一个 bug 再犯一次。

**做法仍然是重放 db_init.sql，不手抄这一条触发器。**

理由与 0005 完全相同，而且这里更没有例外：0005 的 docstring 已经把道理
写清楚了 —— 手抄进 revision 会和那份 SQL 分叉，而分叉之后没人知道触发器
到底以哪一份为准；手抄一份就等于造出第二个事实来源，下次改
``db_init.sql`` 时照旧会被漏掉。**同一个 bug 再犯一次。**

代价是明说的：这条 revision 在一个已经跑过 0005 的库上，会把整份
``db_init.sql`` 的幂等语句再跑一遍（对已经收敛的库全是空转）。这是接受的
代价 —— 另一条路（手抄）已经在生产上漏过一次了，而且漏掉的方式恰恰是
「没人发现」。

**这次改动之前，本工作线先把既有的构造修成了自洽的。**

上一批把这条触发器写出来又测通过、却没有上线，理由是「会让 4 条既有用例
变红」：那些夹具「插子科目、不改父节点」。其中
``tests/test_ledger_openings_rollup.py`` 的 ``_add_child`` 自己的 docstring
写着「走真实的科目接口（父级会自动变成非末级）」，而代码是直接
``db.add(child)`` —— **文档与代码自相矛盾**。夹具已经改成与它们宣称的一致
（显式把父节点改成非末级，docstring 照实改写为「构造底层状态」）。

**取舍结论：护栏上线。** 理由见 db_init.sql 第 5b 段。应用层
（``api/subjects.py``）本来就闭合了这条路径，所以这刀防的不是「远程漏洞」，
而是「绕开 API 直写 SQL / 脚本 / 迁移能制造出汇总科目被当成末级」这种
库里唯一还在敞着的那条缝 —— 而这条缝一旦敞开，汇总科目上的分录在明细账里
会凭空消失，且没有任何报错。

**降级是有损的，但这一条是干净的。** 与 0005 不同，这里可以精确回滚：
只 DROP 0007 引入的那条触发器与那个函数，其余护栏（与 0001/0005 同源）
一概不动 —— 拆掉一半会得到一个既不是旧版本、也不是新版本的库。
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0007_subject_leaf_parent_guard"
down_revision = "0006_hot_path_indexes"
branch_labels = None
depends_on = None

# 与 0001 / 0005 同一个位置、同一份文件。迁移读它而不是复制一份 ——
# 复制就会分叉，而分叉正是 P1-9 / P2-33 的病根。
SQL_FILE = Path(__file__).resolve().parents[2] / "app" / "core" / "db_init.sql"


def upgrade() -> None:
    # 与 0005 同一条路径：db_init.sql 是一整段 psql 风格的 DDL（大量分号与
    # $$ 引用），走 SQLAlchemy 的参数化通道会被 psycopg 当占位符解析，
    # 所以用裸 DBAPI 游标整段执行。
    raw = op.get_bind().connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    conn = op.get_bind()
    # 只回滚本条**新增**的那一件。其余触发器与约束不撤销：它们和 0001/0005
    # 装上去的是同一批，拆掉一半会得到一个既不是旧版本、也不是新版本的库。
    conn.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_subject_leaf_parent_guard ON subject")
    conn.exec_driver_sql("DROP FUNCTION IF EXISTS fn_subject_leaf_parent_guard()")