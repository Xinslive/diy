"""报表的**口径**与对外数据契约（审计 P1-14 / P1-15 / P3-3 / P3-5）。

这一组盯的不是「公式算得对不对」，而是**报表与总账之间的契约**：

* P1-15 —— 利润表用 ``for_report``（排除结转凭证），总账用 ``for_ledger``
  （含结转凭证）。两者对损益科目的差额**恰好是结转凭证金额**：不是 bug，
  但必须让界面看得见，否则 README 开头「不存在某个报表和总账对不上」
  这句话就只是句口号。这里同时钉住两件事：口径确实切对了，
  以及输出里确实带着前端标注所需的信号。
* P1-14 —— 账套首期的 ``{{上期}}`` 必须有明确语义，不能静默返回本期数字。
* P3-3 —— ``balanced`` 这个字段名误导（它说的是「没有公式错误」，
  不是「资产负债两平」）。全仓无消费方，直接改名。
* P3-5 —— 文本比较能用、文本结果不能用，两条都要有明确文案。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import ReportCell, ReportTemplate, Subject
from app.reports.evaluate import evaluate
from app.reports.formula import Evaluator, FormulaError


# --------------------------------------------------------------------- 夹具


def _tpl_id(db, code: str) -> int:
    return db.scalar(select(ReportTemplate.id).where(ReportTemplate.code == code))


def cells(data: dict) -> dict[str, list]:
    return {r["label"]: [c["value"] for c in r["cells"]] for r in data["rows"]}


def num(v) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else Decimal("0")


def _carry_forward_january(db, book, periods, subjects, admin_user, make_voucher):
    """第 1 月：一笔收租 10,000，然后结转损益并记账。

    这之后，损益类科目同时存在「含结转」和「排除结转」两种口径：
    总账的 600101 本期贷方 = 20,000（10,000 收入 + 10,000 结转），
    利润表的「本期金额」= 10,000（排除结转）。
    """
    from app.accounting.periods import carry_forward_profit
    from app.accounting.vouchers import post_vouchers
    from app.models import Voucher

    make_voucher("2026-01-10", dr=[("100201", 10000, "收租")],
                 cr=[("600101", 10000, "收租")], posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)
    ids = [x.id for x in db.scalars(select(Voucher).where(
        Voucher.book_id == book.id, Voucher.source == "CARRY_FORWARD",
        Voucher.period_id == periods[1].id)).all()]
    assert ids, "结转凭证没生成 —— 这个用例的前提不成立"
    post_vouchers(db, book.id, ids, admin_user)
    db.commit()
    return ids


# ------------------------------------------------------------- P1-15 口径契约


def test_利润表本期金额与总账本期贷方差一个结转全额(
        db, book, periods, subjects, admin_user, make_voucher):
    """**差额 = 结转凭证金额**，这是口径契约本身，必须由测试钉住。

    切到 ``for_report`` 之前，利润表这一列走的是手写的
    ``exclude_carry_forward=True``，数字对但契约写在注释里；
    切过去之后数字不变，变的是「这个口径是引擎给的、不是报表自己拼的」。
    """
    from app.ledger.engine import LedgerEngine

    _carry_forward_january(db, book, periods, subjects, admin_user, make_voucher)
    sid = db.scalar(select(Subject.id).where(
        Subject.book_id == book.id, Subject.code == "600101"))

    ledger = LedgerEngine.for_ledger(db, book.id, False).figures(sid, periods[1].ord)
    report = LedgerEngine.for_report(db, book.id, False).figures(sid, periods[1].ord)

    # 收入在**贷方**增加，结转要把它冲回 0，所以结转凭证记在收入的**借方**：
    #   总账 600101：借 10,000（结转）/ 贷 10,000（收入）
    #   报表 600101：借 0（排除结转）/ 贷 10,000
    # 差额 = 10,000 = 结转凭证金额。
    assert ledger.period_dr == Decimal("10000.00"), ledger.period_dr
    assert ledger.period_cr == Decimal("10000.00"), ledger.period_cr
    assert report.period_dr == Decimal("0.00"), report.period_dr
    assert report.period_cr == Decimal("10000.00"), report.period_cr
    assert ledger.period_dr - report.period_dr == Decimal("10000.00")

    # 而利润表这一格显示的正是排除结转后的金额（6001 是贷方，_orient 取正）
    income = cells(evaluate(db, _tpl_id(db, "INCOME_SHEET"), book, periods[1], False))
    assert num(income["一、营业收入"][1]) == Decimal("10000"), income["一、营业收入"]


def test_结转之前两个口径完全相等(db, book, periods, subjects, admin_user, make_voucher):
    """没结转就没有差额 —— 标注必须只在真有差额时出现，否则是噪音。"""
    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)
    data = evaluate(db, _tpl_id(db, "INCOME_SHEET"), book, periods[1], False)
    assert data["carry_forward_period_amount"] is None
    assert data["notices"] == []


def test_输出带上前端标注所需的口径信号(db, book, periods, subjects, admin_user, make_voucher):
    """**数据契约**：前端只认这几个字段，别名不要，名字不要改。

    - ``carry_forward_excluded``：本次求值用的是报表口径（排除结转）；
    - ``cols[].excludes_carry_forward``：哪几列受影响（前端标列头）；
    - ``carry_forward_period_amount`` / ``notices``：差多少、怎么跟用户说。
    """
    _carry_forward_january(db, book, periods, subjects, admin_user, make_voucher)
    data = evaluate(db, _tpl_id(db, "INCOME_SHEET"), book, periods[1], False)

    assert data["carry_forward_excluded"] is True
    # 金额按 Decimal 比，不比字符串：numeric 列出来是 "10000.00"
    assert num(data["carry_forward_period_amount"]) == Decimal("10000")
    assert data["notices"] and data["notices"][0]["code"] == "CARRY_FORWARD_EXCLUDED"
    assert num(data["notices"][0]["amount"]) == Decimal("10000")
    # 利润表两列数值列（本期金额 / 本年累计）都受影响；「项目」列不是数值列
    pf_cols = [c["col_no"] for c in data["cols"] if c["excludes_carry_forward"]]
    assert pf_cols == [2, 3], pf_cols
    assert data["notices"][0]["cols"] == [2, 3]

    # 资产负债表只有余额口径，不该被标成「排除结转」
    bs = evaluate(db, _tpl_id(db, "BALANCE_SHEET"), book, periods[1], False)
    assert [c["col_no"] for c in bs["cols"] if c["excludes_carry_forward"]] == []


def test_未记账的结转凭证不计入损益(db, book, periods, subjects, admin_user, make_voucher):
    """已结转但**没记账**的结转凭证仍然不进报表 —— 与总账口径保持一致。"""
    from app.accounting.periods import carry_forward_profit

    make_voucher("2026-01-10", dr=[("100201", 1000, "x")], cr=[("600101", 1000, "x")],
                 posted=True)
    carry_forward_profit(db, book.id, periods[1], admin_user)   # 故意不记账
    db.commit()

    income = cells(evaluate(db, _tpl_id(db, "INCOME_SHEET"), book, periods[1], False))
    assert num(income["一、营业收入"][1]) == Decimal("1000")
    # 未记账的结转凭证在默认口径下不可见，所以也没有差额可标注
    data = evaluate(db, _tpl_id(db, "INCOME_SHEET"), book, periods[1], False)
    assert data["carry_forward_period_amount"] is None


# --------------------------------------- P1-12 在 HTTP 层的落点：不是 500


@pytest.mark.parametrize("bad", [
    "1+" * 1990 + "1",     # 3981 字，解析期就会 RecursionError
    "-" * 1500 + "1",      # 一元负号链，同样
])
def test_会让渲染500的公式存不进去(client, auth, book, db, bad):
    """修复前这两条会一路穿出 evaluate，直落 HTTP 500。

    现在在**保存**这一层就变成 400 + 一句能照着改的中文 ——
    语法检查放在写入口，而不是指望渲染期逐格兜住。
    """
    tpl = db.scalar(select(ReportTemplate).where(ReportTemplate.code == "BALANCE_SHEET"))
    cell = db.scalar(select(ReportCell).where(
        ReportCell.template_id == tpl.id, ReportCell.row_no == 2, ReportCell.col_no == 2))
    old = cell.formula
    try:
        r = client.put(f"/api/reports/{tpl.id}/cells?book_id={book.id}", headers=auth,
                       json=[{"row_no": 2, "col_no": 2, "formula": bad}])
        assert r.status_code == 400, f"拿到 {r.status_code}，这正是审计说的 500 前身"
        detail = r.json()["detail"]
        assert "第 2 行 / 2 列" in detail and len(detail) > 12, detail
        db.expire_all()
        again = db.scalar(select(ReportCell).where(
            ReportCell.template_id == tpl.id, ReportCell.row_no == 2, ReportCell.col_no == 2))
        assert again.formula == old, "400 了却还是把坏公式存了进去"
    finally:
        cell.formula = old
        db.commit()


def test_位数离谱的四舍五入存得进但渲染时报人话(client, auth, book, db, periods):
    """``四舍五入(1,999999999)`` **语法是合法的** —— 错在求值。

    所以它挡在保存这一层挡不住（也不该挡：语法检查只管语法）。
    真正要保证的是渲染时不 500，而是那一格显示能照着改的文案。
    修复前这里抛的是 ``decimal.InvalidOperation``，穿出 evaluate 就是 500。
    """
    tpl = db.scalar(select(ReportTemplate).where(ReportTemplate.code == "BALANCE_SHEET"))
    cell = db.scalar(select(ReportCell).where(
        ReportCell.template_id == tpl.id, ReportCell.row_no == 2, ReportCell.col_no == 2))
    old = cell.formula
    cell.formula = "四舍五入(1,999999999)"
    db.commit()
    try:
        r = client.get(f"/api/reports/{tpl.id}/render", headers=auth,
                       params={"book_id": book.id, "period_id": periods[12].id})
        assert r.status_code == 200, f"拿到 {r.status_code}：{r.text[:200]}"
        data = r.json()
        assert data["formula_error_free"] is False
        assert any("位数 999999999 超出范围（-15 ~ 15）" in e for e in data["errors"]), \
            data["errors"]
        # 受影响的那一格显示 null + 文案，其余格子照常有数
        row2 = data["rows"][1]
        assert row2["cells"][1]["value"] is None
        assert "超出范围" in row2["cells"][1]["error"]
    finally:
        cell.formula = old
        db.commit()


def test_渲染期遇到坏公式是那一格报错而不是整张表500(client, auth, book, db, periods):
    """绕过保存入口直接写库的坏公式，渲染时也不能 500。

    逐格捕获的设计在这里派上用场：整张表照常渲染出来，只有那一格带错误文案。
    """
    tpl = db.scalar(select(ReportTemplate).where(ReportTemplate.code == "BALANCE_SHEET"))
    cell = db.scalar(select(ReportCell).where(
        ReportCell.template_id == tpl.id, ReportCell.row_no == 2, ReportCell.col_no == 2))
    old = cell.formula
    cell.formula = "四舍五入(1,999999999)"
    db.commit()

    r = client.get(f"/api/reports/{tpl.id}/render", headers=auth,
                   params={"book_id": book.id, "period_id": periods[1].id})
    assert r.status_code == 200, f"拿到 {r.status_code}：{r.text[:200]}"
    data = r.json()
    assert data["formula_error_free"] is False
    assert any("位数" in e for e in data["errors"]), data["errors"]
    # 其余格子照常有数
    assert data["rows"] and len(data["rows"][0]["cells"]) == 3

    cell.formula = old
    db.commit()


# ------------------------------------------------------------- P1-14 首期上期


def test_首期的上期必须报错而不是静默返回本期(db, book, periods, subjects, make_voucher):
    """账套首期没有「上期」。

    以前 ``resolve_period`` 把 ``period.ord - 1`` 夹回首期自身，于是
    ``科目("1001","期末","{{上期}}")`` 在首期拿到的是**首期期末**：
    不报错、不为空，财务无从察觉。现在必须报错，且这一格显示这句话、
    其余格子照常算完。
    """
    from app.models import ReportCol, ReportRow

    tpl = db.scalar(select(ReportTemplate).where(ReportTemplate.code == "BALANCE_SHEET"))
    cell = db.scalar(select(ReportCell).where(
        ReportCell.template_id == tpl.id, ReportCell.row_no == 2, ReportCell.col_no == 3))
    old = cell.formula
    cell.formula = '科目("1001","期末余额",{{上期}})'
    db.commit()
    try:
        data = evaluate(db, tpl.id, book, periods[1], False)
        row = next(r for r in data["rows"] if r["row_no"] == 2)
        target = row["cells"][2]
        assert target["value"] is None
        assert "没有「上期」可取" in target["error"], target["error"]
        # 其余格子不受影响
        assert not [e for e in data["errors"] if "第 1 行" in e], data["errors"]
    finally:
        # 改的是**内置模板**（全局共享），必须还原：db.commit() 是真提交，
        # 夹具的 rollback 回滚不掉它，漏还原会污染同一 session 里后面的用例。
        cell.formula = old
        db.commit()


def test_非首期的上期照常取上一期(db, book, periods, subjects, make_voucher):
    """第二期起 {{上期}} 指向第一期 —— 修首期不能把正常的上期也修坏。"""
    from app.reports.evaluate import EvalContext

    make_voucher("2026-01-10", dr=[("1001", 500, "x")], cr=[("600101", 500, "x")],
                 posted=True)
    make_voucher("2026-02-10", dr=[("1001", 700, "x")], cr=[("600101", 700, "x")],
                 posted=True)

    ctx = EvalContext(db, book, periods[2], {})
    first = ctx.resolve_period("{{上期}}")
    assert (first.year, first.month) == (2026, 1), (first.year, first.month)


# ----------------------------------------------------------------- P3-3 字段名


def test_不再叫balanced_改叫formula_error_free(db, book, periods):
    """``balanced`` 在报表语境下是误导：它说的是「没有公式报错」。

    资产负债表模板里有一行「平衡校验（应为 0）」，看到 ``balanced``
    的人只会理解成「资产 = 负债 + 权益」。全仓检索确认没有消费方
    （前端只读 errors；tests 里的 ``res["balanced"]`` 是期初接口自己的字段），
    所以直接改名而不是留一个会继续被误读的别名。
    """
    tid = _tpl_id(db, "BALANCE_SHEET")
    ok = evaluate(db, tid, book, _period_of(db, book, 12), False)
    assert ok["formula_error_free"] is True, ok["errors"]
    assert "balanced" not in ok

    # 有公式出错时它必须是 false —— 语义确实是「无公式错误」而不是「两平」
    cell = db.scalar(select(ReportCell).where(
        ReportCell.template_id == tid, ReportCell.row_no == 2, ReportCell.col_no == 2))
    old = cell.formula
    cell.formula = "科目(\"不存在的科目\",\"期末\",{{本期}})"
    db.commit()
    bad = evaluate(db, tid, book, _period_of(db, book, 12), False)
    assert bad["formula_error_free"] is False
    cell.formula = old
    db.commit()


def _period_of(db, book, month: int):
    from app.models import Period

    return db.scalar(select(Period).where(
        Period.book_id == book.id, Period.year == 2026, Period.month == month))


# ----------------------------------------------------------------- P3-5 文本


def test_文本比较可用_文本结果报错并说清怎么改(db, book, periods):
    """``IF(名称="甲",1,0)`` 能用；``IF(1>0,"是","否")`` 报错但要说人话。

    金额格最终要的是金额，所以公式整体结果是文本时报错。但**比较**支持文本
    没有任何歧义（Excel 也支持），过去一律走 ``D()`` 把两者一起禁掉了。
    """
    from app.reports.evaluate import EvalContext

    ctx = EvalContext(db, book, periods[1], {})
    assert Evaluator(ctx).run('IF("甲"="甲",1,0)') == Decimal(1)
    assert Evaluator(ctx).run('IF("甲"="乙",1,0)') == Decimal(0)
    with pytest.raises(FormulaError, match="只放金额"):
        Evaluator(ctx).run('IF(1>0,"是","否")')
    # 金额和文本混比是写错了，明确报错而不是静默给 False
    with pytest.raises(FormulaError, match="不能拿金额和文本比大小"):
        Evaluator(ctx).run('IF(1>"甲",1,0)')


# --------------------------------------------- 模板读权限的不变量（不许被拆掉）


def test_公式里无法指定账套ID(db, book, admin_user, plain_user):
    """模板是全局的，取数必须永远锁在**当前账套**。

    这是模板可以跨账套共享的前提：公式里没有账套参数，
    ``科目()`` 只解析 ``Subject.book_id == book.id``。
    给公式加「账套参数」等于开一个跨账套取数后门。
    """
    from app.reports.evaluate import EvalContext

    ctx = EvalContext(db, book, _period_of(db, book, 1), {})
    assert ctx.subject_id("1001") is not None
    # 上下文里只装载了**本账套**的科目：科目() 能解析到的就这些
    assert ctx.subject_id("1001") in {s.id for s in ctx.subjects.values()}
    assert len(ctx.subjects) < 1000
    # 公式里塞「账套参数」只有解析失败一条路 —— 语法上就没有这个位置
    with pytest.raises(FormulaError):
        Evaluator(ctx).run('科目("1001","期末",账套=999)')
    # 第三个实参是**期间**，只解析成本账套的 ord，别的账套根本没有入口
    p = ctx.resolve_period("2026")
    assert p.book_id == book.id
    assert ctx.resolve_period("{{本期}}").book_id == book.id
    # 而 科目() 只按当前账套解析：别的账套的同编码科目不会被串进来
    other = db.scalar(select(Subject).where(
        Subject.book_id != book.id, Subject.code == "1001"))
    if other is not None:
        assert ctx.subject_id("1001") != other.id
