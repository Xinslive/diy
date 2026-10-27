"""OCR 返回的 HTML 表格 → 表格内容。

**为什么单独一个模块**：OCR 服务返回的表格是 **HTML**（``<table><tr><td>``），
不是 Markdown 管道表。发票那条路要「表格摊平成文字行」，对账单那条路要
「**格子的边界**」（摊平会丢掉空格子，列数于是与表头对不上）。两边都要，
所以放在这儿共用一份实现 —— 两份实现迟早会漂移，而漂移的表现是
「表格被认出来了，但金额认错」。

**三个坑**（都踩过）：

1. **HTML 表格常常占一整行**（不是一行一格）—— 不拆开的话，整张表会变成
   一行文本，下游按行切的逻辑全废。
2. **格内换行有两个形态**：真的换行，和 OCR 用来表示换行的**字面量 ``\\n``**
   （两个字符）。两者都要处理。
3. **格内换行不加分隔符**：版面上「1805」与「0.00」上下两行是**同一个金额**
   被拆开，不是两个数。
"""
from __future__ import annotations

import html
import re

#: 一整张表。
_HTML_TABLE = re.compile(r"<table[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
#: 表格里的一行。
_HTML_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
#: 一行里的一个格子（``th`` 是表头格）。
_HTML_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
#: 其余标签（``span`` / ``br`` / ``sup`` 之类）—— 只取里面的文字。
_HTML_TAG = re.compile(r"<[^>]+>")


def find_tables(markdown: str) -> list[str]:
    """markdown 里那些 HTML 表格（原文，不拆格）。"""
    return _HTML_TABLE.findall(markdown or "")


def cell_text(cell_html: str) -> str:
    """一个格子的文字。

    **格内换行不加分隔符** —— 见模块开头的坑 3。
    """
    text = _HTML_TAG.sub(" ", cell_html)
    text = html.unescape(text)
    # OCR 用**字面的两个字符** ``\n`` 表示格内换行（不是真换行），
    # 两者都要处理 —— 只当真换行的话，格子里会留下「1805\n0.00」这么
    # 一串字面量，金额当场认不出来。
    text = text.replace("\\n", "")
    text = re.sub(r"[\r\n\t]+", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def table_to_rows(table_html: str) -> list[list[str]]:
    """一张表 → 行（每行是这一行各格的文本，**空格子也占位**）。"""
    rows: list[list[str]] = []
    for row_html in _HTML_ROW.findall(table_html):
        cells = [cell_text(cell_html) for cell_html in _HTML_CELL.findall(row_html)]
        if cells:
            rows.append(cells)
    return rows


def table_to_lines(table_html: str) -> list[str]:
    """一张表 → 文字行（每行是这一行各格的空格连接）。

    **什么时候用**：只要文字、不要格边界的场合（发票那条路）。
    对账单那边要格边界 —— 摊平会丢掉空格子，列数与表头对不上。
    """
    return [" ".join(row) for row in table_to_rows(table_html)]
