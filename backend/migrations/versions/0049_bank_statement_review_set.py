"""流水行：判过「不是同一笔」的凭证记成**一组**，外加一列记着待复核对着哪张。

Revision ID: 0049_bank_statement_review_set
Revises: 0048_login_token_revocation
Create Date: 2026-10-30

**这一条改的是什么。**

``bank_statement_row`` 两处改动：

1. ``rejected_voucher_id``（一张）→ ``rejected_voucher_ids``（**一组**，JSONB）；
2. 新增 ``review_voucher_id``（这一行待复核时对着的那张凭证）。

**为什么第一处要改（2026-10-30 用户报的死循环）。**

生产上是这样一幕：一份对账单上有两行同额流水（18.05），账上有**四张**同额
凭证（记0045/记0043/记0040/记0041，全是同一个月的高速通行费）。系统给两行
各指一张候选，用户点「不是同一笔」把它否掉 —— **那张凭证会挪到旁边那一行
上重新冒出来**；在那边再否一次，**原来那一行又冒出同一张**。

根因是那一列只装**一张**：第二次判「不是同一笔」把第一次记的那张**覆盖**
掉了。于是第一次的判断凭空消失 —— 用户点过的那张凭证又回来了。

一列只装一张，就只能记住「最近一次」的判断；记判断这件事**必须是累加的**：
否过一张就永远否过它，后来的判断不许把先前的抹掉。所以改成 JSONB 数组。

**为什么第二处要加（候选不许搬家）。**

配对是**一对一**的贪心分配：日期差最小的先配对。于是旁边那一行被否掉之后，
**重新分配**时那张凭证会落到这一行头上 —— 用户看着就是「我刚判过的那张凭证
又跑到另一行去了」。要治住它，就得**记住这一行现在对着哪张凭证**（这一列），
重算时优先兑现它：一张凭证一旦配给某行，没被否掉就不换人。

**降级。**

删掉这两列，把 ``rejected_voucher_ids`` 里的第一张写回 ``rejected_voucher_id``
（退回「只记最近一次」的老口径）。降级之后那个死循环会回来 ——
但不会凭空多记钱、也不会漏记。
"""
from __future__ import annotations

from alembic import op

revision = "0049_bank_statement_review_set"
down_revision = "0048_login_token_revocation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. 判过「不是同一笔」的凭证：一张 → 一组 ----
    #
    # IF NOT EXISTS：这条迁移必须**能重跑**（守卫栏里有一条用例把结构打回
    # 旧形态、stamp 回旧版、再 upgrade 到 head，那条路径会让本段跑第二遍）。
    op.execute("ALTER TABLE bank_statement_row "
               "ADD COLUMN IF NOT EXISTS rejected_voucher_ids JSONB "
               "NOT NULL DEFAULT '[]'::jsonb")

    # 旧的单列值搬进新集合。**只搬还站得住的那些**：老列是外键，
    # 凭证被删了它会是 NULL；新集合里存的是凭证 id，指向一张不存在的凭证
    # 没有意义（那张凭证没了，「不是同一笔」这件事也就无从谈起 ——
    # 删了凭证本来就要重新判一次）。
    #
    # **整段包在 DO 块里**：这条迁移必须**能重跑**（守卫栏里有一条用例把
    # 结构打回旧形态、stamp 回旧版、再 upgrade 到 head）。重跑时老列已经
    # 被上一轮自己删掉了 —— 直接 UPDATE 就会报「column does not exist」，
    # 迁移卡在半路。
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'bank_statement_row'
                   AND column_name = 'rejected_voucher_id'
            ) THEN
                UPDATE bank_statement_row r
                   SET rejected_voucher_ids = to_jsonb(ARRAY[r.rejected_voucher_id])
                 WHERE r.rejected_voucher_id IS NOT NULL
                   AND EXISTS (SELECT 1 FROM voucher v WHERE v.id = r.rejected_voucher_id);
            END IF;
        END $$;
    """)

    # 老的单列与它的外键：搬完就删掉（两份「记了哪张」会打架）。
    op.execute("ALTER TABLE bank_statement_row "
               "DROP CONSTRAINT IF EXISTS fk_bank_statement_row_rejected_voucher")
    op.execute("ALTER TABLE bank_statement_row "
               "DROP COLUMN IF EXISTS rejected_voucher_id")

    # ---- 2. 这一行待复核时对着哪张凭证 ----
    op.execute("ALTER TABLE bank_statement_row "
               "ADD COLUMN IF NOT EXISTS review_voucher_id INTEGER")

    # 外键：那张凭证被删了，这一行就回到「没指认过」——
    # 重算时它照常重新配一张候选出来（删了凭证本来就要重新判一次）。
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                  JOIN pg_namespace n ON n.oid = t.relnamespace
                 WHERE c.conname = 'fk_bank_statement_row_review_voucher'
                   AND n.nspname = current_schema()
            ) THEN
                ALTER TABLE bank_statement_row
                    ADD CONSTRAINT fk_bank_statement_row_review_voucher
                    FOREIGN KEY (review_voucher_id)
                    REFERENCES voucher(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE bank_statement_row "
               "DROP CONSTRAINT IF EXISTS fk_bank_statement_row_review_voucher")
    op.execute("ALTER TABLE bank_statement_row "
               "DROP COLUMN IF EXISTS review_voucher_id")

    # 老列回来：装回集合里的第一张（退回「只记最近一次」的老口径）。
    #
    # **整段包在 DO 块里**：降级也必须**能重跑**（同一条守卫栏用例会把
    # 结构来回打）。集合不在了就跳过 —— 退无可退。
    op.execute("ALTER TABLE bank_statement_row "
               "ADD COLUMN IF NOT EXISTS rejected_voucher_id INTEGER")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'bank_statement_row'
                   AND column_name = 'rejected_voucher_ids'
            ) THEN
                UPDATE bank_statement_row r
                   SET rejected_voucher_id = v.voucher_id
                  FROM (
                        SELECT id,
                               (SELECT x::int
                                  FROM jsonb_array_elements_text(rejected_voucher_ids) AS x
                                 ORDER BY x
                                 LIMIT 1) AS voucher_id
                          FROM bank_statement_row
                       ) v
                 WHERE r.id = v.id
                   AND v.voucher_id IS NOT NULL
                   AND EXISTS (SELECT 1 FROM voucher vw WHERE vw.id = v.voucher_id);
            END IF;
        END $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                  JOIN pg_namespace n ON n.oid = t.relnamespace
                 WHERE c.conname = 'fk_bank_statement_row_rejected_voucher'
                   AND n.nspname = current_schema()
            ) THEN
                ALTER TABLE bank_statement_row
                    ADD CONSTRAINT fk_bank_statement_row_rejected_voucher
                    FOREIGN KEY (rejected_voucher_id)
                    REFERENCES voucher(id) ON DELETE SET NULL;
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE bank_statement_row "
               "DROP COLUMN IF EXISTS rejected_voucher_ids")
