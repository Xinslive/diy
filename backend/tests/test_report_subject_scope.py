"""报表公式：上级科目汇总 + 省掉函数名的简写。

两件事，都是同一个动机 —— 让公式能写得更短、更贴近「这张表上要的那一行」。

**上级科目可以直接取数。** 凭证只允许记到末级（``vouchers._validate_entries``
的硬闸），所以上级科目自己身上永远是 0。以前设计器因此只给末级科目，
``科目("1002","期末余额")`` 这种写法能解析、也能存，**取出来却是一串 0**，
而报表上不会报任何错 —— 银行账户那一行恒为 0，资产合计跟着少一大块，
看报表的人完全看不出少了什么。现在 ``ReportContext.subject_scope`` 把整棵
子树加总，取上级科目才有意义。

**``("1001","期末余额")`` 等同 ``科目("1001","期末余额")``。** 判据是
「``(`` 后紧跟字符串且括号里有顶层逗号」—— 分组表达式里出现逗号本来就是
语法错误，所以这条分支不改变任何一条现有公式的含义。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.reports.formula import FormulaError, parse_formula


def _ctx(db, book, periods, formulas=None):
    from app.reports.evaluate import EvalContext

    return EvalContext(db, book, periods[1], formulas or {})


def _run(db, book, periods, src: str) -> Decimal:
    from app.reports.formula import Evaluator

    return Evaluator(_ctx(db, book, periods)).run(src)


# --------------------------------------------------------------------- 上级科目


def test_上级科目的余额等于其末级之和(db, book, periods, subjects, make_voucher):
    """1002 银行存款 自己身上永远是 0，取数必须等于 100201+100202+100203。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)
    make_voucher("2026-01-11", dr=[("100202", 500, "x")], cr=[("600101", 500, "x")],
                 posted=True)

    assert _run(db, book, periods, '科目("100201","期末余额")') == Decimal("1000.00")
    assert _run(db, book, periods, '科目("1002","期末余额")') == Decimal("1500.00")


def test_上级科目按各自方向取正数后相加(db, book, periods, subjects, make_voucher):
    """贷方科目汇总后仍是正数：每一笔先按自己的方向摆正，再相加。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)
    # 6001 是上级（600101/600102 之上的父），本身不记分录
    assert _run(db, book, periods, '科目("6001","期末余额")') == Decimal("1000.00")


def test_各级取数口径都按子树汇总(db, book, periods, subjects, make_voucher):
    """不只是期末余额：本期金额、借方累计等口径同样走子树。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)

    assert _run(db, book, periods, '科目("1002","本期借方")') == Decimal("1000.00")
    assert _run(db, book, periods, '科目("1002","借方累计")') == Decimal("1000.00")
    assert _run(db, book, periods, '科目("1002","期初余额")') == Decimal("0.00")


def test_上级科目取数与逐个末级相加一致(db, book, periods, subjects, make_voucher):
    """这是「汇总」这件事的定义本身，两条路必须给出同一个数。"""
    make_voucher("2026-01-10", dr=[("100201", 300, "x")], cr=[("600101", 300, "x")],
                 posted=True)
    make_voucher("2026-01-12", dr=[("100203", 200, "x")], cr=[("600101", 200, "x")],
                 posted=True)

    leaves = sum((_run(db, book, periods, f'科目("{c}","期末余额")')
                  for c in ("100201", "100202", "100203")), Decimal("0"))
    assert _run(db, book, periods, '科目("1002","期末余额")') == leaves


def test_科目不存在仍然报错(db, book, periods):
    with pytest.raises(FormulaError, match="科目不存在"):
        _run(db, book, periods, '科目("999999","期末余额")')


def test_停用的末级仍然计入上级汇总(db, book, periods, subjects, make_voucher):
    """汇总口径必须与账簿引擎一致：停用科目**不**从发生额里剔除。

    ``LedgerEngine.compute`` 对全科目集是刻意不过滤 active 的（见那里的
    注释：剔除的话上级科目合计会偏小、报表总额会缺一块，而看报表的人完全
    看不出少了什么）。汇总若在这里另立一套口径，同一批科目在「明细」与
    「上级」两行就会给出两个数 —— 那比偏小更难查。

    停用只影响**选择**：``subjects/flat`` 不再把它列进下拉框。
    """
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)

    subjects["100201"].active = False
    db.commit()
    db.expire_all()

    assert _run(db, book, periods, '科目("1002","期末余额")') == Decimal("1000.00")


# --------------------------------------------------------------------- 简写


def test_简写与全写等价(db, book, periods, subjects, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)
    long_form = _run(db, book, periods, '科目("1002","期末余额")')
    short_form = _run(db, book, periods, '("1002","期末余额")')
    assert short_form == long_form == Decimal("1000.00")


def test_简写可以出现在表达式里(db, book, periods, subjects, make_voucher):
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)
    assert _run(db, book, periods, '("1002","期末余额")+100') == Decimal("1100.00")


def test_简写的第三个参数仍然可用(db, book, periods, subjects, make_voucher):
    """期间没被删掉，只是变成了可选 —— 首期/非首期都要能取。"""
    from app.reports.evaluate import EvalContext

    make_voucher("2026-01-10", dr=[("100201", 500, "x")], cr=[("600101", 500, "x")],
                 posted=True)
    make_voucher("2026-02-10", dr=[("100201", 700, "x")], cr=[("600101", 700, "x")],
                 posted=True)

    ctx = EvalContext(db, book, periods[2], {})
    assert ctx.cell(1, 1) == Decimal("0")  # 无公式

    from app.reports.formula import Evaluator

    assert Evaluator(ctx).run('("1002","期末余额")') == Decimal("1200.00")
    assert Evaluator(ctx).run('("1002","期末余额","{{上期}}")') == Decimal("500.00")


def test_只有一个参数的括号仍是分组(db, book, periods):
    """``("1001")`` 不能被当成科目取数 —— 那会把一句写错的话默默算成一个数。

    它必须仍旧按分组处理，然后由 ``run`` 报「公式结果是文本」。
    """
    assert parse_formula('("1001")') == ("str", "1001")
    with pytest.raises(FormulaError, match="文本"):
        _run(db, book, periods, '("1001")')


def test_分组表达式不受影响(db, book, periods):
    assert _run(db, book, periods, "(1+2)*3") == Decimal("9")
    assert _run(db, book, periods, "合计(B2:B4)") == Decimal("0")


# --------------------------------------------------------------------- 接口


def test_科目平铺默认只给末级(db, book, client, auth):
    """凭证录入、发票制证都依赖这个默认口径，不许被报表的需求带松。"""
    r = client.get(f"/api/books/{book.id}/subjects/flat", headers=auth)
    assert r.status_code == 200
    codes = {s["code"] for s in r.json()}
    assert "100201" in codes
    assert "1002" not in codes


def test_科目平铺可以带上上级(db, book, client, auth):
    r = client.get(f"/api/books/{book.id}/subjects/flat?include_parents=true", headers=auth)
    assert r.status_code == 200
    rows = r.json()
    codes = {s["code"] for s in rows}
    assert {"1002", "100201", "100202", "100203"} <= codes
    assert next(s for s in rows if s["code"] == "1002")["is_leaf"] is False
