"""工具出参模型（Pydantic）。

**为什么要有这一层。**
MCP 客户端拿到工具清单时，会按 ``outputSchema`` 决定「这个工具会返回什么」。
没有 schema，客户端看到的只是一个**没有形状的 dict** —— 它不知道 ``total``
是分页总数还是别的什么，也不知道 ``paid`` 和 ``accrued_only`` 为什么会同时
出现。于是每个 agent 都要自己猜字段的含义，猜错一次答案就错。

所以出参**一律用 Pydantic 模型定义**（计划 §3.7）：模型同时给三样东西 ——

1. **发布出去的 JSON Schema**（agent 看得见的契约）；
2. **返回时的校验**（工具说「我返回了 ``amount`` 是字符串」，那它就得真的是
   字符串；字段名拼错、类型漂了，在这里就现形，而不是等到 agent 那边）；
3. **结构化输出** ``structuredContent``（没有出参模型时，SDK 不会把它一起
   回给客户端，agent 只能去解析那段文本）。

**字段名英文、描述中文。** 字段名是要写进 agent 代码的标识符，跟着现有
``/api/*`` 的命名走；描述是给人（和模型）看的，用中文说清「这个数是什么
口径下的」。

**``extra="allow"``：为什么允许有未声明的字段。**
出参会随实现演进（加一个诊断字段不该是破坏性的变更）。而工具返回的
``dict`` 里若出现模型没声明的键，严格模式会**直接抛校验错**——那会让一次
正常的查询因为「多带了一个字段」而失败。允许额外字段，就多了一层向前兼容；
代价是 schema 里这些字段没有说明（模型里补一个字段声明即可）。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

#: 金额单位。全服务只有一种货币，标记写在出参里是为了让 agent 不用猜
#: 「12800.00 是元还是分」。
CURRENCY = "CNY"


class McpResult(BaseModel):
    """所有工具出参的公共外壳。

    ``caliber`` 与 ``notes`` 是**每个工具都有**的两项 —— 前者说「这份数在
    哪个口径下」，后者用人话解释「这个口径意味着什么」。agent 靠这两项
    判断手里的数能不能直接念给用户听。
    """

    model_config = ConfigDict(extra="allow")

    caliber: str = Field(description="口径标识：这个数是在哪套口径下取出来的")
    notes: list[str] = Field(
        default_factory=list,
        description="口径说明（人话）：agent 要靠它判断这个数能怎么用",
    )
    currency: str = Field(
        default=CURRENCY,
        description="金额单位：人民币元（两位小数，以字符串承载，避免浮点误差）",
    )


class _Row(BaseModel):
    """行级模型的公共配置。"""

    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------------- 导航类


class SubjectRef(_Row):
    """科目的紧凑引用（付款类工具里出现）。"""

    id: int = Field(description="科目 id（**年度专属**，跨年不通用）")
    code: str = Field(description="科目编码")
    name: str = Field(description="科目名")
    category: str = Field(description="科目类别：资产/负债/权益/成本/损益")
    direction: str = Field(description="科目自然方向：DEBIT 借 / CREDIT 贷")


class YearProgress(_Row):
    """一个会计年度的结账进度。"""

    year: int = Field(description="会计年度")
    total: int = Field(description="该年的会计期间总数")
    closed: int = Field(description="已结账的期间数")
    vouchers: int = Field(description="该年的凭证数")
    carried: bool = Field(description="该年是否已全部结完（损益已结转）")


class BookInit(_Row):
    """账套的初始化进度（两步：定科目、录期初）。"""

    book_id: int = Field(description="账套 id")
    year: int = Field(description="当前在记的那一年度")
    step: int = Field(description="当前进行到第几步")
    subjects_ready: bool = Field(description="科目是否已确定")
    subjects_at: Optional[str] = Field(default=None, description="科目确定的时刻")
    subjects_source: Optional[str] = Field(default=None, description="科目来源")
    can_import_subjects: bool = Field(description="是否允许导入科目表")
    can_keep_chart: bool = Field(description="是否允许保留现有科目表")
    openings_ready: bool = Field(description="期初是否已录完")
    openings_at: Optional[str] = Field(default=None, description="期初录完的时刻")
    openings_source: Optional[str] = Field(default=None, description="期初来源")
    opening_count: int = Field(description="已录期初的科目数")


class BookSummary(_Row):
    """一个账套的摘要。"""

    id: int = Field(description="账套 id")
    name: str = Field(description="账套名")
    company_name: str = Field(description="公司名")
    role: str = Field(description="当前用户在这个账套的角色：OWNER/ACCOUNTANT/VIEWER")
    start_year: int = Field(description="账套启用年度")
    latest_year: int = Field(description="账套最近一个有会计期间的年度")
    years: list[YearProgress] = Field(default_factory=list, description="各年度的结账进度")
    init: Optional[BookInit] = Field(default=None, description="初始化进度")


class ListBooksResult(McpResult):
    """``list_books`` 的出参：当前令牌用户**有授权**的账套。"""

    items: list[BookSummary] = Field(default_factory=list, description="账套清单")
    total: int = Field(description="账套总数")


class PeriodRow(_Row):
    """一个会计期间。"""

    id: int = Field(description="期间 id")
    code: str = Field(description="期间编码，形如 2026-01")
    year: int = Field(description="会计年度")
    month: int = Field(description="月份")
    ord: int = Field(description="账套内连续序号（跨年度连续）")
    locked: bool = Field(description="是否已结账（封账后不能再写凭证，但账照读不误）")
    closed_at: Optional[str] = Field(default=None, description="结账时刻")
    closed_by: Optional[str] = Field(default=None, description="结账人")
    vouchers: int = Field(description="该期凭证总数")
    posted: int = Field(description="其中已记账的")
    unposted: int = Field(description="其中未记账的")


class ListPeriodsResult(McpResult):
    """``list_periods`` 的出参：账套的全部会计期间。"""

    book_id: int = Field(description="账套 id")
    items: list[PeriodRow] = Field(default_factory=list, description="期间清单")
    total: int = Field(description="期间总数")


class SubjectRow(_Row):
    """一个科目。"""

    id: int = Field(description="科目 id（**年度专属**：同一编码在不同年度是两个 id）")
    code: str = Field(description="科目编码")
    name: str = Field(description="科目名")
    category: str = Field(description="科目类别：资产/负债/权益/成本/损益")
    category_name: str = Field(description="类别的中文名")
    direction: str = Field(description="自然方向：DEBIT 借 / CREDIT 贷")
    level: int = Field(description="级次")
    is_leaf: bool = Field(description="是否末级（凭证只能挂末级科目）")
    is_cash: bool = Field(description="是否现金类科目（1001/1002/1012）")
    active: bool = Field(description="是否启用")
    year: int = Field(description="所属会计年度（科目表每个年度各一份）")
    parent_code: Optional[str] = Field(default=None, description="上级科目编码")


class FindSubjectsResult(McpResult):
    """``find_subjects`` 的出参：按年度查科目表。"""

    book_id: int = Field(description="账套 id")
    year: int = Field(description="本次查的是哪一年的科目表")
    items: list[SubjectRow] = Field(default_factory=list, description="命中的科目")
    total: int = Field(description="命中总数（不是本页条数）")
    page: int = Field(description="当前页号")
    page_size: int = Field(description="每页条数（超限会被夹住）")


class BookBrief(_Row):
    """账套的简要信息。"""

    id: int = Field(description="账套 id")
    name: str = Field(description="账套名")
    company_name: str = Field(description="公司名")
    start_year: int = Field(description="启用年度")
    start_month: int = Field(description="启用月份")
    subject_code_rule: str = Field(description="科目编码规则")


class ContextYear(YearProgress):
    """``accounting_context`` 的年度行：在结账进度之外再加科目数。"""

    subject_count: int = Field(description="该年度科目表里的科目数")


class ContextPeriod(_Row):
    """``accounting_context`` 里的「当前那一期」。

    **它与 ``list_periods`` 的期间行形状不同**：这里只回答「现在在记哪一期」，
    不带凭证计数（那是 ``list_periods`` 的事）。两种形状各用一个模型 ——
    用同一个模型硬套，多出来的字段只能填 0，而 0 会被读成「这一期没有凭证」。
    """

    id: int = Field(description="期间 id")
    code: str = Field(description="期间编码，形如 2026-12")
    year: int = Field(description="会计年度")
    month: int = Field(description="月份")
    locked: bool = Field(description="是否已结账")


class ClosedPeriod(_Row):
    """已结账的期间。"""

    id: int = Field(description="期间 id")
    code: str = Field(description="期间编码，形如 2026-11")
    locked: bool = Field(description="是否已结账")
    closed_at: Optional[str] = Field(default=None, description="结账时刻")


class CashSubject(_Row):
    """现金类科目。"""

    id: int = Field(description="科目 id")
    code: str = Field(description="科目编码")
    name: str = Field(description="科目名")
    year: int = Field(description="所属会计年度")


class AccountingContextResult(McpResult):
    """``accounting_context`` 的出参：这个账套现在是什么情况。"""

    book: BookBrief = Field(description="账套信息")
    years: list[ContextYear] = Field(default_factory=list, description="有科目表的年度")
    current_accounting_year: int = Field(description="当前在记的那一年度（**不是**服务器日历年）")
    current_period: Optional[ContextPeriod] = Field(
        default=None, description="当前在记的那一期")
    latest_closed_period: Optional[ClosedPeriod] = Field(
        default=None, description="最近一期已结账的期间；还没结过账时为 null")
    subject_count: int = Field(description="当前年度的科目数")
    voucher_count: int = Field(description="当前年度的凭证数")
    cash_subjects: list[CashSubject] = Field(
        default_factory=list, description="现金类科目清单（1001/1002/1012）")


# --------------------------------------------------------------------- 明细类


class VoucherEntryRow(_Row):
    """凭证里的一条分录。"""

    subject_code: str = Field(description="科目编码")
    subject_name: str = Field(description="科目名")
    direction: str = Field(description="方向：DEBIT 借 / CREDIT 贷")
    amount: str = Field(description="金额（两位小数的字符串；红字带负号）")
    summary: str = Field(description="摘要")
    counterparty_cash: bool = Field(description="对方是不是现金类科目（碰到钱）")


class VoucherRow(_Row):
    """一张凭证。"""

    voucher_no: str = Field(description="凭证号")
    voucher_date: str = Field(description="凭证日期（YYYY-MM-DD）")
    status: str = Field(description="状态：POSTED 已记账 / UNPOSTED 未记账")
    source: str = Field(description="来源：MANUAL 手工 / CARRY_FORWARD 结转 / …")
    remark: str = Field(description="备注")
    period_code: str = Field(description="所属期间（YYYY-MM）")
    total_debit: str = Field(description="借方合计")
    total_credit: str = Field(description="贷方合计")
    entries: list[VoucherEntryRow] = Field(default_factory=list, description="分录明细")


class SearchVouchersResult(McpResult):
    """``search_vouchers`` 的出参：凭证检索。"""

    book_id: int = Field(description="账套 id")
    year: int = Field(description="本次查的是哪一年度")
    items: list[VoucherRow] = Field(default_factory=list, description="命中的凭证")
    total: int = Field(description="命中总数（不是本页条数）")
    page: int = Field(description="当前页号")
    page_size: int = Field(description="每页条数（超限会被夹住）")


class PaymentRecord(_Row):
    """一条与目标科目相关的分录记录。"""

    voucher_no: str = Field(description="凭证号")
    voucher_id: int = Field(description="凭证 id")
    date: str = Field(description="日期（YYYY-MM-DD）")
    amount: str = Field(description="金额（两位小数的字符串；红字带负号）")
    subject: Optional[SubjectRef] = Field(default=None, description="目标科目")
    counterparty: Optional[SubjectRef] = Field(default=None, description="对方科目")
    kind: str = Field(
        description="这条分录的性质：PAID 已付 / ACCRUED 仅计提未付 / "
                    "RECEIVED 已收款 / OTHER 其它往来")
    kind_name: str = Field(description="上述性质的中文名")
    status: str = Field(description="凭证状态：POSTED 已记账 / UNPOSTED 未记账")
    period_code: Optional[str] = Field(default=None, description="所属期间（YYYY-MM）")
    summary: str = Field(description="摘要")


class LatestPaymentResult(McpResult):
    """``latest_payment`` 的出参：最近一次真付出去的钱。

    **``found=false`` 时金额、日期、凭证号都是 null** —— 「没付过」不能
    靠一个空的日期字段表达，agent 会把它当成「日期是空的但确实付过」。
    候选记录在 ``alternatives[]`` 里，每条都带 ``kind``。
    """

    book_id: int = Field(description="账套 id")
    year: int = Field(description="本次查的是哪一年度")
    found: bool = Field(description="有没有找到「真付出去」的记录")
    payment_date: Optional[str] = Field(default=None, description="付款日期（YYYY-MM-DD）")
    amount: Optional[str] = Field(default=None, description="付款金额")
    voucher_no: Optional[str] = Field(default=None, description="凭证号")
    voucher_id: Optional[int] = Field(default=None, description="凭证 id")
    subject: Optional[SubjectRef] = Field(default=None, description="付出去的科目")
    counterparty: Optional[SubjectRef] = Field(default=None, description="对方科目（现金类）")
    cash_flow_kind: Optional[str] = Field(
        default=None, description="现金流性质：RECEIPT 收 / PAYMENT 付 / TRANSFER 转账")
    status: Optional[str] = Field(default=None, description="凭证状态")
    period_code: Optional[str] = Field(default=None, description="所属期间（YYYY-MM）")
    alternatives: list[PaymentRecord] = Field(
        default_factory=list, description="其它候选记录（带 kind 标签）")
    note: str = Field(default="", description="一句话结论（人话）")


class CheckPeriodPaymentsResult(McpResult):
    """``check_period_payments`` 的出参：某期间某科目付了没有。

    **三个标志互斥**：``paid`` / ``accrued_only`` / ``unposted_only`` 只会有
    一个为真 —— 「已付」「仅计提未付」「有凭证但未记账」是三件不同的事，
    混起来就会给出错误答案。
    """

    book_id: int = Field(description="账套 id")
    year: int = Field(description="该期间所属年度")
    period: str = Field(description="期间（YYYY-MM）")
    locked: bool = Field(description="该期是否已结账")
    paid: bool = Field(description="本期是否真的付出去过")
    paid_amount: str = Field(description="已付金额合计（带符号；红字冲销自然抵掉）")
    paid_dates: list[str] = Field(default_factory=list, description="付款日期清单")
    accrued_only: bool = Field(description="仅计提未付（对方是应付账款等负债类科目）")
    unposted_only: bool = Field(description="有凭证但未记账（未记账的不算已付）")
    records: list[PaymentRecord] = Field(default_factory=list, description="相关记录明细")
    note: str = Field(default="", description="一句话结论（人话）")


class BalanceRow(_Row):
    """科目余额表的一行。"""

    code: str = Field(description="科目编码")
    name: str = Field(description="科目名")
    category: str = Field(description="科目类别")
    category_name: str = Field(description="类别的中文名")
    direction: Optional[str] = Field(default=None, description="科目的自然方向")
    opening: str = Field(description="期初余额（带符号）")
    period_debit: str = Field(description="本期借方发生额")
    period_credit: str = Field(description="本期贷方发生额")
    ytd_debit: str = Field(description="本年累计借方")
    ytd_credit: str = Field(description="本年累计贷方")
    closing: str = Field(description="期末余额（带符号）")
    side: str = Field(description="余额所在的借贷方：借/贷/平（**按符号**判定）")


class BalanceCheck(_Row):
    """试算平衡结果。"""

    opening_balanced: bool = Field(description="期初是否平衡")
    period_balanced: bool = Field(description="本期发生额是否平衡")
    closing_balanced: bool = Field(description="期末是否平衡")
    total_opening_debit: str = Field(description="期初借方合计")
    total_opening_credit: str = Field(description="期初贷方合计")
    total_debit: str = Field(description="本期借方合计")
    total_credit: str = Field(description="本期贷方合计")
    total_ytd_debit: str = Field(description="本年累计借方合计")
    total_ytd_credit: str = Field(description="本年累计贷方合计")
    total_closing_debit: str = Field(description="期末借方合计")
    total_closing_credit: str = Field(description="期末贷方合计")


class LedgerBalanceResult(McpResult):
    """``ledger_balance`` 的出参：科目余额表。"""

    book_id: int = Field(description="账套 id")
    year: int = Field(description="该期间所属年度")
    period: str = Field(description="期末所在期间（YYYY-MM）")
    rows: list[BalanceRow] = Field(default_factory=list, description="余额行")
    check: BalanceCheck = Field(description="试算平衡结果")


# --------------------------------------------------------------------- 分析类


class TrendPoint(_Row):
    """收支趋势上的一个点（一期）。"""

    period: str = Field(description="期间（YYYY-MM）")
    income: str = Field(description="本期收入（两位小数的字符串）")
    expense: str = Field(description="本期费用（两位小数的字符串）")
    net: str = Field(description="本期净额 = 收入 − 费用")
    income_by_category: dict[str, str] = Field(
        default_factory=dict, description="收入按科目类别分桶")
    expense_by_category: dict[str, str] = Field(
        default_factory=dict, description="费用按科目类别分桶")


class TrendYoy(_Row):
    """同比。"""

    income_change_pct: Optional[str] = Field(default=None, description="收入同比百分比")
    expense_change_pct: Optional[str] = Field(default=None, description="费用同比百分比")


class IncomeExpenseTrendResult(McpResult):
    """``income_expense_trend`` 的出参：收支趋势（报表口径）。"""

    year: int = Field(description="会计年度")
    series: list[TrendPoint] = Field(default_factory=list, description="逐期收支")
    yoy: Optional[TrendYoy] = Field(default=None, description="与上一年的同比")
    carry_forward_excluded: bool = Field(
        description="是否已排除结转凭证（报表口径恒为真）")


class ReportTemplateRef(_Row):
    """报表模板。"""

    id: int = Field(description="模板 id")
    code: str = Field(description="模板编码，如 INCOME_SHEET")
    name: str = Field(description="模板名")
    type: str = Field(description="报表类型")
    builtin: bool = Field(description="是否内置基线")


class ReportPeriod(_Row):
    """报表所属期间。"""

    id: int = Field(description="期间 id")
    code: str = Field(description="期间（YYYY-MM）")
    year: int = Field(description="会计年度")
    month: int = Field(description="月份")
    locked: bool = Field(description="是否已结账")


class ReportCell(_Row):
    """报表的一个单元格。"""

    col_no: int = Field(description="列号")
    label: str = Field(description="列名")
    kind: str = Field(description="取数口径，如 YTD_AMOUNT 本年累计")
    formula: str = Field(description="该格的公式")
    value: Optional[str] = Field(default=None, description="算出来的值（两位小数的字符串）")
    error: Optional[str] = Field(default=None, description="该格的报错（若有）")


class ReportRow(_Row):
    """报表的一行。"""

    row_no: int = Field(description="行号")
    label: str = Field(description="行标题")
    indent: int = Field(description="缩进层级")
    bold: bool = Field(description="是否加粗")
    hidden: bool = Field(description="是否隐藏")
    style: dict[str, Any] = Field(default_factory=dict, description="该行的样式")
    cells: list[ReportCell] = Field(default_factory=list, description="各列的取值")


class RenderReportResult(McpResult):
    """``render_report`` 的出参：渲染一张报表。"""

    template: ReportTemplateRef = Field(description="模板信息")
    period: ReportPeriod = Field(description="报表所属期间")
    rows: list[ReportRow] = Field(default_factory=list, description="报表行")
    carry_forward_excluded: bool = Field(description="是否已排除结转凭证")
    carry_forward_period_amount: Optional[str] = Field(
        default=None, description="本期结转凭证金额")
    notices: list[str] = Field(default_factory=list, description="提示")
    errors: list[str] = Field(default_factory=list, description="报表级报错")


__all__ = [
    "CURRENCY", "AccountingContextResult", "BalanceCheck", "BalanceRow",
    "BookBrief", "BookInit", "BookSummary", "CashSubject", "CheckPeriodPaymentsResult",
    "ClosedPeriod", "ContextPeriod", "ContextYear", "FindSubjectsResult",
    "IncomeExpenseTrendResult",
    "LatestPaymentResult", "LedgerBalanceResult", "ListBooksResult",
    "ListPeriodsResult", "McpResult", "PaymentRecord", "PeriodRow",
    "RenderReportResult", "ReportCell", "ReportPeriod", "ReportRow",
    "ReportTemplateRef", "SearchVouchersResult", "SubjectRef", "SubjectRow",
    "TrendPoint", "TrendYoy", "VoucherEntryRow", "VoucherRow", "YearProgress",
]
