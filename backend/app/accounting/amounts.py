"""共享金额解析器：凭证导入与期初导入**同一套**口径（审查报告 P2-3）。

两条导入路径读的是**同一种外部 Excel**，却在 2026-10 之前各写了一份金额
解析器：期初那边做了 NFKC 归一（全角数字 / 全角小数点 / 空格当千分位），
凭证那边没做。于是同一份表里同一个金额单元格，走凭证导入整批被拒、走期初
导入却读得进来 —— 用户会认为「期初能导，凭证不能导，是凭证导入坏了」。

本模块把两边**合成一份**，下面这些写法**一次认全**：

===================  ==========================================
写法                 结果
===================  ==========================================
``1,234.56``         1234.56
``1 234.56``         1234.56（空格当千分位）
``１２３４．５６``     1234.56（全角数字 + 全角小数点）
``１，２３４．５６``   1234.56（全角数字 + 全角逗号）
``￥1,234.56``       1234.56
``人民币1234.56``    1234.56
``－598.50``         -598.50（U+2212 数学减号 / 各种减号变体）
``(1,234.56)``       -1234.56（会计惯例的括号负数）
===================  ==========================================

**精度口径不在这里**：两位小数、四舍五入、超量级一律拒，仍由
:func:`app.accounting.util.money` 独家负责 —— 它是全系统唯一的那一份。
本模块只做「写法归一」，两条导入路径共用，避免两处各写一份然后再次漂移。
"""
from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal
from typing import Any

from app.accounting.util import money

# 会计惯例里的括号负数：(1,234.56) / （1,234.56）
_PARENTHESES = re.compile(r"^[（(]\s*(.+?)\s*[）)]$")

# 货币前缀：￥ / ¥ / 人民币 / RMB
_CURRENCY_PREFIX = re.compile(r"^(?:[¥￥]|人民币|rmb)\s*", re.IGNORECASE)

# NFKC **不**处理这些减号：U+2212（数学减号）、U+2013/U+2014（短横/长横）
# 都不会被归一成 ASCII 的 '-'。不显式换掉，``money()`` 就会把它判成非法金额。
_MINUSES = {
    "−": "-",   # − MINUS SIGN
    "–": "-",   # – EN DASH
    "—": "-",   # — EM DASH
    "﹣": "-",   # ﹣ SMALL HYPHEN-MINUS
}

# 千分位分隔符：逗号（中英文）与各种空白
_SEPARATORS = re.compile(r"[,\s]")


def _cell_text(value: Any) -> str:
    """把单元格稳定转成文本。

    数字型的 ``1001.0`` 归成 ``1001``（Excel 把整数编码存成浮点，
    不这么处理就永远对不上源文件里的「1001」）。
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value).strip()


def normalize_amount_text(raw: Any) -> str:
    """把金额单元格归一成 :func:`app.accounting.util.money` 能直接吃的文本。

    归一内容：NFKC（全角数字 / 全角小数点 / 全角逗号 / 全角括号 / 货币符号）、
    各种减号变体 → ``-``、货币前缀去掉、千分位分隔符（逗号与空白）去掉、
    括号负数 → 前置负号。
    """
    text = unicodedata.normalize("NFKC", _cell_text(raw))
    for bad, good in _MINUSES.items():
        text = text.replace(bad, good)
    text = text.strip()
    negative = False
    matched = _PARENTHESES.match(text)
    if matched:
        negative = True
        text = matched.group(1).strip()
    text = _CURRENCY_PREFIX.sub("", text).strip()
    text = _SEPARATORS.sub("", text)
    if negative and not text.startswith("-"):
        text = "-" + text
    return text


def is_percent(raw: Any) -> bool:
    """这个单元格是不是百分号写法（金额列里不该出现百分号）。

    整列从别处粘串来的时候，99% 的单元格都带百分号 —— 那是**格式**问题，
    报出来比静默当 0 好（静默当 0 会让一张凭证的金额凭空消失）。
    """
    if raw is None or isinstance(raw, bool):
        return False
    if isinstance(raw, (int, float, Decimal)):
        return False
    return normalize_amount_text(raw).endswith("%")


def parse_amount(raw: Any) -> Decimal:
    """把一个金额单元格解析成两位小数的 Decimal。**解析不了就抛 ValueError。**

    ``None`` 与空串按 0 处理（单元格没填）；``bool`` 一律拒
    （``True`` 不是金额）；数值直接交给 :func:`money`；
    文本先归一再交给 :func:`money`。
    """
    if raw is None:
        return Decimal("0")
    if isinstance(raw, bool):
        raise ValueError(f"{raw!r} 不是合法金额")
    if isinstance(raw, (int, float, Decimal)):
        return money(raw)
    text = normalize_amount_text(raw)
    if not text:
        return Decimal("0")
    return money(text)


__all__ = ["is_percent", "normalize_amount_text", "parse_amount"]
