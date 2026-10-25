"""给 entry 加 (voucher_id, line_no) 唯一约束，并把「已记账凭证的分录不许动」装到库层。

Revision ID: 0038_entry_voucher_line_unique
Revises: 0037_log_retention_indexes
Create Date: 2026-10-25

**这一条改的是什么。**

两件互相配套的事：

1. ``entry`` 加唯一约束 ``uq_entry_voucher_line_no (voucher_id, line_no)``；
2. 重放 ``db_init.sql``，把「已记账凭证的**分录**不许增删改」那道护栏
   装到 ``entry`` 上 —— 判据在 ``fn_entry_lock_guard`` 的第②段，
   触发器是既有的 ``trg_entry_lock_guard``（BEFORE INSERT OR UPDATE OR DELETE）。

**为什么第 2 件事没有做成一个**新**触发器。**
审计报告 §7 第 14 项的字面建议是「给 ``fn_posted_voucher_guard`` 增加一个
entry 侧的触发器」。实际实现把判据**并进了** ``fn_entry_lock_guard``，
而不是新建一个触发器 —— 理由写在 ``db_init.sql`` 里（函数 ``fn_entry_lock_guard``
第②段的注释）：迁移 0032（科目表按年度拆开）搬分录时会**临时关掉**
``trg_entry_lock_guard``，它要改的正是**已记账**的历史凭证的分录。
两道检查放在同一个函数里，迁移关掉它时一起让路；
分成两个触发器，迁移就只关了①、②照样拦 ——
**迁移会在生产库上当场失败**（跨年账套的次年凭证全是已记账的）。

所以这里是**机制不同、保证等价**：护栏照样钉在 ``entry`` 上、照样在库层生效，
只是搭在既有的触发器上，而不是新建一个。

**为什么第 1 件事是必须的（审计报告 §3.15）。**

以前同一张凭证里可以挂两条 ``line_no=1``，而且**没有任何一层会报错**。
它是怎么来的：两个人同时改同一张凭证，B 会话按自己那份**陈旧**的分录集合
「删旧插新」，而 A 会话已经把那几行删掉了 —— B 的 DELETE 命中 0 行，
B 的新行照样插进去。于是这张凭证有 4 条分录、两组重复的行号，
同一笔钱记了两遍，而**借贷仍然平**：

    借方合计 / 贷方合计 ：3000.00 / 3000.00   借贷仍平衡: True

于是所有自检都抓不到它 —— 库层的借贷平衡触发器过、试算平衡过、
科目余额表过、总账过、「整理就绪」体检也过（它只查号段连续性）。
**数字是错的，但没有任何一处会报错。** 有这条唯一约束之后，同样的交错
会在库层炸成 23505（一次**看得见**的失败），而不是留下一张金额记了两遍的凭证。

**为什么第 2 件事是必须的（同一份报告的同一节）。**

``fn_posted_voucher_guard`` 挂在 ``voucher`` 的 BEFORE UPDATE 上，
**只有当 UPDATE 语句真的产生时才执行**。而改凭证走的是
「DELETE 旧 entry 行 + INSERT 新 entry 行」，voucher 行往往没有任何净变化
（status 本来就是 UNPOSTED，又被重新赋成 UNPOSTED，SQLAlchemy 眼里
「没变」→ 根本不生成 UPDATE 语句）—— 于是那道守卫**一次都没跑**。
已记账凭证的金额因此能被**静默改写**：借贷平、试算平、科目余额、总账、
体检全过，而账上的数就是错的。

所以护栏要落在**分录**上：分录才是账上数字的真正载体。
「已记账凭证的分录不许动」这句话必须在这张表上兑现，
哪怕应用层被绕过（psql、脚本、未来的运维命令）也一样。

**为什么先查重复再加约束。**

审计报告明确指出：**活库里可能已经有重复行**（那正是这个缺陷的症状）。
``ALTER TABLE ... ADD CONSTRAINT`` 撞上重复行时，PostgreSQL 报的是一句
英文的 ``could not create unique index ... duplicate key value``，
里面只有约束名和重复的那几个值 —— 运维看到它不知道该去清理哪几张凭证、
更不知道「重复行」意味着账上那笔钱被记了两遍。

所以这里**先自己查一遍**，把话说清楚再拒绝：列出每张出问题的凭证
（voucher_id）与重复的行号，并说清这些行是怎么来的、该怎么处理。
**清理数据是加约束的前置条件**（审计报告 §7 第 1 步第 11 项），
不是「迁移顺手替人清理」—— 哪一条重复该留、哪一条该改金额，
只有懂业务的人能决定。

**降级。**

唯一约束可以删（它不是护栏，是一条不变量）。而 ``db_init.sql`` 里那道
触发器**不撤销** —— 与 ``0033_book_year_guardrails`` 同一个口径：
护栏与 0001 装上去的是同一批，拆掉一半会得到一个既不是旧版本、
也不是新版本的库；回退等于把已知缺陷装回去。
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0038_entry_voucher_line_unique"
down_revision = "0037_log_retention_indexes"
branch_labels = None
depends_on = None

# 与 0001 / 0005 / 0010 / 0031 / 0033 同一个位置、同一份文件。迁移读它而不是
# 复制一份 —— 复制就会分叉，而分叉正是审计反复点名的病根（工程域 P3-2）。
SQL_FILE = Path(__file__).resolve().parents[2] / "app" / "core" / "db_init.sql"

CONSTRAINT_NAME = "uq_entry_voucher_line_no"


def _add_constraint_if_missing(table: str, name: str, definition: str) -> None:
    """约束不存在才加 —— **重复执行不变**。

    ``ALTER TABLE ... ADD CONSTRAINT`` 没有 ``IF NOT EXISTS``，
    同一条迁移再跑一次就是一条 ``DuplicateTable``，整条链断在半路。
    迁移链的测试会把 ``alembic_version`` 打回 0004 再重跑整条链
    （见 ``tests/test_migration_guardrails.py``），所以每一条 revision
    都得受得住「在同一份数据上再跑一次」。

    判存在用 ``pg_constraint`` 配 ``to_regclass``：后者走的是与
    ``ALTER TABLE <table>`` **同一套 search_path 解析** —— 迁移测试用独立
    schema 当「另一个数据库」，用 ``information_schema`` 会看见别的 schema
    下同名但不同样的表。
    """
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = to_regclass('{table}') AND conname = '{name}'
            ) THEN
                EXECUTE 'ALTER TABLE {table} ADD CONSTRAINT {name} {definition}';
            END IF;
        END $$;
    """)


