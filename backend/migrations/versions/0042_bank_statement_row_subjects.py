"""流水对方科目改成子表 + 流水凭证摘要模板。

Revision ID: 0042_bank_statement_row_subjects
Revises: 0041_bank_statement
Create Date: 2026-10-27

**这一条改的是什么。**

三件事：

1. ``bank_statement_row_subject`` —— 一行流水的对方科目从「行上的一个 id」
   改成**子表里的若干条（科目 + 金额）**。理由：一笔流水可能对应好几个
   科目（一笔 1000 元的付款里 800 是房租、200 是押金），而金额必须跟着
   科目走 —— 单值列表达不了「这笔钱分别记到哪几个科目上、各记多少」。
2. ``bank_statement_config`` —— 每个账套一份的流水对账配置，装「流水凭证
   摘要模板」。摘要是给人看的，而各家银行「摘要」那一列装的根本不是
   同一回事，所以它必须能改。
3. ``bank_statement_rule.summary_template`` —— 规则级摘要模板。运费流水与
   工资流水的摘要多半不一样，而它们同属一个账套，所以要能按规则分开设。

**为什么把 ``plan_subject_id`` 删掉，而不是留着。**

留着它就变成**两份真相**：行上那个 id 与子表里的若干条，谁说了算？
两份真相一定会漂 —— 改了一处忘了另一处，于是界面上显示的对方科目与
真正用来制证的不是同一个科目。凭证照样能生成（金额对得上），错得毫无
痕迹。所以这里一次性搬干净：先回填子表，再删旧列。

**回填的数据不会丢。** 旧的 ``plan_subject_id`` 变成子表里的第一条
（金额 = 该行流水金额，来源 = 原来的 ``plan_status``）。

**一个例外要说清楚**：流水金额为 0 或空的行，它的对方科目**搬不过去**
（子表有 ``amount > 0`` 的 CHECK，而 0 金额的分录本来也进不了凭证 ——
库层 ``ck_entry_amount_nonzero`` 会挡）。这些行的 ``plan_status`` 被改回
``PENDING``：原来那个科目 id 随着旧列一起消失了，界面上要重新显示成
「待确认科目」，而不是留一个指向空处的「已定」。

**降级。** 反向搬回单值列（取子表里第一条），再删两张表与那一列。
"""
from __future__ import annotations

from alembic import op

revision = "0042_bank_statement_row_subjects"
down_revision = "0041_bank_statement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS bank_statement_row_subject (
            id SERIAL PRIMARY KEY,
            row_id INTEGER NOT NULL REFERENCES bank_statement_row(id) ON DELETE CASCADE,
            subject_id INTEGER NOT NULL REFERENCES subject(id) ON DELETE RESTRICT,
            amount NUMERIC(18, 2) NOT NULL,
            source VARCHAR(16) NOT NULL DEFAULT 'MANUAL',
            sort_no INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_bank_statement_row_subject UNIQUE (row_id, subject_id),
            CONSTRAINT ck_bank_statement_row_subject_amount CHECK (amount > 0)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_bank_statement_row_subject_row "
               "ON bank_statement_row_subject (row_id, sort_no)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS bank_statement_config (
            id SERIAL PRIMARY KEY,
            book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
            summary_template VARCHAR(256) NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_bank_statement_config_book UNIQUE (book_id)
        )
    """)

    op.execute("ALTER TABLE bank_statement_rule "
               "ADD COLUMN IF NOT EXISTS summary_template VARCHAR(256) NOT NULL DEFAULT ''")

    # ---- 回填：旧的单值列 → 子表第一条 --------------------------------
    #
    # 金额为 0 或空的行搬不过去（子表 CHECK 挡住 0 金额），它们的
    # plan_status 在下面被改回 PENDING —— 界面上要显示成「待确认科目」。
    #
    # **整段包在 DO 块里查列是否存在**：这条迁移必须**能重跑** ——
    # ``tests/test_migration_guardrails.py`` 里有一条用例把结构打回旧形态、
    # stamp 回旧版、再 upgrade 到 head，那条路径会让本段**跑第二遍**。
    # 而第一遍已经把 ``plan_subject_id`` 删掉了，第二遍再直接 SELECT 它
    # 就是 ``UndefinedColumn`` —— 迁移当场失败，发布流程卡在第 4 步。
    # 0001~0041 那些迁移全靠 ``IF NOT EXISTS`` 达到「重跑无害」，
    # 这里也得一样。
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'bank_statement_row'
                   AND column_name = 'plan_subject_id'
            ) THEN
                INSERT INTO bank_statement_row_subject
                    (row_id, subject_id, amount, source, sort_no)
                SELECT r.id, r.plan_subject_id, r.amount,
                       CASE WHEN r.plan_status = 'RULE' THEN 'RULE' ELSE 'MANUAL' END,
                       1
                FROM bank_statement_row r
                WHERE r.plan_subject_id IS NOT NULL
                  AND r.amount IS NOT NULL
                  AND r.amount > 0
                ON CONFLICT (row_id, subject_id) DO NOTHING;

                UPDATE bank_statement_row
                   SET plan_status = 'PENDING'
                 WHERE plan_subject_id IS NOT NULL
                   AND (amount IS NULL OR amount <= 0);
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE bank_statement_row DROP COLUMN IF EXISTS plan_subject_id")


def downgrade() -> None:
    # 反向搬回单值列：取子表里 sort_no 最小的那条。
    op.execute("ALTER TABLE bank_statement_row "
               "ADD COLUMN IF NOT EXISTS plan_subject_id INTEGER "
               "REFERENCES subject(id) ON DELETE RESTRICT")
    op.execute("""
        UPDATE bank_statement_row r
           SET plan_subject_id = s.subject_id
          FROM (
              SELECT DISTINCT ON (row_id) row_id, subject_id
                FROM bank_statement_row_subject
               ORDER BY row_id, sort_no, id
          ) s
         WHERE s.row_id = r.id
    """)
    op.execute("DROP TABLE IF EXISTS bank_statement_config")
    op.execute("DROP TABLE IF EXISTS bank_statement_row_subject")
    op.execute("ALTER TABLE bank_statement_rule "
               "DROP COLUMN IF EXISTS summary_template")
