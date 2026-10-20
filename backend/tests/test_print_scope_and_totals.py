"""打印 / 导出通道的**归属**与**区间**闸门，外加余额表合计行的档位无关性。

三件事，同一个文件域（打印与导出）：

* **P2-1 跨账套的 ``period_id``** —— ``api/printing.py::_period`` 在给了
  ``period_id`` 时走 ``db.get(Period, period_id)``，**完全不看 ``book_id``**。
  ``Period.ord`` 是**账套内**相对序号，于是别家的期间被拿来当「本账套的某一
  期」取数：HTTP 200 + 本账套的数 + 别家的期间标签，顺带泄露「另一个账套
  存在、启用到哪一年哪个月」。

* **P2-2 区间倒置** —— 总账 Excel 导出以前把起止期**交换**后继续跑，而同一组
  参数在余额表导出、总账打印、明细账导出上都是 400。同一件事，三种结果。

* **P1-4 余额表合计行随档位变** —— 试算 ``check`` 只给了四个数（缺期初与本年
  累计），合计行那两列只能回退去表体里自己加，而它按 ``is_leaf`` 筛：
  「仅一级」档位下带下级的一级科目整行被丢掉 ⇒ 合计的「期初」「本年借方」
  写 0，而同一行的「期末」写 1277 —— **同一行内部自相矛盾**。
"""
from __future__ import annotations

import io
import re
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models import Period, Subject

# 余额表导出的列号（表头见 balance_excel）：
# 4 期初 / 5 本期借方 / 6 本期贷方 / 7 本年借方 / 8 本年贷方 / 9 方向 / 10 期末
COL_OPENING, COL_PDR, COL_PCR, COL_YDR, COL_YCR, COL_DIRECTION, COL_CLOSING = 4, 5, 6, 7, 8, 9, 10

#: 期初与本期发生额（下面每个用例都用同一套账，数字要能对得上）。
OPENING_DEBIT = Decimal("500")
OPENING_CREDIT = Decimal("500")
PERIOD = Decimal("777")
CLOSING = Decimal("1277")


def _sheet(data: bytes):
    return load_workbook(io.BytesIO(data)).active


def _total_row(ws) -> int:
    for r in range(1, ws.max_row + 1):
        if ws.cell(row=r, column=2).value == "合计":
            return r
    raise AssertionError(f"这份表里没有合计行（{ws.max_row} 行）")


def _total(ws, col: int) -> Decimal:
    return Decimal(str(ws.cell(row=_total_row(ws), column=col).value))


def _body_totals(ws) -> dict[str, Decimal]:
    """表体逐行相加（期初按「方向」列归借贷，其余直接加）。

    这是「合计 = 表体逐行相加」这条要求的判据。期初那一列在表体里是
    **绝对值**（贷方余额同样存正数，靠旁边的「方向」列区分），所以必须
    按方向归边才能与合计行的那一栏相比。
    """
    total_row = _total_row(ws)
    out = {"opening_debit": Decimal(0), "opening_credit": Decimal(0),
           "period_debit": Decimal(0), "period_credit": Decimal(0),
           "ytd_debit": Decimal(0), "ytd_credit": Decimal(0)}
    for r in range(5, total_row):
        code = ws.cell(row=r, column=1).value
        if code is None or not str(code).strip():
            continue
        opening = Decimal(str(ws.cell(row=r, column=COL_OPENING).value or 0))
        if ws.cell(row=r, column=3).value == "借":
            out["opening_debit"] += opening
        else:
            out["opening_credit"] += opening
        out["period_debit"] += Decimal(str(ws.cell(row=r, column=COL_PDR).value or 0))
        out["period_credit"] += Decimal(str(ws.cell(row=r, column=COL_PCR).value or 0))
        out["ytd_debit"] += Decimal(str(ws.cell(row=r, column=COL_YDR).value or 0))
        out["ytd_credit"] += Decimal(str(ws.cell(row=r, column=COL_YCR).value or 0))
    return out


#: 打印页里的一行。注意**不能**按 class="total" 找合计行 —— 一级科目行
#: （``__kind=SUBTOTAL``）也是那个类，会先被匹配上。
_HTML_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)


def _html_row(html: str, needle: str) -> list[str]:
    """打印页里含有 ``needle`` 的那一行的各格文本。"""
    for match in _HTML_ROW.finditer(html):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", match.group(1), re.S)
        if any(needle in cell for cell in cells):
            return cells
    return []


