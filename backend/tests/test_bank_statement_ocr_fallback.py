"""流水对账的 OCR 兜底：真实 OCR 输出 → 文本行 → 网格 → 流水行。

**为什么要有这条链。** 银行对账单里有一部分是**扫描件**（没有文字层），
对账单解析的第一层（按坐标还原）直接用不了。兜底是把文件交给外部 OCR
服务（PaddleOCR-VL），拿回文本再走「纯文本切分」那一层。

**夹具是真实 OCR 输出**，不是手工编的：2026-10-27 用生产上那套配置
（``paddleocr.aistudio-app.com`` + ``PaddleOCR-VL-1.6``）跑一份兴业对账单
拿回来的 markdown，原样存下来。

**这条链上有三个曾经把结果毁掉的地方**，这里逐个钉住：

1. **OCR 返回的表格是 HTML，不是 Markdown 管道表。** 不拆开的话，
   整张表会变成**一行文本**，下游按行切的逻辑全废（表头认不出来，
   一行流水都出不来）。
2. **格内换行有两个形态**：真的换行，和 OCR 用来表示换行的**字面量
   ``\\n``**（两个字符）。只处理真的换行，格子里就会留下「1805\\n0.00」
   这么一串字面量，金额当场认不出来（真实金额是 18050.00）。
3. **格内换行不加分隔符**：版面上「1805」与「0.00」上下两行是**同一个
   金额**被拆开，不是两个数。
"""
from __future__ import annotations

import pathlib

from app.bankstatement import pdf_table, semantics
from app.invoice.ocr_client import _markdown_to_plain_text

FIXTURE = (pathlib.Path(__file__).resolve().parent
           / "fixtures" / "ocr" / "cib_statement_ocr_markdown.md")


def _ocr_lines() -> list[str]:
    text = _markdown_to_plain_text(FIXTURE.read_text(encoding="utf-8"))
    return [line for line in text.splitlines() if line.strip()]


def test_OCR返回的HTML表格被拆成一行一格():
    """表头 9 列、数据 4 行 —— 列数对得上，才谈得上按位置对号入座。"""
    lines = _ocr_lines()
    grid = pdf_table.grid_from_text_lines(lines)
    assert grid is not None
    assert len(grid.rows[0]) == 9, grid.rows[0]
    # 表头 + 4 行流水 + 表尾的几行合计
    assert len(grid.rows) == 8, len(grid.rows)


def test_OCR兜底解析出的流水与金标准一致():
    """这是 OCR 这条路真正的验收：金额、方向、合计都得对。"""
    parsed = semantics.parse_grid(pdf_table.grid_from_text_lines(_ocr_lines()))
    assert parsed.header_row == 0
    assert len(parsed.rows) == 4, parsed.rows
    assert parsed.diagnosis == [], parsed.diagnosis

    first = parsed.rows[0]
    assert (first.tx_date.isoformat(), first.direction, str(first.amount)) \
        == ("2026-09-26", "OUT", "18050.00"), first
    last = parsed.rows[-1]
    assert (last.tx_date.isoformat(), last.direction, str(last.amount)) \
        == ("2026-09-03", "OUT", "50000.00"), last

    # 合计：文件自称收入 228000.00、支出 1568050.00，解析出来必须对得上
    assert str(parsed.income_total) == "228000.00", parsed.income_total
    assert str(parsed.expense_total) == "1568050.00", parsed.expense_total


def test_格内换行的字面量也要接起来():
    """OCR 用两个字符的 ``\\n`` 表示换行；忘了处理就留在金额里。"""
    lines = _ocr_lines()
    row = next(line for line in lines if "高速公路通行费" in line)
    assert "18050.00" in row, row
    assert "\\n" not in row, row


def test_对账单里的表尾合计行被认成页脚而不是流水():
    """「本期总收入金额：228000.00」是表尾，不能当成一笔流水。"""
    parsed = semantics.parse_grid(pdf_table.grid_from_text_lines(_ocr_lines()))
    assert len(parsed.rows) == 4
    assert all(row.amount is not None for row in parsed.rows)
    assert str(parsed.meta.declared_income_total) == "228000.00"
    assert str(parsed.meta.declared_expense_total) == "1568050.00"
