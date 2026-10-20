"""凭证导入与期初导入的金额解析器**必须是同一套**（审查报告 P2-3）。

两条导入路径读的是**同一种外部 Excel**，却在 2026-10 之前各写了一份金额
解析器：期初那边做了 NFKC 归一（全角数字 / 全角小数点 / 全角逗号 / 空格
当千分位 / U+2212 负号），凭证那边没做。于是同一张表里同一个金额单元格，
走凭证导入整批被拒、走期初导入却读得进来 —— 用户会认为「期初能导，
凭证不能导，是凭证导入坏了」。

还有一条内部不一致：``１２３４``（全角数字、无小数点）在凭证侧能解析成功
（Python 的 ``Decimal`` 接受 Unicode 数字），而 ``１２３４．５６`` 不行
（``．`` 不是 ``Decimal`` 接受的字符）—— **同一个全角数字串，加不加小数点
结果相反**。那不是设计，是 ``Decimal`` 的行为漏出来了。

现在两边共用 :mod:`app.accounting.amounts` 一份实现，下面把这些写法一次
认全。
"""
from __future__ import annotations

import types
from decimal import Decimal

import pytest

from app.accounting import amounts
from app.accounting.importer import _parse_amount, parse_workbook
from app.accounting.opening_importer import _amount, parse_opening_rows

#: (单元格写法, 应当解析出的金额)
CASES = [
    ("1234.56", Decimal("1234.56")),
    ("1,234.56", Decimal("1234.56")),
    ("1 234.56", Decimal("1234.56")),
    ("１２３４", Decimal("1234.00")),
    ("１２３４．５６", Decimal("1234.56")),
    ("１，２３４．５６", Decimal("1234.56")),
    ("¥1,234.56", Decimal("1234.56")),
    ("￥1,234.56", Decimal("1234.56")),
    ("人民币1,234.56", Decimal("1234.56")),
    ("-598.50", Decimal("-598.50")),
    ("－598.50", Decimal("-598.50")),        # U+FF0D 全角减号
    ("−598.50", Decimal("-598.50")),        # U+2212 数学减号
    ("(1,234.56)", Decimal("-1234.56")),     # 会计惯例的括号负数
    ("（1,234.56）", Decimal("-1234.56")),   # 全角括号负数
    (1234.56, Decimal("1234.56")),
    (1234, Decimal("1234.00")),
    (Decimal("1234.56"), Decimal("1234.56")),
]


@pytest.mark.parametrize("raw,expected", CASES)
def test_两条导入路径对同一个写法给同一个结果(raw, expected):
    """同一个金额单元格，走凭证导入与走期初导入必须得到**同一个数**。"""
    voucher_amount, error = _parse_amount(raw)
    assert error is None, f"凭证导入侧不认 {raw!r}：{error}"
    assert voucher_amount == expected, f"凭证导入侧把 {raw!r} 读成了 {voucher_amount}"
    assert _amount(raw) == expected, f"期初导入侧把 {raw!r} 读成了 {_amount(raw)}"


@pytest.mark.parametrize("raw,expected", CASES)
def test_共享解析器本身(raw, expected):
    assert amounts.parse_amount(raw) == expected


def test_空与零():
    assert amounts.parse_amount(None) == Decimal("0")
    assert amounts.parse_amount("") == Decimal("0")
    assert amounts.parse_amount("  ") == Decimal("0")
    assert amounts.parse_amount(0) == Decimal("0.00")


def test_解析不了要抛出来而不是静默变0():
    """金额 0 与「解析不了」必须分得开 —— 静默变 0 会让凭证凭空少记金额。"""
    with pytest.raises(ValueError):
        amounts.parse_amount("金额待定")
    with pytest.raises(ValueError):
        amounts.parse_amount(True)   # bool 不是金额


def test_百分比是格式问题不是金额():
    assert amounts.is_percent("50%")
    assert not amounts.is_percent("50")


def test_凭证导入整批认得全角金额(db, book):
    """端到端：一张金额列写全角数字的凭证表，要能正常入库。"""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"])
    ws.append(["2026-01-15", "记-1", "x", "1002", "１，２３４．５６", None])
    ws.append([None, None, "x", "5001", None, "1 234.56"])
    buf = io.BytesIO()
    wb.save(buf)

    parsed = parse_workbook(buf.getvalue(), book.id, db)
    voucher = parsed["vouchers"][0]
    assert voucher["ok"] is True, voucher["errors"]
    assert [Decimal(e["amount"]) for e in voucher["entries"]] == [
        Decimal("1234.56"), Decimal("1234.56")]


def test_期初导入认得同一批写法():
    """端到端：期初那侧对同一批写法给出同样的数。"""
    by_code = {"1121": types.SimpleNamespace(id=1, code="1121", name="银行存款-工商",
                                            direction="DEBIT", is_leaf=True)}
    by_name = {"银行存款-工商": by_code["1121"]}
    grid = [
        ["会计年度", "会计期间", "科目编码", "科目名称", "外币名称",
         "期初借方", "期初贷方", "本期发生借方", "本期发生贷方", "期末借方", "期末贷方"],
        ["2026", "月份：2026.10-2026.10", "1121", "银行存款-工商", "", "１，２３４．５６",
         0.0, 0.0, 0.0, 0.0, 0.0],
    ]
    out = parse_opening_rows(grid, subjects_by_code=by_code, subjects_by_name=by_name)
    assert out["rows"], out
    assert Decimal(out["rows"][0]["amount"]) == Decimal("1234.56")
