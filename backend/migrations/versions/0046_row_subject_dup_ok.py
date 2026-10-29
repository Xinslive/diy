"""放开「同一个科目在一行流水里只能出现一次」的限制。

Revision ID: 0046_row_subject_dup_ok
Revises: 0045_bank_statement_review
Create Date: 2026-10-29

**这一条改的是什么。**

``bank_statement_row_subject`` 上的 ``UNIQUE (row_id, subject_id)``
（约束名 ``uq_bank_statement_row_subject``）被删掉。

**为什么。**

2026-10-29 用户批注：给流水指定科目时，若多个分录指向同一个科目，接口报
「「6602 业务招待费」在这一行里出现了两次 —— 要记两笔请把金额合并到一条上」，
**「不应该有这个限制」**。

三条理由：

1. **摘要是挂在这一层上的。** 迁移 0043/0044 把凭证摘要从「整张凭证一句」
   下沉到「每个对方科目各自一句」—— 就是为了让一行流水能拆到好几个科目上、
   各写各的摘要。既然摘要的粒度就是这一层，「同一个科目拆成两条」本来就是
   这套设计的用法。禁掉它，那次下沉就白做了一半。
2. **账上两条分录指向同一个科目，本来就是合法的。** 金额各算各的、摘要各写
   各的，凭证照样平。禁掉它，用户只能「先把两条合并成一条、再把其中一条的
   摘要丢掉」—— 那是**逼用户丢信息**。
3. **真正该拦的是「合计对不上」，那个闸还在。** 对方科目的金额合计必须
   等于流水金额（服务层 ``_parse_plan_payload`` 校验，表层有
   ``ck_bank_statement_row_subject_amount``）。放开重复**不会**放松这个。

**数据不会丢。** 这条迁移只删约束，不动任何一行数据 —— 存量库里本来就
不可能有重复的行（有约束在），删完之后它们照样合法。

**降级。** 把约束加回去。**注意**：如果降级时库里已经有同一行里同一个科目
出现多行的情况，加回去会失败（PostgreSQL 报 duplicate key）。所以降级
之前要先清理：

    DELETE FROM bank_statement_row_subject a
     USING bank_statement_row_subject b
      WHERE a.row_id = b.row_id AND a.subject_id = b.subject_id AND a.id > b.id;

（保留每组里 id 最小的那一条，其余删掉 —— 那些是拆分出来的重复项，
金额要并回留下的那一条上，所以**降级前要人工核对金额**。）

**revision id 只有 22 个字符。** ``alembic_version.version_num`` 是
``VARCHAR(32)``，超了会当场报 ``StringDataRightTruncation``：迁移跑到一半
失败，**库停在半路**（``0045`` 已 stamp、``0046`` 没上），而测试与发布都
以为迁移成功了。给新迁移起名时先数一下字符。
"""
from __future__ import annotations

from alembic import op

revision = "0046_row_subject_dup_ok"
down_revision = "0045_bank_statement_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # **IF EXISTS**：这条迁移必须**能重跑** ——
    # ``tests/test_migration_guardrails.py`` 里有一条用例把结构打回旧形态、
    # stamp 回旧版、再 upgrade 到 head，那条路径会让本段跑第二遍。
    op.execute("ALTER TABLE bank_statement_row_subject "
               "DROP CONSTRAINT IF EXISTS uq_bank_statement_row_subject")


def downgrade() -> None:
    # 加回去之前先把重复的清掉 —— 有重复时加约束会当场失败。
    op.execute("""
        DELETE FROM bank_statement_row_subject a
         USING bank_statement_row_subject b
          WHERE a.row_id = b.row_id
            AND a.subject_id = b.subject_id
            AND a.id > b.id
    """)
    op.execute("ALTER TABLE bank_statement_row_subject "
               "ADD CONSTRAINT uq_bank_statement_row_subject "
               "UNIQUE (row_id, subject_id)")
