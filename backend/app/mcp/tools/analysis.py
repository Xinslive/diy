"""MCP 分析类工具：收支趋势、内置报表渲染。

**这一块为什么存在。**
前两类工具回答的是「看到了什么」（有哪些账套、哪些凭证、余额是多少），
这一类回答的是「算出了什么」——今年收入费用怎么变、利润表长什么样。
外部 agent 要回答「今年收入/费用变化」这类问题，只有明细没有口径，
就会自己拿明细去加，**加出来的数与界面上的利润表对不上**。

所以这一类的规矩只有一条：**不自写聚合**。
收入/费用怎么切、方向怎么取正、区间发生额怎么累加、报表怎么求值 ——
全部委托给账簿引擎（``LedgerEngine``）与报表求值器
（``app/reports/evaluate.py``），本模块只做「入参翻译 + 出参整形」。

**两个工具的分工。**

* ``income_expense_trend`` —— 按月给出收入、费用、净额，并按科目类别拆分。
* ``render_report`` —— 直接渲染资产负债表 / 利润表（内置基线或账套自己的副本）。

**为什么工具 8 的年度合计不能用「本年累计相减」。**
账簿引擎的 ``dr_cum / cr_cum`` 是**本年**累计，每年 1 月归零
（``ledger/engine.py`` 的「本年借方累计」注释）。拿它做区间相减，
跨年时相减的是「本年累计 − 上年累计」，得到的是差值而不是本年的发生额 ——
**而且不报错**，只是一个偏小的数。所以年度合计与同比一律用
``range_movements``（区间内逐期累加，跨多少年都对）。

**本模块刻意不写 ``from __future__ import annotations``。**
MCP SDK 解析工具注解时用的是**装饰器所在模块**（``base.py``）的全局命名空间，
而本模块的注解如果写成字符串，就会在那个命名空间里被求值 —— 报一个
看不懂的 ``NameError``。注解保持成对象就没有这个问题（``base.py`` 的
``mcp_tool`` docstring 里写了同一条纪律）。
"""
import logging
from decimal import Decimal
from typing import Any, Optional

from mcp.server.mcpserver import Context
from sqlalchemy import or_, select

from app.accounting.years import subjects_of
from app.ledger.engine import LedgerEngine
from app.models import Period, ReportTemplate, Subject
from app.mcp.caliber import (
    CATEGORY_CN,
    all_periods,
    current_period,
    find_period_by_code,
    money_str,
    orient,
    resolve_year,
)
from app.mcp.errors import McpBadRequest, McpNotFoundError
from app.mcp.schemas import (
    IncomeExpenseTrendResult,
    RenderReportResult,
)
from app.mcp.tools.base import current_tool_context, envelope, mcp_tool, require_book
from app.reports.evaluate import evaluate

log = logging.getLogger("cwq")

ZERO = Decimal("0")
#: 参与损益汇总的科目类别。
#:
#: **只有损益类进损益两方**，资产、负债、权益类一律不进 —— 它们只是收入或
#: 费用的**对手方**，本身不代表任何方向。
#:
#: **为什么成本类（COST）不进。** 内置利润表覆盖的是 5001/5051/5111/5301
#: （收入）与 5401/5402/5403/5601/5602/5603/5711/5801（费用）—— **全是损益类**。
#: 成本类科目（4001 生产成本 / 4101 制造费用 / 4301 研发支出 / 4401 工程施工 /
#: 4403 机械作业）**不在利润表的任何一行里**，把它们算进费用，工具报出的
#: 「今年费用」就会比老板在利润表上看到的费用大 —— 而这种错**看着还挺合理**，
#: 是最难被发现的一类。
#:
#: 这几个成本类科目在用友 4-2-2 科目表里是**末级**（``is_leaf`` 由「有没有
#: 子科目」推导，它们没有子科目），所以**真的能记账**，不是永远为 0 的摆设。
#:
#: 排除它们之后，本工具有一条好用的不变式：**收入 − 费用 == 利润表的净利润**。
PL_CATEGORIES = ("PROFIT",)