@pytest.fixture
def seeded_book(client, auth, db, book, subjects, periods, make_voucher):
    """一个**有期初余额**、且有发生额的账套。

    期初必须非零：期初那一列三档都是 0 的话，「合计随档位变」根本看不出来
    （0 == 0 的恒真断言）。

    * 期初：应收票据 500（借）/ 主营业务收入 500（贷）—— 借方余额走借方、
      贷方余额走贷方，两边都有，合计行才真的在「归边」。
    * 本期：借 水电费（560206，**挂在带下级的 5602 管理费用下面**）777 /
      贷 银行存款 777。「仅一级」档位下这一档要靠 5602 那一行才看得见 ——
      而它 ``is_leaf=False``，正是以前被整行丢掉的那一行。
    """
    r = client.post(f"/api/books/{book.id}/periods", headers=auth, json=[
        {"subject_id": subjects["1121"].id, "amount": "500"},
        {"subject_id": subjects["5001"].id, "amount": "-500"},
    ])
    assert r.status_code == 200, r.text
    make_voucher(date(2026, 1, 5), dr=[("560206", 777, "水电费")],
                 cr=[("1002", 777, "水电费")], posted=True)
    return book


# --------------------------------------------------------------------- P1-4 合计行


def _export(client, auth, book, period, scope):
    r = client.get(f"/api/books/{book.id}/print/balance.xlsx",
                   params={"period_id": period.id, "subject_scope": scope}, headers=auth)
    assert r.status_code == 200, r.text
    return _sheet(r.content)


def test_三档档位的合计行逐列相等(client, auth, seeded_book, periods):
    """合计行**不许随科目范围档位变**。

    档位是页面上一个三档单选（仅一级 / 仅明细 / 全部），而余额表是交给客户
    和审计的纸面数：用户点「导出 Excel」拿到的那一份，与他在屏幕上看到的那一
    份，必须是同一个数。
    """
    period = periods[1]
    sheets = {scope: _export(client, auth, seeded_book, period, scope)
              for scope in ("all", "leaf", "top")}

    columns = {COL_OPENING: "期初", COL_PDR: "本期借方", COL_PCR: "本期贷方",
               COL_YDR: "本年借方", COL_YCR: "本年贷方", COL_CLOSING: "期末"}
    for col, name in columns.items():
        values = {scope: _total(ws, col) for scope, ws in sheets.items()}
        assert values["all"] == values["leaf"] == values["top"], (
            f"合计行的「{name}」随科目档位变了：{values}")


def test_合计行有具体的数而不是零(client, auth, seeded_book, periods):
    """钉住绝对数字：三档的合计都必须是这些数。

    没有这一段，「三档相等」可以是三个 0 —— 而 0 == 0 的恒真断言什么也
    证不了（这正是以前那条「与档位无关」断言的毛病）。
    """
    period = periods[1]
    for scope in ("all", "leaf", "top"):
        ws = _export(client, auth, seeded_book, period, scope)
        assert _total(ws, COL_OPENING) == OPENING_DEBIT, (
            f"{scope}：合计期初应为 {OPENING_DEBIT}，实际 {_total(ws, COL_OPENING)}")
        assert _total(ws, COL_PDR) == PERIOD, scope
        assert _total(ws, COL_PCR) == PERIOD, scope
        assert _total(ws, COL_YDR) == PERIOD, (
            f"{scope}：合计本年借方应为 {PERIOD}，实际 {_total(ws, COL_YDR)}")
        assert _total(ws, COL_YCR) == PERIOD, scope
        assert _total(ws, COL_CLOSING) == CLOSING, scope


def test_合计等于表体逐行相加(client, auth, seeded_book, periods):
    """「仅一级」与「仅明细」两档的表体不重复计数，合计必须等于它逐行相加。

    「全部」那一档的表体里父子同在（父行是子行的上卷），逐行相加天然是双倍 ——
    那一档不参与这条断言（它本来就是一张带汇总行的树）。
    """
    period = periods[1]
    for scope in ("top", "leaf"):
        ws = _export(client, auth, seeded_book, period, scope)
        body = _body_totals(ws)
        assert _total(ws, COL_OPENING) == body["opening_debit"], (
            f"{scope}：合计期初与表体逐行相加对不上")
        assert _total(ws, COL_PDR) == body["period_debit"], scope
        assert _total(ws, COL_PCR) == body["period_credit"], scope
        assert _total(ws, COL_YDR) == body["ytd_debit"], scope
        assert _total(ws, COL_YCR) == body["ytd_credit"], scope


