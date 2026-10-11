"""把存量库收敛到冻结 baseline 的结构（审计 P3-01 的残留物）

Revision ID: 0003_converge_legacy
Revises: 0002_login_lockout
Create Date: 2026-10-12

**为什么需要一条「让旧库长得和新建库一样」的 revision。**

baseline 是**冻结的显式 DDL**，不是 ``create_all``。而引入 Alembic 之前
所有库都是 ``create_all()`` 建的，而 ``create_all`` 只建缺失的表、
一眼都不看已存在的表。于是那些库的结构是「某个历史时刻的模型」的化石，
和 baseline 天然对不上。拿开发库与 baseline 建的库逐项对比，差异有两处：

1. ``period_opening.subject_id`` 的外键还是 ``ON DELETE CASCADE``。
   baseline 里是 RESTRICT —— 但 RESTRICT 是后来（P1-05）才加的，加在
   ``db_init.sql`` 末尾的 DO 块里。这个库自那次改动后没再走过启动重放，
   于是这个「改完了但没生效」的状态一直留在库里。CASCADE 会连带删掉
   期初余额，是审计点名的那类静默数据损失。

2. ``period.ord`` 是 ``smallint``，baseline 和模型都是 ``integer``。
   同理：模型改过类型，存量库没跟上。

这条 revision 不改业务语义，只把这两处差异抹平，让「迁移建出来的库」和
「历史上 ``create_all`` 建出来的库」在结构上真正一致 —— 否则前者永远测不到
后者的坑。

**幂等。** baseline 已经正确的库跑它不会有任何变化：外键按
（约束名 + 删除动作）判断，不是 RESTRICT 才重建；列类型已经是 integer
就跳过。重复执行结果一致。

**降级是有损的，且故意如此。** downgrade 把外键改回 CASCADE、把 ``ord``
收回 smallint —— 那是出问题之前的样子。真正该做的是回到代码对应的版本，
而不是 downgrade。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_converge_legacy"
down_revision = "0002_login_lockout"
branch_labels = None
depends_on = None

# 外键目标：与 app/core/db_init.sql 里 P1-05 那段 DO 块同名同动作，
# 改一处要记得改另一处 —— 两条路径留下的痕迹必须长得一模一样。
FK_NAME = "fk_period_opening_subject"
FK_OLD_NAME = "period_opening_subject_id_fkey"

# pg_constraint.confdeltype 里的小写字母，和 DDL 关键字不是一回事 ——
# ``ON DELETE r`` 是语法错误。分开写，别图省事共用一个常量。
RESTRICT_CODE = "r"      # confdeltype
CASCADE_CODE = "c"       # confdeltype
RESTRICT = "RESTRICT"    # DDL 关键字
CASCADE = "CASCADE"      # DDL 关键字

_FK_SQL = sa.text("""
    SELECT c.conname, c.confdeltype::text
      FROM pg_constraint c
      JOIN pg_attribute a
        ON a.attrelid = c.conrelid
       AND a.attnum = ANY (c.conkey)
       AND a.attname = :column
       AND a.attnum > 0
       AND NOT a.attisdropped
     WHERE c.conrelid = to_regclass(:table)
       AND c.contype = 'f'
""")

_TYPE_SQL = sa.text("""
    SELECT format_type(a.atttypid, a.atttypmod)
      FROM pg_attribute a
     WHERE a.attrelid = to_regclass(:table)
       AND a.attname = :column
       AND a.attnum > 0
       AND NOT a.attisdropped
""")


def _ident(name: str) -> str:
    """PG 标识符加引号。

    DDL 的对象名不能走参数绑定（``ALTER TABLE ... DROP CONSTRAINT $1``
    语法错误），只能自己加引号。名字来自 pg_catalog，不是外部输入。
    """
    return '"' + name.replace('"', '""') + '"'


def _current_fk(conn) -> tuple[str, str] | None:
    row = conn.execute(_FK_SQL, {"table": "period_opening", "column": "subject_id"}).first()
    return (row[0], row[1]) if row else None


def _current_type(conn) -> str | None:
    return conn.execute(_TYPE_SQL, {"table": "period", "column": "ord"}).scalar()


def _set_opening_fk(conn, name: str, action: str) -> None:
    cur = _current_fk(conn)
    if cur and cur[0] != name:
        conn.execute(sa.text(
            f"ALTER TABLE period_opening DROP CONSTRAINT {_ident(cur[0])}"))
    conn.execute(sa.text(
        f"ALTER TABLE period_opening ADD CONSTRAINT {_ident(name)} "
        f"FOREIGN KEY (subject_id) REFERENCES subject(id) ON DELETE {action}"))


def _fix_opening_fk(conn) -> None:
    """把 period_opening.subject_id 的外键收敛成 named RESTRICT。

    按「约束名 + 删除动作」判断，而不是「有没有外键」—— create_all 建的库
    里这条外键一直都在，只是名字和删除动作不对。这正是 P3-01 的病症：
    「在」不等于「对」。
    """
    if _current_fk(conn) == (FK_NAME, RESTRICT_CODE):
        return
    _set_opening_fk(conn, FK_NAME, RESTRICT)


def _fix_period_ord(conn) -> None:
    """period.ord 从 smallint 收敛到 integer。"""
    t = _current_type(conn)
    if t is None or t in ("integer", "int4"):
        return
    conn.execute(sa.text("ALTER TABLE period ALTER COLUMN ord TYPE integer"))


def upgrade() -> None:
    conn = op.get_bind()
    _fix_opening_fk(conn)
    _fix_period_ord(conn)


def downgrade() -> None:
    conn = op.get_bind()
    if _current_fk(conn) != (FK_OLD_NAME, CASCADE_CODE):
        _set_opening_fk(conn, FK_OLD_NAME, CASCADE)
    t = _current_type(conn)
    if t is not None and t not in ("smallint", "int2"):
        conn.execute(sa.text("ALTER TABLE period ALTER COLUMN ord TYPE smallint"))
