"""运算符优先级（审计 P2-02）。

文法层级写反了::

    expr    := compare (('+'|'-') compare)*   # ← 加减法去调用"比较"
    compare := term   (cmpop term)*            # ← 比较去调用"乘除"

标准优先级是 比较 < 加减 < 乘除。写反之后，**含加减法的比较表达式
全部解析成非预期形状**：加减法被当成比较的左操作数，比较反而成了
加减法的右操作数。

    1 + 2 > 5     解析成  bin(+, 1, cmp(>, 2, 5))   求值 1
    (1 + 2) > 5   解析成  cmp(>, bin(+, 1, 2), 5)   求值 0

于是 ``IF(1+2>5,100,200)`` 拿到条件 1（非零）→ 返回 100，
而正确答案是 200。报表公式里"本期数 > 上期数才取差额"这类写法很常见，
全部会静默取错分支 —— 不报错，只是数字错了。

所以这里**断言语法树形状**，而不是只断言求值结果：只钉求值的话，
把解析器改成任何"碰巧算出同样数字"的形状都能过。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.reports.formula import Evaluator, Parser


class _Ctx:
    """最小求值上下文：只实现 cell()，够跑纯算术表达式。"""

    def __init__(self, cells=None):
        self._cells = cells or {}

    def cell(self, row, col):
        return self._cells.get((row, col), Decimal("0"))

    def resolve_period(self, _):
        return None

    def subject_id(self, _):
        return None


def tree(src: str):
    return Parser(src).parse()


def value(src: str, cells=None):
    return Evaluator(_Ctx(cells)).run(src)


# ------------------------------------------------------------------ 语法树形状


def test_加法先于比较结合():
    """1+2>5 必须解析成 (1+2) > 5，而不是 1 + (2>5)。"""
    assert tree("1+2>5") == (
        "cmp", ">", ("bin", "+", ("num", Decimal("1")), ("num", Decimal("2"))),
        ("num", Decimal("5")),
    )


def test_减法先于比较结合():
    assert tree("2>1+3") == (
        "cmp", ">", ("num", Decimal("2")),
        ("bin", "+", ("num", Decimal("1")), ("num", Decimal("3"))),
    )


def test_单元格加法与比较的优先级():
    assert tree("A1+B1>=C1") == (
        "cmp", ">=", ("bin", "+", ("cell", (1, 1)), ("cell", (2, 1))),
        ("cell", (3, 1)),
    )


def test_比较作为函数实参():
    assert tree("IF(1+2>5,100,200)") == (
        "call", "IF", [
            ("cmp", ">", ("bin", "+", ("num", Decimal("1")), ("num", Decimal("2"))),
             ("num", Decimal("5"))),
            ("num", Decimal("100")),
            ("num", Decimal("200")),
        ],
    )


# ------------------------------------------------------------------ 求值语义


@pytest.mark.parametrize("src,expect", [
    ("1+2>5", "0"),          # 3 > 5 假
    ("2>1+3", "0"),          # 2 > 4 假
    ("2>1+3", "0"),
    ("5>1+3", "1"),          # 5 > 4 真
    ("1+2<5", "1"),
    ("IF(1+2>5,100,200)", "200"),
    ("IF(5>1+3,100,200)", "100"),
    ("2*3>4+1", "1"),        # 乘除本来就比加减紧，保持不变
    ("10-2-3", "5"),         # 减法左结合
])
def test_求值结果(src, expect):
    assert value(src) == Decimal(expect)


def test_纯加减乘除行为不变():
    """改文法不该碰到不含比较的表达式。"""
    assert value("1+2*3") == Decimal("7")
    assert value("(1+2)*3") == Decimal("9")
    assert value("2*(3+4)") == Decimal("14")
    assert value("100/5-2") == Decimal("18")
    assert value("-1+2") == Decimal("1")


def test_连续比较的结合方向():
    """比较是左结合：1>2>0 解析成 (1>2)>0。"""
    assert tree("1>2>0") == (
        "cmp", ">", ("cmp", ">", ("num", Decimal("1")), ("num", Decimal("2"))),
        ("num", Decimal("0")),
    )
    assert value("1>2>0") == Decimal("0")


def test_嵌套括号仍能比较():
    assert value("IF((1+2)*(3)>9,1,0)") == Decimal("0")
    assert value("IF((1+2)*(4)>9,1,0)") == Decimal("1")