#: 工具 8 的口径标识。取数走 ``LedgerEngine.for_report``（排除结转凭证），
#: 与利润表的口径一致。
TREND_CALIBER = "report"

TREND_DESCRIPTION = (
    "按月给出某个账套某一会计年度的收入、费用与净额（损益口径，已排除结转凭证）。"
    "金额单位为元、两位小数，以字符串承载。"
)

REPORT_DESCRIPTION = (
    "渲染某个账套的报表：内置基线（资产负债表 BALANCE_SHEET / 利润表 INCOME_SHEET）"
    "或该账套自己的模板副本。返回行与单元格、单元格级公式报错，以及口径标注。"
)


# --------------------------------------------------------------------- 损益两方


def _category_wanted(category: Optional[str], wanted: Optional[list[str]]) -> bool:
    """这个科目类别要不要参与汇总。

    ``categories`` 允许写英文类别码（``PROFIT``），也允许写界面上
    显示的中文名（``损益``）—— agent 手里的类别词往往是中文，
    只认英文码的话它会传一个空列表进来，然后拿到一个空序列还以为是账上没钱。

    **注意只有损益类参与汇总**（见 ``PL_CATEGORIES``），所以按「成本 / COST」
    过滤会得到空序列 —— 那是口径决定的，不是查错了。
    """
    if not wanted:
        return True
    keys = {str(c).strip().upper() for c in wanted if str(c).strip()}
    if not keys:
        return True
    code = (category or "").upper()
    cn = CATEGORY_CN.get(category or "", category or "")
    return code in keys or cn in keys


def _pl_subjects(db, book_id: int, year: int,
                 categories: Optional[list[str]]) -> list[Subject]:
    """某一会计年度里**参与损益汇总**的末级科目。

    **年度必须显式。** 科目表每个会计年度各有一份（迁移 0032），2026 的
    ``5001`` 与 2027 的 ``5001`` 是两行、两个 id。拿错年度的 id 不会报错，
    只会静默返回空 —— 那是最坏的一种错（计划 §8.2 R8）。

    **为什么只取末级。** 凭证分录只允许挂末级科目（写路径
    ``accounting/vouchers.py`` 的末级科目关口），而账簿引擎把发生额**上卷**
    到各级父科目。于是「末级相加」等于「父科目的引擎值」—— 与利润表模板里
    ``科目("5602", "本年累计")`` 那一格取到的是同一个数。反过来把父级和末级
    一起加，同一笔钱会被算两遍，而且两遍的数都「像那么回事」。

    末级的判据用 ``parent_id``（谁是谁的父），而不是 ``is_leaf`` 字段 ——
    引擎的上卷也是按 ``parent_id`` 走的，两边用同一套判据才不会分叉。
    """
    subjects = subjects_of(db, book_id, year)
    child_ids = {s.parent_id for s in subjects if s.parent_id is not None}
    out: list[Subject] = []
    for s in subjects:
        if (s.category or "") not in PL_CATEGORIES:
            continue
        if s.id in child_ids:
            continue          # 上级科目：它的数引擎已经上卷过了，再加就是重复
        if not _category_wanted(s.category, categories):
            continue
        out.append(s)
    return out


