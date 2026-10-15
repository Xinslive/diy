"""发票导入、OCR 结果留存与自动凭证生成。

Revision ID: 0016_invoice_import
Revises: 0015_book_data_version
Create Date: 2026-10-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016_invoice_import"
down_revision = "0015_book_data_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE voucher DROP CONSTRAINT IF EXISTS ck_voucher_source")
    op.execute(
        "ALTER TABLE voucher ADD CONSTRAINT ck_voucher_source "
        "CHECK (source IN ('MANUAL','IMPORT','INVOICE','CARRY_FORWARD'))"
    )
    op.create_table(
        "invoice_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("taxpayer_id", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("input_tax_subject_id", sa.Integer(), nullable=True),
        sa.Column("output_tax_subject_id", sa.Integer(), nullable=True),
        sa.Column("input_credit_subject_id", sa.Integer(), nullable=True),
        sa.Column("output_debit_subject_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["book.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["input_tax_subject_id"], ["subject.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["output_tax_subject_id"], ["subject.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["input_credit_subject_id"], ["subject.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["output_debit_subject_id"], ["subject.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("book_id", name="uq_invoice_config_book"),
        if_not_exists=True,
    )

    op.create_table(
        "invoice_rule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("match_type", sa.String(length=16), nullable=False),
        sa.Column("invoice_type", sa.String(length=8), nullable=False, server_default="BOTH"),
        sa.Column("keyword", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["book.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["subject.id"], ondelete="RESTRICT"),
        if_not_exists=True,
    )
    op.create_index("ix_invoice_rule_book_active", "invoice_rule", ["book_id", "active", "priority"], if_not_exists=True)
    op.create_index("ix_invoice_rule_book_type", "invoice_rule", ["book_id", "match_type", "invoice_type"], if_not_exists=True)

    op.create_table(
        "invoice_batch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("file_path", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ready_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ocr_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("committed_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["book.id"], ondelete="CASCADE"),
        if_not_exists=True,
    )
    op.create_index("ix_invoice_batch_book_id", "invoice_batch", ["book_id", "id"], if_not_exists=True)
    op.create_index("ix_invoice_batch_status_created_at", "invoice_batch", ["status", "created_at"], if_not_exists=True)

    op.create_table(
        "invoice_record",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("book_id", sa.Integer(), nullable=False),
        sa.Column("page_index", sa.Integer(), nullable=False),
        sa.Column("invoice_no", sa.String(length=64), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("invoice_type", sa.String(length=8), nullable=False, server_default="UNKNOWN"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("parse_warning", sa.Text(), nullable=False, server_default=""),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("voucher_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["invoice_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["book_id"], ["book.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["voucher_id"], ["voucher.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("book_id", "invoice_no", name="uq_invoice_record_book_no"),
        if_not_exists=True,
    )
    op.create_index("ix_invoice_record_batch_id", "invoice_record", ["batch_id", "page_index"], if_not_exists=True)
    op.create_index("ix_invoice_record_voucher_id", "invoice_record", ["voucher_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_invoice_record_voucher_id", table_name="invoice_record")
    op.drop_index("ix_invoice_record_batch_id", table_name="invoice_record")
    op.drop_table("invoice_record")
    op.drop_index("ix_invoice_batch_status_created_at", table_name="invoice_batch")
    op.drop_index("ix_invoice_batch_book_id", table_name="invoice_batch")
    op.drop_table("invoice_batch")
    op.drop_index("ix_invoice_rule_book_type", table_name="invoice_rule")
    op.drop_index("ix_invoice_rule_book_active", table_name="invoice_rule")
    op.drop_table("invoice_rule")
    op.drop_table("invoice_config")
    op.execute("ALTER TABLE voucher DROP CONSTRAINT IF EXISTS ck_voucher_source")
    op.execute(
        "ALTER TABLE voucher ADD CONSTRAINT ck_voucher_source "
        "CHECK (source IN ('MANUAL','IMPORT','CARRY_FORWARD'))"
    )
