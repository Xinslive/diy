"""给 ``invoice_config`` 加两列：进项/销项的兜底科目。

Revision ID: 0036_invoice_fallback_subject
Revises: 0035_profit_subject_setting
Create Date: 2026-10-24

**这一条改的是什么。**

发票科目规则是**关键词 → 科目**的映射，而进项发票上的项目名千奇百怪：
"*油料*粮油米面"、"*美容护肤品*日用杂品" —— 带税局分类前缀，还带规格型号。
用户不可能把见过的项目名都配成规则，于是总有一批项目配不出来。

配不出来时以前是**整张发票报错、整批都生成不了**。一张怪项目就能把一叠
正常发票全堵住，而用户除了「给每个项目都配一条规则」之外没有第二条路。

所以给配置加一个**兜底科目**：没命中规则的项目落到这个科目上，不再报错。
分进项、销项两列，不合并 —— 进项兜底是成本费用，销项兜底是收入，
混用必有一边记错。

**空值 = 沿用旧行为**（报错），不是「随便找个科目」。没配兜底的用户看到的
还是原来那句报错，一个字节都不变。

**降级。** 丢掉这两列等于丢掉用户手工设定的那份事实 —— 那些账套会重新回到
「配不出规则就整批生成不了」的状态。所以降级只删列，不假装这份设定从来没
存在过。
"""
from __future__ import annotations

from alembic import op

revision = "0036_invoice_fallback_subject"
down_revision = "0035_profit_subject_setting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 幂等：迁移可能被手工重跑（见 0034 的说明），所以带 IF NOT EXISTS。
    #
    # RESTRICT 与其余发票科目一致：科目被账套引用着就不许删，删了会让
    # 兜底指向一个不存在的科目。
    op.execute("ALTER TABLE invoice_config ADD COLUMN IF NOT EXISTS "
               "input_fallback_subject_id INTEGER REFERENCES subject(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE invoice_config ADD COLUMN IF NOT EXISTS "
               "output_fallback_subject_id INTEGER REFERENCES subject(id) ON DELETE RESTRICT")


def downgrade() -> None:
    op.execute("ALTER TABLE invoice_config DROP COLUMN IF EXISTS output_fallback_subject_id")
    op.execute("ALTER TABLE invoice_config DROP COLUMN IF EXISTS input_fallback_subject_id")
