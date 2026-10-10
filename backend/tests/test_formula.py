"""报表公式引擎。

刻意不用 ``eval``：公式内容来自数据库里的模板，而模板是财务能编辑的。
递归下降解析器多写三百行，换来「公式永远跑不出沙箱」。
"""
from decimal import Decimal

import pytest

from app.reports.formula import Evaluator, FormulaError, col_to_no, no_to_col


class FakeCtx:
    """最小可用的求值上下文。真实实现是 app.reports.evaluate.ReportContext。"""

    def __init__(self, cells=None, figures=None, subjects=None):
        # cells: {"B2": 10}
        # figures: {"1002": {"closing_amt": "300"}} —— 键是 ctx 原语的方法名，
        #          对应公式里「期末余额」这类中文口径在 _f_subject 里映射到的名字。
        self.cells = {k: Decimal(str(v)) for k, v in (cells or {}).items()}
        self.figures = figures or {}
        self.subjects = {c: i + 1 for i, c in enumerate(subjects if subjects is not None else self.figures)}

    def cell(self, row, col):
        # 与真实的 evaluate.ReportContext.cell 保持一致：
        # 空单元格（没有公式）返回 0，而不是报错 —— 报表里大量引用是有意留空的。
        return self.cells.get(f"{no_to_col(col)}{row}", Decimal(0))

    def subject_id(self, code):
        return self.subjects.get(code)

    def resolve_period(self, expr):
        return expr or "P"

    def _fig(self, code, kind):
        return Decimal(str(self.figures.get(code, {}).get(kind, 0)))

    def opening(self, sid, p):
        return self._fig(self._code(sid), "opening")

    def debit_cum(self, sid, p):
        return self._fig(self._code(sid), "dr_cum")

    def credit_cum(self, sid, p):
        return self._fig(self._code(sid), "cr_cum")

    def closing(self, sid, p):
        return self._fig(self._code(sid), "closing")

    def period_dr(self, sid, p):
        return self._fig(self._code(sid), "period_dr")

    def period_cr(self, sid, p):
        return self._fig(self._code(sid), "period_cr")

    def period_amount(self, sid, p):
        return self._fig(self._code(sid), "period_amount")

    def ytd_amount(self, sid, p):
        return self._fig(self._code(sid), "ytd_amount")

    def closing_amt(self, sid, p):
        return self._fig(self._code(sid), "closing_amt")

    def opening_amt(self, sid, p):
        return self._fig(self._code(sid), "opening_amt")

    def _code(self, sid):
        return next(c for c, i in self.subjects.items() if i == sid)


def run(src, ctx=None):
    return Evaluator(ctx or FakeCtx()).run(src)


CELLS = FakeCtx(cells={"B2": 10, "B3": 20, "B4": 30, "B17": 110, "C17": 10})


# --------------------------------------------------------------------- 算术与引用


@pytest.mark.parametrize(
    "src,expect",
    [
        ("1+2", "3"),
        ("10-4", "6"),
        ("3*4", "12"),
        ("7/2", "3.5"),
        ("1+2*3", "7"),            # 优先级：先乘后加
        ("(1+2)*3", "9"),
        ("-5+8", "3"),             # 一元负号
        ("=B17", "110"),           # 允许像 Excel 一样以 = 开头
        ("B17+C17", "120"),
        ("SUM(B2:B4)", "60"),      # 区域
        ("SUM(B2,B4)", "40"),      # 多参数
        ("B2:B3", "30"),
        ("合计(B2,B3)", "30"),     # 中文别名
    ],
)
def test_算术与区域(src, expect):
    assert run(src, CELLS) == Decimal(expect)


@pytest.mark.parametrize(
    "src,expect",
    [("1<2", True), ("1>2", False), ("1<=1", True), ("2>=3", False),
     ("1=1", True), ("1<>1", False), ("1!=2", True)],
)
def test_比较运算符(src, expect):
    assert run(src) == expect


def test_比较可以被IF消费():
    assert run("IF(1>0,100,200)") == Decimal("100")
    assert run("IF(1<0,100,200)") == Decimal("200")


def test_上期取左边一格():
    # C17 的上期是 B17
    assert run("上期(C17)", CELLS) == Decimal("110")
    # 左边是空单元格时返回 0（与报表里 A 列只有行标签的布局一致）
    assert run("上期(B17)", CELLS) == Decimal("0")


def test_上期只接受单元格():
    with pytest.raises(FormulaError):
        run("上期(1+2)")


def test_单元格别名():
    assert run("单元格(B2)", CELLS) == Decimal("10")


def test_函数():
    assert run("ROUND(3.14159,2)") == Decimal("3.14")
    assert run("四舍五入(3.14159,2)") == Decimal("3.14")
    assert run("ABS(0-7)") == Decimal("7")
    assert run("绝对值(0-7)") == Decimal("7")
    assert run("MAX(1,9,3)") == Decimal("9")
    assert run("MIN(1,9,3)") == Decimal("1")


def test_函数名大小写不敏感():
    assert run("sum(1,2)") == Decimal("3")
    assert run("Round(1.234,1)") == Decimal("1.2")


# --------------------------------------------------------------------- 科目取数


def test_科目取数按口径():
    ctx = FakeCtx(figures={"1002": {"closing_amt": "300", "opening_amt": "100",
                                    "period_amount": "200", "closing": "250"}})
    assert run('科目("1002","期末余额")', ctx) == Decimal("300")
    assert run('科目("1002","期初余额")', ctx) == Decimal("100")
    assert run('科目("1002","本期金额")', ctx) == Decimal("200")
    assert run('科目("1002","期末")', ctx) == Decimal("250")
    # 第二个参数省略时默认取「期末」
    assert run('科目("1002")', ctx) == Decimal("250")


def test_科目取数支持期间占位符():
    ctx = FakeCtx(figures={"1002": {"closing": "1"}})
    # 占位符原样交给 resolve_period 解析，这里只验证它能被解析器识别并透传
    for ph in ("{{本期}}", "{{上期}}", "{{年初}}"):
        assert run(f'科目("1002","期末","{ph}")', ctx) == Decimal("1")


def test_空公式返回0而不是报错():
    assert run("") == Decimal("0")
    assert run("   ") == Decimal("0")


# --------------------------------------------------------------------- 错误处理


@pytest.mark.parametrize(
    "src,frag",
    [
        ("1+", "不完整"),
        ("(1+2", "括号"),
        ("1++*2", "语法"),
        ("FOO(1)", "未知函数"),
        ("1 2", "语法"),
    ],
)
def test_非法公式必须报错(src, frag):
    with pytest.raises(Exception):
        run(src)


def test_空白单元格按0处理():
    # 与生产行为一致：引用空格子不算错，报表里这是常态
    assert run("ZZ99", CELLS) == Decimal("0")
    assert run("B2+ZZ99", CELLS) == Decimal("10")


def test_除零报明确错误():
    with pytest.raises(FormulaError):
        run("1/0")


def test_未知取数口径报可用值():
    ctx = FakeCtx(figures={"1002": {"closing": "1"}})
    with pytest.raises(FormulaError) as e:
        run('科目("1002","瞎写的口径")', ctx)
    assert "期末" in str(e.value)


def test_科目不存在报明确错误2():
    ctx = FakeCtx(figures={"1002": {"closing": "1"}})
    with pytest.raises(FormulaError) as e:
        run('科目("9999","期末")', ctx)
    assert "9999" in str(e.value)


def test_列名与列号互转():
    assert col_to_no("A") == 1 and col_to_no("Z") == 26 and col_to_no("AA") == 27
    assert no_to_col(1) == "A" and no_to_col(27) == "AA"
