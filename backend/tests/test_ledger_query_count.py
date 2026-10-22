"""聚合次数：总账一页一次全账聚合，报表逐科目取数不再 N+1（审计 P2-03 / P2-15）。

这里断言的是**查询次数**，不是「结果对不对」—— 结果对而慢，用户一样会骂。
计数方式是把 SQLAlchemy 的 ``before_cursor_execute`` 挂上去，抓本次请求里
真正发出去的语句，再按特征筛（``entry`` 上的聚合 / ``period_opening`` 扫描）。

记忆化只存在于 LedgerEngine 实例内（实例只读，写后换实例），
所以这些断言是「单个实例内」的承诺，不涉及跨请求缓存。
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import sqlalchemy

from app.core.db import engine as app_engine
from app.ledger import queries
from app.ledger.engine import LedgerEngine


@contextmanager
def _statements():
    """收集本次调用期间真正发出去的 SQL。"""
    stmts: list[str] = []
    handler = lambda conn, cursor, statement, params, ctx, many: stmts.append(statement)
    sqlalchemy.event.listen(app_engine, "before_cursor_execute", handler)
    try:
        yield stmts
    finally:
        sqlalchemy.event.remove(app_engine, "before_cursor_execute", handler)


def _entry_aggregates(stmts) -> list[str]:
    """账簿最热的那条查询：entry 上的发生额聚合。"""
    return [s for s in stmts if "FROM entry" in s and "GROUP BY" in s]


def _opening_scans(stmts) -> list[str]:
    return [s for s in stmts if "FROM period_opening" in s]


def _tree_scans(stmts) -> list[str]:
    """科目**上卷树**（祖先链）那一条扫描。

    引擎建祖先链时只取 id 与 parent_id 两列；科目表本身的 SELECT 会带上
    code 等整行字段 —— 用「不取 code」把两者分开，否则总账那几条
    「按年度取科目表」的扫描会被误算成上卷树。
    """
    return [s for s in stmts
            if "FROM subject" in s and "subject.parent_id" in s and "subject.code" not in s]


def _seed(make_voucher, months=(1, 2, 3, 5, 6, 8)):
    for month in months:
        make_voucher(f"2026-{month:02d}-10",
                     dr=[("1002", 100 * month, "收款")],
                     cr=[("5001", 100 * month, "收款")], posted=True)


def test_总账一页只跑一次全账聚合(db, book, periods, subjects, make_voucher):
    _seed(make_voucher)
    tree = queries.subject_tree(db, book.id, book.start_year)
    # 总账的单位是**一级科目**（用友的「总分类账」），所以这里问的是那一行，
    # 不是整棵树 —— 它下面所有明细科目的分录由引擎上卷进来。
    subject = next(s for s in tree if s.level == 1)

    with _statements() as stmts:
        payload = queries.general_ledger(
            db, book.id, subject, periods[3], periods[8], False)

    assert len(_entry_aggregates(stmts)) == 1, (
        f"总账应该只跑一次全账聚合，实际 {len(_entry_aggregates(stmts))} 次")
    assert len(_opening_scans(stmts)) == 1, "期初快照只该读一次"
    assert len(_tree_scans(stmts)) == 1, "科目上卷树只该建一次"
    assert payload["rows"], "总账不该是空的"


def test_首年区间也只跑一次全账聚合(db, book, periods, subjects, make_voucher):
    """from 就是账套首期这一条：以前要多算一次 from-1，现在也只是一次聚合。

    修 P0-03 时区间期初统一走「上一期期末」，如果不记忆化，
    这里会变成 compute(to) + range(to) + compute(from-1) 三次全账扫描。
    """
    _seed(make_voucher)
    tree = queries.subject_tree(db, book.id, book.start_year)
    subject = next(s for s in tree if s.level == 1)

    with _statements() as stmts:
        queries.general_ledger(db, book.id, subject, periods[1], periods[6], False)

    assert len(_entry_aggregates(stmts)) == 1, (
        f"首年区间应该只跑一次全账聚合，实际 {len(_entry_aggregates(stmts))} 次")


def test_报表逐科目取数不再每个科目一次全账聚合(db, book, periods, subjects, make_voucher):
    """复刻 reports/evaluate.py 的调用形状：同一个引擎逐个 figures(sid, ord)。

    以前每个科目一次全账聚合（审计 P2-15 的 N+1），现在整份报表一次。
    这里**没有**改 evaluate.py —— 记忆化放在引擎实例内，调用形状不变也受益。
    """
    _seed(make_voucher)
    codes = ["1002", "1012", "1001", "5001", "5602", "1122"]
    eng = LedgerEngine.for_report(db, book.id)

    with _statements() as stmts:
        figures = [eng.figures(subjects[c].id, periods[6].ord) for c in codes]
        # 报表还要「年初」那一列 = 另一期，两期也不该各跑一次
        figures += [eng.figures(subjects[c].id, periods[1].ord) for c in codes]

    assert len(_entry_aggregates(stmts)) == 1, (
        f"{len(codes)} 个科目两个期间应该只跑一次全账聚合，"
        f"实际 {len(_entry_aggregates(stmts))} 次")
    assert len(_opening_scans(stmts)) == 1
    assert len(figures) == len(codes) * 2


def test_批量预热接口一次算完(db, book, periods, subjects, make_voucher):
    """batch_figures：报表可以先批量预热，结果与逐个取数完全一致。"""
    _seed(make_voucher)
    ids = [subjects[c].id for c in ("1002", "5001", "1122")]

    eng = LedgerEngine.for_report(db, book.id)
    with _statements() as stmts:
        pre = eng.batch_figures(ids, periods[6].ord)
        after = {sid: eng.figures(sid, periods[6].ord) for sid in ids}

    assert len(_entry_aggregates(stmts)) == 1
    for sid in ids:
        assert pre[sid].closing == after[sid].closing
        assert pre[sid].opening == after[sid].opening


def test_账簿口径与报表口径是两个明确的引擎(db, book, periods, subjects, make_voucher):
    """P1-15 口径契约的锚点测试：两个工厂造出来的引擎口径确实不同。

    差额恰好是结转凭证的金额 —— 这不是 bug，界面必须标注（由报表/前端负责）。
    """
    make_voucher("2026-01-10", dr=[("1002", 900, "租金")], cr=[("5001", 900, "租金")],
                 posted=True)
    make_voucher("2026-01-31", dr=[("5001", 900, "结转")], cr=[("3103", 900, "结转")],
                 posted=True, source="CARRY_FORWARD")

    revenue = subjects["5001"].id
    ledger = LedgerEngine.for_ledger(db, book.id).figures(revenue, periods[1].ord)
    report = LedgerEngine.for_report(db, book.id).figures(revenue, periods[1].ord)

    # 账簿看全账：结转后收入归零
    assert ledger.period_dr == Decimal("900") and ledger.closing == Decimal("0")
    # 报表看真实经营：排除结转凭证
    assert report.period_dr == Decimal("0")
    assert report.period_cr == Decimal("900")
    assert report.closing == Decimal("-900")
    # 两个口径的差额 == 结转凭证金额
    assert report.period_dr - ledger.period_dr == Decimal("-900")
