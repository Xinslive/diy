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
* 两份都不干净 → 取问题更少的那份，并把诊断带出去（不静默）。
"""
from __future__ import annotations

import pathlib

import pytest

from app.bankstatement import pdf_table, service

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
OCR_MARKDOWN = FIXTURES / "ocr" / "cib_statement_ocr_markdown.md"
STATEMENT_PDF = FIXTURES / "statements" / "cib_pdf.pdf"


def _ocr_grid():
    """OCR 那一路给出的网格：用真实 OCR 输出走同一条转换链。"""
    from app.invoice.ocr_client import _markdown_to_plain_text

    text = _markdown_to_plain_text(OCR_MARKDOWN.read_text(encoding="utf-8"))
    lines = [line for line in text.splitlines() if line.strip()]
    return pdf_table.grid_from_text_lines(lines)


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
    dirty = pdf_table.extract_grid(STATEMENT_PDF.read_bytes())
    assert dirty is not None
    from app.bankstatement import semantics
    text_parsed = semantics.parse_grid(dirty)
    text_parsed.diagnosis.append("人为制造的自检不过")

    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: text_parsed)

    parsed = service._parse_pdf(None, STATEMENT_PDF.read_bytes())
    assert calls, "文字层自检不过时必须问一次 OCR"
    assert parsed.via_ocr is True
    assert parsed.diagnosis == [], parsed.diagnosis
    assert len(parsed.rows) == 4
    assert str(parsed.income_total) == "228000.00"
    assert str(parsed.expense_total) == "1568050.00"


def test_两路都不干净时取问题更少的那份并留下诊断(ocr_calls, monkeypatch):
    """不静默：诊断一定要带到界面上。"""
    _calls, state = ocr_calls
    state["grid"] = _ocr_grid()

    from app.bankstatement import semantics
    real_parse_grid = semantics.parse_grid

    # OCR 这一路：解析得出来，但自检也不过（比文字层多一条问题）。
    def dirty_parse_grid(grid, **kwargs):
        parsed = real_parse_grid(grid, **kwargs)
        parsed.diagnosis.append("OCR 这一路的问题")
        parsed.diagnosis.append("OCR 这一路的第二个问题")
        return parsed

    monkeypatch.setattr(semantics, "parse_grid", dirty_parse_grid)

    # 文字层这一路：只有一条问题 → 按「问题更少」应该选它。
    dirty = real_parse_grid(pdf_table.extract_grid(STATEMENT_PDF.read_bytes()))
    dirty.diagnosis.append("文字层的问题")
    monkeypatch.setattr(semantics, "parse_statement",
                        lambda data, **kwargs: dirty)

    parsed = service._parse_pdf(None, STATEMENT_PDF.read_bytes())
    assert parsed.diagnosis, "两路都不干净时诊断必须留着"
    assert parsed.diagnosis == ["文字层的问题"]
    assert parsed.via_ocr is False


def test_没有文本层又没配OCR时给出明确报错(ocr_calls):
    """连报错都要说清楚：原件留着了，用户能下载。"""
    calls, state = ocr_calls
    state["grid"] = None
    blank = b"%PDF-1.4\n"  # 空壳，没有文字层
    with pytest.raises(ValueError) as excinfo:
        service._parse_pdf(None, blank)
    assert "原件已保留" in str(excinfo.value)
