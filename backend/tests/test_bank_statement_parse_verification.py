"""对账单解析的**两路解析 + 严格自检**。

**要钉住的行为**：一份 PDF 至少有两路可走 —— 文字层（按坐标还原，精确取字）
与 OCR（看渲染出来的图像）。**只有自检通过的那一路才算数**。

**为什么必须这样**：2026-10-27 实测，Linux 上 pdfium 算出来的 CID 字形框
高度退化成 0，表头认不出来，解析**静默**退回「纯文本流切分」，把
「18050.00」截成「1805」、合计对不上、方向认不出 —— **而且不报错**。
「不报错」是这类缺陷里最坏的一种：数据错得看起来很正常。

所以这里的判据是：

* 文字层自检通过 → 直接用，**不去问 OCR**（没必要多走一趟）；
* 文字层自检不过 → **问一次 OCR**，OCR 那份干净就用它；
* 两份都不干净 → **合并**（两边各认出一半），合并之后**重跑自检**，
  取自检结论更少的那一种口径。

**这些用例模拟 Linux**：把文字层那一路的方向抹掉（Linux 上「借/贷」是
单字对象，整个不进文字层），于是它自检不过 —— 这样同一份用例在 macOS 与
Linux 上都成立，测的始终是**生产那条路**。
"""
from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest

from app.bankstatement import pdf_table, semantics, service

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
OCR_MARKDOWN = FIXTURES / "ocr" / "cib_pdf_statement_ocr_markdown.md"
CEB_OCR_MARKDOWN = FIXTURES / "ocr" / "ceb_corp_pdf_statement_ocr_markdown.md"
STATEMENT_PDF = FIXTURES / "statements" / "cib_pdf.pdf"
CEB_PDF = FIXTURES / "statements" / "ceb_corp_pdf.pdf"


def _ocr_grid(markdown: pathlib.Path = OCR_MARKDOWN):
    """OCR 那一路给出的网格：真实 OCR 输出走**生产那条**转换链。"""
    return service._grid_from_ocr_markdown(
        [markdown.read_text(encoding="utf-8")])


@pytest.fixture()
def ocr_calls(monkeypatch):
    """把 OCR 那一路换成可控的假实现，并记录它被叫了几次。"""
    calls: list[bytes] = []
    state = {"grid": None}

    def fake_ocr_grid(db, data):
        calls.append(data)
        return state["grid"]

    monkeypatch.setattr(service, "_ocr_grid", fake_ocr_grid)
    return calls, state


def _linux_like(parsed: semantics.ParsedStatement) -> semantics.ParsedStatement:
    """把一份解析改成**Linux 上那个样子**：方向认不出来。

    **为什么要在用例里造这个**：Linux 上 pdfium 把「借/贷」这种单字对象
    整个丢出文字层，于是方向是 UNKNOWN、自检不过。用例在 macOS 上跑，
    造不出这个形态就得靠平台差异去撞 —— 那种用例在 CI（Linux）上才会
    红，本地永远是绿的。**自己造，跨平台都成立。**
    """
    for row in parsed.rows:
        row.direction = "UNKNOWN"
        row.problems = [p for p in row.problems if "方向未知" not in p]
        row.problems.append("方向未知")
    semantics.self_check(parsed)
    return parsed


def test_文字层自检通过就不去问OCR(ocr_calls):
    """没必要多走一趟：文字层是精确取字，又不花钱。"""
    calls, _state = ocr_calls
    parsed = service._parse_pdf(None, STATEMENT_PDF.read_bytes())
    assert parsed.diagnosis == [], parsed.diagnosis
    assert len(parsed.rows) == 4
    assert calls == [], "文字层自检通过时不该调用 OCR"


def test_文字层自检不过就问一次OCR并采用干净的那份(ocr_calls, monkeypatch):
    """文字层解析不出来（Linux 上的那种）时，OCR 是第二意见。"""
    calls, state = ocr_calls
    state["grid"] = _ocr_grid()

    # 模拟「文字层自检不过」：把文字层那一路换成一份有诊断的结果。
    text_parsed = _linux_like(semantics.parse_statement(STATEMENT_PDF.read_bytes()))
    assert text_parsed.diagnosis, "前置条件：文字层这一路自检不过"

    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: text_parsed)

    parsed = service._parse_pdf(None, STATEMENT_PDF.read_bytes())
    assert calls, "文字层自检不过时必须问一次 OCR"
    assert parsed.via_ocr is True
    assert parsed.diagnosis == [], parsed.diagnosis
    assert len(parsed.rows) == 4
    assert str(parsed.income_total) == "228000.00"
    assert str(parsed.expense_total) == "1568050.00"


