"""报表公式里的**畸形期间字面量**：只让那一格报错，绝不整张表 500。

两条缺陷，同一处代码（``app/reports/evaluate.py::resolve_period``）：

* **P1-1** —— ``"2026-"`` / ``"2026-ab"`` 这类字面量走 ``int(y), int(m)``，
  ``int("")`` / ``int("ab")`` 抛 ``ValueError``。它既不是 ``FormulaError``，
  也不在 ``formula_guard`` 接的异常里，于是穿过 ``evaluate`` 的
  ``except FormulaError``，最后由 FastAPI 变成 **HTTP 500**：
  **整张表**渲染不出来，其余几十格已经算好的数一起丢掉。
* **P1-2** —— ``"2026-13"`` / ``"2026-00"`` / ``"2026-99"`` / ``"1900"`` /
  ``"9999"`` 这类**格式合法但取值荒谬**的字面量被静默折算成**另一个期间**：
  HTTP 200、格子里**有值**、``errors`` 是空的。资产负债表上「2026-12」那一栏
  印出来的其实是 2027-01 的数 —— 它不是 0、不是空、不是红字，而是一个正常的
  金额，财务不会怀疑它。

波及范围：报表页、报表打印（``report.html``）、报表 Excel 导出
（``report.xlsx``）、报表结果落库（``persist``）**四个入口共用 ``evaluate``**，
所以四条都要验。

判据是「**那一格报错、其余格照常**」而不是「有没有报错」：把坏公式从
``"2026-"`` 改成 ``"abc"`` 只是把 500 变成 200，那是治标；真正要钉住的是
**绝不静默取到另一个期间**。
"""
from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models import ReportCell, ReportTemplate

#: 内置资产负债表（会小企01表，8 列一行两栏）。
TEMPLATE_CODE = "BALANCE_SHEET"

#: 目标格：第 2 行第 3 列 = 资产侧的「期末余额」。
TARGET_ROW, TARGET_COL = 2, 3
#: 对照格：同一行第 4 列 = 资产侧的「年初余额」。同一行、同一张表，
#: 用来证明「坏的只有那一格」。
NEIGHBOUR_COL = 4

#: 格式非法（int() 抛 ValueError）的那一批。
MALFORMED = ("2026-", "2026-ab", "abc")

#: 格式合法但取值荒谬的那一批（以前被静默折算成别的期间）。
OUT_OF_RANGE = ("2026-13", "2026-00", "2026-99", "1900", "9999")


@pytest.fixture
def tpl(db):
    row = db.scalar(select(ReportTemplate).where(ReportTemplate.code == TEMPLATE_CODE))
    assert row is not None, "内置资产负债表模板没建出来，种子数据有问题"
    return row


def _break(db, tpl, formula: str):
    """把目标格的公式换掉，返回那一行（改之前要还原，见 finally）。"""
    cell = db.scalar(select(ReportCell).where(
        ReportCell.template_id == tpl.id, ReportCell.row_no == TARGET_ROW,
        ReportCell.col_no == TARGET_COL))
    assert cell is not None, "目标格不存在，内置模板的形状变了"
    original = cell.formula
    cell.formula = formula
    db.commit()
    return cell, original


def _cell_of(data: dict, row_no: int, col_no: int) -> dict:
    row = next(r for r in data["rows"] if r["row_no"] == row_no)
    return next(c for c in row["cells"] if c["col_no"] == col_no)


def _render(client, auth, book, tpl, period):
    """报表页（``/api/reports/{id}/render``）。"""
    return client.get(f"/api/reports/{tpl.id}/render", headers=auth,
                      params={"book_id": book.id, "period_id": period.id})


# --------------------------------------------------------------------- P1-1


@pytest.mark.parametrize("literal", MALFORMED)
def test_畸形期间字面量只让那一格报错而不是整张表500(client, auth, db, book, tpl, periods,
                                                literal):
    """**整张表 500** 是这条缺陷最重的后果：几十格已经算好的数一起丢掉。

    现在必须退化成「这一格报一行红字」，其余格子照常出数。
    """
    cell, original = _break(db, tpl, f'科目("1001","期末余额","{literal}")')
    try:
        r = _render(client, auth, book, tpl, periods[3])
        assert r.status_code == 200, (
            f"拿到的不是 200（很可能就是 500）：{r.status_code} {r.text[:300]}")
        data = r.json()

        assert data["errors"], "这一格必须报错，errors 不能是空的"
        assert data["formula_error_free"] is False

        bad = _cell_of(data, TARGET_ROW, TARGET_COL)
        assert bad["value"] is None, f"坏格子竟取到了数：{bad}"
        assert "无法识别的期间" in bad["error"], bad["error"]

        # **其余格照常**：同一行的另一格必须有值、没有错。
        good = _cell_of(data, TARGET_ROW, NEIGHBOUR_COL)
        assert good["error"] == "", good
        assert good["value"] is not None, good
    finally:
        cell.formula = original
        db.commit()


# --------------------------------------------------------------------- P1-2


@pytest.mark.parametrize("literal", OUT_OF_RANGE)
def test_越界的期间字面量不会静默取到另一个期间(client, auth, db, book, tpl, periods,
                                            literal):
    """以前这些字面量取到的是**另一个期间**的数，而且 errors 是空的。

    现在这一格必须报错、**不能有值** —— 有值就说明它从某个期间取到了数。
    """
    cell, original = _break(db, tpl, f'科目("1001","期末余额","{literal}")')
    try:
        r = _render(client, auth, book, tpl, periods[3])
        assert r.status_code == 200, r.text[:300]
        data = r.json()

        bad = _cell_of(data, TARGET_ROW, TARGET_COL)
        assert bad["value"] is None, (
            f"这一格竟拿到了数（多半是折算到了别的期间）：{bad}")
        assert bad["error"], "越界的期间字面量必须报错，不能一声不响"
        assert "无法识别的期间" in bad["error"], bad["error"]
        assert any(literal in e for e in data["errors"]), data["errors"]
    finally:
        cell.formula = original
        db.commit()


