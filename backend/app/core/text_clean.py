"""文本清洗：**XML 1.0 非法字符**的单一出处。

**为什么要有这个模块**：Excel 底层是 XML，lxml 遇到 XML 1.0 不允许的字符
直接抛

    ValueError: All strings must be XML compatible:
    Unicode or ASCII, no NULL bytes or control characters

抛在**写文件**的环节 —— 不是那一格没写上，是整个 xlsx 一个字节都生成不出来，
接口 500，前端只显示「导出失败」。一行坏字符能拖垮一份导出件，不管那份有多少行
（生产事故：导出「管理费用明细账」1934 行直接失败，实测 9360 行 / 5.5 秒 / 31 MB
从来不是撑不住，是撞上了一个坏字符）。

脏字符的现实来源是**发票 PDF 的文字层**：pypdfium2 的 ``get_text_range()``
会把 UTF-16 的 BOM 在某些字形映射里漏成 U+FFFE，混进摘要，例如
「*电线电缆*广州五羊ZC\\ufffeBV2.5平方线」。这份字符被原样存进
``entry.summary``，之后每一条含它的明细账/总账导出都会整份失败。

**三层防线**（缺一不可）：

1. **入口**（本模块 + ``app/models/text_guard.py``）：写进库之前就洗掉，
   以后不再攒新的脏字符。
2. **导出层**（``app/printing/excel.py`` 的 ``_scrub_workbook``）：落盘前把整本
   工作簿扫一遍 —— 历史脏数据、以及将来有人绕过入口的写入点，都由它兜住。
3. **解析层**（``app/invoice/text_parser.py``）：PDF/OCR 抽出来的文字当场洗。

**这个模块不依赖 app 里的任何东西**（零 import），所以模型层、打印层、发票层
都能引它，不会绕成环。
"""
from __future__ import annotations

import re

#: **XML 1.0 不允许写进文档的字符**：
#:
#:   * C0 控制字符（U+0000–U+001F），除 TAB(U+0009) / LF(U+000A) / CR(U+000D)
#:   * C1 控制字符（U+007F–U+009F）
#:   * U+FFFE / U+FFFF —— 任何上下文下都非法
#:
#: 后面这组不是「少见」，是**有前科的**：PDF 文字层漏出来的 UTF-16 BOM。
_ILLEGAL_XML_CHARS = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffe\uffff]")

#: XML 1.0 **允许**的三个控制字符。它们在内容里合法，但会让表格单元格的
#: 版面被撑开（一个换行就是一格多高），所以在**写进 Excel** 的那一步换成
#: 等宽空格；写进数据库时保留（解析器按行切分要靠它们定位）。
_XML_SAFE_CONTROL = {"\x09": " ", "\x0a": " ", "\x0d": " "}


def scrub_xml_chars(text: str) -> str:
    """去掉 XML 1.0 写不出去的控制字符，其余内容原样保留。

    TAB/LF/CR 换成等宽空格（合法但会撑开单元格版面）。

    这是**写进 Excel 前**用的那一份。
    """
    if not text:
        return text
    for bad, good in _XML_SAFE_CONTROL.items():
        if bad in text:
            text = text.replace(bad, good)
    return _ILLEGAL_XML_CHARS.sub("", text)


def scrub_control_chars(text: str) -> str:
    """去掉 XML 1.0 写不出去的控制字符，**保留 TAB/LF/CR**。

    这是**写进数据库前**用的那一份：解析器按行切分要靠 TAB/LF/CR 定位，
    把它们换成空格会把一张发票的表格结构拆坏。

    数据库这一层为什么也要洗：库里躺着一个 U+FFFE，Excel 导出会整份失败；
    而 Excel 那侧的清洗是**导出时**做的 —— 库里那份原样留着，下次别的
    通道（报表、打印、MCP 读取）照样会踩到。
    """
    if not text:
        return text
    return _ILLEGAL_XML_CHARS.sub("", text)


def has_illegal_xml_chars(text: str) -> bool:
    """这段文字里有没有 XML 写不出去的控制字符。"""
    if not text:
        return False
    return bool(_ILLEGAL_XML_CHARS.search(text))
