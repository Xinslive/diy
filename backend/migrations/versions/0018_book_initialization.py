"""账套初始化状态：第 1 步（科目）、第 2 步（期初余额）（2026-10）

Revision ID: 0018_book_initialization
Revises: 0017_system_settings
Create Date: 2026-10-15

**为什么要有这两列。**

建完账套以前只弹一条小提示「下一步录期初余额」，提示挂在**账套管理页的
新建弹窗里**，用户点「关闭」就再也没人提醒了 —— 于是绝大多数新账套停在
「科目有了、期初是空的」这个状态，开始记第一个月的凭证，
而上一个年度结转过来的钱和债一笔都没进来。期初没录就直接记账，
总账、试算平衡表、资产负债表从第一张凭证起就是错的，而且**看不出来**
（页面不提示错误，只是数不对）。

所以初始化被显式拆成两步并**记在账套上**：

    1. 导入科目（或确认沿用系统装好的标准科目表）
    2. 导入期初余额

第 2 步必须等第 1 步完成 —— 期初余额是**挂在科目上的**，账上一张科目表都
没有就录期初，录进去的数挂不到任何科目上，只能整批丢掉。用户自己不会知道
这个先后关系（界面上期初表和科目表是两个页面），所以由系统记着。

**两列而不是一张初始化表。** 初始化只有两步、且天然有序，用两个时间戳就能
表达「第 1 步做没做」「第 2 步做没做」，外加一列来源（是从用友导的科目表，
还是直接用了系统标准表）。再建一张表要多一次 JOIN、多一套级联，
而这两列永远只和 book 1:1。

**存量账套为什么直接标记成已完成。**

升级那一刻库里已有的账套，每一个都已经装好了标准科目表，也已经在记凭证了 ——
对它们来说第 1 步事实上早就做完了。如果不给它们回填，
「期初余额还没录」这个新闸门会把**正在用的老账套**一起拦住：
用户打开老账套想改个期初余额，被 409 顶回来，而界面上告诉他
「请先完成初始化第 1 步」，这在一个已经记了两年凭证的账上毫无意义。

回填用 ``now()`` 而不是账套的 created_at：回填表达的是
「系统承认你这一步已完成」，时间取迁移时刻才不会伪造出
「这个账套建好当天就确认过科目」这种没发生过的历史。
``source='CHART'`` 与新账套点「沿用系统标准科目表」是同一个含义。
"""
from __future__ import annotations

from alembic import op


revision = "0018_book_initialization"
down_revision = "0017_system_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 用裸 SQL 的 ADD COLUMN IF NOT EXISTS，而不是 op.add_column：
    # 这条 revision 会被**重放**。tests/test_migration_guardrails.py 里
    # 「存量库升级后新护栏真的出现」那几条把库 stamp 回 0004 再重跑整条链，
    # 于是 0018 会在一张**已经有这三列**的表上再执行一遍 ——
    # op.add_column 到这里就是 DuplicateColumn 直接炸掉。
    # 0015_book_data_version 加 data_version 时踩过同一个坑，形状照抄它。
    op.execute(
        "ALTER TABLE book ADD COLUMN IF NOT EXISTS "
        "init_subjects_at TIMESTAMP WITH TIME ZONE"
    )
    # 来源只有 IMPORT（从外部系统导入科目表）与 CHART（沿用系统标准科目表）两种，
    # 写死长度而不是枚举表：它是审计用的一个说明字段，不是业务分支依据，
    # 真要多第三种来源时也只需要在这里放宽。
    op.execute(
        "ALTER TABLE book ADD COLUMN IF NOT EXISTS "
        "init_subjects_source VARCHAR(16)"
    )
    op.execute(
        "ALTER TABLE book ADD COLUMN IF NOT EXISTS "
        "init_openings_at TIMESTAMP WITH TIME ZONE"
    )
    # 第 2 步的**来源**：IMPORT = 导入过余额表；NONE = 本期本来就没有期初
    # （新公司第一年、上一年度是空账）。不记来源的话，「已导入 213 条」和
    # 「确认过没有期初」在库里长得一模一样 —— 而后者恰恰是最需要留痕的一种：
    # 出了事要查「当时是不是本来就没有」，而界面上一个数字都没有。
    op.execute(
        "ALTER TABLE book ADD COLUMN IF NOT EXISTS "
        "init_openings_source VARCHAR(16)"
    )
    # 存量账套：装好标准科目表 = 第 1 步已完成（理由见模块 docstring）
    op.execute(
        "UPDATE book SET init_subjects_at = now(), init_subjects_source = 'CHART' "
        "WHERE init_subjects_at IS NULL"
    )
    # 第 2 步同理，而且**按事实回填**：库里已经有期初余额的账套，
    # 说明期初这步它早就做完了。只回填第 1 步的话，它们会在账套卡片上
    # 永远挂一个「待初始化」，而向导点进去发现第 2 步其实有数据 ——
    # 状态与事实对不上，比没有这个标记更让人不信任。
    op.execute(
        "UPDATE book SET init_openings_at = now(), init_openings_source = 'IMPORT' "
        "WHERE init_openings_at IS NULL AND EXISTS ("
        "  SELECT 1 FROM period_opening po "
        "  JOIN period p ON p.id = po.period_id "
        "  WHERE p.book_id = book.id)"
    )
    # 存量账套里「本来就没有期初」的那部分无从判断（它们多半也没录过），
    # 老实停在第 2 步让用户自己声明 —— 猜成 NONE 等于替人断言一件没证据的事。


def downgrade() -> None:
    op.execute("ALTER TABLE book DROP COLUMN IF EXISTS init_openings_source")
    op.execute("ALTER TABLE book DROP COLUMN IF EXISTS init_openings_at")
    op.execute("ALTER TABLE book DROP COLUMN IF EXISTS init_subjects_source")
    op.execute("ALTER TABLE book DROP COLUMN IF EXISTS init_subjects_at")