def test_合计行内部不矛盾(client, auth, seeded_book, periods):
    """**同一行内部**必须自洽：期初 + 本期发生 = 期末。

    以前「仅一级」档位下这一行是：期初 0、本期借方 777、期末 1277 ——
    0 + 777 ≠ 1277，一行之内自己跟自己打架，而纸下方两行还打着「平衡 ✓」。

    这里比的是**同一侧**：合计行的「期初」与「期末」放的都是**借方合计**
    （单栏版式放不下两边），所以是「期初借 + 本期借 = 期末借」。这个账套里
    没有哪个科目的余额在期内反过方向（借方余额仍是借方、贷方余额仍是贷方），
    所以同侧相加是成立的 —— 这一点由下面的期望值钉住。
    """
    period = periods[1]
    for scope in ("all", "leaf", "top"):
        ws = _export(client, auth, seeded_book, period, scope)
        opening = _total(ws, COL_OPENING)
        dr = _total(ws, COL_PDR)
        closing = _total(ws, COL_CLOSING)
        assert opening + dr == closing, (
            f"{scope}：合计行内部不自洽：期初 {opening} + 本期借 {dr} ≠ 期末 {closing}")


# --------------------------------------------------------------------- P3-3 HTML 合计


def test_HTML打印的余额表也有合计行与试算行(client, auth, seeded_book, periods):
    """Excel 有合计与试算、HTML 没有 —— 两份纸面对不上。

    断言的是**两边的数一样**：HTML 的合计行与 Excel 的合计行逐列相等。
    """
    period = periods[1]
    html = client.get(f"/api/books/{seeded_book.id}/print/balance.html", headers=auth,
                      params={"period_id": period.id, "subject_scope": "top"})
    assert html.status_code == 200, html.text

    cells = _html_row(html.text, "合计")
    assert cells, "HTML 打印的余额表没有合计行"
    assert len(cells) == 9, cells
    # 列：科目编码 / 科目名称 / 级次 / 期初借方 / 期初贷方 / 本期借方 /
    #     本期贷方 / 期末借方 / 期末贷方
    assert cells[3] == "500.00", cells
    assert cells[4] == "500.00", cells
    assert cells[5] == "777.00", cells
    assert cells[6] == "777.00", cells
    assert cells[7] == "1,277.00", cells
    assert cells[8] == "1,277.00", cells

    # 试算两行：平衡与否都写在纸上
    assert "试算平衡" in html.text
    assert "平衡 ✓" in html.text, html.text[-800:]
    assert re.search(r'<tr class="check">', html.text), "HTML 打印没有试算行"


def test_HTML与Excel的合计行逐列相等(client, auth, seeded_book, periods):
    """两条通道是同一张纸 —— 数字必须一样。"""
    period = periods[1]
    html = client.get(f"/api/books/{seeded_book.id}/print/balance.html", headers=auth,
                      params={"period_id": period.id, "subject_scope": "top"}).text
    ws = _export(client, auth, seeded_book, period, "top")

    cells = _html_row(html, "合计")
    html_numbers = [Decimal(c.replace(",", "")) for c in cells[3:]]
    assert html_numbers[0] == _total(ws, COL_OPENING)
    assert html_numbers[2] == _total(ws, COL_PDR)
    assert html_numbers[3] == _total(ws, COL_PCR)
    assert html_numbers[4] == _total(ws, COL_CLOSING)


# --------------------------------------------------------------------- P2-1 跨账套


@pytest.fixture
def other_book(client, auth, db):
    """另一个账套（同一个用户，也是 OWNER）。

    账套名**必须每次不同**：建账是**真提交**（不是夹具里的回滚），同名再建
    会撞上 ``uq_book`` 唯一约束 —— 同一个进程里第二个用例就 400「已存在」。
    """
    import uuid

    tag = uuid.uuid4().hex[:8]
    r = client.post("/api/books", headers=auth, json={
        "company_name": f"别家公司{tag}", "name": f"别家账套{tag}", "start_year": 2026})
    assert r.status_code == 201, r.text
    book_id = r.json()["id"]
    period = db.scalar(select(Period).where(Period.book_id == book_id).order_by(Period.ord))
    assert period is not None
    return {"id": book_id, "period_id": period.id, "code": period.code}


