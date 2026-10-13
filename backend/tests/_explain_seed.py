"""P2-6 索引收益实测用的造数脚本（只在 cwq_explain_test 上跑，不是测试）。

**造数要够大，否则测不出差别。** 表只有几千行时，PG 直接 Seq Scan 也只要
1~2 ms，两种执行计划的差异被噪声盖住 —— 实测过一次：5.7 万行、
10 个账套时，带不带新索引都是 ``ix_entry_subject_ord`` + Bitmap Index Scan，
Execution Time 5.6ms → 3.2ms，看不出「为什么该加这个索引」。

所以这里按「一个用了十年的账套」的形状造：
  * 热账套 1 个：120 个期间（10 年）× 400 张凭证 × 8 条分录 ≈ 38 万行 entry
  * 冷账套 5 个：各 12 个期间 × 80 张凭证
查询固定取 ``book_id=1 AND period_ord<=12`` —— 也就是「本年度累计」这个
账簿上最常问的问题，它只覆盖热账套 10% 的分录，正是 skip scan 最吃亏、
连续区间索引最占便宜的场景。

用 ``COPY`` 而不是逐行 INSERT：40 多万行逐行插要十几分钟，COPY 几秒钟。

用法： CWQ_EXPLAIN_URL=postgresql+psycopg://... .venv/bin/python tests/_explain_seed.py seed|indexes|explain
"""
from __future__ import annotations

import io
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

URL = os.environ["CWQ_EXPLAIN_URL"]
os.environ["DATABASE_URL"] = URL
os.environ["JWT_SECRET"] = "x"
os.environ["APP_ENV"] = "test"

HOT_PERIODS = 120
HOT_VOUCHERS_PER_PERIOD = 400
COLD_BOOKS = 5
COLD_PERIODS = 120        # 每个账套都用满十年
COLD_VOUCHERS_PER_PERIOD = 80
LINES = 8
SUBJECTS = 40
OPENING_SUBJECTS = 40     # 每个期间给全部科目录期初

BOOKS = [(1, HOT_PERIODS, HOT_VOUCHERS_PER_PERIOD)] + \
        [(i, COLD_PERIODS, COLD_VOUCHERS_PER_PERIOD) for i in range(2, 2 + COLD_BOOKS)]

# 与 ledger/engine.py::_query_movements 同形
MOVEMENT_SQL = """
SELECT e.subject_id, e.period_ord,
       COALESCE(SUM(e.amount) FILTER (WHERE e.direction = 'DEBIT'), 0)  AS dr,
       COALESCE(SUM(e.amount) FILTER (WHERE e.direction = 'CREDIT'), 0) AS cr
  FROM entry e JOIN voucher v ON v.id = e.voucher_id
 WHERE e.book_id = :b AND e.period_ord <= :ord AND v.status = 'POSTED'
 GROUP BY e.subject_id, e.period_ord
"""

# 与 ledger/engine.py::_roll_up_openings 同形
OPENINGS_SQL = """
SELECT po.subject_id, p.ord, po.amount
  FROM period_opening po JOIN period p ON p.id = po.period_id
 WHERE po.book_id = :b
"""

NEW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_entry_book_period ON entry "
    "(book_id, period_ord) INCLUDE (subject_id, direction, amount)",
    "CREATE INDEX IF NOT EXISTS ix_period_opening_book ON period_opening "
    "(book_id, period_id)",
]


def _year_month(ord_: int) -> tuple[int, int]:
    """period.ord = (year - start_year) * 12 + month，所以 120 期 = 10 年。"""
    return 2026 + (ord_ - 1) // 12, (ord_ - 1) % 12 + 1


def _copy(conn, table: str, cols: list[str], rows) -> int:
    """COPY 进表。

    用的是 COPY **文本**格式，所以空值必须写成 ``\\N``（空字段会被当成 NULL，
    在 NOT NULL 的列上直接失败）。必须给 **二进制** 文件对象：
    psycopg3 的 ``copy()`` 也接受 ``str``，
    而 str 是可迭代的 —— 传进去会被逐字符当成一行，于是「成功」返回、
    表里一行都没有。踩过：EXPLAIN 给出 rows=0 的空计划，差点被当成
    「索引没用」。
    """
    buf = io.BytesIO()
    for r in rows:
        buf.write(("\t".join(r) + "\n").encode())
    buf.seek(0)
    # ``cur.copy()`` 返回的是**上下文管理器**：不 with 就不会执行 ——
    # 不报错、也不插任何行，表看起来是灌过了却依然是空的（踩过）。
    with conn.cursor() as cur:
        with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
            cp.write(buf.read())