def test_月份13不会折算成次年1月(client, auth, db, book, tpl, periods):
    """``"2026-13"`` 以前折成 2027-01（``period_ord`` 不查月份）。

    折算出来的那个期间**不允许出现在任何一个通道的输出里** —— 那正是
    「资产负债表上 2026-12 那一栏印的是 2027-01 的数」的表现。
    """
    cell, original = _break(db, tpl, '科目("1001","期末余额","2026-13")')
    try:
        html = client.get(f"/api/books/{book.id}/print/report.html", headers=auth,
                          params={"template_id": tpl.id, "period_id": periods[3].id})
        assert html.status_code == 200, html.text[:300]
        assert "2027-01" not in html.text, "折算出来的相邻期间出现在了打印件上"

        x = client.get(f"/api/books/{book.id}/print/report.xlsx", headers=auth,
                       params={"template_id": tpl.id, "period_id": periods[3].id})
        assert x.status_code == 200, x.text[:300]
        texts = [str(c.value) for row in load_workbook(io.BytesIO(x.content)).active.iter_rows()
                 for c in row if c.value is not None]
        assert not any("2027-01" in t for t in texts), texts[:20]
    finally:
        cell.formula = original
        db.commit()


# --------------------------------------------------------------------- 四个入口


def test_四个入口都不会500(client, auth, db, book, tpl, periods):
    """报表页 / 打印 / Excel 导出 / 结果落库**共用 evaluate**，一起挂。

    只验一个入口是不够的：坏公式是**持久状态**（存在 ``report_cell`` 表里），
    任何一条通道渲染它都会 500。
    """
    cell, original = _break(db, tpl, '科目("1001","期末余额","2026-")')
    try:
        period = periods[3]

        page = _render(client, auth, book, tpl, period)
        assert page.status_code == 200, f"报表页：{page.status_code} {page.text[:200]}"
        assert page.json()["errors"], "报表页必须把这一格的错报出来"

        printed = client.get(f"/api/books/{book.id}/print/report.html", headers=auth,
                             params={"template_id": tpl.id, "period_id": period.id})
        assert printed.status_code == 200, (
            f"报表打印：{printed.status_code} {printed.text[:200]}")
        assert "无法识别的期间" in printed.text, (
            "打印件上这一格应当印出错误文案，而不是一个空格子"
            "（空格与「这一项真的是 0」在纸上长得一模一样）")

        exported = client.get(f"/api/books/{book.id}/print/report.xlsx", headers=auth,
                              params={"template_id": tpl.id, "period_id": period.id})
        assert exported.status_code == 200, (
            f"报表导出：{exported.status_code} {exported.text[:200]}")
        assert exported.content[:2] == b"PK", "导出返回的不是 xlsx"
        texts = [str(c.value) for row in load_workbook(io.BytesIO(exported.content)).active.iter_rows()
                 for c in row if c.value is not None]
        assert any("无法识别的期间" in t for t in texts), texts[-20:]

        saved = client.post(f"/api/reports/{tpl.id}/persist", headers=auth,
                            params={"book_id": book.id, "period_id": period.id})
        assert saved.status_code == 200, f"结果落库：{saved.status_code} {saved.text[:200]}"
        assert saved.json()["errors"], "落库那次求值也必须把错报出来"
    finally:
        cell.formula = original
        db.commit()


# --------------------------------------------------------------------- 正常值不许被误伤


def test_账套里真实存在的期间照常取数(client, auth, db, book, tpl, periods):
    """闸门不能顺手把正常写法也拦了。

    裸年份「2026」= 那一年的 12 期，「2026-03」= 那一年的 3 期 ——
    两者都必须照常出数、errors 为空。
    """
    cell, original = _break(db, tpl, '科目("1001","期末余额","2026-03")')
    try:
        r = _render(client, auth, book, tpl, periods[3])
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert not data["errors"], data["errors"]
        assert data["formula_error_free"] is True
        assert _cell_of(data, TARGET_ROW, TARGET_COL)["value"] is not None
    finally:
        cell.formula = original
        db.commit()


def test_裸年份指向那一年的12期(db, book, tpl, periods):
    """裸年份不是「随便哪个月」：它固定是 12 月，且必须在账套区间内。"""
    from app.reports.evaluate import EvalContext

    ctx = EvalContext(db, book, periods[3], {})
    p = ctx.resolve_period("2026")
    assert (p.year, p.month) == (2026, 12), (p.year, p.month)


def test_账套之外的年份被明确拒绝(db, book, tpl, periods):
    """账套没有的年份 → 公式错误，不是「折算成首期」。

    以前 ``"1900"`` 会被折成账套首期（``_synthetic`` 夹取），
    于是「1900 年」这一栏印出来的是**首期**的数。
    """
    from app.reports.formula import FormulaError
    from app.reports.evaluate import EvalContext

    ctx = EvalContext(db, book, periods[3], {})
    for literal in ("1900", "9999", "2026-13", "2026-00"):
        with pytest.raises(FormulaError) as exc:
            ctx.resolve_period(literal)
        assert "无法识别的期间" in str(exc.value), (literal, str(exc.value))
