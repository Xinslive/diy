"""修数据：凭证被删掉的那些发票记录还停在「已生成」。

Revision ID: 0047_invoice_voucher_release
Revises: 0046_row_subject_dup_ok
Create Date: 2026-10-29

**这一条改的是什么。**

``invoice_record`` 上的不变式只有一条：

    ``status == 'COMMITTED'`` ⟺ ``voucher_id IS NOT NULL``

外键 ``invoice_record.voucher_id → voucher.id`` 是 ``ON DELETE SET NULL``：
凭证一删，``voucher_id`` 自动变空，**但 ``status`` 还停在「已生成」**。

于是这条记录变成「孤儿」—— 状态说已生成，账上却什么都没有。它不是显示
难看的问题，是**这张发票再也变不出凭证**：

* 服务端对「已生成」的记录**跳过重算**，它不会变回「可生成」；
* 「确认生成」只认「可生成」的那些，所以点下去是 0 张。

用户的处境是：账上那张凭证他亲手删了，界面上这张发票还标着「已生成」，
却再也没有任何按钮能让他用这张票重新生成一张凭证。

**应用层已经补上了防线**（``app/invoice/release.py`` 与
``app/invoice/service.py``）：删凭证时把发票放回「可生成」，读路径也会把
孤儿认成「可生成」。这条迁移补的是**存量** —— 那些在修复之前就被删掉凭证、
已经卡住的记录。它们不会自己变好，得有人把它们放回去。

**降级。** **不改数据。** 升级改的是「哪些记录卡在已生成、动不了」这个状态，
而它的**逆操作没有定义**：原来有几张是「已生成」、现在有几张是「可生成」，
库里不记这个历史（凭证删了就查不回来）。硬凑一个数字等于往库里写一个编出来
的值。

而且降级**不需要**改回去：修复后的状态在新旧两版代码下**都能用** ——
新代码把它当「可生成」，旧代码也把它当「可生成」（旧代码只有「状态写着
已生成」才卡住）。所以降级只把**批次的计数**留在修正后的值：那是个事实
（现在账上确实有几张已入账），比还原成编出来的数字更诚实。

**revision id 只有 22 个字符。** ``alembic_version.version_num`` 是
``VARCHAR(32)``，超了会当场报 ``StringDataRightTruncation``：迁移跑到一半
失败，**库停在半路**（``0046`` 已 stamp、``0047`` 没上），而测试与发布都
以为迁移成功了。给新迁移起名时先数一下字符。
"""
from __future__ import annotations

from alembic import op

revision = "0047_invoice_voucher_release"
down_revision = "0046_row_subject_dup_ok"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # **1. 孤儿记录放回「可生成」。**
    #
    # 只碰「状态是已生成、却没有凭证」的：那些是确确实实不在账上的。
    # 有凭证的那些一个字都不动 —— 它们好好地入着账。
    #
    # 这条 SQL 必须能重跑（``tests/test_migration_guardrails.py`` 里有一条
    # 用例把结构打回旧形态、stamp 回旧版、再 upgrade 到 head）。
    op.execute("""
        UPDATE invoice_record
           SET status = 'READY', updated_at = now()
         WHERE status = 'COMMITTED'
           AND voucher_id IS NULL
    """)
    # **2. 批次的「已生成凭证」张数按实际重数一遍。**
    #
    # ``invoice_batch.committed_count`` 是落库的计数，删凭证时没人回头改它
    # —— 界面上的「N 张已入账」就与实际对不上了。
    #
    # 有已入账记录的批次：按实际张数写回去。
    op.execute("""
        UPDATE invoice_batch b
           SET committed_count = c.cnt, updated_at = now()
          FROM (SELECT batch_id, COUNT(*) AS cnt
                  FROM invoice_record
                 WHERE status = 'COMMITTED' AND voucher_id IS NOT NULL
                 GROUP BY batch_id) c
         WHERE b.id = c.batch_id
           AND b.committed_count IS DISTINCT FROM c.cnt
    """)
    # 一张都不剩的批次：计数归零。
    op.execute("""
        UPDATE invoice_batch b
           SET committed_count = 0, updated_at = now()
         WHERE b.committed_count <> 0
           AND NOT EXISTS (SELECT 1 FROM invoice_record r
                            WHERE r.batch_id = b.id
                              AND r.status = 'COMMITTED'
                              AND r.voucher_id IS NOT NULL)
    """)
    # **3. 一张都没剩的批次，从「已生成」回到「待确认」。**
    #
    # 那一栏写着「已生成」而实际上一个凭证都没有，是骗人的。回到「待确认」
    # 之后，「作废批次」也重新可点 —— 凭证都没了，把这批记录清掉是安全的。
    op.execute("""
        UPDATE invoice_batch b
           SET status = 'PENDING',
               message = '凭证已删除，可以重新生成',
               updated_at = now()
         WHERE b.status = 'COMMITTED'
           AND NOT EXISTS (SELECT 1 FROM invoice_record r
                            WHERE r.batch_id = b.id
                              AND r.status = 'COMMITTED'
                              AND r.voucher_id IS NOT NULL)
    """)


def downgrade() -> None:
    # **不改数据。** 理由见文件头的「降级」一段：
    #
    # * 逆操作**没有定义** —— 原来有几张是「已生成」，库里不记这个历史
    #   （凭证删了就查不回来）。硬凑一个数字等于往库里写编出来的值。
    # * 而且**不需要**改回去 —— 修复后的状态（可生成）在新旧两版代码下
    #   都能用：新代码把它当「可生成」，旧代码也把它当「可生成」
    #   （旧代码只有「状态写着已生成」才卡住）。批次的计数留在修正后的
    #   值上，那是个事实。
    pass
