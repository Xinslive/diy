"""报表模板按账套隔离（写时复制）（2026-10）

Revision ID: 0020_report_template_book_scope
Revises: 0019_book_order
Create Date: 2026-10-15

**为什么是这一列。** 报表模板以前是全局的：所有账套共用同一套行、列和取数
公式，任何账套改了公式，所有账套立刻跟着变。审计 P1-05 当时只能做应用层
收紧（``api/reports.py::_require_template_write`` 的 docstring 里写着
「根治同样是给模板加 book_id，那需要一条 Alembic revision，归迁移工作包」）——
根治就是这一条。

**语义是「写时复制」而不是「全量复制」。**

* ``book_id IS NULL`` = 官方内置基线（资产负债表 / 利润表）。所有账套共看
  同一份，而且只读。
* ``book_id = X`` = 归属账套 X，X 的 OWNER 可改。

某账套第一次进入设计模式要改内置表时，API 先把基线整份复制一份归属该
账套（``POST /api/reports/templates/{id}/fork``），之后的改动只落在那份
副本上。所以：**没动过内置表的账套一个副本都不会产生**（不膨胀），而
**动过的账套也影响不到别人**（不串改）。两者不是二选一。

**存量数据怎么归。**

* 内置的两份（``BALANCE_SHEET`` / ``INCOME_SHEET``，``is_builtin``）留在
  NULL —— 它们本来就是基线，现在仍然是。
* 非内置模板按名字归到 ``星悦荟商管``：现有的 ``IMPORTED_*`` 两份是
  「星悦荟2026资产负债表 / 利润表」，是在这个账套里导入并改过的。

为什么能按名字断：``report_result`` 表是空的（没有任何账套 persist 过
报表结果），所以**没有**「哪个账套真的在用这张表」的数据证据可用，
只有模板自己的名字。迁移脚本因此**按名字认领**，而不是猜一个
「最近的账套」—— 后者在多账套下会把模板塞给一个从没碰过它的账套，
比塞错人更难查。

这不是一条普适的回填规则，只描述**这一次**存量迁移的样子。新装的库走
``0001_baseline``，那张表本来就是空的。

**唯一约束为什么拆成两条。** 原来 ``code`` 是全局 UNIQUE。隔离之后同一份
导入文件要能在每个账套各导一次（code 是内容指纹），所以必须按账套唯一；
而 NULL 在普通 UNIQUE 里允许重复（PG 默认 NULLS DISTINCT），基线恰恰要求
``code`` 唯一。一条约束表达不了，用两个部分唯一索引：

    UNIQUE (code)         WHERE book_id IS NULL
    UNIQUE (book_id, code) WHERE book_id IS NOT NULL

**为什么可空。** NULL 在这里不是「还没填」，是一个有明确含义的状态：
官方基线。它与「有归属」的模板读起来一样简单，列表去重也就是
「本账套副本优先于同 code 的基线」。
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "0020_report_template_book_scope"
down_revision = "0019_book_order"
branch_labels = None
depends_on = None


#: 存量非内置模板的归属。键是模板名，值是账套名 —— 按**名字**写死，
#: 因为这台库上没有别的证据能说明它们属于谁（report_result 为空）。
#: 找不到就保持 NULL 让人来认领，宁可让它暂时不出现，也不要塞给错账套。
_STOCK_TEMPLATES = {
    "星悦荟2026资产负债表": "星悦荟商管",
    "星悦荟2026利润表": "星悦荟商管",
}


def upgrade() -> None:
    # 裸 SQL 而不是 op.add_column：这条 revision 会被重放
    # （tests/test_migration_guardrails.py 把存量库 stamp 回早期版本再跑
    # 整条链），到这儿列已经存在时 op.add_column 就是 DuplicateColumn。
    # 形状照抄 0015 / 0018 / 0019。
    op.execute("ALTER TABLE report_template ADD COLUMN IF NOT EXISTS book_id INTEGER")
    # book_id 可空：NULL 就是「官方内置基线」，不是缺值，所以不加
    # NOT NULL。ondelete CASCADE 与 models/report.py 一致 —— 账套没了，
    # 它的报表模板也不该留着。
    # 外键同样要防重放，而 PG 没有 ADD CONSTRAINT IF NOT EXISTS —— 裸写就是
    # DuplicateObject。写法照抄 db_init.sql 里「按列找外键」的那段 DO 块：
    # 先问「book_id 上现在有没有外键」，再决定要不要建。
    #
    # 判据刻意是「名字 + 删除动作都对」而不只是「有没有」：P3-2 的教训是
    # 「在」不等于「对」，重放时若是同名但动作错了，跳过就等于把错的留下。
    op.execute(
        """
        DO $$
        DECLARE
            v_fk_name   text;
            v_fk_action text;
        BEGIN
            SELECT conname, confdeltype::text
              INTO v_fk_name, v_fk_action
              FROM pg_constraint
             WHERE conrelid = 'report_template'::regclass
               AND contype = 'f'
               AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                                    WHERE attrelid = 'report_template'::regclass
                                      AND attname = 'book_id')];

            IF v_fk_name IS DISTINCT FROM 'fk_report_template_book'
               OR v_fk_action IS DISTINCT FROM 'c' THEN
                IF v_fk_name IS NOT NULL THEN
                    EXECUTE format(
                        'ALTER TABLE report_template DROP CONSTRAINT %I', v_fk_name);
                END IF;
                ALTER TABLE report_template
                    ADD CONSTRAINT fk_report_template_book
                    FOREIGN KEY (book_id) REFERENCES book (id) ON DELETE CASCADE;
            END IF;
        END $$
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_report_template_book_id ON report_template (book_id)")

    # 内置基线保持 NULL：它本来就是所有账套共看的那一份，现在仍然是。
    op.execute("UPDATE report_template SET book_id = NULL WHERE is_builtin")

    # 存量非内置模板按名字认领到星悦荟商管。
    #
    # 走 bind 而不是 op.execute(sql, params)：op.execute 的第二个位置参数是
    # execution_options，不是绑定参数，传字典会直接 TypeError。
    bind = op.get_bind()
    for template_name, book_name in _STOCK_TEMPLATES.items():
        bind.execute(
            text(
                """
                UPDATE report_template SET book_id = (
                    SELECT b.id FROM book b WHERE b.name = :book_name LIMIT 1
                )
                WHERE name = :template_name AND book_id IS NULL AND is_builtin = false
                """
            ),
            {"template_name": template_name, "book_name": book_name},
        )

    # 旧的全局 UNIQUE(code) 换成两条部分唯一索引。
    op.execute("ALTER TABLE report_template DROP CONSTRAINT IF EXISTS report_template_code_key")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_report_template_builtin_code "
        "ON report_template (code) WHERE book_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_report_template_book_code "
        "ON report_template (book_id, code) WHERE book_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_report_template_book_code")
    op.execute("DROP INDEX IF EXISTS uq_report_template_builtin_code")
    op.execute("DROP INDEX IF EXISTS ix_report_template_book_id")
    # 回到「全局唯一 code」之前必须先把重名清掉，否则 ADD CONSTRAINT 直接失败。
    # 同 code 多份时保留 id 最小的一份，其余删掉（它们的行/列/单元格随
    # 外键 CASCADE 一起消失）—— 降级本身就是有损操作，这里只保证能跑通。
    op.execute(
        """
        DELETE FROM report_template t USING report_template keep
        WHERE t.code = keep.code AND t.id > keep.id
        """
    )
    op.execute(
        "ALTER TABLE report_template ADD CONSTRAINT report_template_code_key UNIQUE (code)"
    )
    op.execute("ALTER TABLE report_template DROP CONSTRAINT IF EXISTS fk_report_template_book")
    op.execute("ALTER TABLE report_template DROP COLUMN IF EXISTS book_id")
