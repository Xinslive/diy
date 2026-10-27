"""新建流水对账四张表：对账单、流水行、科目规则、格式画像。

Revision ID: 0041_bank_statement
Revises: 0040_retained_subject_setting
Create Date: 2026-10-27

**这一条改的是什么。**

新增「流水对账」功能的四张表。改之前，系统只认「发票」这一种外部单据：
网银导出的对账单要么没人看，要么被人手工抄成凭证。抄一遍漏一笔，
少抄一笔；抄错了科目，余额表上只看得见一个差值。

有了这四张表之后：

  · ``bank_statement`` —— 一份对账单文件（原件、账户、期间、期末余额）；
  · ``bank_statement_row`` —— 摊开的一条条流水，带「有没有入账、
    被哪张发票占用过、对方科目定了没有」；
  · ``bank_statement_rule`` —— 摘要/用途/对方户名关键词 → 对方科目；
  · ``bank_statement_profile`` —— 手工指定过一次列映射之后，把这个格式记住。

**为什么用裸 SQL 而不是 ``op.create_table``。** 理由与 0026 / 0030 / 0034 /
0039 一致：迁移必须能重跑（``tests/test_migration_guardrails.py`` 里有一条
「把结构打回旧形态、stamp 回旧版、再 upgrade 到 head」的用例，那条路径会让
这段迁移**跑第二遍**）。``IF NOT EXISTS`` 让重跑无害。

**为什么流水行的账套 id 要冗余一份。** 发票匹配是「同账套 + 同方向 +
同金额」的热路径，每次都 JOIN 回对账单表多一次连接。而「这条流水属于哪个
账套」这件事必须在**行**上就能判断 —— 跨账套的数据串味是这类系统最不能
出的错，而多存一个 int 就能让那类判断不必依赖 JOIN。

**为什么对账单表上有「同账户 + 同期间」的唯一约束。** 同一份银行流水传两遍
就会生成两遍凭证 —— 钱在账上多记一次，而用户界面上看着「我明明只传了一次」。
这不是概率问题，是「再点一次上传按钮」就会发生的事。

不过 **PostgreSQL 的唯一约束把 NULL 视作互不相等**：对账单没解析出期间
（``period_start``/``period_end`` 为 NULL）时，这条约束**不拦**。
所以服务层另有一次显式检查（``service.upload_statement``）—— 两道都要有：
约束拦得住绝大多数，那一次检查负责给出**人话**提示。

**降级。** 删表即回到「没有流水对账」这个状态。里面的数据全是用户刚上传的
原件与解析结果，没有别的表依赖它，删掉没有孤儿问题。
"""
from __future__ import annotations

from alembic import op

revision = "0041_bank_statement"
down_revision = "0040_retained_subject_setting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS bank_statement (
            id SERIAL PRIMARY KEY,
            book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
            account_subject_id INTEGER NOT NULL REFERENCES subject(id) ON DELETE RESTRICT,
            account_subject_code VARCHAR(32) NOT NULL DEFAULT '',
            filename VARCHAR(256) NOT NULL DEFAULT '',
            file_path VARCHAR(512) NOT NULL DEFAULT '',
            container_format VARCHAR(8) NOT NULL DEFAULT '',
            bank_name VARCHAR(64) NOT NULL DEFAULT '',
            account_no VARCHAR(64) NOT NULL DEFAULT '',
            account_name VARCHAR(128) NOT NULL DEFAULT '',
            period_start DATE,
            period_end DATE,
            opening_balance NUMERIC(18, 2),
            closing_balance NUMERIC(18, 2),
            income_total NUMERIC(18, 2),
            expense_total NUMERIC(18, 2),
            row_count INTEGER NOT NULL DEFAULT 0,
            parse_status VARCHAR(16) NOT NULL DEFAULT 'OK',
            parse_message TEXT NOT NULL DEFAULT '',
            diagnosis JSONB NOT NULL DEFAULT '{}'::jsonb,
            uploaded_by VARCHAR(64) NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_bank_statement_book_subject_period
                UNIQUE (book_id, account_subject_id, period_start, period_end)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_bank_statement_book_id "
               "ON bank_statement (book_id, id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_bank_statement_book_subject "
               "ON bank_statement (book_id, account_subject_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS bank_statement_row (
            id SERIAL PRIMARY KEY,
            statement_id INTEGER NOT NULL REFERENCES bank_statement(id) ON DELETE CASCADE,
            book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            tx_date DATE,
            tx_time VARCHAR(16) NOT NULL DEFAULT '',
            direction VARCHAR(8) NOT NULL DEFAULT 'UNKNOWN',
            amount NUMERIC(18, 2),
            balance NUMERIC(18, 2),
            summary TEXT NOT NULL DEFAULT '',
            purpose TEXT NOT NULL DEFAULT '',
            counterparty_name VARCHAR(128) NOT NULL DEFAULT '',
            counterparty_account VARCHAR(64) NOT NULL DEFAULT '',
            extra JSONB NOT NULL DEFAULT '{}'::jsonb,
            status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
            matched_voucher_id INTEGER REFERENCES voucher(id) ON DELETE SET NULL,
            matched_invoice_record_id INTEGER REFERENCES invoice_record(id) ON DELETE SET NULL,
            plan_subject_id INTEGER REFERENCES subject(id) ON DELETE RESTRICT,
            plan_status VARCHAR(16) NOT NULL DEFAULT 'NONE',
            problems JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # 发票匹配的热路径：同账套 + 同方向 + 同金额 + 日期。
    op.execute("CREATE INDEX IF NOT EXISTS ix_bank_statement_row_match "
               "ON bank_statement_row (book_id, direction, amount, tx_date)")
    # 制证候选：同账套 + 状态。
    op.execute("CREATE INDEX IF NOT EXISTS ix_bank_statement_row_status "
               "ON bank_statement_row (book_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_bank_statement_row_statement "
               "ON bank_statement_row (statement_id, seq)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS bank_statement_rule (
            id SERIAL PRIMARY KEY,
            book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
            match_field VARCHAR(16) NOT NULL DEFAULT 'SUMMARY',
            keyword VARCHAR(128) NOT NULL,
            subject_id INTEGER NOT NULL REFERENCES subject(id) ON DELETE RESTRICT,
            priority INTEGER NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_by VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_bank_statement_rule_keyword
                UNIQUE (book_id, match_field, keyword)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_bank_statement_rule_book_active "
               "ON bank_statement_rule (book_id, active, priority)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS bank_statement_profile (
            id SERIAL PRIMARY KEY,
            book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
            fingerprint VARCHAR(32) NOT NULL,
            column_mapping JSONB NOT NULL DEFAULT '{}'::jsonb,
            hit_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_bank_statement_profile_fingerprint
                UNIQUE (book_id, fingerprint)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS bank_statement_profile")
    op.execute("DROP TABLE IF EXISTS bank_statement_rule")
    op.execute("DROP TABLE IF EXISTS bank_statement_row")
    op.execute("DROP TABLE IF EXISTS bank_statement")
