"""数据层三条整改的钉子（审计 2026-10-14）。

这份文件钉三件事，各自对应一条迁移 / 一处模型改动：

1. ``entry`` 上终于有一条**以 subject_id 为首列**的索引，
   ``import_batch`` 上终于不再是「只有主键」。既断言索引真的送到了库
   （迁移跑了），也断言 ORM 侧声明了**同名同列**的 Index ——
   后者才是 autogenerate 不会生成 DROP/CREATE 来「修」差异的原因。
2. 三条查询形状（凭证按科目筛选 / 导入历史列表 / 过期批次清理）
   各自 EXPLAIN 出预期的索引，而不是退化成 Seq Scan。
3. 账簿引擎把 ``_figures`` 从 O(科目数 × 期间数) 收紧到 O(科目数) 之后，
   **逐期数值一个都没动** —— 包括跨年归零与「账套首期之前」那两条分支。

**为什么 EXPLAIN 断言要显式关掉 seqscan。** 本机测试库只有几千行，
PG 在那个量级上 Seq Scan 与索引扫描的差别在噪声里（同 ``tests/_explain_seed.py``
开头那段说明：造数不够大就测不出差别）。所以这里断言的是
「**存在一条能用的索引，优化器可以选它**」，用 ``enable_seqscan = off``
把这个选择变成确定的 —— 否则这条用例会因为「表太小」而变红，
而那不是回归。真实量级下的计划由 ``tests/test_perf_data_layer.py``
末尾那条连开发库的用例负责（未升级到 0011 时自动跳过）。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from decimal import Decimal

import pytest
import sqlalchemy
from sqlalchemy import select, text

from app.core.db import engine as app_engine
from app.ledger.engine import ZERO, Figures, LedgerEngine
from app.models import Book, Entry, ImportBatch, Period, PeriodOpening, Subject
from app.models.book import book_first_ord

# 开发库：只读连接，仅用于末尾那条「真实数据上 EXPLAIN」的用例。
# 没连上 / 没升级到 0011 时整条跳过 —— 绝不在这里建索引或写数据。
DEV_DB_URL = os.environ.get(
    "CWQ_DEV_DATABASE_URL", "postgresql+psycopg://cwq:cwq123@localhost:15432/cwq")
DEV_BOOK_ID = 59

# 三条查询形状，与被服务的调用点一一对应。
# 1. accounting/vouchers.py::list_vouchers 的「按科目筛选凭证」子查询
SUBJECT_FILTER_SQL = (
    "SELECT e.voucher_id FROM entry e WHERE e.subject_id = :sid")
# 2. api/vouchers.py 的导入历史列表：WHERE book_id = ? ORDER BY id DESC LIMIT 100
BATCH_LIST_SQL = (
    "SELECT b.id FROM import_batch b WHERE b.book_id = :bid "
    "ORDER BY b.id DESC LIMIT 100")
# 3. accounting/importer.py::purge_stale_batches
BATCH_TTL_SQL = (
    "DELETE FROM import_batch WHERE status = 'PENDING' AND created_at < :cutoff")


def _plan(db, sql: str, params: dict, *, force_index: bool = True,
          no_bitmap: bool = False) -> list[str]:
    """跑一条 EXPLAIN 并返回计划文本。

    ``force_index=True`` 时关掉 seqscan：断言的是「**存在一条能用的索引**」，
    而测试库只有几千行、PG 在那个量级上选 Seq Scan 与索引扫描纯属噪声。
    关掉只是把这个选择固定住，免得用例随统计信息抖动（VACUUM 一次就翻面）。

    ``no_bitmap=True`` 时连 bitmap 路径也关掉：有些断言要钉的是**计划的形状**
    （走索引、且不用回堆），而 bitmap 扫描与索引扫描的**成本可能只差 0.01**
    （实测 8.14 vs 8.15），谁赢取决于当轮库里累积的物理状态 ——
    留着它就等于把断言交给运气。
    """
    if force_index:
        db.execute(text("SET enable_seqscan = off"))
    if no_bitmap:
        db.execute(text("SET enable_bitmapscan = off"))
    try:
        return [r[0] for r in db.execute(text("EXPLAIN " + sql), params)]
    finally:
        if no_bitmap:
            db.execute(text("SET enable_bitmapscan = on"))
        if force_index:
            db.execute(text("SET enable_seqscan = on"))


# --------------------------------------------------------------------- 索引到位


def _db_index_columns(db, table: str, name: str) -> list[str]:
    """从库里读回一条索引的列序（剥掉括号与 INCLUDE 尾巴）。"""
    rows = db.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :n"
    ), {"t": table, "n": name}).scalars().all()
    assert len(rows) == 1, f"{table}.{name} 不存在（pg_indexes 查到 {len(rows)} 条）"
    head = rows[0].split(" USING btree ", 1)[1].split(" INCLUDE", 1)[0]
    return [c.strip() for c in head.strip("()").split(",")]


def test_迁移把三条索引送到了库(db):
    """迁移链跑完，三条索引都在，列序与设计一致。"""
    assert _db_index_columns(db, "entry", "ix_entry_subject_voucher") == \
        ["subject_id", "voucher_id"]
    assert _db_index_columns(db, "import_batch", "ix_import_batch_book_id") == \
        ["book_id", "id"]
    # 等值列在前、范围列在后 —— 反过来就只剩「扫时间再滤状态」
    assert _db_index_columns(db, "import_batch", "ix_import_batch_status_created_at") == \
        ["status", "created_at"]


def test_ORM侧声明了同名同列的Index(db):
    """模型不声明，autogenerate 就会生成 DROP/CREATE 来「修」一个并不存在的差异。

    这正是 0006 docstring 里写的那条规矩：迁移建了什么，模型就得原样声明什么。
    这里断言声明本身，不连库 —— 模型与库的一致性由上一条和
    ``test_migration_guardrails.py`` 一起兜着。
    """
    declared = {
        (idx.name,): [c.name for c in idx.columns]
        for tbl in (Entry.__table__, ImportBatch.__table__)
        for idx in tbl.indexes
    }
    assert declared.get(("ix_entry_subject_voucher",)) == ["subject_id", "voucher_id"]
    assert declared.get(("ix_import_batch_book_id",)) == ["book_id", "id"]
    assert declared.get(("ix_import_batch_status_created_at",)) == \
        ["status", "created_at"]


# --------------------------------------------------------------------- 执行计划


def test_按科目筛选凭证不再全表扫(db, book, subjects, make_voucher):
    """accounting/vouchers.py 的「按科目筛选」子查询现在有索引可用。

    这是本条迁移要修的那一处：改动前 entry 上没有任何以 subject_id 打头的
    索引，这条子查询只能 Seq Scan（开发库实测：读 580 行、丢 438 行）。
    """
    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)

    plan = "\n".join(_plan(db, SUBJECT_FILTER_SQL,
                           {"sid": subjects["1001"].id}, force_index=True))
    assert "ix_entry_subject_voucher" in plan, (
        "以 subject_id 打头的索引没有被选中：\n" + plan)
    assert "Seq Scan" not in plan, plan


def test_凭证按科目筛选不再需要回堆(db, book, subjects, make_voucher):
    """第二列带 voucher_id 是为了 Index Only：子查询的输出列就在索引里。

    这条断言的是**收益**而不是「有索引就行」—— 单列 subject_id 索引同样
    能过上一条，却要多一次 heap fetch 才能拿到 voucher_id。

    **这条用例曾经时好时坏，原因不在「有没有索引」，而在「优化器在两条
    成本几乎一样的路里挑了哪条」。** 目标形状有两条候选：``Index Only Scan``
    （voucher_id 就在索引里，不用回堆）和 ``Bitmap Heap Scan``（同一个索引，
    但要回堆取页）。两条路的价格随**命中页在可见性图里是不是 all-visible**
    摆动：页不是 all-visible 时，优化器给 Index Only 补一笔「回堆取数」的
    估算成本，合计 **8.14**，而 Bitmap Heap Scan 是 **8.15** —— **只差 0.01**，
    谁赢取决于当轮库里累积的物理状态；页碰巧是 all-visible 时 Index Only
    降到 4.15，余量变成 2 倍，于是又稳稳地赢。这就是「单跑永远绿、全量跑
    时好时坏」的来源。

    **为什么不能靠 VACUUM 把「页 all-visible」这个前提做实。** 夹具每条用例
    前会把业务表整表 DELETE 干净（conftest 里「为什么不用 TRUNCATE」那段：
    外键是 NO ACTION/RESTRICT，只能按依赖顺序逐表删）。清库之后这条表被
    截成 0 页，新灌进去的两行落在一页**全新的页**上，那一页上没有死元组
    可剪，VACUUM 也就没有理由给它打 all-visible 标记。实测在失败那一轮：
    VACUUM 之后 ``pg_class.relallvisible`` 仍是 0，
    ``pg_visibility_map('entry')`` 里第 0 页仍是 ``all_visible=False``。
    更要紧的是 **REINDEX 重建的是索引，碰不到堆的可见性图** ——
    2026-10 那次「加 REINDEX 就好了」的修复其实没修掉偶发红，只是把症状
    推迟了（当年 docstring 里「死元组卡在页中间 VACUUM 清不掉」是误诊）。

    **所以这里换一种钉法：把优化器的竞争路径摘掉，而不是放宽期望。**
    ``_plan(..., no_bitmap=True)`` 关掉 bitmap 这条路，于是计划形状恒定：
    索引覆盖输出列时是 ``Index Only Scan``（不回堆，正是这条用例要的收益），
    索引退回单列时是 ``Index Scan``（要回堆，正是要防的那个退化）——
    **索引缺列这个回归照样会红**，期望值没有被削弱。

    VACUUM 不能跑在 ``db`` 夹具的事务里，自己开一条 autocommit 连接。
    """
    import sqlalchemy as sa

    make_voucher("2026-01-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)

    vacuum = sa.create_engine(app_engine.url, isolation_level="AUTOCOMMIT")
    try:
        with vacuum.connect() as c:
            # ANALYZE：刷新 planner 的行数/页数估算，让计划基于刚灌进去的
            # 这两行，而不是上一轮清库之后的旧统计。
            c.execute(text("VACUUM (ANALYZE) entry"))
            # REINDEX：把清库留下的索引膨胀清掉、把索引页数刷成真实值。
            # 注意它**只重建索引**，碰不到堆的可见性图 —— 别再把它当成
            # 「让页 all-visible」的手段。
            c.execute(text("REINDEX TABLE entry"))
    finally:
        vacuum.dispose()

    plan = "\n".join(_plan(db, SUBJECT_FILTER_SQL,
                           {"sid": subjects["1001"].id}, force_index=True,
                           no_bitmap=True))
    assert "Index Only Scan" in plan, (
        "子查询没能 Index Only，说明白带了 voucher_id 的收益没拿到：\n" + plan)


def test_导入历史列表走book_id加id的索引(db, book):
    """`WHERE book_id = ? ORDER BY id DESC LIMIT 100` 顺着索引取，不用再排一遍。"""
    plan = "\n".join(_plan(db, BATCH_LIST_SQL, {"bid": book.id}, force_index=True))
    assert "ix_import_batch_book_id" in plan, plan
    assert "Seq Scan" not in plan, plan
    # 索引列序 (book_id, id) 的意义就在这里：反向扫索引天然就是 id DESC，
    # LIMIT 100 可以取满就停，不必把该账套的所有批次都拉出来再排。
    assert "Sort" not in plan, (
        "明明能靠索引顺序免掉排序却还是排了，说明列序没起作用：\n" + plan)


def test_过期批次清理走status加created_at的索引(db):
    """`WHERE status = 'PENDING' AND created_at < ?`：等值在前、范围在后。"""
    plan = "\n".join(_plan(db, BATCH_TTL_SQL, {"cutoff": "2099-01-01"}, force_index=True))
    assert "ix_import_batch_status_created_at" in plan, plan
    assert "Seq Scan" not in plan, plan


# --------------------------------------------------------------------- 引擎记忆化收紧


def _reference_all_periods(eng: LedgerEngine, sids: list[int],
                           top: int) -> dict[tuple[int, int], Figures]:
    """**改动前**的逐期推进：每一期都留一张 Figures。

    这是旧算法本身，不是另一个模型 —— 目的就是让新引擎逐期对上它，
    证明「只留最后一期」没有动过任何一个数字。逐字照抄 engine.compute()
    改动前的循环体。
    """
    start = book_first_ord(eng._book())
    moves = eng._cum_movements(top)
    snapshots = eng._openings()
    out: dict[tuple[int, int], Figures] = {}
    for sid in sids:
        opening = snapshots.get((sid, start), ZERO)
        dr_cum = cr_cum = ZERO
        for o in range(start, top + 1):
            if (sid, o) in snapshots:
                opening = snapshots[(sid, o)]
            if o > start and (o - 1) % 12 == 0:
                dr_cum = cr_cum = ZERO
            dr, cr = moves.get((sid, o), (ZERO, ZERO))
            dr_cum += dr
            cr_cum += cr
            out[(sid, o)] = Figures(
                subject_id=sid, ord=o, opening=opening,
                dr_cum=dr_cum, cr_cum=cr_cum, period_dr=dr, period_cr=cr)
            opening = opening + dr - cr
    return out


def _same(a: Figures, b: Figures, what: str) -> None:
    assert (a.ord, a.opening, a.dr_cum, a.cr_cum, a.period_dr, a.period_cr, a.closing) == \
           (b.ord, b.opening, b.dr_cum, b.cr_cum, b.period_dr, b.period_cr, b.closing), \
           f"{what}：引擎算出来的和改动前的逐期推进对不上"


def test_逐期数值与改动前完全一致(db, book, periods, subjects, make_voucher):
    """同一批数据下，新引擎每一期的五个量都与旧算法逐位相等。"""
    db.add(PeriodOpening(book_id=book.id, period_id=periods[1].id,
                         subject_id=subjects["1001"].id, amount=Decimal("1000")))
    db.commit()
    for m, amt in ((1, 500), (3, 300), (3, 200), (7, 900), (12, 50)):
        make_voucher(f"2026-{m:02d}-10",
                     dr=[("1001", amt, "收")], cr=[("5001", amt, "收")], posted=True)

    ids = [subjects[c].id for c in ("1001", "1002", "5001", "5602", "1122")]
    top = periods[12].ord

    ref = _reference_all_periods(LedgerEngine(db, book.id), ids, top)
    eng = LedgerEngine(db, book.id)
    # 先批量推到最后一期，再用 figures() 取**中间**各期 ——
    # 这正是收紧记忆化之后唯一可能算错的那条路径。
    eng.batch_figures(ids, top)
    for sid in ids:
        for o in sorted({k[1] for k in ref if k[0] == sid}):
            _same(eng.figures(sid, o), ref[(sid, o)], f"subject={sid} ord={o}")


def test_跨年时本年累计归零且数值不变(db, book, subjects, make_voucher):
    """`o > start and (o - 1) % 12 == 0` 这条归零分支不能动。

    ord 是跨年度连续的，2027 年 1 月（ord=13）的「本年累计」必须只算 2027 年。
    """
    from app.accounting.periods import ensure_periods
    from app.accounting.years import copy_chart
    ensure_periods(db, book, 2027)
    # **2027 年也要有自己的科目表**（科目表每个年度各有一份，迁移 0032）：
    # 2027 年的凭证挂的是 2027 那张表上的科目，2026 年的 id 在 2027 年
    # 根本不存在。
    copy_chart(db, book.id, 2026, 2027)
    db.commit()
    per = {p.ord: p for p in db.scalars(
        select(Period).where(Period.book_id == book.id)
        .order_by(Period.ord)).all()}
    # 2026 年有发生额，2027 年也有：归零分支一旦失效，2027 年的
    # dr_cum 会把 2026 全年算进去，而这种错在「列与列之间」是自洽的。
    # 两个年份都用**借方**，这样归零有没有生效直接看 dr_cum 的数值。
    make_voucher("2026-11-10", dr=[("5001", 700, "2026收入")],
                 cr=[("1001", 700, "2026收入")], posted=True)
    make_voucher("2027-02-10", dr=[("5001", 300, "2027收入")],
                 cr=[("1001", 300, "2027收入")], posted=True)

    def sid_of(code, year):
        return db.scalar(select(Subject.id).where(
            Subject.book_id == book.id, Subject.year == year, Subject.code == code))

    sid26 = sid_of("5001", 2026)
    sid27 = sid_of("5001", 2027)
    dec26 = per[12].ord          # 2026-12
    jan27 = per[13].ord          # 2027-01 —— 归零就发生在这里
    feb27 = per[14].ord          # 2027-02
    ids = [sid_of(c, y) for y in (2026, 2027) for c in ("5001", "1001")]

    ref = _reference_all_periods(LedgerEngine(db, book.id), ids, feb27)
    eng = LedgerEngine(db, book.id)
    eng.batch_figures(ids, feb27)

    assert eng.figures(sid26, dec26).dr_cum == Decimal("700")
    # 2026 那张表上的科目在 2027 年的序上，本年累计必须**已经归零** ——
    # 归零分支一旦失效，这里会变成 700。
    assert eng.figures(sid26, feb27).dr_cum == Decimal("0"), \
        "2026 那张表在 2027 年的本年累计没有归零"
    assert eng.figures(sid27, feb27).dr_cum == Decimal("300"), \
        "2027 年 2 月的本年借方累计不是 300 —— 2026 年的 700 被算进来了"
    assert eng.figures(sid27, jan27).dr_cum == ZERO, "2027 年 1 月应归零"
    for s in ids:
        for o in (dec26, jan27, feb27):
            _same(eng.figures(s, o), ref[(s, o)], f"跨年 subject={s} ord={o}")


def test_账套首期之前那张零发生额图不变(db, book, subjects, make_voucher):
    """P0-03 的正确性契约：区间查询取「起始期上一期的期末」靠的就是它。

    ``before_book`` 分支返回的必须是零发生额、零累计、期初 = 首期的期初。
    """
    first = db.scalars(
        select(Period).where(Period.book_id == book.id).order_by(Period.ord).limit(1)
    ).one()
    db.add(PeriodOpening(book_id=book.id, period_id=first.id,
                         subject_id=subjects["1001"].id, amount=Decimal("8888")))
    db.commit()
    make_voucher("2026-02-10", dr=[("1001", 100, "x")], cr=[("5001", 100, "x")],
                 posted=True)

    sid = subjects["1001"].id
    start = book_first_ord(db.get(Book, book.id))
    eng = LedgerEngine(db, book.id)

    # 2026-01（ord=1）是首期，ord=0 就是「首期之前」
    f = eng.figures(sid, start - 1)
    assert f.ord == start - 1
    assert f.opening == Decimal("8888")
    assert (f.dr_cum, f.cr_cum, f.period_dr, f.period_cr, f.closing) == \
           (ZERO, ZERO, ZERO, ZERO, Decimal("8888"))
    # 契约本身：首期的期初 == 首期上一期的期末
    assert f.closing == eng.figures(sid, start).opening

    # 再取一次仍是同一张（记忆化不能把它算成别的）
    _same(eng.figures(sid, start - 1), f, "before_book 重复取")


def test_只留最后一期后内存是O科目数(db, book, periods, subjects, make_voucher):
    """峰值从 O(科目数 × 期间数) 降到 O(科目数)。

    12 个期间 × 5 个科目：改动前是 60 个 Figures，现在是 5 个。
    """
    ids = [subjects[c].id for c in ("1001", "1002", "5001", "5602", "1122")]
    eng = LedgerEngine(db, book.id)
    eng.batch_figures(ids, periods[12].ord)
    assert len(eng._figures) == len(ids), (
        f"每个科目只该留最后一期，实际留了 {len(eng._figures)} 个（12 期 × {len(ids)} 科目）")
    assert len(eng._computed_through) == len(ids)


def test_重复请求同一科目不重推(db, book, periods, subjects, make_voucher):
    """``_computed_through`` 的幂等语义不能因为收紧记忆化而失效。"""
    ids = [subjects[c].id for c in ("1001", "5001")]
    eng = LedgerEngine(db, book.id)
    top = periods[12].ord
    first = eng.compute(ids, top)
    seen = dict(eng._computed_through)
    second = eng.compute(ids, top)
    assert eng._computed_through == seen
    for sid in ids:
        _same(second[sid], first[sid], f"重复 compute subject={sid}")


@contextmanager
def _statements():
    """收集本次调用期间真正发出去的 SQL（与 test_ledger_query_count 同一种计数）。"""
    stmts: list[str] = []
    handler = lambda conn, cursor, statement, params, ctx, many: stmts.append(statement)
    sqlalchemy.event.listen(app_engine, "before_cursor_execute", handler)
    try:
        yield stmts
    finally:
        sqlalchemy.event.remove(app_engine, "before_cursor_execute", handler)


def test_取中间期重推不发SQL(db, book, periods, subjects, make_voucher):
    """收紧记忆化的代价是「中间期重推」——但必须是纯算术，一条 SQL 都不补。

    期初快照、发生额聚合、科目树都已在实例内；``_cum_movements`` 对更小的
    上界只过滤不重查（engine.py 里那条记忆化的原话）。
    """
    ids = [subjects[c].id for c in ("1001", "5001")]
    eng = LedgerEngine(db, book.id)
    with _statements() as stmts:
        eng.batch_figures(ids, periods[12].ord)
        base = len(stmts)
        eng.figures(ids[0], periods[3].ord)
        eng.figures(ids[1], periods[7].ord)
    added = len(stmts) - base
    assert added == 0, (
        f"取中间期补发了 {added} 条 SQL：\n" + "\n".join(stmts[base:]))


# --------------------------------------------------------------------- 真实数据 EXPLAIN


@pytest.mark.skipif(not DEV_DB_URL, reason="未提供 CWQ_DEV_DATABASE_URL")
def test_开发库上按科目筛选的计划():
    """在有真实数据的开发库上跑 EXPLAIN（**只读**：只跑 EXPLAIN，不建索引、不写数据）。

    开发库没升级到 0011 时跳过。

    断言的是「这条索引在真实数据 + 真实统计信息下**可用**」，所以同样要
    ``enable_seqscan = off``。默认开销下的计划**也一并打出来**：开发库
    book 59 只有 580 行 entry，PG 按开销选 Seq Scan 是**正确**的决策
    （15 个页的全表扫比任何索引都便宜），不是回归 —— 真正会退化的是
    行数上来的生产量级，那正是审计留待复测的部分。
    """
    import sqlalchemy as sa

    try:
        eng = sa.create_engine(DEV_DB_URL)
        with eng.connect() as c:
            has = c.execute(text(
                "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_entry_subject_voucher'"
            )).scalar()
            if not has:
                pytest.skip("开发库尚未 upgrade 到 0011；先 alembic upgrade head")
            rows = c.execute(text(
                "SELECT count(*) FROM entry WHERE book_id = :b"
            ), {"b": DEV_BOOK_ID}).scalar()
            sid = c.execute(text(
                "SELECT subject_id FROM entry WHERE book_id = :b "
                "GROUP BY subject_id ORDER BY count(*) DESC LIMIT 1"
            ), {"b": DEV_BOOK_ID}).scalar()
            if sid is None:
                # **没有数据就没有计划可验。**
                #
                # 这条用例的前提是「在**有真实数据**的开发库上验 EXPLAIN」。
                # 开发库里那个账套没有分录时，``sid`` 取到 None，于是
                # ``WHERE subject_id = NULL`` 恒不成立 —— PostgreSQL 的计划器
                # 直接短路成一个 ``Result (cost=0.00..0.00)`` 节点，
                # 索引根本不会出现在计划里。
                #
                # 那不是「索引用不上」，而是「这个查询本来就没有结果」——
                # 两种情况长得一样，断言分不出来。与其让它红成一个
                # 「索引没被用上」的假警报，不如按**前提不满足**跳过：
                # 与上面那条「开发库还没 upgrade」是同一类处理。
                pytest.skip(
                    f"开发库的账套 {DEV_BOOK_ID} 没有分录数据，"
                    f"无从验 EXPLAIN（该账套共 {rows} 行）")
            default_plan = "\n".join(r[0] for r in c.execute(
                text("EXPLAIN " + SUBJECT_FILTER_SQL), {"sid": sid}))
            c.execute(text("SET enable_seqscan = off"))
            forced_plan = "\n".join(r[0] for r in c.execute(
                text("EXPLAIN " + SUBJECT_FILTER_SQL), {"sid": sid}))
        eng.dispose()
    except sa.exc.SQLAlchemyError as exc:  # 开发库没起 / 连不上
        pytest.skip(f"开发库不可用：{exc}")

    print(f"\n[开发库 EXPLAIN] book {DEV_BOOK_ID}，entry {rows} 行，subject_id={sid}"
          f"\n--- 默认开销 ---\n{default_plan}\n--- 关掉 seqscan ---\n{forced_plan}")
    assert "ix_entry_subject_voucher" in forced_plan, (
        f"开发库（book {DEV_BOOK_ID}，{rows} 行真实数据）上这条索引用不上：\n{forced_plan}")