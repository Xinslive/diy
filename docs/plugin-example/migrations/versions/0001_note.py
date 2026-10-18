from alembic import op
import sqlalchemy as sa

revision = "0001_note"
down_revision = None


def upgrade():
    op.create_table("plugin_4b8db6655ff5_note",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("book.id"), nullable=False),
        sa.Column("label", sa.String(120), nullable=False))


def downgrade():
    op.drop_table("plugin_4b8db6655ff5_note")