def _split_totals(subjects: list[Subject],
                  moves: dict[int, tuple[Decimal, Decimal]]
                  ) -> tuple[Decimal, Decimal, dict[str, Decimal], dict[str, Decimal]]:
    """把区间发生额拆成「收入 / 费用」两方，并按科目类别分桶。

    **方向判据照抄 ``reports/evaluate.py`` 的 ``_orient``**（:274-279）：
    ``subject.direction == "CREDIT"`` ⇒ 取 ``-signed``（收入类取正），
    否则取 ``signed``（成本费用类取正）。这里调的是 ``caliber.orient``，
    它与那一条是同一个规则。

    为什么不能自己判方向：判错一次，收入和费用就**互换**了，而数字看着
    仍然合理 —— agent 会拿着一个「收入是费用」的结论去回答老板。

    **收入 / 费用的归属**：自然方向为贷的（PROFIT 类的收入科目）算收入，
    为借的（成本费用类）算费用。资产、负债、权益类不参与（见 ``PL_CATEGORIES``）。

    返回：``(收入合计, 费用合计, 收入分类字典, 费用分类字典)``。
    分类字典的键是 **``CATEGORY_CN`` 的中文类别名** —— agent 看中文更准，
    界面上显示的也是中文。
    """
    income = ZERO
    expense = ZERO
    income_by_category: dict[str, Decimal] = {}
    expense_by_category: dict[str, Decimal] = {}
    for s in subjects:
        dr, cr = moves.get(s.id, (ZERO, ZERO))
        value = orient(dr - cr, credit_natural=(s.direction == "CREDIT"))
        bucket = CATEGORY_CN.get(s.category or "", s.category or "")
        if s.direction == "CREDIT":
            income += value
            income_by_category[bucket] = income_by_category.get(bucket, ZERO) + value
        else:
            expense += value
            expense_by_category[bucket] = expense_by_category.get(bucket, ZERO) + value
    return income, expense, income_by_category, expense_by_category


def _change_pct(current: Decimal, base: Decimal) -> Optional[str]:
    """同比变化百分比。**用 Decimal 算** —— 浮点会算出 13.999999999999998
    这种数，agent 念出来就是错的。基数是 0 时无解（返回 None，由调用方在
    ``notes`` 里说明）。"""
    if base == 0:
        return None
    value = ((current - base) / base * Decimal(100)).quantize(Decimal("0.01"))
    return f"{value:f}"


# --------------------------------------------------------------------- 工具 8