def test_打印与导出的九个端点都不接受别家账套的期间(client, auth, book, other_book,
                                                subjects, db):
    """``_period`` 是**九个**打印/导出端点共用的，归属校验收在这一处。

    以前给了 ``period_id`` 就直接 ``db.get``，别家的期间被拿来当「本账套的
    某一期」：本账套的数 + 别家的期间标签，200 + 错数据，还泄露了别家账套
    启用到哪一年哪个月。
    """
    own_period = db.scalar(select(Period).where(
        Period.book_id == book.id).order_by(Period.ord))
    assert own_period is not None
    subject_id = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.code == "1002"))
    report_id = _baseline_report_id(db)

    base = f"/api/books/{book.id}/print"
    foreign = {"period_id": other_book["period_id"]}
    calls = [
        ("打印凭证", f"{base}/vouchers.html", foreign),
        ("打印余额表", f"{base}/balance.html", foreign),
        ("打印总账", f"{base}/general-ledger.html", foreign),
        ("打印明细账", f"{base}/detail-ledger.html", {**foreign, "subject_id": subject_id}),
        ("打印报表", f"{base}/report.html", {**foreign, "template_id": report_id}),
        ("导出凭证", f"{base}/vouchers.xlsx", foreign),
        ("导出余额表", f"{base}/balance.xlsx", foreign),
        ("导出总账", f"{base}/general-ledger.xlsx", foreign),
        ("导出报表", f"{base}/report.xlsx", {**foreign, "template_id": report_id}),
    ]
    for name, url, params in calls:
        r = client.get(url, params=params, headers=auth)
        assert r.status_code == 404, (
            f"{name}：别家账套的期间必须 404，实际 {r.status_code}：{r.text[:200]}")
        assert "会计期间不存在" in r.text, f"{name}：{r.text[:200]}"


def _baseline_report_id(db) -> int:
    from app.models import ReportTemplate

    tid = db.scalar(select(ReportTemplate.id).where(
        ReportTemplate.code == "BALANCE_SHEET", ReportTemplate.book_id.is_(None)))
    assert tid is not None, "内置报表基线没建出来"
    return tid


def test_别家账套的期间码不出现在响应里(client, auth, book, other_book, db):
    """跨账套信息泄露：别家的期间码（存在了、启用到哪年哪月）不能回给用户。"""
    own_period = db.scalar(select(Period).where(Period.book_id == book.id).order_by(Period.ord))
    r = client.get(f"/api/books/{book.id}/print/balance.html", headers=auth,
                   params={"period_id": other_book["period_id"]})
    assert r.status_code == 404
    assert other_book["code"] not in r.text, r.text[:200]
    # 自家的照常
    ok = client.get(f"/api/books/{book.id}/print/balance.html", headers=auth,
                    params={"period_id": own_period.id})
    assert ok.status_code == 200, ok.text[:200]


# --------------------------------------------------------------------- P2-2 区间倒置


def test_区间倒置在四条通道上都是400(client, auth, book, periods, subjects, db):
    """同一组参数，四条通道，三种结果 —— 现在统一成 400。

    以前总账 Excel 导出把起止期**交换**后继续跑（200 + 一份文件），而余额表
    导出、总账打印、明细账导出都是 400。用户若在页面上看到过那句报错，改走
    导出却拿到文件，会以为页面报错是多余的。
    """
    subject_id = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.code == "1002"))
    params = {"period_id": periods[1].id, "from_period_id": periods[3].id}
    base = f"/api/books/{book.id}/print"
    calls = [
        ("导出总账", f"{base}/general-ledger.xlsx", params),
        ("导出余额表", f"{base}/balance.xlsx", params),
        ("打印总账", f"{base}/general-ledger.html", params),
        ("导出明细账", f"{base}/detail-ledger.xlsx", {**params, "subject_id": subject_id}),
    ]
    for name, url, query in calls:
        r = client.get(url, params=query, headers=auth)
        assert r.status_code == 400, (
            f"{name}：倒置区间必须 400，实际 {r.status_code}：{r.text[:200]}")
        assert "起始期间不能晚于结束期间" in r.text, f"{name}：{r.text[:200]}"


# --------------------------------------------------------------------- 导出错误清单


def test_导出的报表里公式错误一条不少():
    """Excel 通道以前只写前 20 条公式错误，第 21 条起被**静默藏起来**。

    JSON 通道（``api/reports.py``）返回全量 ``errors``，HTML 通道逐格印错误
    文案，只有 Excel 通道截断 —— 同一张表「错了几条」在三个通道上给出三种
    答案，用户在 Excel 里根本不知道自己漏看了哪些格子。
    """
    from app.printing.excel import report_excel
    from app.printing.template import REPORT_STATIC

    errors = [f"第 {i} 号格子的公式错误" for i in range(1, 26)]
    payload = {
        "name": "测试报表",
        "period": "2026-01",
        "cols": [{"col_no": 1, "label": "项目", "kind": REPORT_STATIC, "width": 120}],
        "rows": [{"label": "项目", "cells": []}],
        "errors": errors,
    }
    ws = load_workbook(io.BytesIO(report_excel(payload, book_name="测试账套"))).active
    texts = {str(ws.cell(row=r, column=1).value) for r in range(1, ws.max_row + 1)}
    missing = [e for e in errors if e not in texts]
    assert not missing, f"公式错误清单被截断了，缺 {len(missing)} 条：{missing[:3]}"
