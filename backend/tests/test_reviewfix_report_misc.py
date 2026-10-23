"""报表侧的两处护栏与三处「注释与实现对齐」（审查修复）。

* **越界的单元格引用要报错**（``reports/evaluate.py::cell``）
  —— 公式里写 ``=Z99``（模板里根本没有这一格），以前那一格会**安静地
  变成 0**：整张表看起来正常得很，而那个 0 是「引用错了」不是「这一格
  确实是 0」。金额是 0 还是压根没这一格，财务分得清，程序分不清就该
  说清楚 —— 与「期间字面量越界」同一个口径。

* **落库前量化**（``api/reports.py::persist``）
  —— ``report_result.value`` 是 ``Numeric(20,2)``，而落库时丢进去的是
  未量化的原值：数据库**静默四舍五入**（1/3 → 0.33），而页面上显示的
  是未量化的原值 —— 两份数对不上。

另外三条是「注释 / 约定不许与实现脱节」的护栏：
  · 引擎的期初快照**不是**「结账时冻结」的（结账根本不写快照）；
  · ``trg_voucher_balance`` **不拦**空凭证（0 = 0 是过的）；
  · 账簿引擎只许用 ``for_ledger`` / ``for_report`` 两个工厂。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import ReportCell, ReportResult, ReportTemplate

TEMPLATE_CODE = "BALANCE_SHEET"
TARGET_ROW, TARGET_COL = 2, 3
NEIGHBOUR_COL = 4


@pytest.fixture
def tpl(db):
    row = db.scalar(select(ReportTemplate).where(ReportTemplate.code == TEMPLATE_CODE))
    assert row is not None, "内置资产负债表模板没建出来，种子数据有问题"
    return row


def _cell(db, tpl, row_no: int, col_no: int) -> ReportCell:
    cell = db.scalar(select(ReportCell).where(
        ReportCell.template_id == tpl.id, ReportCell.row_no == row_no,
        ReportCell.col_no == col_no))
    assert cell is not None, f"模板里没有第 {row_no} 行第 {col_no} 列"
    return cell


def _cell_of(data: dict, row_no: int, col_no: int) -> dict:
    row = next(r for r in data["rows"] if r["row_no"] == row_no)
    return next(c for c in row["cells"] if c["col_no"] == col_no)


def _render(client, auth, book, tpl, period):
    return client.get(f"/api/reports/{tpl.id}/render", headers=auth,
                      params={"book_id": book.id, "period_id": period.id})


# --------------------------------------------------------------------- 1. 越界引用


def test_公式引用不存在的单元格报错(client, auth, db, book, tpl, periods):
    """``=Z99`` 指向模板里根本没有的一格 → 那一格报错，其余格照常。"""
    cell = _cell(db, tpl, TARGET_ROW, TARGET_COL)
    original = cell.formula
    cell.formula = "=Z99"
    db.commit()
    try:
        r = _render(client, auth, book, tpl, periods[1])
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        target = _cell_of(data, TARGET_ROW, TARGET_COL)
        assert target["value"] is None, "越界引用那一格应当没有值"
        assert target["error"], "越界引用那一格必须报错"
        assert "不存在" in target["error"], target["error"]
        # 其余格照常算完（不能因为一格坏掉就整张表报错）
        assert data["errors"], "错误清单里应当有这一条"
    finally:
        cell.formula = original
        db.commit()


def test_模板里存在但公式为空的格子仍然取0(client, auth, db, book, tpl, periods):
    """**存在**这一格、只是公式为空 → 合法，取 0（不是越界）。"""
    target = _cell(db, tpl, TARGET_ROW, TARGET_COL)
    neighbour = _cell(db, tpl, TARGET_ROW, NEIGHBOUR_COL)
    original_target, original_neighbour = target.formula, neighbour.formula
    target.formula = ""
    neighbour.formula = "=C2"
    db.commit()
    try:
        r = _render(client, auth, book, tpl, periods[1])
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        value = _cell_of(data, TARGET_ROW, NEIGHBOUR_COL)["value"]
        assert value is not None, "空公式的格子应当算出 0，而不是报错"
        assert Decimal(str(value)) == Decimal("0"), value
        assert not _cell_of(data, TARGET_ROW, NEIGHBOUR_COL)["error"]
    finally:
        target.formula = original_target
        neighbour.formula = original_neighbour
        db.commit()


# --------------------------------------------------------------------- 2. 落库前量化


def test_报表结果落库前量化(client, auth, db, book, tpl, periods):
    """1/3 落库必须是 0.33 —— 页面显示的是未量化的原值，落库要同一个口径。"""
    cell = _cell(db, tpl, TARGET_ROW, TARGET_COL)
    original = cell.formula
    cell.formula = "=1/3"
    db.commit()
    try:
        r = client.post(f"/api/reports/{tpl.id}/persist", headers=auth,
                        params={"book_id": book.id, "period_id": periods[1].id})
        assert r.status_code == 200, r.text[:300]

        stored = db.scalar(select(ReportResult).where(
            ReportResult.template_id == tpl.id, ReportResult.book_id == book.id,
            ReportResult.period_id == periods[1].id,
            ReportResult.row_no == TARGET_ROW, ReportResult.col_no == TARGET_COL))
        assert stored is not None, "结果没有落库"
        assert stored.value == Decimal("0.33"), (
            f"落库的值应当是量化后的 0.33，实际 {stored.value}")
    finally:
        cell.formula = original
        db.commit()


# --------------------------------------------------------------------- 3. 注释与实现对齐


def _source(relative: str) -> str:
    return (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")


def test_期初快照不是结账时冻结的():
    """引擎 docstring 原来写「结账时冻结」—— 结账根本不写快照。"""
    src = _source("app/ledger/engine.py")
    assert "结账时冻结" not in src, "期初快照的来源描述与实现不符"


def test_空凭证触发器的注释与实现一致():
    """``trg_voucher_balance`` 只查借贷平衡，0 = 0 是放行的。

    空凭证由**应用层**拦（``_validate_entries``：「凭证至少需要一条分录」）。
    触发器的**行为不许改**（改触发器函数体要动迁移）。
    """
    src = _source("app/core/db_init.sql")
    assert "空凭证（一条分录都没有）同样要拦住" not in src, \
        "触发器的注释仍在宣称它会拦空凭证 —— 与实现不符"
    assert "它不拦空凭证" in src, "注释应当说清它拦什么、不拦什么"


def test_账簿引擎只用两个工厂():
    """``LedgerEngine(db, book_id)`` 这种直接构造不许再出现。

    两个口径只差一个 ``exclude_carry_forward``，靠人记迟早会在某个新
    查询里漏掉（ARCHITECTURE §「两个工厂方法就是口径契约」）。
    """
    for relative in ("app/accounting/periods.py", "app/ledger/queries.py",
                     "app/reports/evaluate.py"):
        src = _source(relative)
        assert "LedgerEngine(" not in src, (
            f"{relative} 里还有直接构造 LedgerEngine 的地方 —— "
            f"只能用 for_ledger / for_report")