def _duplicate_line_no(bind=None) -> list[tuple[int, list[int], int]]:
    """找出重复的 (voucher_id, line_no)，返回 [(voucher_id, 行号列表, 份数)]。

    **判据本身在** ``app.accounting.data_health.duplicate_line_no`` **里**，
    这里只是一层壳：把「没给 bind 就用 alembic 当前那条连接」这件事
    翻译一下。

    **为什么不把查询抄一份在这里（审计报告 §4.5）。**
    这个判据在这条链上要出现在三处：本迁移的前置检查、上线前体检脚本
    （``scripts/data_health_check.py``）、以及用例。抄一份就有三份，
    三份会分叉，而分叉出来的那两份会**安静地**给出不同答案
    （有的说重复、有的说干净）。这里出错的代价不是报错，是
    **账上的数被记了两遍却没有任何一处提示** —— §3.15 那个场景里
    借贷仍然平、试算平、科目余额平、总账平、「整理就绪」体检也过，
    全绿，但数字是错的。

    所以三处都调同一份实现（迁移读它而不是复制一份 —— 与 ``db_init.sql``
    同一个口径：复制就会分叉，而分叉正是审计反复点名的病根）。
    """
    from app.accounting.data_health import duplicate_line_no

    bind = op.get_bind() if bind is None else bind
    return duplicate_line_no(bind)


def _preflight(bind=None) -> None:
    """重复行号检查：有问题就**大声失败**，并说清是哪几张凭证、哪几个行号。

    ``ALTER TABLE ... ADD CONSTRAINT`` 撞上重复行时，PostgreSQL 报的是一句
    英文的 ``could not create unique index ... duplicate key value``，
    里面只有约束名和重复的那几个值 —— 运维看到它不知道该去清理哪几张凭证、
    更不知道「重复行」意味着账上那笔钱被记了两遍。

    所以这里**先自己查一遍**，把话说清楚再拒绝：列出每张出问题的凭证
    （voucher_id）与重复的行号，并说清这些行是怎么来的、该怎么处理。
    **清理数据是加约束的前置条件**（审计报告 §7 第 1 步第 11 项），
    不是「迁移顺手替人清理」—— 哪一条重复该留、哪一条该改金额，
    只有懂业务的人能决定。

    **文案与上线前体检脚本共用一份**（``app.accounting.data_health``）：
    同一件事的说明写两遍，两遍就会慢慢不一样，而这段话是给不懂技术的人看的。
    """
    from app.accounting.data_health import (
        DUPLICATE_HOW_IT_HAPPENED,
        DUPLICATE_HOW_TO_FIX,
        describe_duplicate,
    )

    dups = _duplicate_line_no(bind)
    if not dups:
        return
    detail = "；".join(describe_duplicate(d) for d in dups)
    raise RuntimeError(
        f"库里已经有重复的分录行号，无法加唯一约束 {CONSTRAINT_NAME}。\n"
        f"重复明细：{detail}\n"
        f"{DUPLICATE_HOW_IT_HAPPENED}\n"
        f"{DUPLICATE_HOW_TO_FIX}"
        "清理完再重新执行本条迁移。"
    )


def upgrade() -> None:
    # ---- 前置检查：库里已经有重复行号的话，**先说清楚再拒绝** ----
    _preflight()

    _add_constraint_if_missing(
        "entry", CONSTRAINT_NAME, "UNIQUE (voucher_id, line_no)")

    # ---- 重放 db_init.sql：装上「已记账凭证的分录不许动」那道护栏 ----
    #
    # db_init.sql 是一整段 psql 风格的 DDL（大量分号与 $$ 引用），走 SQLAlchemy 的
    # 参数化通道会被 psycopg 当占位符解析，所以用裸 DBAPI 游标整段执行。
    # 和 0001 / 0005 / 0033 用的是同一条路径 —— 这正是「两条路径不留分叉」的含义。
    raw = op.get_bind().connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(f"ALTER TABLE entry DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME}")
    # **触发器不撤销**（理由见模块 docstring，与 0033 同口径）：
    # 它是 db_init.sql 交付的护栏，与 0001 装上去的是同一批；
    # 拆掉一半会得到一个既不是旧版本、也不是新版本的库。