def seed() -> None:
    import psycopg
    from sqlalchemy import create_engine

    eng = create_engine(URL)
    with eng.begin() as c:
        c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    from app.core.schema_version import upgrade_to_head

    upgrade_to_head(eng)

    # **不走 SQLAlchemy 的连接池。** 池在归还连接时会发 ROLLBACK，
    # 而 COPY 走的是 driver 级 autocommit，于是刚灌进去的 42 万行被回滚掉
    # —— 表看起来建好了、却是空的，EXPLAIN 给出 rows=0 的计划，差点当成
    # 「索引没用」。这里直接开一条裸 psycopg 连接，autocommit 由它自己管。
    conn = psycopg.connect(URL.replace("postgresql+psycopg://", "postgresql://"),
                           autocommit=True)

    with conn.cursor() as cur:
        # 先关掉几条与造数无关的护栏：它们只在**触犯**时才报错，而 COPY
        # 造出来的正是「大量合法但没走业务规则」的行（借方多于贷方等）。
        # 只关这一个库、只关这一次，测完就删库重建。
        # trg_entry_sync 也要关：它要求每条分录的 voucher_id 指向一张
        # 真实凭证，而 COPY 的行是按「先 voucher 后 entry」的顺序灌的 ——
        # 顺序没错，但 COPY 期间外键/触发器的逐行校验本来就是我们要绕开的。
        OFF = ("ALTER TABLE entry DISABLE TRIGGER trg_entry_balance",
               "ALTER TABLE entry DISABLE TRIGGER trg_entry_sync",
               "ALTER TABLE voucher DISABLE TRIGGER trg_voucher_balance",
               "ALTER TABLE voucher DISABLE TRIGGER trg_voucher_guard")
        for stmt in OFF:
            cur.execute(stmt)

        _copy(conn, "book",
              ["id", "company_name", "name", "start_year", "start_month",
               "subject_code_rule", "status", "created_at", "updated_at"],
              [(str(b), f"公司{b}", f"账套{b}", "2026", "1", "4-2-2-2", "ACTIVE",
                "now()", "now()") for b, _, _ in BOOKS])

        periods_base, acc = {}, 0
        prows = []
        for b, nper, _ in BOOKS:
            periods_base[b] = acc
            acc += nper
            for o in range(1, nper + 1):
                y, m = _year_month(o)
                prows.append((str(b), str(y), str(m), str(o)))
        _copy(conn, "period", ["book_id", "year", "month", "ord", "locked",
                               "version", "created_at", "updated_at"],
              [(b, y, m, o, "false", "1", "now()", "now()")
               for b, y, m, o in prows])

        _copy(conn, "subject",
              ["book_id", "code", "name", "parent_id", "level", "category",
               "direction", "is_leaf", "carry_forward", "qty_calc", "aux_calc",
               "fx_calc", "sort_no", "active", "ext_attrs", "created_at", "updated_at"],
              [(str(b), f"{1000 + k}", f"科目{k}", "\\N", "1", "ASSET", "DEBIT",
                "true", "false", "false", "false", "false", "0", "true",
                "{}", "now()", "now()")
               for b, _, _ in BOOKS for k in range(SUBJECTS)])

        vrows, erows, orows = [], [], []
        vid = eid = oid = 0
        for b, nper, vpp in BOOKS:
            for o in range(1, nper + 1):
                y, m = _year_month(o)
                pid = periods_base[b] + o
                for v in range(vpp):
                    vid += 1
                    vrows.append((str(vid), str(b), str(pid), f"记-{v + 1:04d}",
                                  f"{y:04d}-{m:02d}-15", "POSTED", "MANUAL",
                                  "0", "备注", "{}", "now()", "now()"))
                    for ln in range(LINES):
                        eid += 1
                        erows.append((
                            str(eid), str(vid), str(b), str(o), str(ln + 1),
                            str((b - 1) * SUBJECTS + (ln % SUBJECTS) + 1),
                            "DEBIT" if ln % 2 == 0 else "CREDIT", "100.00",
                            "摘要", "{}", "now()", "now()"))
                for k in range(OPENING_SUBJECTS):
                    oid += 1
                    orows.append((str(oid), str(b), str(pid),
                                  str((b - 1) * SUBJECTS + k + 1),
                                  "1000.00", "now()", "now()"))

        _copy(conn, "voucher",
              ["id", "book_id", "period_id", "voucher_no", "voucher_date",
               "status", "source", "attachments", "remark", "ext_attrs",
               "created_at", "updated_at"], vrows)
        _copy(conn, "entry",
              ["id", "voucher_id", "book_id", "period_ord", "line_no",
               "subject_id", "direction", "amount", "summary", "aux_ref",
               "created_at", "updated_at"], erows)
        _copy(conn, "period_opening",
              ["id", "book_id", "period_id", "subject_id", "amount",
               "created_at", "updated_at"], orows)

        cur.execute("ANALYZE")
        cur.execute("ANALYZE")
        for stmt in OFF:
            cur.execute(stmt.replace("DISABLE", "ENABLE"))

    conn.close()
    print(f"seeded: entry={eid} voucher={vid} period_opening={oid} "
          f"books={len(BOOKS)} hot_periods={HOT_PERIODS}")
    eng.dispose()


def indexes(on: bool = True) -> None:
    """建/删 0006 那两条索引。"""
    from sqlalchemy import create_engine, text

    eng = create_engine(URL)
    with eng.begin() as c:
        for stmt in (NEW_INDEXES if on else
                     ["DROP INDEX IF EXISTS ix_period_opening_book",
                      "DROP INDEX IF EXISTS ix_entry_book_period"]):
            c.exec_driver_sql(stmt)
        c.exec_driver_sql("ANALYZE")
    print("indexes:", "ON" if on else "OFF")
    eng.dispose()


def explain(repeats: int = 5) -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(URL)
    with eng.connect() as c:
        for name, sql, p in (
            ("A. 账簿最热聚合 _query_movements(book_id=1, period_ord<=12)",
             MOVEMENT_SQL, {"b": 1, "ord": 12}),
            ("B. 期初汇总 _roll_up_openings(book_id=1)", OPENINGS_SQL, {"b": 1}),
        ):
            print(f"\n===== {name} =====")
            best = None
            for _ in range(repeats):
                t0 = time.perf_counter()
                c.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + sql), p).scalars()
                dt = time.perf_counter() - t0
                best = dt if best is None else min(best, dt)
            print(f"(client wall, best of {repeats}: {best * 1000:.2f} ms)")
            print("\n".join(c.execute(
                text("EXPLAIN (ANALYZE, BUFFERS) " + sql), p).scalars()))
    eng.dispose()


if __name__ == "__main__":
    {"seed": seed, "explain": explain,
     "indexes": lambda: indexes(True), "noindexes": lambda: indexes(False),
     }[sys.argv[1]]()
