"""流水行可以人工指定凭证摘要。

Revision ID: 0043_bank_statement_row_summary
Revises: 0042_bank_statement_row_subjects
Create Date: 2026-10-28

**这一条改的是什么。**

给 ``bank_statement_row`` 加一列 ``summary_override``：这一行的**凭证摘要**
由人工写死。留空 = 跟以前一模一样（按规则/模板算）。

**为什么需要它。**

规则算出来的摘要（用途优先、摘要兜底、模板占位符）对**大多数**行是对的，
但总有行不对 —— 某一笔该记成「备用金」而不是「办公室租金」，而规则管不了
「这一笔是例外」。更实际的是：摘要这件事**本来就是逐笔看**的，一次性给全
账套定一个口径，剩下那些笔总要有人能改。

**为什么不改 ``summary`` 那一列。**

``summary`` 是**对账单上原样抄下来的**那一列（「转入」「支取」……
各家银行写法不一）。它是**证据**：制证时用它的哪一部分、模板里的
「{摘要}」指的是什么，都以它为准。把它改成「人工写的凭证摘要」，
就等于把证据改成了结论 —— 以后没法回头核对「这笔到底为什么记成这个摘要」。

所以另起一列：**证据照旧，结论另存**。制证时优先用结论，没有结论才用
规则算出来的那个。

**降级。** 删列即可；丢掉的是人工写过的摘要（那本来就在界面上重填一次
的事）。

**revision id 只有 31 个字符 —— 这是有原因的。**
``alembic_version.version_num`` 是 ``VARCHAR(32)``，超了会当场报
``StringDataRightTruncation``：迁移跑到一半失败，**库停在半路**
（``0042`` 已 stamp、``0043`` 没上），而测试与发布都以为迁移成功了。
上一个 id 叫 ``0043_bank_statement_row_summary_override``（40 字符）就是
这么炸的 —— 缩短之后重跑即可。**给新迁移起名时先数一下字符。**
"""
from __future__ import annotations

from alembic import op

revision = "0043_bank_statement_row_summary"
down_revision = "0042_bank_statement_row_subjects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS：这条迁移必须**能重跑** ——
    # ``tests/test_migration_guardrails.py`` 里有一条用例把结构打回旧形态、
    # stamp 回旧版、再 upgrade 到 head，那条路径会让本段跑第二遍。
    op.execute("ALTER TABLE bank_statement_row "
               "ADD COLUMN IF NOT EXISTS summary_override TEXT NOT NULL DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE bank_statement_row "
               "DROP COLUMN IF EXISTS summary_override")