@mcp_tool(name="income_expense_trend", description=TREND_DESCRIPTION)
def income_expense_trend(
    ctx: Context,
    book_id: int,
    year: int,
    granularity: str = "month",
    categories: Optional[list[str]] = None,
) -> IncomeExpenseTrendResult:
    """某一会计年度的收支趋势（按月）。

    入参：

    * ``book_id`` —— 账套 id（**必填**，可见性随令牌绑定用户的授权走）。
    * ``year`` —— 会计年度（**必填**）。科目表每个年度各有一份，跨年请按
      年度显式指定。
    * ``granularity`` —— 目前只支持 ``"month"``。
    * ``categories`` —— 可选的科目类别过滤（英文码或中文名）。
    """
    tc = current_tool_context(ctx)
    book = require_book(tc, book_id)

    if (granularity or "").strip().lower() != "month":
        raise McpBadRequest(
            f"暂不支持的粒度「{granularity}」；当前只支持按月（month）")

    year = resolve_year(tc.db, book, year)

    periods = [p for p in all_periods(tc.db, book_id) if p.year == year]
    if not periods:
        every = all_periods(tc.db, book_id)
        span = (f"{every[0].code} ~ {every[-1].code}" if every else "（该账套还没有会计期间）")
        raise McpBadRequest(
            f"该账套没有 {year} 年的会计期间（现有期间：{span}）")

    subjects = _pl_subjects(tc.db, book_id, year, categories)

    notes = [
        "损益取数已排除结转凭证；若与总账『本期发生额』不一致，差额即结转凭证金额",
        "口径为报表口径：只统计已记账（POSTED）的凭证，金额单位为元、两位小数；"
        "负数是红字（冲销），照原样保留符号。",
        "收入与费用按科目自然方向取正（与报表求值器同一套判据）：自然方向为贷的"
        "损益类科目算收入，为借的算费用。",
        "只统计**损益类**科目，与内置利润表口径一致：收入＝营业收入＋投资收益＋"
        "营业外收入，费用＝营业成本＋税金及附加＋销售/管理/财务费用＋营业外支出＋"
        "所得税费用。于是**收入 − 费用 == 利润表的净利润**。",
        "成本类科目（4001 生产成本、4101 制造费用、4301 研发支出等）**不在内置"
        "利润表的任何一行里**，所以不计入费用 —— 与老板在利润表上看到的数一致。",
        "年度合计按区间逐期累加得出，不是「本年累计相减」—— 本年累计每年 1 月"
        "归零，跨年相减会算错。",
    ]
    if not subjects:
        notes.append(
            f"该账套 {year} 年的科目表里没有可参与损益汇总的末级科目，返回的数全为 0。")

    series: list[dict[str, Any]] = []
    annual_income = ZERO
    annual_expense = ZERO

    # **报表口径**：排除结转凭证。与 ``for_ledger`` 的差额恰是结转凭证金额。
    # 引擎是只读的（实例内记忆化），本模块一次调用只建一个。
    eng = LedgerEngine.for_report(tc.db, book_id)
    ids = [s.id for s in subjects]

    if ids:
        # 年度合计：区间逐期累加。先算它 —— 它覆盖的区间最大，
        # 引擎的聚合缓存命中它之后，各月的那几次调用就不用再发 SQL 了。
        annual_moves = eng.range_movements(ids, periods[0].ord, periods[-1].ord)
        annual_income, annual_expense, _, _ = _split_totals(subjects, annual_moves)

    for p in periods:
        # 没有可汇总的科目时也要把这一期列出来（全 0）——
        # 空序列会让 agent 以为「这个月不存在」，而不是「这个月没有发生额」。
        moves = eng.range_movements(ids, p.ord, p.ord) if ids else {}
        income, expense, income_cat, expense_cat = _split_totals(subjects, moves)
        series.append({
            "period": p.code,
            "income": money_str(income),
            "expense": money_str(expense),
            "net": money_str(income - expense),
            "income_by_category": {k: money_str(v) for k, v in income_cat.items()},
            "expense_by_category": {k: money_str(v) for k, v in expense_cat.items()},
        })

    # 同比：与**上一年同期**比。上一年用**它自己那一年的科目表** ——
    # 科目 id 跨年不通用，拿本年的 id 去查上年的分录只会静默返回 0。
    yoy: Optional[dict[str, Any]] = None
    prev_year = year - 1
    prev_periods = [p for p in all_periods(tc.db, book_id) if p.year == prev_year]
    prev_subjects = _pl_subjects(tc.db, book_id, prev_year, categories)
    if not prev_periods or not prev_subjects:
        notes.append(f"上一年（{prev_year}）在本账套没有会计期间或没有科目表，无法计算同比。")
    else:
        prev_ids = [s.id for s in prev_subjects]
        prev_moves = eng.range_movements(
            prev_ids, prev_periods[0].ord, prev_periods[-1].ord)
        prev_income, prev_expense, _, _ = _split_totals(prev_subjects, prev_moves)
        if prev_income == 0 and prev_expense == 0:
            notes.append(f"上一年（{prev_year}）没有损益发生额，无法计算同比。")
        else:
            income_pct = _change_pct(annual_income, prev_income)
            expense_pct = _change_pct(annual_expense, prev_expense)
            yoy = {"income_change_pct": income_pct, "expense_change_pct": expense_pct}
            if income_pct is None:
                notes.append(f"上一年（{prev_year}）收入为 0，无法计算收入同比。")
            if expense_pct is None:
                notes.append(f"上一年（{prev_year}）费用为 0，无法计算费用同比。")

    tc.count(len(series))
    return envelope(IncomeExpenseTrendResult, 
        caliber=TREND_CALIBER,
        notes=notes,
        year=year,
        series=series,
        yoy=yoy,
        carry_forward_excluded=True,
    )


# --------------------------------------------------------------------- 工具 9


