"""账套数据版本号：跨请求聚合缓存的失效依据（2026-10 性能收尾 P0）

Revision ID: 0015_book_data_version
Revises: 0014_official_template
Create Date: 2026-10-15

**这个版本号是给什么用的。**

``app/ledger/engine.py`` 的 ``LedgerEngine`` 是单次请求内的只读计算器，
期初汇总与发生额聚合都在实例内记忆化。压测（``压测基线报告_2026-10-15.md``）
量出来一次明细账请求的 85.9ms 里有 77.7ms 花在全账套聚合上，而多个用户
同时看同一个账套时，这些聚合**算的是同一份数**。

要跨请求复用，就得回答一个问题：**什么时候算的东西过期了？**

**为什么不用 TTL。** 这是本条 revision 存在的前提，值得写清楚：

README 的核心承诺是「凭证和分录是唯一事实来源，余额、账簿、报表、打印全部
由同一套聚合引擎派生。因此不存在『某个报表和总账对不上』这种事」。
TTL 缓存直接违反这条承诺 —— 用户刚记完一张凭证，立刻刷新总账，看到的还是
记之前的数，而且在几秒内一直是这样。对记账系统来说这不是「稍微不准」，
这是「刚保存的东西没生效」，用户会反复点保存、怀疑系统坏了，甚至照着旧数
去做决定。

**所以用「数据库里那个单调递增的整数」当缓存键的一部分。**

* 任何影响聚合的写（凭证、分录、科目、期初、期间）都在**同一个事务里**
  把 ``book.data_version`` +1；
* 引擎构造时**先读版本号**，再用它当缓存键；
* 版本号只增不减，所以旧版本的键一旦失效就再也不会被读到。

于是保证的是：写事务提交之后，**任何在此之后开始的请求**一定读到新数据
（read-your-writes），且多个 worker 之间天然一致（版本号在库里，不在进程里）。

**为什么必须是语句级触发器，不是行级。** 这是本条 revision 最容易被人
「优化」掉的地方，所以单独说：

行级触发器会在每插入/更新/删除**一行**时执行一次。一次 5000 行的凭证导入
就是 5000 次对 ``book`` 行的 UPDATE —— 而 ``book`` 行是**整个账套共用**的一行，
这些 UPDATE 会互相排队。本条 revision本来的目的是让读变快，结果给写加了一
个串行瓶颈。

语句级触发器（``FOR EACH STATEMENT``）配合过渡表（``REFERENCING ... TABLE``）
只在**每条语句**执行一次，且靠过渡表拿到受影响的 book_id。5000 行导入 = 1 次。

代价是语法上必须写三个触发器而不是一个：``INSERT`` 只能引用 ``NEW TABLE``、
``DELETE`` 只能引用 ``OLD TABLE``、``UPDATE`` 两者皆可。

``tests/test_data_version.py::test_版本号每条语句只加一次`` 钉住了这条 ——
将来有人「顺手改成行级」，那条会立刻红。

**锁的代价说清楚。** 同一账套的并发写会在 ``book`` 行上短暂串行。这是设计上的
有意取舍：用一点写吞吐换全局读正确性。实测口径见 ``deploy/tuning.md``；
写路径压测见 ``scripts/loadtest.py --scenario import-commit``。

**降级是安全的。** downgrade 删掉列与触发器，引擎就退回纯实例级缓存 ——
行为回到本条之前，只是慢一点，不算错。
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0015_book_data_version"
down_revision = "0014_official_template"
branch_labels = None
depends_on = None

# 覆盖这五张表：任何一张的写入都会改变聚合结果或账簿结构。
# * voucher / entry —— 余额与发生额
# * subject        —— 科目树（上卷）与类别
# * period_opening —— 期初余额
# * period         —— 期间顺序与结账状态
BUMPED_TABLES = ("voucher", "entry", "subject", "period_opening", "period")

FN_NAME = "fn_bump_book_data_version"

# 每个 (表, 事件) 一个触发器：过渡表的名字由 REFERENCING 决定，
# 函数体里统一叫 book_impact，三种写法指向同一个名字。
TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
DECLARE
    v_book bigint;
BEGIN
    -- 一条语句理论上可以影响多个账套（跨账套批量操作），
    -- 所以是 DISTINCT + 循环，不是 SELECT INTO 单值。
    FOR v_book IN SELECT DISTINCT book_id FROM book_impact LOOP
        UPDATE book SET data_version = data_version + 1 WHERE id = v_book;
    END LOOP;
    RETURN NULL;   -- 语句级触发器必须返回 NULL（返回 NULL 表示「不中断」）
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute("ALTER TABLE book ADD COLUMN IF NOT EXISTS data_version bigint NOT NULL DEFAULT 0")

    op.execute(TRIGGER_SQL.format(fn=FN_NAME))

    for table in BUMPED_TABLES:
        # **先 DROP 再 CREATE，不是多余的。** `0005_db_guardrails` 会把整份
        # `db_init.sql` 在存量库上重放一遍，而本段也已经写进那份 SQL 了 ——
        # 所以跑到 0015 时触发器**通常已经存在**。直接 CREATE 会撞
        # DuplicateObject。
        #
        # 这也正是 0005 docstring 里那条「手抄会分叉」的教训：既然两边是同一份
        # 事实来源（db_init.sql），那本条 migration 就必须按「重放也可能已经
        # 装上了」的假设来写，而不是假设自己是第一个。
        for suffix in ("ins", "upd", "del"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_dv_{suffix} ON {table}")

    for table in BUMPED_TABLES:
        # INSERT：只有 NEW TABLE 可引
        op.execute(f"""
        CREATE TRIGGER trg_{table}_dv_ins
        AFTER INSERT ON {table}
        REFERENCING NEW TABLE AS book_impact
        FOR EACH STATEMENT EXECUTE FUNCTION {FN_NAME}();
        """)
        # UPDATE：OLD / NEW 都可引，用 OLD（被改掉的那些行）
        op.execute(f"""
        CREATE TRIGGER trg_{table}_dv_upd
        AFTER UPDATE ON {table}
        REFERENCING OLD TABLE AS book_impact
        FOR EACH STATEMENT EXECUTE FUNCTION {FN_NAME}();
        """)
        # DELETE：只有 OLD TABLE 可引
        op.execute(f"""
        CREATE TRIGGER trg_{table}_dv_del
        AFTER DELETE ON {table}
        REFERENCING OLD TABLE AS book_impact
        FOR EACH STATEMENT EXECUTE FUNCTION {FN_NAME}();
        """)

    # 存量行的版本号从 0 起。**不是 bug**：0 也是一个合法的版本号，
    # 它的含义是「从本条 migration 生效起算，一次写都没有过」。
    # 引擎不区分 0 和 1，它只比较相等与否。
    # 刻意**不**设成 1：没有理由假装发生过写。
    op.execute(text("SELECT 1"))


def downgrade() -> None:
    for table in BUMPED_TABLES:
        for suffix in ("ins", "upd", "del"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_dv_{suffix} ON {table}")
    op.execute(f"DROP FUNCTION IF EXISTS {FN_NAME}()")
    op.execute("ALTER TABLE book DROP COLUMN IF EXISTS data_version")