def test_两路都不干净时合并并按自检取舍(ocr_calls, monkeypatch):
    """两路各认出一半 → 合并；**自检当裁判**。

    光大那份实测：文字层认得出金额、认不出方向；OCR 认得出方向、
    金额列**整体右移一位**（多识别出一个空格子）。合并起来才是一行
    完整的流水 —— 而哪一份对，由自检（拿文件自称的笔数与合计对账）裁定。
    """
    _calls, state = ocr_calls
    state["grid"] = _ocr_grid(CEB_OCR_MARKDOWN)

    text_parsed = _linux_like(semantics.parse_statement(CEB_PDF.read_bytes()))
    assert text_parsed.diagnosis, "前置条件：文字层这一路自检不过"

    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: text_parsed)

    parsed = service._parse_pdf(None, CEB_PDF.read_bytes())
    # 金额取文字层（精确取字），方向取 OCR（识别）。
    assert [str(row.amount) for row in parsed.rows] == ["700.00", "130500000.00"]
    assert [row.direction for row in parsed.rows] == ["IN", "IN"]
    # 自检过了：文件自称收入 2 笔、合计 130500700.00，解析出来分毫不差。
    assert parsed.diagnosis == [], parsed.diagnosis
    assert str(parsed.income_total) == "130500700.00"
    assert parsed.via_merge is True
    # 方向补齐了，行上就不该再挂着「方向未知」——
    # 挂着它，这一行会被标成「先定方向」，既不能制证也不能跟发票匹配。
    assert all("方向未知" not in row.problems for row in parsed.rows)


def test_合并补上方向后自检的结论要重算(ocr_calls, monkeypatch):
    """**这条是「合并完就没动静」的看门狗。**

    合并补上方向之后，「有 N 行认不出收支方向」这条**必须消失**。
    曾经不消失：自检的结论是在解析末尾追加的，而合并之后没人重跑自检，
    于是界面上一直挂着一条**已经不成立**的抱怨（方向早就补齐了）。
    """
    _calls, state = ocr_calls
    state["grid"] = _ocr_grid(CEB_OCR_MARKDOWN)

    text_parsed = _linux_like(semantics.parse_statement(CEB_PDF.read_bytes()))
    assert any("认不出收支方向" in note for note in text_parsed.diagnosis), \
        "前置条件：文字层这一路认不出方向"

    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: text_parsed)

    parsed = service._parse_pdf(None, CEB_PDF.read_bytes())
    joined = " ".join(parsed.diagnosis)
    assert "认不出收支方向" not in joined, parsed.diagnosis
    assert "解析出 0 笔" not in joined, parsed.diagnosis
    assert parsed.diagnosis == [], parsed.diagnosis


def test_两路认得不一样要留下记录但不因此判解析不可信(ocr_calls, monkeypatch):
    """差异要**能被人看见**，但差异本身不是「解析失败」。

    光大那份的 OCR 金额列整体错位一位，于是余额、对方账号、对方户名
    三个字段两路认得不一样。哪一份对由自检裁定，**裁定完照样能记账** ——
    要是把差异当成失败，这份本来没问题的对账单就被判成「部分解析」，
    用户得多做一遍人工核对。
    """
    _calls, state = ocr_calls
    state["grid"] = _ocr_grid(CEB_OCR_MARKDOWN)

    text_parsed = _linux_like(semantics.parse_statement(CEB_PDF.read_bytes()))
    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: text_parsed)

    parsed = service._parse_pdf(None, CEB_PDF.read_bytes())
    assert parsed.merge_conflicts, "两路认得不一样，必须留下记录"
    assert "余额" in " ".join(parsed.merge_conflicts), parsed.merge_conflicts
    # 但自检过了 = 这份解析可信，状态不能变成「部分解析」。
    assert parsed.diagnosis == [], parsed.diagnosis
    assert "两路解析有" in service._parse_message(parsed)


def test_日期对不上就不合并(ocr_calls, monkeypatch):
    """宁可少一个方向，也不能错行。

    合并错行 = 把甲流水的金额记到乙头上 —— 那比缺一个方向糟得多。
    """
    _calls, state = ocr_calls
    state["grid"] = _ocr_grid(CEB_OCR_MARKDOWN)

    text_parsed = _linux_like(semantics.parse_statement(CEB_PDF.read_bytes()))
    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: text_parsed)

    real_parse_grid = semantics.parse_grid
    shifted = {"n": 0}

    def shifting_parse_grid(grid, **kwargs):
        parsed = real_parse_grid(grid, **kwargs)
        shifted["n"] += 1
        if shifted["n"] == 1 and parsed.rows:
            # 把第一行的日期挪开一天 —— 两路不再对齐
            parsed.rows[0].tx_date = parsed.rows[0].tx_date.replace(day=1)
        return parsed

    monkeypatch.setattr(semantics, "parse_grid", shifting_parse_grid)

    parsed = service._parse_pdf(None, CEB_PDF.read_bytes())
    assert [row.direction for row in parsed.rows] == ["UNKNOWN", "UNKNOWN"], \
        "日期对不上时不能合并"
    assert any("认不出收支方向" in note for note in parsed.diagnosis), \
        parsed.diagnosis


