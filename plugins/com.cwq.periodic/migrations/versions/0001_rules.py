"""账套级周期规则 + 执行台账。

台账（run）是幂等的落脚点：一条规则在一个会计期间内最多生成一张凭证，靠
``(book_id, rule_id, period_ord)`` 唯一约束在数据库层钉死，而不是靠插件先查
后写 —— 后者在手动执行与结账钩子同时命中同一规则时会重复记账。
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_rules"
down_revision = None

RULE_TABLE = "plugin_769c8e79a72c_rule"
RUN_TABLE = "plugin_769c8e79a72c_run"


def upgrade():
    # 一条规则 = 一个账套里的一项每月固定支出（房租、管理费、社保…）。
    # 科目存**编码**而不是 subject_id：科目在不同账套 id 不同，规则要能按编码
    # 在各账套各自解析，存 id 会把规则钉死在一本账上。
    op.create_table(
        RULE_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("book.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("day_of_month", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("amount", sa.String(32), nullable=False, server_default="0"),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("remark", sa.String(120), nullable=False, server_default=""),
        sa.Column("attachments", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # 起止用 YYYY-MM 字面量而不是 Period.ord：插件读不到宿主的 period 表，
        # 拿不到可靠的序号；而年月是用户自己填、宿主凭证校验必然接受的月份。
        # 零填充定长，字典序即时间序。
        sa.Column("start_label", sa.String(7), nullable=False, server_default=""),
        sa.Column("end_label", sa.String(7), nullable=False, server_default=""),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("sort_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("book_id", "name", name="uq_plugin_769c8e79a72c_rule_book_name"),
        # 台账要用 (book_id, rule_id) 复合外键指向规则，因此这里先有同形状的
        # 唯一键。宿主要求账套表之间的外键必须同时包含 book_id，否则 A 账套的
        # 台账就能引用 B 账套的规则。
        sa.UniqueConstraint("book_id", "id", name="uq_plugin_769c8e79a72c_rule_book_id"),
    )
    op.create_index("ix_plugin_769c8e79a72c_rule_book", RULE_TABLE, ["book_id"])

    # 执行台账：每次生成一张凭证就落一行，作为「这个月已经记过」的凭据。
    # 凭证被用户删掉后台账仍在，所以页面显式提供「撤销记账记录」让它重新生成，
    # 而不是悄悄自动重记 —— 自动重记会在用户手动删错凭证后把账又填回去。
    op.create_table(
        RUN_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("book_id", sa.Integer(), sa.ForeignKey("book.id"), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("period_year", sa.Integer(), nullable=False),
        sa.Column("period_month", sa.Integer(), nullable=False),
        sa.Column("voucher_id", sa.Integer(), nullable=True),
        sa.Column("voucher_no", sa.String(32), nullable=False, server_default=""),
        sa.Column("voucher_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("amount", sa.String(32), nullable=False, server_default="0"),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("remark", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # 幂等的落脚点：一条规则一个月份最多一行。手动执行与定时任务同时命中
        # 同一规则同一月份时，第二条插入在这里被数据库挡住，不会生成第二张凭证。
        sa.UniqueConstraint("book_id", "rule_id", "period_year", "period_month",
                            name="uq_plugin_769c8e79a72c_run_period"),
        # 复合外键带 book_id：台账只能引用**同一个账套**里的规则。
        sa.ForeignKeyConstraint(["book_id", "rule_id"],
                                [RULE_TABLE + ".book_id", RULE_TABLE + ".id"],
                                name="fk_plugin_769c8e79a72c_run_rule", ondelete="CASCADE"),
    )
    op.create_index("ix_plugin_769c8e79a72c_run_book_period", RUN_TABLE, ["book_id", "period_year", "period_month"])


def downgrade():
    op.drop_table(RUN_TABLE)
    op.drop_table(RULE_TABLE)
