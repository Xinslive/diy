"""凭证摘要从「整张凭证一句」改成「每个对方科目各自一句」。

Revision ID: 0044_row_subject_summary
Revises: 0043_bank_statement_row_summary
Create Date: 2026-10-28

**这一条改的是什么。**

摘要的位置**下沉一级**：从 ``bank_statement_row.summary_override``
（挂在流水行上 = 一整张凭证一句话）挪到
``bank_statement_row_subject.summary_override``（挂在对方科目上 = 每个
对方科目各有各的一句）。

**为什么。**

一张凭证有 N+1 条分录：现金那一条 + 对方那 N 条。每条分录的摘要**本来
就是各自独立的字段**（``entry.summary``，凭证编辑页里逐条可改）—— 而
流水这一侧却只有一个「整张凭证的摘要」，管着所有分录。这对不上：
一笔 5000 的付款拆成「房租 800 + 押金 200」，两个科目该写的话本来就不
一样（「支付办公室房租」「收取房屋押金」），而流水这一侧只能给出一句。

所以摘要的粒度跟着分录走：**每个对方科目一句，各写各的**。

**数据不会丢。**

先把行级已有的摘要**下放**到该行的每一个对方科目上（它本来适用于整张
凭证的每一条分录），再删掉行级那一列。实测生产库上行级摘要**一条都没录**
（0 行），所以这段回填在生产上是空转 —— 但迁移不能假设库是空的：
别的环境、别的账套、别的时段都可能有值，而「删列时顺手把唯一的副本扔掉」
正是这种迁移最容易犯的错。

**降级。** 把子表里第一条的摘要搬回行级，再删子表那一列。降级后摘要
回到「整张凭证一句」的粒度 —— 多个对方科目的不同摘要会塌回第一句，
这是粒度变粗的必然结果，不是数据损坏。

**revision id 只有 24 个字符 —— 这是有原因的。**
``alembic_version.version_num`` 是 ``VARCHAR(32)``，超了会当场报
``StringDataRightTruncation``：迁移跑到一半失败，**库停在半路**
（``0043`` 已 stamp、``0044`` 没上），而测试与发布都以为迁移成功了。
上一个 id 叫 ``0043_bank_statement_row_summary_override``（40 字符）就是
这么炸的 —— 缩短之后重跑即可。**给新迁移起名时先数一下字符。**
"""
from __future__ import annotations

from alembic import op

revision = "0044_row_subject_summary"
down_revision = "0043_bank_statement_row_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS：这条迁移必须**能重跑** ——
    # ``tests/test_migration_guardrails.py`` 里有一条用例把结构打回旧形态、
    # stamp 回旧版、再 upgrade 到 head，那条路径会让本段跑第二遍。
    op.execute("ALTER TABLE bank_statement_row_subject "
               "ADD COLUMN IF NOT EXISTS summary_override TEXT NOT NULL DEFAULT ''")

    # ---- 下放：行级摘要 → 该行的每一个对方科目 ------------------------
    #
    # **整段包在 DO 块里查列是否存在**：重跑时行级那一列已经没了，
    # 直接 SELECT 它就是 ``UndefinedColumn`` —— 迁移当场失败，
    # 发布流程卡在第 4 步（迁移那一步）。0001~0043 那些迁移全靠
    # ``IF NOT EXISTS`` / ``IF EXISTS`` 达到「重跑无害」，这里也得一样。
    #
    # 用 ``<> ''`` 而不是 ``IS NOT NULL``：这一列是 ``NOT NULL DEFAULT ''``，
    # 两者效果一样，但写 ``<> ''`` 顺带把「有内容」这件事说清楚了。
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'bank_statement_row'
                   AND column_name = 'summary_override'
            ) THEN
                UPDATE bank_statement_row_subject s
                   SET summary_override = r.summary_override
                  FROM bank_statement_row r
                 WHERE s.row_id = r.id
                   AND r.summary_override <> ''
                   AND s.summary_override = '';
            END IF;
        END $$;
    """)

    op.execute("ALTER TABLE bank_statement_row "
               "DROP COLUMN IF EXISTS summary_override")


def downgrade() -> None:
    # 反向：把子表里 sort_no 最小的第一条搬回行级（摘要回到「整张凭证一句」）。
    op.execute("ALTER TABLE bank_statement_row "
               "ADD COLUMN IF NOT EXISTS summary_override TEXT NOT NULL DEFAULT ''")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'bank_statement_row_subject'
                   AND column_name = 'summary_override'
            ) THEN
                UPDATE bank_statement_row r
                   SET summary_override = s.summary_override
                  FROM (
                      SELECT DISTINCT ON (row_id) row_id, summary_override
                        FROM bank_statement_row_subject
                       WHERE summary_override <> ''
                       ORDER BY row_id, sort_no, id
                  ) s
                 WHERE s.row_id = r.id
                   AND r.summary_override = '';
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE bank_statement_row_subject "
               "DROP COLUMN IF EXISTS summary_override")
