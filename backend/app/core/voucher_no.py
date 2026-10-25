"""凭证号里的序号 —— **全系统只有这一份判据**。

**为什么要单独成模块**：凭证号是「凭证字 + 数字」（记0012）。这个数字决定凭证在
期间里的先后（列表「凭证号顺序」那一档、「前一张 / 后一张」翻的就是它），
但它是**字符串**：按字符串比，"记10" 排在 "记2" 前面 —— 列表里翻到第十张，
顺序却是乱的。

抠数字的判据以前散在两处（``accounting/vouchers._no_seq`` 一份 SQL 表达式、
``mcp/tools/detail._no_seq`` 一份 Python），两处各抠一次，早晚会分道扬镳。
现在合成一处，两边都从这里取。

**抠不出数字时给 -1**（排在最前，而不是报错或排到最后）：
库里真有过这种号 —— 2026-10-19 生产事故，"wochakan" 这个凭证号让凭证列表的
四种排序全部 500（``regexp_replace`` 无匹配时返回原字符串，外层的
``coalesce(... AS INTEGER, -1)`` 永远等不到 NULL）。抠不出就当它排最前，
脏号不拖垮整个列表；写路径那边另有闸门，新脏号进不来。

    from app.core.voucher_no import seq_expr, seq_of

    seq_expr(Voucher.voucher_no)   # SQL 表达式：按整数排
    seq_of("记0012")                # Python：12
"""
from __future__ import annotations

import re

from sqlalchemy import Integer, func

#: 尾部数字。SQL 与 Python 共用这一条正则 —— 两边抠出来的必须是同一个数。
_TRAILING_DIGITS = r"(\d+)$"


def seq_expr(col):
    """SQL 表达式：把一列凭证号折成「号段数字」。

    ``col`` 要是 ``Voucher.voucher_no`` 这样的列表达式；
    传一个字面量也能用（给一张**还没落库**的凭证算它将来排在第几号时用得上）。

    抠不出数字给 -1（见模块 docstring）。
    """
    digits = func.substring(col, _TRAILING_DIGITS)
    return func.coalesce(func.cast(digits, Integer), -1)


def seq_of(no: str | None) -> int:
    """Python 版：把一个凭证号折成「号段数字」，抠不出给 -1。

    与 :func:`seq_expr` 同一套判据（同一份正则）——
    两边必须对同一个号给出同一个数，
    ``tests/test_voucher_no.py`` 里有一条用例把两者对着掐。
    """
    if not no:
        return -1
    m = re.search(_TRAILING_DIGITS, str(no))
    if not m:
        return -1
    try:
        return int(m.group(1))
    except ValueError:      # 理论上到不了：正则只匹配数字
        return -1
