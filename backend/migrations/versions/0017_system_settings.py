"""系统级键值配置：第三方服务凭据从 .env 搬进库。

原先 PaddleOCR 的 token 只能改 ``.env`` 再重启后端。凭据是会过期、要轮换的
东西，而「改配置要重启、要 ssh、要有��门人」的结果只有一个 —— 没人改，于是
扫描件这条路某天悄悄失效，而配置「明明配过」。

现在管理员在「系统管理 → 外部服务」里改完即生效，并且留下一条「谁改的」。

Revision ID: 0017_system_settings
Revises: 0016_invoice_import
Create Date: 2026-10-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0017_system_settings"
down_revision = "0016_invoice_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sys_setting",
        sa.Column("key", sa.String(length=64), primary_key=True),
        # Text 而不是 String(N)：token 与可选载荷 JSON 都没有可靠的上界，
        # 猜一个长度截断会在「配置看着存进去了、实际调不通」时把人引向错误方向。
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        # 谁最后改的：凭据外泄时要查的不只是「现在配的是谁的」。
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("sys_setting")
