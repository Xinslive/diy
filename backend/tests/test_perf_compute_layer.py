"""计算层性能整改的钉子测试（审计 2026-10-14 · 中等 5 / 中等 11 附带项）。

这一组同时管三件事：

* **AST 缓存真的生效** —— 断言 ``Parser.parse`` 的调用次数，而不是断言
  「跑得快一点」这种没法验收的说法。缓存不生效时这些用例会红。
* **数值逐格不变** —— 加缓存是纯粹的求值层优化，任何一格的数字或报错文案
  变了都是事故。这里用两层证据：公式库级别的黄金值（**改动之前**从引擎里
  抓下来的原始输出），以及同一进程内「空缓存 vs 热缓存」渲染同一张报表的
  逐格比对。循环引用、除零、空单元格这些边界必须原样保留 —— 缓存最容易
  串味的恰恰是「这一格报错 / 为空」这种非典型路径。
* **导出行数上限按真实行数算** —— 记账凭证导出的行数是**分录数**不是凭证数。

本文件不建业务凭证（不走 ``make_voucher``）：那套夹具经过业务服务层，
而服务层的审计快照正在被并行整改改动；这里只需要「若干张凭证的序列化
形状」，直接写 ORM 行既够用又不受其它整改影响。所有测试都不 commit，
数据随 ``db`` 夹具的回滚一起消失，不会污染同一个库里的其它用例。
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy
from sqlalchemy import select

from app.core.db import engine as app_engine
from app.models import (Entry, Period, ReportCell, ReportCol, ReportRow,
                        ReportTemplate, Subject, Voucher)
from app.printing.excel import MAX_EXPORT_ROWS, ExportTooLarge, vouchers_excel
from app.printing.preview import voucher_rows
from app.reports.evaluate import evaluate
from app.reports.formula import (MAX_AST_CACHE, Evaluator, FormulaError, Parser,
                                 parse_formula)

ZERO = Decimal("0")


# --------------------------------------------------------------------- 取数桩


SUBJECTS = {"1001": 11, "6001": 12, "1002": 13}
FIG = {
    11: dict(opening=Decimal("100.00"), debit_cum=Decimal("300.00"),
             credit_cum=Decimal("50.00"), closing=Decimal("350.00"),
             period_dr=Decimal("200.00"), period_cr=Decimal("50.00")),
    12: dict(opening=Decimal("0.00"), debit_cum=Decimal("0.00"),
             credit_cum=Decimal("900.00"), closing=Decimal("900.00"),
             period_dr=Decimal("0.00"), period_cr=Decimal("400.00")),
    13: dict(opening=Decimal("-20.00"), debit_cum=Decimal("-20.00"),
             credit_cum=Decimal("0.00"), closing=Decimal("-20.00"),
             period_dr=Decimal("0.00"), period_cr=Decimal("0.00")),
}
DIRECTION = {11: "DEBIT", 12: "CREDIT", 13: "DEBIT"}
#: (row, col) -> 金额。没列到的格子按空单元格 = 0。
GRID = {(1, 1): Decimal("10.5"), (2, 1): Decimal("20.25"), (3, 1): Decimal("-5.5"),
        (5, 3): Decimal("999.99")}


class StubCtx:
    """``Evaluator`` 需要的最小求值上下文，不碰数据库。

    公式引擎与账簿是解耦的（``Evaluator.__init__`` 只约定方法名），
    所以取数口径与报错语义能不能原样保住，用一个可控的桩就能钉住，
    比建一套真账再对一遍更快也更稳。
    """

    def subject_id(self, code):
        return SUBJECTS.get(str(code).strip())

    def resolve_period(self, expr):
        return f"period:{expr!r}"

    def _fig(self, sid, period):
        return FIG[sid]

    def opening(self, sid, p):
        return self._fig(sid, p)["opening"]

    def debit_cum(self, sid, p):
        return self._fig(sid, p)["debit_cum"]

    def credit_cum(self, sid, p):
        return self._fig(sid, p)["credit_cum"]

    def closing(self, sid, p):
        return self._fig(sid, p)["closing"]

    def period_dr(self, sid, p):
        return self._fig(sid, p)["period_dr"]

    def period_cr(self, sid, p):
        return self._fig(sid, p)["period_cr"]

    def closing_amt(self, sid, p):
        v = self._fig(sid, p)["closing"]
        return -v if DIRECTION[sid] == "CREDIT" else v

    def opening_amt(self, sid, p):
        v = self._fig(sid, p)["opening"]
        return -v if DIRECTION[sid] == "CREDIT" else v

    def period_amount(self, sid, p):
        f = self._fig(sid, p)
        v = f["period_dr"] - f["period_cr"]
        return -v if DIRECTION[sid] == "CREDIT" else v

    def ytd_amount(self, sid, p):
        f = self._fig(sid, p)
        v = f["debit_cum"] - f["credit_cum"]
        return -v if DIRECTION[sid] == "CREDIT" else v

    period_amt = period_amount
    ytd_amt = ytd_amount

    def cell(self, row, col):
        return GRID.get((row, col), ZERO)


# ----------------------------------------------------------- 解析次数的计数


@contextlib.contextmanager
def _count_parses():
    """数 ``Parser.parse`` 真被调了几次。

    刻意挂在类上而不是去看缓存自己的计数：证据要由**外面**来数，
    否则「缓存命中」就成了被测代码自己给自己发的合格证。
    """
    calls = 0
    orig = Parser.parse

    def counting_parse(self, *a, **kw):
        nonlocal calls
        calls += 1
        return orig(self, *a, **kw)

    Parser.parse = counting_parse
    try:
        yield lambda: calls
    finally:
        Parser.parse = orig


@pytest.fixture
def parses():
    """``parses()`` 返回到目前为止的解析次数。用例自己决定从哪一刻开始数。"""
    parse_formula.cache_clear()
    with _count_parses() as n:
        yield n
    parse_formula.cache_clear()


# ------------------------------------------- 黄金值（**加缓存之前**抓下来的）
#
# 缓存是求值层的纯优化，任何一格的值或报错文案变了都算回归 —— 这张表就是判据。
# 覆盖：四则与优先级、区域 / 单元格 / 上期、空格子、六种取数口径、函数库，
# 以及每一条报错路径（除零、文本结果、未知函数、未知科目、未知口径、
# 非法字符、位数越界、嵌套越界、区域过大、实参类型错）。

GOLDEN: dict[str, dict[str, str]] = {
    # 算术与优先级
    "1+2*3": {"ok": "7"},
    "(1+2)*3": {"ok": "9"},
    "10/4": {"ok": "2.5"},
    "-5+3": {"ok": "-2"},
    "--5": {"ok": "5"},
    "1+2>5": {"ok": "0"},
    "1+2>=3": {"ok": "1"},
    "2<>2": {"ok": "0"},
    # 区域 / 单元格 / 上期 / 空格子
    "合计(A1:A3)": {"ok": "25.25"},
    "SUM(A1:A3)": {"ok": "25.25"},
    "A1": {"ok": "10.5"},
    "A99": {"ok": "0"},
    "单元格(B2)": {"ok": "0"},
    "上期(C5)": {"ok": "0"},
    "合计(A1:A2)+单元格(A3)": {"ok": "25.25"},
    # 科目取数与取数口径
    '科目("1001","期初")': {"ok": "100.00"},
    '科目("1001","期末")': {"ok": "350.00"},
    '科目("1001","本期借方")': {"ok": "200.00"},
    '科目("1001","本期贷方")': {"ok": "50.00"},
    '科目("6001","期末余额")': {"ok": "-900.00"},
    '科目("1002","期初余额")': {"ok": "-20.00"},
    '科目("6001","本期金额")': {"ok": "400.00"},
    '科目("6001","本年累计")': {"ok": "900.00"},
    '科目("1001","期末",{{本期}})': {"ok": "350.00"},
    '科目("1001","期末","2026-09")': {"ok": "350.00"},
    '科目("1001","期末",2026)': {"ok": "350.00"},
    '科目("","期末")': {"ok": "0"},
    # 函数
    "IF(1>0,100,200)": {"ok": "100"},
    'IF("甲"="甲",1,0)': {"ok": "1"},
    'IF("甲"="乙",1,0)': {"ok": "0"},
    "四舍五入(1.005,2)": {"ok": "1.01"},
    "四舍五入(2.675,2)": {"ok": "2.68"},
    "四舍五入(10,-1)": {"ok": "1E+1"},
    "ROUND(1.2345,3)": {"ok": "1.235"},
    "ABS(-3)": {"ok": "3"},
    "绝对值(-3.5)": {"ok": "3.5"},
    "MAX(1,9,3)": {"ok": "9"},
    "MIN(1,9,3)": {"ok": "1"},
    "IF(A1>0,A1,A2)": {"ok": "10.5"},
    # 空输入
    "": {"ok": "0"},
    "   ": {"ok": "0"},
    # 报错路径
    "1/0": {"err": "除数为 0"},
    "1@2": {"err": "无法识别的字符 '@'（第 2 字）"},
    '未知函数(1)': {"err": "未知函数: 未知函数"},
    '科目("9999","期末")': {"err": "科目不存在: 9999"},
    '科目("1001","不存在的口径")': {
        "err": "未知取数口径: 不存在的口径（可用：期初、借方累计、贷方累计、期末、"
               "本期借方、本期贷方、本期金额、本年累计、期末余额、期初余额、"
               "本期余额、本年余额）"},
    "四舍五入(1,16)": {"err": "四舍五入的位数 16 超出范围（-15 ~ 15）"},
    "合计(A1:ZZZ99999)": {
        "err": "区域 A1:ZZZ99999 有 1827781722 格，超过单次上限 20000 格，请缩小范围"},
    "(" * 70 + "1" + ")" * 70: {
        "err": "公式括号 / 函数嵌套超过 64 层，请拆成几行分别计算"},
    "单元格(1)": {"err": "单元格() 只能接受单元格引用"},
    "1+": {"err": "无法解析的记号: None"},
    'IF(1>0,"是","否")': {
        "err": "公式结果是文本 '是'，而单元格只放金额。要按条件切换请返回数字，"
               "例如 IF(1>0,1,0)（文本**比较**是支持的，如 IF(名称=\"甲\",1,0)）"},
    # 链长上限：200 个运算符放行（上限不是「刚好卡死」）
    "1+" * 200 + "1": {"ok": "201"},
}


def _run_one(ev: Evaluator, src: str) -> dict[str, str]:
    """求值一条公式；只认 FormulaError，其他异常都算事故（会直接冒出去）。"""
    try:
        return {"ok": str(ev.run(src))}
    except FormulaError as e:
        return {"err": str(e)}


# ================================================================ 缓存确实生效


def test_同一条公式只解析一次(parses):
    """同一公式串出现在很多格是常态（合计行、期初列、同比列）。"""
    ev = Evaluator(StubCtx())
    for _ in range(50):
        assert ev.run('科目("1001","期末")') == Decimal("350.00")
    assert parses() == 1, f"同一条公式解析了 {parses()} 次 —— 缓存没生效"


def test_不同公式各解析一次(parses):
    ev = Evaluator(StubCtx())
    for src in ("1+1", "2*3", "合计(A1:A3)"):
        ev.run(src)
    assert parses() == 3


def test_两条只差一个字的公式不会共用同一棵AST(parses):
    """``科目("1001","期初")`` 与 ``科目("1001","期末")`` 必须分别解析、分别取值。"""
    ev = Evaluator(StubCtx())
    assert ev.run('科目("1001","期初")') == Decimal("100.00")
    assert ev.run('科目("1001","期末")') == Decimal("350.00")
    assert parses() == 2


def test_缓存里被遍历过之后结果不变(parses):
    """缓存里存的是 AST，求值时会遍历它 —— 第二遍必须与第一遍相同。

    这条盯的是「AST 被求值过程写坏」这类串味：跑一次没事、跑第二次就变，
    是缓存最容易出的事故。
    """
    ev = Evaluator(StubCtx())
    srcs = ("1+2*3", '科目("1001","期末")', "合计(A1:A3)", 'IF(A1>0,A1,A2)')
    first = [ev.run(s) for s in srcs]
    second = [ev.run(s) for s in srcs]
    assert first == second, (first, second)
    assert parses() == len(srcs)


def test_求值期报错每次都重报(parses):
    """求值期的错（除零）每次都要重新报出来。

    缓存里存的是 AST，不是结果 —— 所以除零会在每次求值时照常抛出，
    用户不会因为「上一次算过」就看不到错误。
    """
    ev = Evaluator(StubCtx())
    msgs = []
    for _ in range(3):
        with pytest.raises(FormulaError) as ei:
            ev.run("1/0")
        msgs.append(str(ei.value))
    assert msgs == ["除数为 0"] * 3
    assert parses() == 1, "1/0 的 AST 被反复解析了（它只解析一次，错误来自求值）"


def test_解析期报错不进缓存且每次都一样(parses):
    """语法错 / 复杂度超限在**解析期**失败，而 ``lru_cache`` 不缓存异常。

    这条是缓存最容易出事的地方 —— 万一有人改成「把报错也缓存」，
    用户改完公式就会一直看到旧报错，而且再也刷不掉。
    """
    ev = Evaluator(StubCtx())
    src = "1+" * 201 + "1"          # 201 个运算符 > MAX_CHAIN_OPS(200)
    msgs = []
    for _ in range(3):
        with pytest.raises(FormulaError) as ei:
            ev.run(src)
        msgs.append(str(ei.value))
    assert msgs == ["同一条加减链上超过 200 个运算符，请拆成几行分别计算"] * 3
    assert parses() == 3, "解析期失败的公式被缓存住了"


def test_解析期上限照旧拦得住且不留脏缓存(parses):
    ev = Evaluator(StubCtx())
    with pytest.raises(FormulaError, match="单次上限 20000 格"):
        ev.run("合计(A1:ZZZ99999)")
    # 失败的那次没有在缓存里留下半成品，后面的正常公式照常
    assert ev.run("合计(A1:A3)") == Decimal("25.25")


def test_缓存有上限不会无限长(parses):
    """模板频繁改动时不能让缓存白占内存。"""
    ev = Evaluator(StubCtx())
    for i in range(MAX_AST_CACHE + 50):
        ev.run(f"{i}+1")
    assert parse_formula.cache_info().currsize <= MAX_AST_CACHE
    assert parses() == MAX_AST_CACHE + 50


def test_缓存命中可观测(parses):
    ev = Evaluator(StubCtx())
    for _ in range(10):
        ev.run("1+2*3")
    info = parse_formula.cache_info()
    assert (info.hits, info.misses) == (9, 1), info


def test_连续渲染同一张报表解析次数不增长(db, book, periods, parses):
    """证据落在真实的 ``evaluate`` 上：第二次渲染一次都不用再解析。"""
    tid = _tpl_id(db, "BALANCE_SHEET")
    evaluate(db, tid, book, periods[1], False)
    first = parses()
    assert first > 0, "第一次渲染总得解析点什么"
    evaluate(db, tid, book, periods[1], False)
    assert parses() == first, f"第二次渲染又解析了 {parses() - first} 次 —— 缓存没生效"


# ================================================================ 数值逐格不变


@pytest.mark.parametrize("src", sorted(GOLDEN))
def test_公式求值与加缓存之前逐条一致(src):
    got = _run_one(Evaluator(StubCtx()), src)
    assert got == GOLDEN[src], f"{src!r}：改动后 {got}，改动前 {GOLDEN[src]}"


@pytest.mark.parametrize("src", sorted(GOLDEN))
def test_命中缓存的那一次与第一次一样(src):
    ev = Evaluator(StubCtx())
    assert _run_one(ev, src) == _run_one(ev, src) == GOLDEN[src]


@pytest.mark.parametrize("code", ["BALANCE_SHEET", "INCOME_SHEET"])
def test_空缓存与热缓存渲染同一张报表逐格一致(db, book, periods, code):
    """一次渲染里同一公式被反复用到；冷热两遍必须**完全**一样。

    比的是整个返回体：每格的值、每格的报错文案、公式原文、错误列表、
    列定义与口径标注 —— 任何一处不同都算回归。
    """
    tid = _tpl_id(db, code)
    parse_formula.cache_clear()
    cold = evaluate(db, tid, book, periods[6], False)
    hot = evaluate(db, tid, book, periods[6], False)
    assert _grid(cold) == _grid(hot)
    assert cold["errors"] == hot["errors"]
    assert cold["cols"] == hot["cols"]
    assert cold["notices"] == hot["notices"]
    assert (cold["carry_forward_period_amount"]
            == hot["carry_forward_period_amount"])
    assert cold["formula_error_free"] is hot["formula_error_free"] is True


def test_改公式之后缓存不会串味(db, book, periods, one_cell):
    """改模板是日常操作：新公式必须立刻生效，不能命中旧 AST。"""
    tid, cell = one_cell
    cell.formula = "0+1"
    db.flush()
    assert _value(db, tid, book, periods[6]) == Decimal("1")
    cell.formula = "0+7"
    db.flush()
    assert _value(db, tid, book, periods[6]) == Decimal("7")


def test_空公式不参与求值(db, book, periods):
    tid = _tpl_id(db, "INCOME_SHEET")
    data = evaluate(db, tid, book, periods[3], False)
    blanks = [c for r in data["rows"] for c in r["cells"] if not c["formula"]]
    assert blanks, "样例模板里应当有空格子"
    assert all(c["value"] is None and c["error"] == "" for c in blanks)


# ================================================================ 边界语义不变


def test_自引用照旧报错(db, book, periods, one_cell):
    """自己引用自己 —— 缓存不能把环检测绕过去。"""
    tid, cell = one_cell
    cell.formula = "B2+B2"
    db.flush()
    data = evaluate(db, tid, book, periods[3], False)
    target = _cell_at(data, cell.row_no, cell.col_no)
    assert target["value"] is None
    assert "公式循环引用" in target["error"], target["error"]


def test_两格互相引用照旧报错(db, book, periods, two_cells):
    tid, cells = two_cells
    a, b = cells
    a.formula = f"{_addr(b.row_no, b.col_no)}+1"
    b.formula = f"{_addr(a.row_no, a.col_no)}+1"
    db.flush()
    data = evaluate(db, tid, book, periods[3], False)
    errors = [c["error"] for r in data["rows"] for c in r["cells"] if c["error"]]
    assert any("公式循环引用" in m for m in errors), errors


def test_除零照旧报错(db, book, periods, one_cell):
    tid, cell = one_cell
    cell.formula = '科目("1001","期初")/0'
    db.flush()
    target = _cell_at(evaluate(db, tid, book, periods[3], False),
                      cell.row_no, cell.col_no)
    assert target["value"] is None and "除数为 0" in target["error"]


def test_空单元格是零不是报错(db, book, periods, one_cell):
    """引用一个没有公式的格子 = 0。这条最容易被「缓存了 None」之类的改动弄坏。"""
    tid, cell = one_cell
    cell.formula = "D9+D10"
    db.flush()
    assert _value(db, tid, book, periods[3]) == Decimal("0")


# ============================================ 记账凭证导出的行数上限（分录口径）


def _voucher(no: str, entry_count: int) -> dict:
    return {
        "voucher_date": "2026-01-05", "voucher_no": no,
        "entries": [{"summary": "测试", "subject_code": "1001",
                     "subject_name": "库存现金", "direction": "DEBIT",
                     "amount": "1.00"} for _ in range(entry_count)],
        "total_debit": "1.00", "total_credit": "0",
    }


def test_凭证导出按分录数卡上限而不是凭证数(monkeypatch):
    """导出的是**分录**：2 张各 3 行 = 6 行，上限 5 就该被拦下。

    上限若按凭证数算，这个请求会带着 6 行（openpyxl 是全内存构建，
    真实场景里 `page_size=5000` × 每张 8 行 ≈ 4 万行）一路写进内存工作簿。
    """
    monkeypatch.setattr("app.printing.excel.MAX_EXPORT_ROWS", 5)
    with pytest.raises(ExportTooLarge) as ei:
        vouchers_excel([_voucher("记-1", 3), _voucher("记-2", 3)])
    assert "记账凭证" in str(ei.value)
    assert vouchers_excel([_voucher("记-1", 3), _voucher("记-2", 2)])  # 恰好 5 行


def test_凭证导出行数超限给出的文案可操作(monkeypatch):
    monkeypatch.setattr("app.printing.excel.MAX_EXPORT_ROWS", 3)
    with pytest.raises(ExportTooLarge) as ei:
        vouchers_excel([_voucher("记-1", 4)])
    msg = str(ei.value)
    assert "记账凭证共 4 行" in msg, msg
    assert "缩小查询范围" in msg, msg


def test_凭证导出用的就是全局上限():
    """其余四个导出函数都卡 ``MAX_EXPORT_ROWS``，凭证这一路不能是例外。"""
    with pytest.raises(ExportTooLarge):
        vouchers_excel([_voucher("记-1", 1)] * (MAX_EXPORT_ROWS + 1))


# ============================================== 预览行：科目一次取好（潜伏 N+1）


def _make_vouchers(db, book, period, codes, count: int) -> list[Voucher]:
    """直接写 ORM 行建凭证 —— 只要「序列化凭证」这个形状，不走业务服务层。"""
    subs = {c: db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.code == c)) for c in codes}
    out = []
    for i in range(count):
        v = Voucher(book_id=book.id, period_id=period.id, voucher_no=f"记-{i:03d}",
                    voucher_date=date(period.year, period.month, 5))
        db.add(v)
        db.flush()
        for j, code in enumerate(codes):
            db.add(Entry(voucher_id=v.id, book_id=book.id, period_ord=period.ord,
                         line_no=j + 1, subject_id=subs[code], direction="DEBIT",
                         amount=Decimal("10.00"), summary=f"第{i}笔"))
        out.append(v)
    db.flush()
    return out


@contextlib.contextmanager
def _statements():
    """收集这段代码里真正发出去的 SQL（与 tests/test_ledger_query_count.py 同法）。"""
    stmts: list[str] = []
    handler = lambda conn, cur, statement, params, ctx, many: stmts.append(statement)  # noqa: E731
    sqlalchemy.event.listen(app_engine, "before_cursor_execute", handler)
    try:
        yield stmts
    finally:
        sqlalchemy.event.remove(app_engine, "before_cursor_execute", handler)


def test_预览凭证行只查一次科目(db, book, periods, subjects):
    """每张凭证各查一次全账套科目 = N+1。科目一次取好就够。

    这段代码目前**没有调用方**（全仓检索确认，见交付说明），但它是潜伏的坑：
    一旦有人把打印预览接回来就立刻复现，所以顺手修好并钉住。
    """
    vs = _make_vouchers(db, book, periods[1], ["1001", "6001"], count=8)
    with _statements() as stmts:
        rows = voucher_rows(db, book, vs)
    assert len([s for s in stmts if "FROM subject" in s]) == 1, (
        f"8 张凭证发了 {len([s for s in stmts if 'FROM subject' in s])} 次科目查询 —— N+1 还在")
    assert len(rows) == 8


def test_预览凭证行的内容与一次取好科目后逐张序列化一致(db, book, periods, subjects):
    from app.api.vouchers import _book_subjects, _serialize

    vs = _make_vouchers(db, book, periods[1], ["1001", "6001"], count=3)
    assert voucher_rows(db, book, vs) == [
        _serialize(db, v, _book_subjects(db, book.id)) for v in vs]


# --------------------------------------------------------------------- 小工具


def _tpl_id(db, code: str) -> int:
    return db.scalar(select(ReportTemplate.id).where(ReportTemplate.code == code))


def _addr(row: int, col: int) -> str:
    from app.reports.formula import no_to_col

    return f"{no_to_col(col)}{row}"


def _grid(data: dict) -> list:
    """把渲染结果压成可比对的纯数据（值与报错文案都要在）。"""
    return [[(c["col_no"], c["value"], c["error"], c["formula"])
             for c in r["cells"]] for r in data["rows"]]


def _cell_at(data: dict, row_no: int, col_no: int) -> dict:
    row = next(r for r in data["rows"] if r["row_no"] == row_no)
    return next(c for c in row["cells"] if c["col_no"] == col_no)


def _value(db, tid, book, period) -> Decimal:
    """整张表里唯一有数的那一格。"""
    got = [c for r in evaluate(db, tid, book, period, False)["rows"]
           for c in r["cells"] if c["value"] is not None]
    assert len(got) == 1, [(c["col_no"], c["value"], c["error"]) for c in got]
    return Decimal(str(got[0]["value"]))


@pytest.fixture
def one_cell(db, book):
    """一张只有一个公式格的临时报表模板（不 commit，随夹具回滚消失）。"""
    tid = _make_tpl(db, book, {(2, 2): ""})
    return tid, db.get(ReportCell, _first_cell_id(db, tid))


@pytest.fixture
def two_cells(db, book):
    tid = _make_tpl(db, book, {(2, 2): "", (2, 3): ""})
    cells = list(db.scalars(select(ReportCell).where(
        ReportCell.template_id == tid).order_by(ReportCell.col_no)).all())
    return tid, cells


def _make_tpl(db, book, cells: dict[tuple[int, int], str]) -> int:
    tpl = ReportTemplate(code=f"__perf_{uuid.uuid4().hex[:10]}", name="性能整改测试用",
                         type="CUSTOM")
    db.add(tpl)
    db.flush()
    for col_no in (1, 2, 3):
        db.add(ReportCol(template_id=tpl.id, col_no=col_no, label=f"值{col_no}",
                         kind="PERIOD_AMOUNT"))
    for row_no in (1, 2):
        db.add(ReportRow(template_id=tpl.id, row_no=row_no, label=f"行{row_no}", indent=0))
    db.flush()
    for (row_no, col_no), formula in cells.items():
        db.add(ReportCell(template_id=tpl.id, row_no=row_no, col_no=col_no,
                          formula=formula))
    db.flush()
    return tpl.id


def _first_cell_id(db, tid: int) -> int:
    return db.scalar(select(ReportCell.id).where(
        ReportCell.template_id == tid, ReportCell.row_no == 2, ReportCell.col_no == 2))