"""流水行加一列：用户判过「不是同一笔」的那张凭证。

Revision ID: 0045_bank_statement_review
Revises: 0044_row_subject_summary
Create Date: 2026-10-29

**这一条改的是什么。**

``bank_statement_row`` 加一列 ``rejected_voucher_id``（可空，外键指向
``voucher.id``，``ON DELETE SET NULL``）。

**为什么。**

2026-10-29 用户报了一个问题：账上有一笔支出，对账单上也有，但两者
**日期差了 10 天**（账上记的是发票日期，对账单是实际支付日期），系统
没把它判成「已入账」。

修法是给流水行加一档新状态「**待复核**」（``status = 'REVIEW'``）：
金额相等、方向对应，但日期差得比主窗口（7 天）大、比复核窗口（30 天）
小 —— 报出来让人判。判「是同一笔」就挂上那张凭证；判「不是同一笔」
就放行，让它正常制证。

**「不是同一笔」需要一个落脚点。** 判完之后，这一行回到 ``OPEN``，
但**下一次重算时那张同额的分录又会被捞出来** —— 用户会以为没生效
（判了等于没判）。所以要记下**否掉的是哪一张凭证**，重算时跳过它。

**为什么记凭证 id 而不是布尔。** 否掉这一张，不代表以后冒出来的另一张
同额凭证也不是它 —— 那一张仍然该报出来让用户判。布尔会把「这一行
判过了」和「这一行判过这一张」混成一件事。

**外键用 ``SET NULL``**：那张凭证被删了，这一行就回到「没判过」，
下次重算照常报 —— 删了凭证本来就要重新判一次。

**``status`` 本身不用改结构**：那一列是 ``String(16)`` 且**没有 CHECK
约束**，新增一个取值不需要动 DDL。真正需要改的只有本列。

**降级。** 删掉这一列即可 —— 「判过不是同一笔」这件事退回「没判过」，
下次重算时那一行会被重新报出来让人判（不会凭空多记钱，也不会漏记）。

**revision id 只有 22 个字符。** ``alembic_version.version_num`` 是
``VARCHAR(32)``，超了会当场报 ``StringDataRightTruncation``：迁移跑到
一半失败，**库停在半路**（``0044`` 已 stamp、``0045`` 没上），而测试与
发布都以为迁移成功了。给新迁移起名时先数一下字符。
"""
from __future__ import annotations

from alembic import op

revision = "0045_bank_statement_review"
down_revision = "0044_row_subject_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS：这条迁移必须**能重跑** ——
    # ``tests/test_migration_guardrails.py`` 里有一条用例把结构打回旧形态、
    # stamp 回旧版、再 upgrade 到 head，那条路径会让本段跑第二遍。
    op.execute("ALTER TABLE bank_statement_row "
               "ADD COLUMN IF NOT EXISTS rejected_voucher_id INTEGER")

    # 外键：删了凭证，这一行回到「没判过」（SET NULL）。
    #
    # **IF NOT EXISTS**：PostgreSQL 不支持「ADD CONSTRAINT IF NOT EXISTS」，
    # 所以整段包在 DO 块里先查再添 —— 重跑时约束已经在了，再添一次会报错。
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'fk_bank_statement_row_rejected_voucher'
            ) THEN
                ALTER TABLE bank_statement_row
                    ADD CONSTRAINT fk_bank_statement_row_rejected_voucher
                    FOREIGN KEY (rejected_voucher_id)
                    REFERENCES voucher(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE bank_statement_row "
               "DROP CONSTRAINT IF EXISTS fk_bank_statement_row_rejected_voucher")
    op.execute("ALTER TABLE bank_statement_row "
               "DROP COLUMN IF EXISTS rejected_voucher_id")
