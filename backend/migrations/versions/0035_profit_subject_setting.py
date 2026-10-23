"""给 ``book_year`` 加一列：手工设定的「本年利润」科目编码。

Revision ID: 0035_profit_subject_setting
Revises: 0034_mcp_token
Create Date: 2026-10-23

**这一条改的是什么。**

结转损益要把损益类科目的发生额冲到「本年利润」上，而这个目的科目原先
**只能靠编码猜**：``PROFIT_SUBJECT_CODES = ("3103", "4103")``，一个都不在
就整段报错「账套缺少「本年利润」科目」。

科目表是各人自己的：改成 3111 的有、叫「本年损益」的也有、系统自带的
标准科目表又只在建账那一次装上。识别不出来时，**界面上没有任何地方能改** ——
用户只能看到一句报错，然后无路可走。

所以给年度账套加一列 ``profit_subject_code``，把「本年利润是哪一个科目」
变成**可设定的事实**，而不是猜出来的结果。

**为什么挂在年度上而不是账套上。** 科目表每个年度各有一份（迁移 0032），
2026 与 2027 是两张不同的表、同一套编码。挂在账套上就得回答「这一本账
到底哪个年度的表说了算」，答案是没有 —— 所以按年度存。

**为什么存编码而不是科目 id。** 同一编码在两个年度是两行不同的记录，
存 id 就只能管一年；存编码，这一年设了，下一年若是同一套结构
（年度结转就是照抄上一年的科目表），照样能用。

**空值 = 走自动识别**，不是「不识别」。这一列是**覆盖**，不是替代：
没设的账套一个字节都不变，行为与迁移前完全一致。

**降级。** 丢掉这一列等于丢掉用户手工设定的那份事实 —— 那些账套会
重新回到「识别不到就报错、且无处可改」的状态。所以降级只删列，
不假装这份设定从来没存在过。
"""
from __future__ import annotations

from alembic import op

revision = "0035_profit_subject_setting"
down_revision = "0034_mcp_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 幂等：迁移可能被手工重跑（见 0034 的说明），所以带 IF NOT EXISTS。
    op.execute("ALTER TABLE book_year ADD COLUMN IF NOT EXISTS "
               "profit_subject_code VARCHAR(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE book_year DROP COLUMN IF EXISTS profit_subject_code")
