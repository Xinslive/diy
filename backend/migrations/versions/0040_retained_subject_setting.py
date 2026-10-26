"""给 ``book_year`` 加一列：手工设定的「未分配利润」科目编码。

Revision ID: 0040_retained_subject_setting
Revises: 0039_voucher_idempotency
Create Date: 2026-10-26

**这一条改的是什么。**

年度结转要把本年利润的余额冲到「未分配利润」上，而这个目的科目原先
**只能靠编码猜**：``RETAINED_SUBJECT_CODES = ("310415", "3104", "4104")``，
一个都不在就整段报错「没有「未分配利润」科目，请先补上该科目再结转」。

而科目表是各人自己的：编码改成 310411 的有、把「未分配利润」挂在别的
大类码底下的也有。**识别不出来时，年度结转就结不了，而界面上没有任何
地方能改** —— 用户只能看着一句报错，无路可走。

所以给年度账套加一列 ``retained_subject_code``，把「未分配利润是哪一个科目」
变成**可设定的事实**，而不是猜出来的结果。

**为什么跟「本年利润科目」一样挂在年度上、一样存编码不存 id。**
见迁移 0035 的说明：科目表每个年度各有一份，同一编码在 2026 与 2027
是两行不同的记录；存 id 就只能管一年，存编码才跨年可用。

**空值 = 走自动识别**，不是「不识别」。这一列是**覆盖**，不是替代：
没设过的账套一个字节都不变，行为与迁移前完全一致。

**降级。** 丢掉这一列等于丢掉用户手工设定的那份事实 —— 那些账套会重新
回到「识别不到就报错、且无处可改」的状态。所以降级只删列，不假装这份
设定从来没存在过。
"""
from __future__ import annotations

from alembic import op

revision = "0040_retained_subject_setting"
down_revision = "0039_voucher_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 幂等：迁移可能被手工重跑（见 0034 的说明），所以带 IF NOT EXISTS。
    op.execute("ALTER TABLE book_year ADD COLUMN IF NOT EXISTS "
               "retained_subject_code VARCHAR(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE book_year DROP COLUMN IF EXISTS retained_subject_code")
