"""允许无本年发生额的科目修改编码。

Revision ID: 0009_subject_year_code_edit
Revises: 0008_login_ip_throttle
Create Date: 2026-10-13

重放 db_init.sql 更新存量数据库的科目保护触发器，与 0005 / 0007 保持同源。
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0009_subject_year_code_edit"
down_revision = "0008_login_ip_throttle"
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parents[2] / "app" / "core" / "db_init.sql"


def upgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    raw = op.get_bind().connection.driver_connection
    with raw.cursor() as cur:
        cur.execute("""
            CREATE OR REPLACE FUNCTION fn_subject_guard() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE'
                   AND (NEW.code IS DISTINCT FROM OLD.code
                     OR NEW.direction IS DISTINCT FROM OLD.direction
                     OR NEW.category IS DISTINCT FROM OLD.category) THEN
                    IF EXISTS (SELECT 1 FROM entry WHERE subject_id = OLD.id) THEN
                        RAISE EXCEPTION '科目 % 已被凭证使用，不能修改编码/类别/余额方向', OLD.code
                            USING ERRCODE = 'check_violation';
                    END IF;
                END IF;
                RETURN NEW;
            END; $$ LANGUAGE plpgsql;
        """)
