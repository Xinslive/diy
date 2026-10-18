"""账套级科目映射配置 + 全局表页关键词映射。"""
from alembic import op
import sqlalchemy as sa

revision = "0001_config"
down_revision = None

CONFIG_TABLE = "plugin_f869d8c6ad46_config"
SHEETMAP_TABLE = "plugin_f869d8c6ad46_sheetmap"


def upgrade():
    # 账套级：对方科目、待确认科目、关键词到科目的映射。宿主配置服务要求
    # id/book_id/key/value(JSON)/version 五列和 (book_id, key) 唯一约束。
    op.create_table(
        CONFIG_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("book.id"), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("book_id", "key", name="uq_plugin_f869d8c6ad46_config_book_key"),
    )
    # 全局：表页关键词到账套名称的映射，只有一行，属于安装级配置而不是账套数据。
    op.create_table(
        SHEETMAP_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("key", name="uq_plugin_f869d8c6ad46_sheetmap_key"),
    )


def downgrade():
    op.drop_table(SHEETMAP_TABLE)
    op.drop_table(CONFIG_TABLE)