def _find_template(db, book_id: int, template_code: Optional[str],
                   template_id: Optional[int]) -> ReportTemplate:
    """找报表模板：**内置基线（book_id IS NULL）与该账套自己的副本都算数**。

    账套 fork 过模板之后，它的副本与内置基线**同一个 code** 两行
    （``report_template`` 上有两条唯一约束分别管这两种情况）。只查内置基线
    就查不到账套自己那份，只查账套自己的就查不到没 fork 过的账套 ——
    两个方向都会让 agent 拿到「模板不存在」。

    ``template_code`` 与 ``template_id`` 都给了以 **id** 为准（id 更精确）。
    """
    if template_id is not None:
        tpl = db.get(ReportTemplate, template_id)
        if tpl is None or (tpl.book_id is not None and tpl.book_id != book_id):
            raise McpNotFoundError(f"报表模板不存在：{template_id}")
        return tpl

    code = (template_code or "").strip()
    if not code:
        raise McpBadRequest("必须指定 template_code（如 INCOME_SHEET）或 template_id")
    tpl = db.scalar(
        select(ReportTemplate)
        .where(
            ReportTemplate.code == code,
            or_(ReportTemplate.book_id.is_(None), ReportTemplate.book_id == book_id),
        )
        # 账套自己的副本优先：它才是这个账套在界面上看到的那一份。
        .order_by(ReportTemplate.book_id.is_(None))
        .limit(1)
    )
    if tpl is None:
        raise McpNotFoundError(f"报表模板不存在：{code}")
    return tpl


def _period_by_code(db, book_id: int, code: str) -> Period:
    """按 ``YYYY-MM`` 取期间。

    **口径以 caliber 为准**（它那条查询已经改成按 ``year`` / ``month`` 查了）。
    它的报错文案（「这个账套从 X 月起启用」）比「期间不存在」有用得多 ——
    agent 问 2026-01 而账套 2026-03 才启用，它需要知道是「没这个期间」
    还是「问错了年份」。

    ``caliber`` 抛的 ``McpPeriodError`` 由**错误层统一**翻成结构化 400
    （``errors.to_mcp_error`` 认它）—— 工具层不必各写一遍 try/except。
    """
    return find_period_by_code(db, book_id, code)


def _find_period(db, book_id: int, period_code: Optional[str],
                 period_id: Optional[int]) -> Period:
    """找会计期间。

    * 给了 ``period_id`` → 按主键找，**并核对是不是这个账套的** ——
      期间 id 是账套内连续的序号，拿别家账套的 id 来会被算成别的期间。
    * 给了 ``period_code`` → 按 ``YYYY-MM`` 找（找不到时 ``caliber`` 会说清
      「这个账套从哪个月起启用」）。
    * 都没给 → 取「当前在记的那一期」。

    两个都给了以 **id** 为准（id 更精确，不会歧义）。
    """
    if period_id is not None:
        period = db.get(Period, period_id)
        if period is None or period.book_id != book_id:
            raise McpNotFoundError(f"会计期间不存在：{period_id}")
        return period
    if period_code:
        return _period_by_code(db, book_id, period_code)
    period = current_period(db, book_id)
    if period is None:
        raise McpNotFoundError("该账套还没有会计期间")
    return period


def _cell_money(cell: dict) -> dict:
    """单元格的值统一成「两位小数的字符串」。

    报表求值器给的是 ``str(Decimal)``，零值会写成 ``"0"``。界面上显示的是
    两位小数（前端统一格式化），所以这里也按同一套格式给出去 ——
    数值一个子儿没变，只是小数位对齐，agent 念出来与界面一致。
    """
    out = dict(cell)
    value = out.get("value")
    if value is None:
        return out
    text = str(value).strip()
    if not text:
        return out
    try:
        out["value"] = money_str(text)
    except ArithmeticError:
        # 不是金额（例如公式留下的文本），原样带出去，别把它改成 0。
        out["value"] = text
    return out


