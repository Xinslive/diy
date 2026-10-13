"""把「写进 db_init.sql 的护栏」真正交付给存量库（审计 P1-9 / P2-33）

Revision ID: 0005_db_guardrails
Revises: 0004_token_version
Create Date: 2026-10-13

**这条 revision 存在的理由，是一条流程性缺陷而不是某个 bug。**

``app/core/db_init.sql`` 里全部的会计护栏（借贷平衡、结账禁写、已记账锁定、
科目不可变、is_leaf 自洽……）只有**一个**读者：
``migrations/versions/0001_baseline.py``，而它只在第 0 版执行一次。
于是往 ``db_init.sql`` 里加一行触发器，对**任何已经跑过 0001 的库**
—— 生产库、开发库、以及被 ``conftest.py`` 重建的测试库 —— 都不会产生任何效果：
``alembic upgrade head`` 是个空操作，那条触发器永远不会出现。

``cd1cded``（P1-01 把 ``is_leaf`` 改成服务端派生）正是踩进了这个坑：
它在 ``db_init.sql`` 里加了 ``fn_subject_leaf_guard``，没有配套 revision，
于是「已修复 P1-01」这件事在生产库上从未生效。而 CI 全绿，因为
``conftest.py`` 每次都在**全新库**上跑 ``upgrade head``，护栏天天齐全；
唯一断言触发器存在性的 ``test_schema_version.py`` 里的清单又恰好漏了它。

更糟的是 ``ARCHITECTURE.md`` §11 明确教人「需要护栏就在 db_init.sql 加触发器」，
而文档 §9.6 无条件承诺库级兜底。照着做的人永远不会发现自己的护栏没上线。

**所以这条 revision 的做法是：把整份 db_init.sql 在存量库上重放一次。**

不是手抄触发器定义 —— 0001 的 docstring 已经把道理写清楚了：
「手抄进 revision 会和那份 SQL 分叉，而分叉之后没人知道触发器到底以哪一份为准。」
手抄一份就等于造出第二个事实来源，下次改 db_init.sql 时照旧会被漏掉，
**同一个 bug 再犯一次**。唯一能把这条路径钉死的办法，是让「改 db_init.sql」
和「存量库拿到新护栏」变成同一个动作。

**代价与边界，说清楚：**

* ``db_init.sql`` 必须保持幂等（这是它本来就有的约定，每段都写着「本段幂等」）。
  本条 revision 会执行它里面**全部**的幂等语句，包括 ``UPDATE book SET ...``
  这类补数据语句。对存量库而言它们都是空转，但**将来**有人往 db_init.sql 里加一条
  不幂等的语句，就会被这条 revision 放大成「升级时全库跑一遍」。
  这是接受的代价：另一条路（手抄）已经在生产上漏过一次了。
* ``ADD CONSTRAINT ... FOREIGN KEY`` 会校验全表并持有 ACCESS EXCLUSIVE。
  大账套上跑这条要安排维护窗口。这是「把库层护栏真的装上去」的固有成本，
  不是本条 revision 特有的问题 —— 只是过去它被「触发器根本没上线」掩盖了。
* **降级是有损的。** downgrade 只回滚本条**新增**的约束，触发器无法「撤销」
  （它们和 0001 装上去的其余触发器同源）。真正该做的是回到代码对应的版本。

**同时在这一版交付的（都属于「写进 db_init.sql 就该生效」这一个动作）：**

* P2-04 叶子护栏扩到 ``BEFORE INSERT OR UPDATE`` 与两个方向。
  「插一个子科目、却完全不碰父节点」这条路径由
  ``0007_subject_leaf_parent_guard`` 补上（``DEFERRABLE INITIALLY DEFERRED``
  的约束触发器）—— **注意那条护栏是这条 revision 之后才写进 db_init.sql 的，
  所以本条不会把它送到已 stamp 的库上，必须由后一条 revision 送**；
  理由与取舍见 0007 的 docstring 与 db_init.sql 第 5b 段。
* P1-2 三处结账守卫改用 ``SELECT ... FOR SHARE``，堵住 TOCTOU 窗口；
* P2-05 分录冗余列的同步触发器扩到任意 UPDATE，凭证回填触发器补上 book_id；
* P2-08 ``entry.period_ord`` 收敛到 integer（与来源 ``period.ord`` 一致）；
* P2-09 ``updated_at`` 在触发器改写行时也刷新；
* P2-10 业务默认值补上 ``server_default``；
* P1-10 ``period_opening.subject_id`` 的外键由库收敛成 named RESTRICT
  （模型侧已同步，见 ``models/book.py``）；
* P2-05 ``entry`` 冗余列的组合外键、``period(book_id, ord)`` 唯一键；
* P1-5 根治项：``print_template.book_id``（模板归属账套，NULL = 全局兜底）；
* P2-12 根治项：``sys_user_book.role`` 的 CHECK 约束。
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0005_db_guardrails"
down_revision = "0004_token_version"
branch_labels = None
depends_on = None

# 与 0001 同一个位置、同一份文件。迁移读它而不是复制一份 —— 复制就会分叉，
# 而分叉正是 P1-9/P2-33 的病根。
SQL_FILE = Path(__file__).resolve().parents[2] / "app" / "core" / "db_init.sql"


def upgrade() -> None:
    # db_init.sql 是一整段 psql 风格的 DDL（大量分号与 $$ 引用），走 SQLAlchemy 的
    # 参数化通道会被 psycopg 当占位符解析，所以用裸 DBAPI 游标整段执行。
    # 和 0001 用的是同一条路径 —— 这正是「两条路径不留分叉」的含义。
    raw = op.get_bind().connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    conn = op.get_bind()
    # 触发器与 db_init.sql 里其余的约束**不撤销**：它们和 0001 装上去的是同一批，
    # 拆掉一半会得到一个既不是旧版本、也不是新版本的库。
    # 这里只回滚本条**新增**的东西（0001 的 downgrade 也会连带清理）。
    conn.exec_driver_sql(
        "ALTER TABLE sys_user_book DROP CONSTRAINT IF EXISTS ck_sys_user_book_role")
    conn.exec_driver_sql("DROP INDEX IF EXISTS ix_print_template_book_scope")
    conn.exec_driver_sql("ALTER TABLE print_template DROP COLUMN IF EXISTS book_id")
