"""本年发生额的判据从「科目自己」扩到「整棵子树」。

Revision ID: 0010_subject_year_code_subtree
Revises: 0009_subject_year_code_edit
Create Date: 2026-10-13

**要修的现象。** 同一棵科目树上，末级 100201 挂了本年度分录 → 改编码被挡；
它的上级 1002 一条直接分录都没有（分录全在 100201 上）→ 放行。
于是「儿子改不了、爹能改」，而这两者在账上是同一笔钱：引擎把分录上卷到
各级父科目，1002 在总账 / 科目余额表 / 报表里的本年发生额就是 100201 的那些数。

**为什么必须一起堵上库层。** 应用层的 400 是给人看的报错，触发器才是
最后一道闸 —— 直接 UPDATE subject 的脚本、导入工具、未来的运维命令都绕得过
路由。0009 建立的触发器里是同一个 ``e.subject_id = OLD.id`` 判据，
只改应用层的话，库里那扇门照样开着。两者必须同口径。

重放 db_init.sql 更新存量数据库的科目保护触发器，与 0005 / 0007 / 0009 保持同源。
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0010_subject_year_code_subtree"
down_revision = "0009_subject_year_code_edit"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parents[2] / "app" / "core" / "db_init.sql"


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    """回到 0009 的形态：**只**按科目自己的分录判本年发生额。

    也就是把「儿子改不了、爹能改」那个 bug 原样还回去 —— 所以 downgrade
    不是「回到某个更正确的样子」，只是「回到上一个 revision 的样子」。
    """
    raw = op.get_bind().connection.driver_connection
    with raw.cursor() as cur:
        cur.execute("""
            CREATE OR REPLACE FUNCTION fn_subject_guard() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' AND NEW.code IS DISTINCT FROM OLD.code THEN
                    IF EXISTS (
                        SELECT 1
                        FROM entry e
                        JOIN period p ON p.book_id = e.book_id AND p.ord = e.period_ord
                        WHERE e.book_id = OLD.book_id
                          AND e.subject_id = OLD.id
                          AND e.amount <> 0
                          AND p.year = EXTRACT(YEAR FROM CURRENT_DATE)::int
                    ) THEN
                        RAISE EXCEPTION '科目 % 本年度已有发生额，不能修改编码', OLD.code
                            USING ERRCODE = 'check_violation';
                    END IF;
                END IF;
                IF TG_OP = 'UPDATE'
                   AND (NEW.direction IS DISTINCT FROM OLD.direction
                     OR NEW.category IS DISTINCT FROM OLD.category) THEN
                    IF EXISTS (SELECT 1 FROM entry WHERE subject_id = OLD.id) THEN
                        RAISE EXCEPTION '科目 % 已被凭证使用，不能修改类别/余额方向', OLD.code
                            USING ERRCODE = 'check_violation';
                    END IF;
                END IF;
                RETURN NEW;
            END; $$ LANGUAGE plpgsql;
        """)