@mcp_tool(name="render_report", description=REPORT_DESCRIPTION)
def render_report(
    ctx: Context,
    book_id: int,
    template_code: Optional[str] = None,
    template_id: Optional[int] = None,
    period_code: Optional[str] = None,
    period_id: Optional[int] = None,
) -> RenderReportResult:
    """渲染某个账套的报表。

    入参：

    * ``book_id`` —— 账套 id（**必填**）。
    * ``template_code`` / ``template_id`` —— 二选一。内置基线的 code 是
      ``BALANCE_SHEET``（资产负债表）与 ``INCOME_SHEET``（利润表）。
    * ``period_code`` / ``period_id`` —— 二选一；都不传时取「当前在记的那一期」。
    """
    tc = current_tool_context(ctx)
    book = require_book(tc, book_id)

    template = _find_template(tc.db, book_id, template_code, template_id)
    period = _find_period(tc.db, book_id, period_code, period_id)

    # **整张表都交给报表求值器** —— MCP 不自己算报表。
    # evaluate() 抛 HTTPException 时由 MCP 的错误层翻成结构化错误，
    # 这里**不要**自己 try/except 吞掉它。
    result = evaluate(tc.db, template.id, book, period, False)

    rows = []
    for row in result.get("rows") or []:
        rows.append({**row, "cells": [_cell_money(c) for c in row.get("cells") or []]})

    cols = result.get("cols") or []
    # 口径标识按**这张表实际用了哪套取数口径**给：
    # 有「本期金额 / 本年累计」这类损益列 ⇒ 报表口径（排除结转凭证）；
    # 只有期末 / 年初余额列 ⇒ 账簿口径（含结转凭证，与总账逐分相同）。
    caliber = "report" if any(c.get("excludes_carry_forward") for c in cols) else "ledger"

    notes = [
        "本表由报表求值器渲染，数字与界面上的报表逐格一致。",
        "金额单位为元、两位小数，以字符串承载；负数是红字（冲销），照原样保留符号。",
    ]
    if caliber == "report":
        notes.append(
            "「本期金额」「本年累计」等损益列已排除结转凭证；"
            "这些列与总账 / 科目余额表的差额，即本期结转凭证金额。")
    else:
        notes.append("期末 / 年初余额列走账簿口径（含结转凭证），与总账、科目余额表逐分相同。")

    # 结转金额也是金额 ⇒ 统一成两位小数的字符串；本期没结转时保持 null
    # （null 与 0 是两件事：0 是「结转了一笔 0」，null 是「本期没有结转」）。
    carry = result.get("carry_forward_period_amount")
    carry = money_str(carry) if carry is not None else None
    if carry is not None:
        notes.append(f"本期结转凭证借方合计 {money_str(carry)} 元。")

    errors = result.get("errors") or []
    if errors:
        notes.append(f"有 {len(errors)} 个单元格的公式报错（见 errors），其余单元格照常算出。")
    if period.locked:
        notes.append("该期间已结账（locked=true），报表照读不误。")

    tc.count(len(rows))
    return envelope(RenderReportResult, 
        caliber=caliber,
        notes=notes,
        template={
            "id": template.id,
            "code": template.code,
            "name": template.name,
            "type": template.type,
            "builtin": template.book_id is None,
        },
        period={
            "id": period.id,
            "code": period.code,
            "year": period.year,
            "month": period.month,
            "locked": bool(period.locked),
        },
        rows=rows,
        carry_forward_excluded=bool(result.get("carry_forward_excluded")),
        carry_forward_period_amount=carry,
        notices=result.get("notices") or [],
        errors=errors,
    )


# --------------------------------------------------------------------- 注册


def register(server) -> None:
    """把本模块的工具注册到 MCP 服务上。"""
    server.add_tool(income_expense_trend, name="income_expense_trend",
                    description=TREND_DESCRIPTION)
    server.add_tool(render_report, name="render_report",
                    description=REPORT_DESCRIPTION)


__all__ = ["income_expense_trend", "register", "render_report"]