def test_文字层的数字被截断时以自检为准改用OCR(ocr_calls, monkeypatch):
    """**自检当裁判**的另一半：文字层认出来的数**对不上**时，改听 OCR 的。

    Linux 上「18050.00」被截成「1805」就是这种形态：方向对、笔数对，
    只有金额是错的。合并有两种口径（文字层优先 / OCR 优先），哪一种的
    **合计与文件自称的对得上**，就采哪一种。

    **两路都得脏**，否则根本走不到合并（OCR 那份干净时会被直接采用，
    合并这条路压根不会执行 —— 那种用例是**假绿**）。
    """
    _calls, state = ocr_calls
    state["grid"] = _ocr_grid()

    # 文字层：金额被截断（18050.00 → 1805.00），方向是准的。
    text_parsed = semantics.parse_statement(STATEMENT_PDF.read_bytes())
    for row in text_parsed.rows:
        if row.amount is not None:
            row.amount = row.amount / 10        # 截断
    semantics.self_check(text_parsed)
    assert text_parsed.diagnosis, "前置条件：截断之后文字层自检不过"

    # OCR：金额是准的，但方向认不出来（同样自检不过）。
    real_parse_grid = semantics.parse_grid

    def direction_less_parse_grid(grid, **kwargs):
        parsed = real_parse_grid(grid, **kwargs)
        for row in parsed.rows:
            row.direction = "UNKNOWN"
        semantics.self_check(parsed)
        assert parsed.diagnosis, "前置条件：OCR 这一路自检也不过"
        return parsed

    monkeypatch.setattr(semantics, "parse_grid", direction_less_parse_grid)
    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: text_parsed)

    parsed = service._parse_pdf(None, STATEMENT_PDF.read_bytes())
    assert parsed.diagnosis == [], parsed.diagnosis
    assert str(parsed.income_total) == "228000.00", parsed.income_total
    assert str(parsed.expense_total) == "1568050.00", parsed.expense_total


def test_OCR串错列的数字不能填进对方户名(ocr_calls, monkeypatch):
    """自由文本字段**不收纯数字**。

    **为什么**：OCR 认歪列时，错位的那一格通常是**数字**（金额、余额、
    账号）。光大那份实测就是「对方名称」那一栏落进来一串余额数字。
    自由文本没法自校验（金额能验「是不是一个数」），只能按形态拒掉
    最可疑的那一类 —— 对方户名填错，流水就配到了错的科目上。
    """
    _calls, state = ocr_calls
    state["grid"] = _ocr_grid(CEB_OCR_MARKDOWN)

    text_parsed = _linux_like(semantics.parse_statement(CEB_PDF.read_bytes()))
    for row in text_parsed.rows:
        row.counterparty_name = ""          # 文字层这一路没认出对方户名
    semantics.self_check(text_parsed)

    real_parse_grid = semantics.parse_grid

    def shifted_parse_grid(grid, **kwargs):
        parsed = real_parse_grid(grid, **kwargs)
        for row in parsed.rows:
            row.counterparty_name = "130500700.00"   # 串错列：落进来的是余额
        return parsed

    monkeypatch.setattr(semantics, "parse_grid", shifted_parse_grid)
    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: text_parsed)

    parsed = service._parse_pdf(None, CEB_PDF.read_bytes())
    assert all(row.counterparty_name == "" for row in parsed.rows), \
        [row.counterparty_name for row in parsed.rows]
    assert any("串错列" in note for note in parsed.merge_conflicts), \
        parsed.merge_conflicts


def test_没有文本层又没配OCR时给出明确报错(ocr_calls):
    """报错要说清**用户下一步能做什么**（外层还会补上「原件已保留」）。"""
    calls, state = ocr_calls
    state["grid"] = None
    blank = b"%PDF-1.4\n"  # 空壳，没有文字层
    with pytest.raises(ValueError) as excinfo:
        service._parse_pdf(None, blank)
    message = str(excinfo.value)
    assert "OCR" in message
    assert "Excel" in message, message


def test_文字层自检不过时不会去问两次OCR(ocr_calls, monkeypatch):
    """OCR 是「第二意见」，不是「反复问」—— 一次就够。"""
    calls, state = ocr_calls
    state["grid"] = _ocr_grid()

    text_parsed = _linux_like(semantics.parse_statement(STATEMENT_PDF.read_bytes()))
    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: text_parsed)

    service._parse_pdf(None, STATEMENT_PDF.read_bytes())
    assert len(calls) == 1, calls


def test_没有文本层的文件走的是OCR那一路(ocr_calls, monkeypatch):
    """扫描件（没有文字层）也要能解析 —— 那是 OCR 存在的原意。"""
    calls, state = ocr_calls
    state["grid"] = _ocr_grid()

    def no_text_layer(data: bytes) -> bool:
        return False

    monkeypatch.setattr(pdf_table, "has_text_layer", no_text_layer)

    parsed = service._parse_pdf(None, STATEMENT_PDF.read_bytes())
    assert calls, "没有文字层时必须问 OCR"
    assert parsed.via_ocr is True
    assert parsed.diagnosis == [], parsed.diagnosis
