"""MCP 明细类工具：凭证检索、最近付款、期间支付核对、科目余额。

**这一块为什么存在。**
外部 agent 要回答的是「最近一笔租金是什么时候付出去的」「这个月电费付了
没有」这类问题，而这三个答案都落在这四个工具上：

* ``search_vouchers`` —— 兜底的凭证明细检索（「这个账套里发生了什么」）；
* ``latest_payment`` —— 「最近一次**真的**付出去」是什么时候；
* ``check_period_payments`` —— 「这个月付了没有」；
* ``ledger_balance`` —— 科目余额（与界面同一份实现）。

**最容易答错的一件事：「钱到底付出去没有」。**
系统里**没有银行流水表、没有收付款单据表**（见计划 §2.2），所以「付出去」
是**推导**出来的。推导的依据必须是**对方科目的编码前缀**，不是「这条分录
落在借方还是贷方」—— 于是有四种状态必须分得开：

======================  ====================================================
状态                    判据
======================  ====================================================
已付                    费用科目借方 + **现金类**对方（1001/1002/1012）
仅计提未付              费用科目借方 + **负债类**对方（2202 应付账款等）
有凭证但未记账          ``voucher.status == "UNPOSTED"``
本期无记录              该期间该科目一条相关凭证都没有
======================  ====================================================

现金类科目的判据是**编码前缀**（``accounting/organize.CASH_SUBJECT_PREFIXES``），
不是科目类别、也不是分录方向；「收入方」则按**顶层科目名白名单**
（收入/收益/补偿）判定。这两套判据都在 ``app/mcp/caliber.py`` 里，本模块
只调用、不重写。

**口径不重写。**
余额委托 ``ledger/queries.balance_sheet_of_accounts``（与界面同一份实现），
凭证检索委托 ``accounting/vouchers.query_vouchers``，收付判定委托
``accounting/organize.cash_flow_kind``，金额与红字处理委托
``accounting.vouchers.side_totals``（按列求和、负数照减）。本模块**不写第二套聚合** —— 本仓审计
第 7.3 节记过的「导出与页面显示对不上」，根子就是同一个口径有了两份实现。

**为什么本模块不写 ``from __future__ import annotations``。**
MCP SDK 解析工具注解时用的是装饰器所在模块（``tools/base.py``）的全局命名
空间；注解一旦变成字符串，那个命名空间里就没有本模块的类型，工具注册不上。
"""
import re
from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Iterable, Optional

from mcp.server.mcpserver import Context
from pydantic import Field
from sqlalchemy import select

from app.accounting.organize import is_carry_forward
from app.accounting.vouchers import query_vouchers, side_totals
from app.accounting.years import subjects_of
from app.core.config import settings
from app.ledger.queries import (
    balance_sheet_of_accounts,
    filter_balance_rows,
    subjects_for_periods,
)
from app.models import Entry, Period, Subject, Voucher

from app.mcp.caliber import (
    CATEGORY_CN,
    all_periods,
    build_income_lookup,
    cash_flow_kind_of,
    check_enum,
    check_range,
    clamp_page,
    clamp_page_size,
    current_period,
    find_period_by_code,
    is_cash_code,
    iso,
    match_keyword,
    money_str,
    money_value,
    parse_date,
    resolve_year,
)
from app.mcp.coverage import coverage_note, voucher_coverage
from app.mcp.errors import McpBadRequest, McpNotFoundError
from app.mcp.schemas import (
    CheckPeriodPaymentsResult,
    LatestPaymentResult,
    LedgerBalanceResult,
    SearchVouchersResult,
)
from app.mcp.tools.base import (
    current_tool_context,
    envelope,
    mcp_tool,
    require_book,
)

# --------------------------------------------------------------------- 常量

#: 多科目合并时一次最多取多少张凭证。
#:
#: 合并的实现是「按科目分别调 query_vouchers，再在 Python 里并起来」，所以
#: 一次要把某个科目在区间内的**全部**凭证拉进来才能算准去重后的总数。这个
#: 上限是那道闸：超过它就要求 agent 缩小日期范围，而不是让它把整本账拉走。
#:
#: **实际生效值是 ``settings.mcp_max_merge_vouchers``**（默认 2000，与这里
#: 一致）。这个常量只作为兜底默认值 —— 配置才是唯一的口径来源。
MERGE_FETCH_LIMIT = 2000

#: 一个关键词最多允许命中多少个科目（含跨年度）。
#:
#: 命中的科目越多，要跑的查询越多（每个科目一次 ``query_vouchers``）。
#: 真要一次看几十个科目，agent 应该改用 ``subject_id`` 精确指定 —— 这条闸
#: 逼它把话说清楚，而不是让它悄悄截断。
#:
#: **实际生效值是 ``settings.mcp_max_subjects``**（默认 60）。这条闸**同时**
#: 约束显式传入的 ``subject_ids`` —— 以前它只管关键词路径，显式传 id 是条
#: 没有闸的路（审查 H-03）。
MAX_SUBJECTS = 60


def _max_subjects() -> int:
    """单次调用允许的科目个数上限（配置为准）。"""
    return max(1, int(getattr(settings, "mcp_max_subjects", MAX_SUBJECTS)))


def _max_merge_vouchers() -> int:
    """多科目合并集的总张数上限（配置为准）。"""
    return max(1, int(getattr(settings, "mcp_max_merge_vouchers", MERGE_FETCH_LIMIT)))

#: ``alternatives[]`` 最多回多少条候选。
MAX_ALTERNATIVES = 10

#: 「计提未付」的对方科目类别：**负债类**（2202 应付账款、2203 预收账款…）。
#:
#: **按类别判，不按编码判。** 真实账套的编码可以是用友那套（完全不同的
#: 编号体系），硬编码「2202」换个账套就失效（计划 §2.2 的结论）。负债类
#: 才是「钱还欠着」这件事的本质。
ACCRUAL_CATEGORY = "LIABILITY"

#: 一条候选记录的状态。**这四个就是「钱到底付出去没有」的答案空间。**
KIND_PAID = "PAID"            # 已付：费用借方 + 现金类对方
KIND_ACCRUED = "ACCRUED"      # 仅计提未付：对方是负债类科目
KIND_RECEIVED = "RECEIVED"    # 收到钱：收入贷方 + 现金类对方
KIND_OTHER = "OTHER"          # 其它往来（对方既不碰现金也不是负债类）

_KIND_CN = {
    KIND_PAID: "已付",
    KIND_ACCRUED: "仅计提未付",
    KIND_RECEIVED: "已收款",
    KIND_OTHER: "其它往来",
}


# --------------------------------------------------------------------- 描述

_DESC_SEARCH = (
    "检索账套里的凭证。可按年度、科目、关键字、日期区间、金额区间筛选，"
    "结果分页返回（带过滤后的总条数）。"
    "**默认只读已记账凭证**；要包含未记账凭证请显式传 include_unposted=true。"
    "注意：keyword 只搜凭证号、备注与分录摘要，**不搜科目名**（找科目请用 "
    "find_subjects）；min_amount/max_amount 筛的是**单条分录**金额，不是整张"
    "凭证合计。"
)

_DESC_LATEST_PAYMENT = (
    "查「最近一次**真的**付出去」是什么时候：按科目（名称关键字或科目 id）"
    "找最近的一笔与现金类科目对上的凭证，并区分「已付 / 仅计提未付 / 有凭证"
    "但未记账 / 没有记录」四种状态。现金类科目按编码前缀判定（1001 库存现金、"
    "1002 银行存款、1012 其他货币资金）。命中多个科目时借方与贷方两侧都会给出"
    "候选，便于区分「付出去的租金」与「收到的租金」。"
)

_DESC_CHECK_PERIOD = (
    "核对某个会计期间某个科目**付了没有**。返回 paid（已付）、"
    "accrued_only（仅计提未付）、unposted_only（有凭证但未记账）三个互斥的"
    "结论标志，并给出已付金额与已付日期。对方是现金类科目（1001/1002/1012）"
    "才算真付出去；对方是应付账款等负债类科目只是计提，钱还没出去。"
)

_DESC_BALANCE = (
    "科目余额表。与界面上的「科目余额表」是**同一份实现**（"
    "ledger/queries.balance_sheet_of_accounts），所以同一期间的同一行数字"
    "必然一致。side 是期末余额所在的借贷方（借/贷/平），按**符号**判定，"
    "不是按科目自然方向反推。"
)


# --------------------------------------------------------------------- 小工具


def _money_param(value, name: str) -> Optional[Decimal]:
    """入参里的金额 → ``Decimal``。

    ``Decimal("abc")`` 抛的是 ``InvalidOperation``（不是 ``ValueError``），
    不接住的话会一路走到 500，agent 只看到「内部错误」。
    """
    if value in (None, ""):
        return None
    try:
        return money_value(value)
    except (InvalidOperation, ArithmeticError, ValueError):
        raise McpBadRequest(f"{name} 不是合法金额：{value!r}") from None


def _status_filter(status: Optional[str], include_unposted: bool) -> Optional[str]:
    """把 ``status`` 与 ``include_unposted`` 合成一个查询过滤。

    **默认只读已记账。** ``query_vouchers`` 自己**不带**状态过滤（传
    ``None`` 就是全都要），所以工具层必须显式给 —— 否则「默认只读已记账」
    这条口径在第一层就漏了，agent 会把未记账的凭证当成已入账的事实。
    """
    value = (status or "POSTED").strip().upper()
    if value == "ALL":
        return None
    if value not in ("POSTED", "UNPOSTED"):
        raise McpBadRequest(
            "status 只能是 POSTED（已记账）/ UNPOSTED（未记账）/ ALL")
    if include_unposted and value == "POSTED":
        # 「把未记账的也一起给我」—— 于是不再筛状态。
        return None
    return value


def _subject_map(db, subject_ids) -> dict[int, Subject]:
    """科目 id → 科目。

    **一次查完，不逐条懒加载。** 分录的 ``subject`` 关系是 lazy 的，逐条读
    会变成「每条分录一次查询」—— 一页 200 张凭证就是上千次往返。这里先把
    需要的科目捞进 identity map，后面的 ``entry.subject`` 就都是内存命中。
    """
    ids = {int(s) for s in subject_ids if s is not None}
    if not ids:
        return {}
    rows = db.scalars(select(Subject).where(Subject.id.in_(ids))).all()
    return {s.id: s for s in rows}


def _period_map(db, period_ids) -> dict[int, Period]:
    """期间 id → 期间（同样是一次查完，不逐条懒加载）。"""
    ids = {int(p) for p in period_ids if p is not None}
    if not ids:
        return {}
    rows = db.scalars(select(Period).where(Period.id.in_(ids))).all()
    return {p.id: p for p in rows}


def _cross_year_subject_error(subject: Subject, expected: Iterable[int]) -> McpBadRequest:
    """「科目 id 属于别的年度科目表」⇒ 一条**结构化 400**。

    **为什么是报错，不是空结果。** 科目表每个会计年度各有一份（迁移 0032），
    同一个编码在两个年度是两个 id、两个行。拿错年度的 id 去查，**匹配不到
    任何分录** —— 不报错，只是返回空。

    而「空」是最坏的一种答法：agent 会把它读成「那一年没有这笔业务」，
    然后把这个结论告诉老板。计划 §5.3 要求这里回 400 并说明「两份科目表，
    请指定 year」—— 让 agent 知道是**自己传错了 id**，它才能换个年度重取。

    所以：**凡是工具已经确定了「本次查的是哪一年/哪一期」，而调用方给的
    科目 id 来自别的年度，就是调用方的错**，直接指出来。
    """
    years = sorted({int(y) for y in expected})
    scope = "、".join(str(y) for y in years)
    return McpBadRequest(
        f"科目 {subject.id}（{subject.code} {subject.name}）属于 {subject.year} "
        f"年度科目表，与本次查询的 {scope} 年度不是同一份 —— 每个会计年度各有一份"
        "科目表，同一编码在两个年度是两个 id。请按本次查询的年度重新取科目"
        f"（find_subjects 的 year={scope}）。")


def _subject_ref(subject: Optional[Subject]) -> Optional[dict]:
    """科目的出参形状（紧凑版）。"""
    if subject is None:
        return None
    return {
        "id": subject.id,
        "code": subject.code,
        "name": subject.name,
        "category": subject.category,
        "direction": subject.direction,
    }


def _no_seq(voucher_no: str) -> int:
    """凭证号里的序号，按整数排序用。

    与 ``accounting/vouchers._no_seq`` 同一套判据：抠出**尾部**数字，抠不出
    给 -1（排最前）。按字符串比会踩 ``"记100" < "记099"`` 的坑（字典序），
    而凭证号恰恰是补零的。
    """
    matched = re.search(r"(\d+)$", voucher_no or "")
    return int(matched.group(1)) if matched else -1


def _sort_key(voucher: Voucher, period_ord: int, sort: str):
    """合并多科目结果时的排序键。

    **与 ``accounting/vouchers._apply_sort`` 同一套键。** 凭证号排序先按
    期间（``记001`` 与 ``记012`` 分属不同月，跨月比大小没意义），再按号段
    数字，最后按 id 兜底；日期排序则直接比日期。
    """
    seq = _no_seq(voucher.voucher_no)
    if sort == "no_asc":
        return (period_ord, seq, voucher.id)
    if sort == "no_desc":
        return (-period_ord, -seq, -voucher.id)
    if sort == "date_asc":
        return (voucher.voucher_date.toordinal(), seq, voucher.id)
    return (-voucher.voucher_date.toordinal(), -seq, -voucher.id)


def _query_by_subjects(db, book_id, subject_ids, *, from_period, to_period,
                       date_from, date_to, status, keyword, min_amount,
                       max_amount, sort, exclude_carry_forward=False):
    """取「含这些科目中任意一个」的凭证（去重后的并集）。

    **为什么不自己写一条 ``Entry.subject_id IN (...)`` 的查询。**
    ``query_vouchers`` 的过滤语义（状态 / 关键字 / 日期 / 金额 / 排序）是
    全仓的既有口径；照着它再写一遍 SQL 等于给同一个口径造第二份实现 ——
    那正是本仓审计第 7.3 节记过的病根。所以这里改成**按科目分别调它，
    再在 Python 里并起来**：过滤口径 100% 来自既有实现，合并只是集合并集，
    不涉及任何会计口径。

    代价是要把每个科目的结果整份取回来（去重后的总数才算得准），所以有
    ``settings.mcp_max_merge_vouchers`` 那道闸。

    **那道闸现在是「总量闸」，不是「每个科目各一份」。** 以前每个科目都能
    单独取满 2000 张，全部驻留进同一个 ``merged`` 字典 —— 传 60 个科目就是
    12 万张凭证（还带着全部分录的对象图）一次性进内存，一次调用就能把进程
    拖垮。现在：**合并集累计超过上限就报错**，并且错误信息会分清是
    「单个科目就超了」还是「合并起来超了」—— 两者的处置不一样。

    ``exclude_carry_forward`` **原样传给 ``query_vouchers``**：剔除要发生
    在每一条子查询的计数与分页之前，合并之后再剔就晚了（合并的基数是
    未剔除的）。
    """
    limit = _max_merge_vouchers()
    merged: dict[int, Voucher] = {}
    for subject_id in subject_ids:
        rows, total = query_vouchers(
            db, book_id,
            from_period=from_period, to_period=to_period,
            date_from=date_from, date_to=date_to,
            status=status, keyword=keyword,
            min_amount=min_amount, max_amount=max_amount,
            subject_id=subject_id,
            exclude_carry_forward=exclude_carry_forward,
            page=1, page_size=limit, sort=sort,
        )
        if total > len(rows):
            # **单个科目就超了**：与合并无关，缩小这个科目的区间才行。
            raise McpBadRequest(
                f"科目 {subject_id} 在该区间内就有 {total} 张凭证，超过单次上限 "
                f"{limit} 张；请缩小日期范围，或改用更精确的科目/关键字过滤后重试")
        for voucher in rows:
            merged[voucher.id] = voucher
            if len(merged) > limit:
                # **合并起来超了**：每个科目单独看都没超，是并集太大。
                raise McpBadRequest(
                    f"这些科目合并后共有 {len(merged)} 张凭证，超过单次上限 "
                    f"{limit} 张；请减少科目数量或缩小日期范围后重试")
    return list(merged.values())


# --------------------------------------------------------------------- 分录判定


def _counterparty_cash(entry: Entry, entries, subjects) -> bool:
    """这条分录的**对方**（另一侧）里有没有现金类科目。

    对方＝同一张凭证里方向相反的那些分录。这张凭证里只要有一笔现金类科目
    落在对面，这笔分录就「碰到了钱」。
    """
    for other in entries:
        if other.direction == entry.direction:
            continue
        subject = subjects.get(other.subject_id)
        if subject is not None and is_cash_code(subject.code):
            return True
    return False


def _classify_entry(entry: Entry, entries, subjects):
    """这条分录与对方科目合起来算什么 → ``(kind, 对方科目)``。

    **判据看对方的科目，不看分录落在哪一边。** 一张「借 560207 房屋租赁费
    / 贷 1002 银行存款」的凭证里，费用那条分录的对方是现金 ⇒ **钱出去了**；
    对方换成 2202 应付账款 ⇒ 费用确认了、**钱没出去**。两条分录各自的方向
    跟这件事没关系。
    """
    cash_partner: Optional[Subject] = None
    liability_partner: Optional[Subject] = None
    for other in entries:
        if other.direction == entry.direction:
            continue
        subject = subjects.get(other.subject_id)
        if subject is None:
            continue
        if cash_partner is None and is_cash_code(subject.code):
            cash_partner = subject
        if liability_partner is None and subject.category == ACCRUAL_CATEGORY:
            liability_partner = subject

    if entry.direction == "DEBIT":
        if cash_partner is not None:
            return KIND_PAID, cash_partner
        if liability_partner is not None:
            return KIND_ACCRUED, liability_partner
        return KIND_OTHER, None
    # 贷方：对方是现金类 ⇒ 钱进来了。
    if cash_partner is not None:
        return KIND_RECEIVED, cash_partner
    return KIND_OTHER, None


def _candidate_records(db, vouchers, subject_ids, periods):
    """把凭证拆成「一条条与目标科目相关的记录」，并逐条判定状态。

    返回的每条记录都带 ``kind``（已付 / 仅计提未付 / 已收款 / 其它往来）、
    对方科目与凭证号 —— agent 靠它区分「付出去的租金」与「收到的租金」。
    """
    # 科目表**一次**查完 —— 逐张凭证查就是「每张凭证一次查询」。
    subjects = _subject_map(
        db, [e.subject_id for v in vouchers for e in v.entries])
    records = []
    for voucher in vouchers:
        entries = list(voucher.entries)
        period = periods.get(voucher.period_id)
        for entry in entries:
            if entry.subject_id not in subject_ids:
                continue
            kind, partner = _classify_entry(entry, entries, subjects)
            records.append({
                "voucher_no": voucher.voucher_no,
                "voucher_id": voucher.id,
                "date": iso(voucher.voucher_date),
                "amount": money_str(entry.amount),
                "subject": _subject_ref(subjects.get(entry.subject_id)),
                "counterparty": _subject_ref(partner),
                "kind": kind,
                "kind_name": _KIND_CN.get(kind, kind),
                "status": voucher.status,
                "period_code": period.code if period is not None else None,
                "summary": entry.summary or "",
                # **结转凭证的标记**（单一事实源：``accounting.organize``）。
                # 结转凭证会把损益余额搬到「本年利润」，把它当成一笔
                # 业务分录参与聚合就会**重复计数** —— 这是本次评估里
                # 「2025 年水电费」算错 48 万的同一个坑。
                "is_carry_forward": is_carry_forward(voucher),
            })
    return records


def _paid_amount_by_voucher(records: list[dict]) -> dict[int, Decimal]:
    """按凭证归集「已付」金额。

    **为什么按凭证而不是按分录。** 一张付款凭证上可能有**好几条**同科目的
    分录（电费 3 笔 + 水费 2 笔），对方的现金类分录只有一条。把「最近一条
    命中分录的金额」当付款金额报出去，会差一个数量级 —— 实测 记0041 的真实
    付款是 146,324.31，而「最近一条分录」只有 3,950.80。

    **与 ``check_period_payments`` 的 ``paid_amount`` 同一个口径**：
    命中科目那几条分录的带符号合计（红字冲销自然抵掉）。
    """
    out: dict[int, Decimal] = {}
    for r in records:
        if r["kind"] != KIND_PAID:
            continue
        sid = r["voucher_id"]
        out[sid] = out.get(sid, Decimal("0")) + money_value(r["amount"])
    return out


def _voucher_cash_amount(voucher, subjects) -> Decimal:
    """这张凭证**现金侧**的带符号合计（现金类科目那几条分录）。

    付款时它在贷方、收款时它在借方；金额取原值（红字是负数，照原样保留）。
    """
    total = Decimal("0")
    for e in voucher.entries:
        subject = subjects.get(e.subject_id)
        if subject is not None and is_cash_code(subject.code):
            total += e.amount
    return total


def _fetch_all_matching(db, book_id, subject_ids, *, from_period, to_period,
                        date_from, date_to, status, keyword, min_amount,
                        max_amount, sort,
                        exclude_carry_forward=False) -> list[Voucher]:
    """取**全部**命中的凭证（透视用），带上限闸门。

    **为什么不能只聚合当前这一页。** ``group_by`` 要回答的是
    「2025 年电费一共多少」——那是**整批**命中凭证的合计。拿当前页（默认
    50 张）去汇总，报出来的数就是错的，而且看着还挺正常。

    上限用 ``settings.mcp_max_merge_vouchers``（默认 2000 张），超了直接报错
    并说清怎么缩小范围 —— 宁可让调用方缩小区间，也不要给一份只算了前 2000 张
    的「合计」。
    """
    limit = _max_merge_vouchers()
    if subject_ids:
        return _query_by_subjects(
            db, book_id, subject_ids, from_period=from_period, to_period=to_period,
            date_from=date_from, date_to=date_to, status=status,
            keyword=keyword, min_amount=min_amount, max_amount=max_amount,
            sort=sort, exclude_carry_forward=exclude_carry_forward)
    rows, total = query_vouchers(
        db, book_id, from_period=from_period, to_period=to_period,
        date_from=date_from, date_to=date_to, status=status,
        keyword=keyword, min_amount=min_amount, max_amount=max_amount,
        subject_id=None, exclude_carry_forward=exclude_carry_forward,
        page=1, page_size=limit, sort=sort)
    if total > len(rows):
        raise McpBadRequest(
            f"这个区间里命中的凭证有 {total} 张，超过透视上限 "
            f"{limit} 张；请缩小日期范围或加上科目过滤后重试")
    return rows


def _group_vouchers(vouchers, group_by: str, db) -> tuple[list[dict], dict]:
    """按 ``group_by`` 把凭证的分录分桶汇总。

    **分桶的单位是「分录」，不是「凭证」。** 「2025 年电费一共多少」问的
    是那些摘要写着「电费」的**分录**的合计；一张凭证里可能还有水费、
    还有现金那一头。把凭证整张塞进一个桶，报出来的数就不是问的那个数。

    借方贷方合计与凭证合计**同一口径**（按列求和、负数照减，见
    ``accounting.vouchers.side_totals``）。
    """
    subject_of = _subject_map(
        db, [e.subject_id for v in vouchers for e in v.entries])
    period_of = _period_map(db, [v.period_id for v in vouchers])
    buckets: dict[str, dict[str, Any]] = {}
    for voucher in vouchers:
        period = period_of.get(voucher.period_id)
        for entry in voucher.entries:
            subject = subject_of.get(entry.subject_id)
            if group_by == "summary":
                key = (entry.summary or "").strip()
                label = key or "（空摘要）"
            elif group_by == "subject":
                key = subject.code if subject else ""
                label = f"{key} {subject.name}" if subject else key
            elif group_by == "period":
                key = period.code if period else ""
                label = key
            elif group_by == "counterparty":
                key, label = _counterparty_bucket(entry, voucher, subject_of)
            else:   # direction
                key = entry.direction
                label = "借方" if entry.direction == "DEBIT" else "贷方"
            bucket = buckets.setdefault(key, {
                "key": key, "label": label, "vouchers": set(), "entries": [],
            })
            bucket["vouchers"].add(voucher.id)
            bucket["entries"].append(
                {"amount": entry.amount, "direction": entry.direction})

    groups: list[dict[str, Any]] = []
    total_vouchers: set[int] = set()
    total_entries: list[dict] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        debit, credit = side_totals(bucket["entries"])
        groups.append({
            "key": bucket["key"],
            "label": bucket["label"],
            "vouchers": len(bucket["vouchers"]),
            "entries": len(bucket["entries"]),
            "debit": money_str(debit),
            "credit": money_str(credit),
            "net": money_str(debit - credit),
        })
        total_vouchers |= bucket["vouchers"]
        total_entries.extend(bucket["entries"])
    t_debit, t_credit = side_totals(total_entries)
    totals = {
        "vouchers": len(total_vouchers),
        "entries": len(total_entries),
        "debit": money_str(t_debit),
        "credit": money_str(t_credit),
        "net": money_str(t_debit - t_credit),
    }
    return groups, totals


def _counterparty_bucket(entry, voucher, subject_of) -> tuple[str, str]:
    """这条分录的**对方科目** → ``(分桶键, 显示名)``。

    **对方是谁**：同一张凭证里**方向相反**的那些分录所涉及的科目。
    「借 560207 房屋租赁费 / 贷 1002 银行存款」这一张里，房屋租赁费那条的
    对方是银行存款，银行存款那条的对方是房屋租赁费 —— 所以
    ``group_by=counterparty`` 给出的是**对方视角**的分布：读「1002 银行存款」
    那个桶，就知道银行存款这一头对着的是哪些科目、各多少钱。

    **对方有多个时取金额最大的那一条。** 一张凭证里对方方向可能有好几条
    （借 A 100 / 借 B 50 / 贷 C 80 / 贷 D 70）：A 与 B 的对方都落在
    {C, D} 里，取金额最大的 C。**取「第一条」是不行的** —— 那取决于数据库
    返回分录的顺序，同一份数据换个次序答案就变了。「全列出来」也不行：
    分桶键必须是单个值。

    **并列时按科目编码取小的那个**，保证同一个键在任何一次调用里都稳定
    （金额并列的两条分录不该让结果随排序抖动）。

    对方方向一条分录都没有（单边凭证）⇒ 桶键为空，显示名写「（无对方分录）」
    —— 不静默地把这条分录算进某个桶里。
    """
    others = [e for e in voucher.entries if e.direction != entry.direction]
    if not others:
        return "", "（无对方分录）"
    # 按「金额从大到小、科目编码从小到大」取第一条。未知科目排在最后 ——
    # 一个查不到科目的分录不该抢走「对方是谁」的位置。
    others.sort(key=lambda e: (-abs(e.amount),
                               _subject_code(e, subject_of) == "",
                               _subject_code(e, subject_of)))
    best = others[0]
    code = _subject_code(best, subject_of)
    if not code:
        return "", "（对方科目未知）"
    subject = subject_of.get(best.subject_id)
    return code, f"{code} {subject.name}" if subject else code


def _subject_code(entry, subject_of) -> str:
    subject = subject_of.get(entry.subject_id)
    return subject.code if subject else ""


def _window_periods(db, book_id, from_period, to_period) -> list[Period]:
    """窗口内**真实存在**的期间（按 ord 升序）。"""
    return [p for p in all_periods(db, book_id)
            if from_period.ord <= p.ord <= to_period.ord]


def _record_sort_key(record):
    """候选记录的新旧排序（日期倒序，同日按凭证 id 倒序）。"""
    return (record["date"] or "", record["voucher_id"])


# --------------------------------------------------------------------- 工具 4


@mcp_tool(name="search_vouchers", description=_DESC_SEARCH)
def search_vouchers(
    ctx: Context,
    book_id: int,
    year: Optional[int] = None,
    subject_ids: Optional[Annotated[list[int], Field(
        max_length=settings.mcp_max_subjects,
        description=(
            f"要按科目过滤时传科目 id 列表，**最多 {settings.mcp_max_subjects} 个**。"
            "上限同时写在 JSON Schema 的 maxItems 上（给 agent 看），"
            "服务端还会再判一次 —— schema 是给人看的，不是护栏。"
        ),
    )]] = None,
    keyword: Optional[str] = None,
    status: str = "POSTED",
    include_unposted: bool = False,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_amount: Optional[str] = None,
    max_amount: Optional[str] = None,
    sort: str = "date_desc",
    group_by: Optional[str] = None,
    exclude_carry_forward: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> SearchVouchersResult:
    """检索凭证（强制分页）。

    入参：

    * ``book_id`` —— 账套 id（**必填**）。
    * ``year`` —— 会计年度。默认取账套当前在记的年度（不是服务器日历年）。
    * ``subject_ids`` —— 限定科目（可多选，取并集）。
    * ``keyword`` —— 搜凭证号、备注与分录摘要（**不搜科目名**）。
    * ``status`` —— ``POSTED``（默认）/ ``UNPOSTED`` / ``ALL``。
    * ``include_unposted`` —— 是否把未记账凭证也算进来。
    * ``group_by`` —— 透视维度：``summary`` / ``subject`` / ``period`` /
      ``counterparty`` / ``direction``。给值则返回 ``groups[]`` 与
      ``group_totals``，聚合范围是**全部命中的凭证**（不是当前这一页）。
    * ``exclude_carry_forward`` —— **是否剔除损益结转凭证**。
      结转凭证把损益余额搬到「本年利润」，统计费用/收入时算进去会**重复
      计入**（本次评估里「2025 年水电费」多算 48 万就是没剔它）。

      **剔除作用在返回的凭证本身**（``items`` 与 ``total`` 都不含它们），
      不是只作用在透视聚合上 —— 剔除在 SQL 层完成，落在计数与分页之前，
      所以 ``total`` 与页内张数永远对得上。
    """
    tc = current_tool_context(ctx)
    book = require_book(tc, book_id)
    db = tc.db

    notes: list[str] = []
    resolved_year = resolve_year(db, book, year)
    if year is None:
        notes.append(
            f"未指定年度，按账套当前在记的年度 {resolved_year} 取数"
            "（不是服务器日历年）。")
    notes.append(
        "默认只读已记账凭证（status=POSTED）；include_unposted=true 才包含"
        "未记账凭证。")
    # **把「按年查询」这件事说清楚**（审查 L-27）：``mcp_max_range_months``
    # 那条闸只约束跨期查询；本工具的窗口已经被年度夹住（最多 12 个月），
    # 天然比那条闸更严。不说清楚，agent 会以为那条闸没生效。
    notes.append(
        f"本次查询按年度取数（窗口已夹在 {resolved_year} 年内，最多 12 个月）；"
        "跨期查询的区间上限（mcp_max_range_months）只约束可跨年的工具，"
        "按年查询天然比它更严。")
    notes.append(
        "keyword 只搜凭证号、备注与分录摘要，不搜科目名 —— 找科目请用 "
        "find_subjects。")
    notes.append(
        "min_amount/max_amount 筛的是单条分录的金额，不是整张凭证的合计；"
        "matched_entries 给出**命中过滤条件的分录条数**（与返回的凭证张数"
        "不是一回事：一张凭证里可能只有一条分录命中）。")
    notes.append(
        "金额单位是元（两位小数字符串）；负数是红字，凭证合计按「红字算到"
        "对面」的口径。")

    sort = check_enum(sort, ("date_desc", "date_asc", "no_asc", "no_desc"), "sort")
    group_key = None
    if group_by is not None and str(group_by).strip():
        group_key = check_enum(
            group_by, ("summary", "subject", "period", "counterparty", "direction"),
            "group_by")
        notes.append(
            "本次按 group_by 聚合了**全部命中的凭证**（不是当前这一页），"
            "groups[] 给出透视结果，group_totals 是它的合计。")
        if group_key == "counterparty":
            notes.append(
                "对方口径：对方＝同一张凭证里方向相反的分录；对方有多个时取"
                "**金额最大的那一条**（并列按科目编码取小的），单边凭证归入"
                "「（无对方分录）」。所以这个维度给的是**对方视角**的分布。")

    page = clamp_page(page)
    page_size = clamp_page_size(page_size)
    status_filter = _status_filter(status, include_unposted)
    d_from = parse_date(date_from)
    d_to = parse_date(date_to)
    min_amt = _money_param(min_amount, "min_amount")
    max_amt = _money_param(max_amount, "max_amount")

    # 年度显式化：把查询圈在**该年度的期间**里。
    #
    # 科目表每个会计年度各有一份（迁移 0032），2026 的 1002 与 2027 的 1002
    # 是两行、两个 id。跨年 id 混用**不会报错、只会静默返回空** —— 那是最坏
    # 的一种错，所以这里把「年度」变成硬边界：查询只落在这一年的期间上，
    # 混进来的另一年科目 id 自然一条都匹配不到，并在 notes 里说明为什么空。
    periods = all_periods(db, book_id)
    year_periods = [p for p in periods if p.year == resolved_year]
    if not year_periods:
        notes.append(
            f"该账套在 {resolved_year} 年没有会计期间，返回空结果。")
        return envelope(SearchVouchersResult, 
            caliber="voucher", notes=notes, book_id=book_id,
            year=resolved_year, total=0, page=page, page_size=page_size,
            items=[], groups=[], group_totals=None, matched_entries=0)
    from_period = year_periods[0]
    to_period = year_periods[-1]
    check_range(from_period, to_period)

    ids = [int(s) for s in (subject_ids or []) if s is not None]
    # **科目个数闸（审查 H-03）。**
    #
    # 以前这条闸只管关键词路径（``MAX_SUBJECTS``），**显式传 subject_ids 是
    # 一条没有闸的路**：调用方可以一次传整个账套的科目（真实账套常有
    # 100~500 个），每个科目各自取满合并上限张凭证，全部驻留进同一个字典 ——
    # 一次调用就能把内存放大成一次拒绝服务。
    #
    # 现在两条路径**同一个口径**（``settings.mcp_max_subjects``）。
    max_subjects = _max_subjects()
    if len(ids) > max_subjects:
        raise McpBadRequest(
            f"这次传了 {len(ids)} 个科目，超过单次上限 {max_subjects} 个；"
            "请改用关键字缩小科目范围，或分几次查询")
    known = _subject_map(db, ids)
    for subject_id in ids:
        subject = known.get(subject_id)
        if subject is None or subject.book_id != book_id:
            raise McpBadRequest(
                f"科目 {subject_id} 不存在或不属于该账套")
        if subject.year != resolved_year:
            raise _cross_year_subject_error(subject, [resolved_year])

    if not ids:
        rows, total = query_vouchers(
            db, book_id, from_period=from_period, to_period=to_period,
            date_from=d_from, date_to=d_to, status=status_filter,
            keyword=keyword, min_amount=min_amt, max_amount=max_amt,
            subject_id=None, exclude_carry_forward=exclude_carry_forward,
            page=page, page_size=page_size, sort=sort)
    elif len(ids) == 1:
        rows, total = query_vouchers(
            db, book_id, from_period=from_period, to_period=to_period,
            date_from=d_from, date_to=d_to, status=status_filter,
            keyword=keyword, min_amount=min_amt, max_amount=max_amt,
            subject_id=ids[0], exclude_carry_forward=exclude_carry_forward,
            page=page, page_size=page_size, sort=sort)
    else:
        # 多科目：分别查再合并（见 _query_by_subjects 的说明）。
        merged = _query_by_subjects(
            db, book_id, ids, from_period=from_period, to_period=to_period,
            date_from=d_from, date_to=d_to, status=status_filter,
            keyword=keyword, min_amount=min_amt, max_amount=max_amt, sort=sort,
            exclude_carry_forward=exclude_carry_forward)
        total = len(merged)
        period_ords = {p.id: p.ord for p in year_periods}
        merged.sort(key=lambda v: _sort_key(v, period_ords.get(v.period_id, 0),
                                             sort))
        start = (page - 1) * page_size
        rows = merged[start:start + page_size]

    period_of = _period_map(db, [v.period_id for v in rows])
    subject_of = _subject_map(
        db, [e.subject_id for v in rows for e in v.entries])

    # **透视用的全量集合**。group_by 要的是「这一整批命中凭证」的汇总，
    # 不是当前这一页 —— 否则「2025 年电费一共多少」会被分页切成前 50 张。
    #
    # **结转凭证的剔除已经在 SQL 层做过了**（``exclude_carry_forward`` 传给了
    # ``query_vouchers``），所以这里拿到的集合已经不含结转凭证，不再二次过滤。
    group_vouchers = rows
    if group_key is not None:
        group_vouchers = _fetch_all_matching(
            db, book_id, ids, from_period=from_period, to_period=to_period,
            date_from=d_from, date_to=d_to, status=status_filter,
            keyword=keyword, min_amount=min_amt, max_amount=max_amt, sort=sort,
            exclude_carry_forward=exclude_carry_forward)

    # **命中过滤条件的分录条数**（凭证是按分录筛的）。
    matched_entries = 0
    for voucher in rows:
        for entry in voucher.entries:
            amt = entry.amount
            if min_amt is not None and amt < min_amt:
                continue
            if max_amt is not None and amt > max_amt:
                continue
            matched_entries += 1

    items = []
    for voucher in rows:
        entries = list(voucher.entries)
        debit, credit = side_totals([
            {"amount": e.amount, "direction": e.direction} for e in entries])
        period = period_of.get(voucher.period_id)
        entry_items = []
        for entry in entries:
            subject = subject_of.get(entry.subject_id)
            entry_items.append({
                "subject_code": subject.code if subject else None,
                "subject_name": subject.name if subject else None,
                "direction": entry.direction,
                "amount": money_str(entry.amount),
                "summary": entry.summary or "",
                # 对方（同一张凭证里方向相反的那些分录）里有没有现金类科目。
                "counterparty_cash": _counterparty_cash(entry, entries, subject_of),
            })
        items.append({
            "voucher_no": voucher.voucher_no,
            "voucher_date": iso(voucher.voucher_date),
            "status": voucher.status,
            "source": voucher.source,
            "remark": voucher.remark or "",
            "period_code": period.code if period is not None else None,
            "total_debit": money_str(debit),
            "total_credit": money_str(credit),
            "entries": entry_items,
            # **结转凭证的标记**（单一事实源）。聚合收入/费用时通常要
            # 排除它 —— 它把损益余额搬到「本年利润」，算进去就重复了。
            "is_carry_forward": is_carry_forward(voucher),
        })

    groups: list[dict[str, Any]] = []
    group_totals = None
    if group_key is not None:
        group_vouchers.sort(key=lambda v: _sort_key(
            v, {p.id: p.ord for p in year_periods}.get(v.period_id, 0), sort))
        groups, group_totals = _group_vouchers(
            group_vouchers, group_key, db)
        notes.append(
            "聚合口径：按每条分录的「摘要 / 科目 / 期间 / 方向」分桶，"
            "借方贷方合计与凭证合计同口径（按列求和、负数照减）。")
    # **结转凭证的口径说明放在「有没有 group_by」之外** ——
    # 剔除与否影响的是**返回的凭证本身**（items / total），不只是聚合。
    # 以前这句话只挂在 group_by 的分支里，于是「不聚合时结转凭证到底剔没剔」
    # 没人说得清 —— 而那正是最容易算错数的地方。
    notes.append(
        "已剔除损益结转凭证（items 与 total 都不含它们）。"
        if exclude_carry_forward else
        "**未剔除**损益结转凭证 —— 结转凭证把损益余额搬到本年利润，"
        "统计费用/收入时通常要加 exclude_carry_forward=true，否则会重复计入。")

    coverage = voucher_coverage(
        db, book_id, year_periods,
        # **标签要与真正用的状态过滤一致** —— ``status`` 可以是
        # ``POSTED`` / ``UNPOSTED`` / ``ALL``（``status_filter`` 为
        # ``None`` 表示不限），标签说错等于告诉 agent 「我按已记账口径
        # 取的数」，而实际取的是未记账。
        include_unposted=(status_filter in (None, "UNPOSTED")),
        include_posted=(status_filter in (None, "POSTED")))
    cov_note = coverage_note(coverage)
    if cov_note:
        notes.insert(0, cov_note)

    return envelope(SearchVouchersResult, 
        caliber="voucher", notes=notes, book_id=book_id, year=resolved_year,
        total=total, page=page, page_size=page_size, items=items,
        groups=groups, group_totals=group_totals,
        matched_entries=matched_entries, coverage=coverage)


# --------------------------------------------------------------------- 工具 5


def _resolve_window(db, book_id, *, year, as_of, lookback_months):
    """算出「往回看」的期间区间 → ``(from_period, to_period, 年度列表, 是否空)``。

    ``year`` 给了就整年看；没给就从 ``as_of`` 往回看 ``lookback_months`` 个
    月。区间里涉及哪几个年度，取决于它跨没跨年界 —— 而**跨年时科目表要按
    年度分别取**（同一编码在两个年度是两个 id）。
    """
    periods = all_periods(db, book_id)
    if not periods:
        raise McpBadRequest("该账套还没有任何会计期间")

    if year is not None:
        year = int(year)
        picked = [p for p in periods if p.year == year]
        if not picked:
            raise McpBadRequest(f"该账套在 {year} 年没有会计期间")
        from_period, to_period = picked[0], picked[-1]
        if as_of:
            last = parse_date(as_of)
            if last is not None:
                anchor = last.year * 100 + last.month
                bounded = [p for p in picked if p.year * 100 + p.month <= anchor]
                if not bounded:
                    # as_of 在这一年的第一期之前 —— 区间是空的。
                    return from_period, from_period, [year], True
                to_period = bounded[-1]
        return from_period, to_period, [year], False

    lookback = max(1, int(lookback_months or 1))
    if as_of:
        anchor = parse_date(as_of)
    else:
        current = current_period(db, book_id)
        if current is None:
            raise McpBadRequest("该账套还没有会计期间，无法定位「最近」")
        anchor = date(current.year, current.month,
                      monthrange(current.year, current.month)[1])
    to_period = find_period_by_code(
        db, book_id, f"{anchor.year}-{anchor.month:02d}")
    target_ord = to_period.ord - lookback + 1
    first_ord = periods[0].ord
    from_period = next(
        (p for p in periods if p.ord == max(target_ord, first_ord)), periods[0])
    window = [p for p in periods if from_period.ord <= p.ord <= to_period.ord]
    years = sorted({p.year for p in window})
    return from_period, to_period, years, False


@mcp_tool(name="latest_payment", description=_DESC_LATEST_PAYMENT)
def latest_payment(
    ctx: Context,
    book_id: int,
    subject_keyword: Optional[str] = None,
    subject_id: Optional[int] = None,
    as_of: Optional[str] = None,
    lookback_months: int = 12,
    year: Optional[int] = None,
    include_unposted: bool = False,
) -> LatestPaymentResult:
    """查最近一次真付出去的记录，并区分四种状态。"""
    tc = current_tool_context(ctx)
    book = require_book(tc, book_id)
    db = tc.db

    if subject_keyword is None and subject_id is None:
        raise McpBadRequest(
            "请指定 subject_keyword（科目名关键字）或 subject_id（科目 id）")

    notes: list[str] = []
    resolved_year = resolve_year(db, book, year)
    if year is None:
        notes.append(
            f"未指定年度，按账套当前在记的年度 {resolved_year} 取数"
            "（不是服务器日历年）。")
    notes.append(
        "「真付出去」= 对方是现金类科目（1001/1002/1012，判据是编码前缀）；"
        "对方是应付账款等负债类科目只算「仅计提未付」。")
    notes.append(
        "结论（已付 / 仅计提未付）只按已记账凭证算；未记账凭证仍会被检查，"
        "用来报出「有凭证但未记账」，include_unposted=true 才把它们列进候选。")

    from_period, to_period, years, empty = _resolve_window(
        db, book_id, year=year, as_of=as_of, lookback_months=lookback_months)
    if empty:
        return envelope(LatestPaymentResult, 
            caliber="payment", notes=notes, book_id=book_id,
            year=resolved_year, found=False, payment_date=None, amount=None,
            amount_basis="none", entry_amount=None, voucher_cash_amount=None,
            voucher_no=None, voucher_id=None, subject=None, counterparty=None,
            cash_flow_kind=None, status=None, period_code=None, alternatives=[],
            note=f"查询区间为空（as_of 在 {to_period.code} 之前），没有记录。")
    check_range(from_period, to_period)
    coverage = voucher_coverage(
        db, book_id, _window_periods(db, book_id, from_period, to_period),
        include_unposted=include_unposted)
    cov_note = coverage_note(coverage)
    if cov_note:
        notes.insert(0, cov_note)
    if len(years) > 1:
        notes.append(
            "查询区间跨了 " + "、".join(str(y) for y in years)
            + " 几个年度；科目表每个年度各有一份，跨年按编码分别匹配，"
            "所以区间里两个年度的同名科目都会作为候选。")

    # 科目定位：**按窗口涉及的每一张年度科目表**分别匹配。
    #
    # 跨年时同一编码在两个年度是两个 id，只取一张表就会漏掉另一年的那一半。
    target_ids: list[int] = []
    if subject_id is not None:
        subject = _subject_map(db, [int(subject_id)]).get(int(subject_id))
        if subject is None or subject.book_id != book_id:
            raise McpBadRequest(
                f"科目 {subject_id} 不存在或不属于该账套")
        target_ids = [subject.id]
        if subject.year not in years:
            raise _cross_year_subject_error(subject, years)
    else:
        for table_year in years:
            for subject in subjects_of(db, book_id, table_year):
                if match_keyword(subject, subject_keyword):
                    target_ids.append(subject.id)
        if len(target_ids) > _max_subjects():
            raise McpBadRequest(
                f"关键字命中了 {len(target_ids)} 个科目，超过单次上限 "
                f"{_max_subjects()} 个；请换一个更具体的关键字，或改用 subject_id。")
        if not target_ids:
            return envelope(LatestPaymentResult, 
                caliber="payment", notes=notes, book_id=book_id,
                year=resolved_year, found=False, payment_date=None, amount=None,
                voucher_no=None, voucher_id=None, subject=None,
                counterparty=None, cash_flow_kind=None, status=None,
                period_code=None, alternatives=[],
                note=f"没有找到名称里带「{subject_keyword}」的科目"
                     f"（查的是 " + "、".join(str(y) for y in years)
                     + " 年度的科目表）。")

    # 已记账与未记账的凭证**都要看**。
    #
    # 「有凭证但未记账」是四种状态里的一种，要报出它就必须看未记账的凭证；
    # 但结论（found / 已付）只按**已记账**的算，未记账的只在
    # ``include_unposted=true`` 时才进 alternatives。
    vouchers = _query_by_subjects(
        db, book_id, target_ids, from_period=from_period, to_period=to_period,
        date_from=None, date_to=None, status=None, keyword=None,
        min_amount=None, max_amount=None, sort="date_desc")
    periods = _period_map(db, [v.period_id for v in vouchers])
    records = _candidate_records(db, vouchers, set(target_ids), periods)
    records.sort(key=_record_sort_key, reverse=True)

    posted = [r for r in records if r["status"] == "POSTED"]
    unposted = [r for r in records if r["status"] != "POSTED"]
    # **结转凭证一律不算「付款」。** 结转凭证把损益余额搬到「本年利润」，
    # 它不是一笔业务付款；把它当成付款会把金额算大（本次评估里
    # 「头号金额取的是结转凭证里的单条分录」就是这一条）。
    carry = [r for r in posted if r.get("is_carry_forward")]
    business = [r for r in posted if not r.get("is_carry_forward")]
    paid = [r for r in business if r["kind"] == KIND_PAID]
    accrued = [r for r in business if r["kind"] == KIND_ACCRUED]
    received = [r for r in business if r["kind"] == KIND_RECEIVED]
    if carry:
        notes.append(
            f"这个区间里有 {len({r['voucher_id'] for r in carry})} 张损益结转凭证，"
            f"它们不是业务付款，已排除在「已付 / 仅计提未付」的结论之外。")

    def _visible(record):
        return include_unposted or record["status"] == "POSTED"

    if paid:
        primary = paid[0]
        voucher = db.get(Voucher, primary["voucher_id"])
        income_of = build_income_lookup(db, book_id)
        kind = cash_flow_kind_of(list(voucher.entries), income_of)
        alternatives = [r for r in records
                        if r is not primary and _visible(r)][:MAX_ALTERNATIVES]
        subject_name = (primary["subject"] or {}).get("name") or ""
        partner_name = (primary["counterparty"] or {}).get("name") or "现金类科目"
        # **金额按整张凭证算，不是那一条分录。** 一张付款凭证上常常有
        # 好几条同科目的分录（电费 3 笔 + 水费 2 笔），对方只有一条现金分录 ——
        # 只报「最近那条分录」会把 146,324.31 说成 3,950.80（差 37 倍）。
        # 口径与 ``check_period_payments`` 的 ``paid_amount`` 完全一致。
        paid_by_voucher = _paid_amount_by_voucher(paid)
        amount = paid_by_voucher.get(voucher.id, Decimal("0"))
        entry_amount = money_value(primary["amount"])
        voucher_subjects = _subject_map(db, [e.subject_id for e in voucher.entries])
        cash_amount = _voucher_cash_amount(voucher, voucher_subjects)
        note = (f"已付：{subject_name} 的对方是 {partner_name}，"
                f"{money_str(amount)} 元（凭证 {primary['voucher_no']} 里该科目的"
                f"合计，不是单条分录的 {money_str(entry_amount)}），"
                f"{primary['date']}。")
        if accrued:
            note += f" 另有 {len(accrued)} 条仅计提未付的记录（钱还没出去）。"
        if received:
            note += f" 另有 {len(received)} 条收款记录（收到钱，与付出去是两回事）。"
        if unposted:
            note += f" 另有 {len(unposted)} 条未记账凭证，未计入上述结论。"
        visible_rest = len([r for r in records if r is not primary and _visible(r)])
        if visible_rest > len(alternatives):
            note += (f" 候选共 {visible_rest} 条，这里只列出最近的 "
                     f"{len(alternatives)} 条。")
        return envelope(LatestPaymentResult, 
            caliber="payment", notes=notes, book_id=book_id, year=resolved_year,
            found=True, payment_date=primary["date"],
            amount=money_str(amount),
            amount_basis="voucher_focus_side",
            entry_amount=money_str(entry_amount),
            voucher_cash_amount=money_str(cash_amount),
            voucher_no=primary["voucher_no"], voucher_id=primary["voucher_id"],
            subject=primary["subject"], counterparty=primary["counterparty"],
            cash_flow_kind=kind, status=primary["status"],
            period_code=primary["period_code"], alternatives=alternatives,
            note=note, coverage=coverage)

    # 没有「已付」。把四种状态里实际命中的那一种讲清楚。
    if accrued:
        note = (f"仅计提未付：找到 {len(accrued)} 条费用已确认、但对方是应付"
                f"账款等负债类科目的记录（最近一条 {accrued[0]['date']}，"
                f"{accrued[0]['amount']} 元），钱还没有付出去。")
        alternatives = accrued[:MAX_ALTERNATIVES]
        if received:
            note += f" 另有 {len(received)} 条收款记录（收到钱，不是付出去）。"
    elif received:
        note = (f"只找到收款记录：{len(received)} 条相关凭证是「收到钱」"
                f"（收入方 + 现金类对方，最近一条 {received[0]['date']}，"
                f"{received[0]['amount']} 元），没有付出去的记录。")
        alternatives = received[:MAX_ALTERNATIVES]
    elif unposted:
        note = (f"有凭证但未记账：找到 {len(unposted)} 条相关凭证（最近一条 "
                f"{unposted[0]['date']}，{unposted[0]['amount']} 元，状态 "
                f"{unposted[0]['status']}），尚未记账，不能算已付。")
        alternatives = [r for r in unposted if _visible(r)][:MAX_ALTERNATIVES]
    else:
        note = (f"没有付款记录：{from_period.code} 至 {to_period.code} 之间，"
                f"该科目没有任何相关凭证。")
        alternatives = []

    return envelope(LatestPaymentResult, 
        caliber="payment", notes=notes, book_id=book_id, year=resolved_year,
        found=False, payment_date=None, amount=None, amount_basis="none",
        entry_amount=None, voucher_cash_amount=None, voucher_no=None,
        voucher_id=None, subject=None, counterparty=None, cash_flow_kind=None,
        status=None, period_code=None, alternatives=alternatives, note=note,
        coverage=coverage)


# --------------------------------------------------------------------- 工具 6


@mcp_tool(name="check_period_payments", description=_DESC_CHECK_PERIOD)
def check_period_payments(
    ctx: Context,
    book_id: int,
    period_code: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    subject_keyword: Optional[str] = None,
    subject_id: Optional[int] = None,
    include_unposted: bool = False,
) -> CheckPeriodPaymentsResult:
    """核对某个期间某个科目付了没有。"""
    tc = current_tool_context(ctx)
    # 这一行**本身就是闸门**：require_book 先查账套授权（没有 ⇒ 403），
    # 再查账套存在（没有 ⇒ 404）。它不返回需要用的东西，所以不接返回值。
    require_book(tc, book_id)
    db = tc.db

    if subject_keyword is None and subject_id is None:
        raise McpBadRequest(
            "请指定 subject_keyword（科目名关键字）或 subject_id（科目 id）")

    notes: list[str] = []
    # 期间解析：period_code 与 year+month 两种写法给一种就行。
    if period_code:
        period = find_period_by_code(db, book_id, period_code)
    elif year is not None and month is not None:
        period = find_period_by_code(
            db, book_id, f"{int(year)}-{int(month):02d}")
    else:
        period = current_period(db, book_id)
        if period is None:
            raise McpBadRequest("该账套还没有会计期间")
        notes.append(
            f"未指定期间，取账套当前在记的那一期 {period.code}。")

    notes.append(
        f"结论基于 {period.code} 的已记账凭证；「真付出去」= 对方是现金类科目"
        "（1001/1002/1012），对方是应付账款等负债类科目只算「仅计提未付」。")
    notes.append(
        "未记账凭证默认不进 records[]，但仍会检查其存在并用 unposted_only "
        "标出（include_unposted=true 可把它们也列出来）。")

    # 科目定位：**该期间所在年度**那张科目表。
    #
    # 科目表每个会计年度各有一份；用别的年度的科目 id 查这一期，
    # 结果是空（而不是串数据）。
    table_year = period.year
    target_ids: list[int] = []
    if subject_id is not None:
        subject = _subject_map(db, [int(subject_id)]).get(int(subject_id))
        if subject is None or subject.book_id != book_id:
            raise McpBadRequest(
                f"科目 {subject_id} 不存在或不属于该账套")
        if subject.year != table_year:
            raise _cross_year_subject_error(subject, [table_year])
        target_ids = [subject.id]
    else:
        for subject in subjects_of(db, book_id, table_year):
            if match_keyword(subject, subject_keyword):
                target_ids.append(subject.id)
        if len(target_ids) > _max_subjects():
            raise McpBadRequest(
                f"关键字命中了 {len(target_ids)} 个科目，超过单次上限 "
                f"{_max_subjects()} 个；请换一个更具体的关键字，或改用 subject_id。")

    if not target_ids:
        note = (f"没有找到匹配的科目（查的是 {table_year} 年度的科目表）。"
                if subject_id is None else
                f"该科目不属于 {table_year} 年度科目表，本期没有它的记录。")
        return envelope(CheckPeriodPaymentsResult, 
            caliber="payment", notes=notes, book_id=book_id,
            year=table_year, period=period.code, locked=bool(period.locked),
            paid=False, paid_amount="0.00", paid_dates=[],
            accrued_only=False, unposted_only=False, records=[], note=note,
            coverage=voucher_coverage(db, book_id, [period],
                                      include_unposted=include_unposted))

    # 状态过滤：**已记账与未记账都要看**。
    #
    # 「有凭证但未记账」是四种状态里的一种，要报出它就必须看未记账的凭证；
    # 但结论（已付 / 仅计提未付）与 records[] 仍然只按已记账的算，
    # 未记账的只在 include_unposted=true 时才进 records[]。
    vouchers = _query_by_subjects(
        db, book_id, target_ids, from_period=period, to_period=period,
        date_from=None, date_to=None, status=None, keyword=None,
        min_amount=None, max_amount=None, sort="date_desc")
    periods = _period_map(db, [v.period_id for v in vouchers])
    records = _candidate_records(db, vouchers, set(target_ids), periods)

    posted = [r for r in records if r["status"] == "POSTED"]
    unposted = [r for r in records if r["status"] != "POSTED"]
    # **结转凭证不是业务付款。** 它的对方是「本年利润」（权益类），
    # 天然不会被判成「已付」；这里再显式排除一道，免得哪天判据变了
    # 把它算进来（本次评估的「2025 年水电费」就差点这么错）。
    carry = [r for r in posted if r.get("is_carry_forward")]
    business = [r for r in posted if not r.get("is_carry_forward")]

    paid_records = [r for r in business if r["kind"] == KIND_PAID]
    accrued_records = [r for r in business if r["kind"] == KIND_ACCRUED]
    if carry:
        notes.append(
            f"本期有 {len({r['voucher_id'] for r in carry})} 张损益结转凭证，"
            f"它们不是业务付款，未计入已付金额。")

    # 三个标志**互斥**：先看已付，再看仅计提，最后看有没有未记账的。
    paid = bool(paid_records)
    accrued_only = (not paid) and bool(accrued_records)
    unposted_only = (not paid) and (not accrued_only) and bool(unposted)

    # 已付金额：各条**带符号**相加。
    #
    # 这里**不能用 side_totals** —— 它算的是「一张凭证两栏的合计」，而这里
    # 要的是「多张凭证里支付金额的净额」。红字冲销的那笔是负数，相加自然
    # 把之前的付款抵掉，这正是净额该有的行为。
    paid_amount = money_str(sum(
        (money_value(r["amount"]) for r in paid_records), Decimal("0")))
    paid_dates = sorted({r["date"] for r in paid_records if r["date"]})

    listed = posted + (unposted if include_unposted else [])
    listed.sort(key=_record_sort_key, reverse=True)

    coverage = voucher_coverage(db, book_id, [period],
                                include_unposted=include_unposted)
    cov_note = coverage_note(coverage)
    if cov_note:
        notes.insert(0, cov_note)

    if paid:
        note = (f"本期已付：共 {len(paid_records)} 笔，合计 {paid_amount} 元，"
                f"支付日期 {'、'.join(paid_dates)}。")
    elif accrued_only:
        note = (f"本期仅计提未付：{len(accrued_records)} 笔费用已确认，"
                f"对方是应付账款等负债类科目，钱还没有付出去。")
    elif unposted_only:
        note = (f"本期有凭证但未记账：{len(unposted)} 张未记账凭证"
                f"（最近一张 {unposted[0]['voucher_no']}，"
                f"{unposted[0]['date']}），尚未记账，不能算已付。")
    else:
        note = "本期没有该科目的相关记录。"
    if unposted and not unposted_only:
        note += f" 另有 {len(unposted)} 张未记账凭证未计入上述结论。"
    if accrued_records and paid:
        note += f" 另有 {len(accrued_records)} 笔仅计提未付的记录。"

    return envelope(CheckPeriodPaymentsResult, 
        caliber="payment", notes=notes, book_id=book_id, year=table_year,
        period=period.code, locked=bool(period.locked), paid=paid,
        paid_amount=paid_amount, paid_dates=paid_dates,
        accrued_only=accrued_only, unposted_only=unposted_only,
        records=listed, note=note, coverage=coverage)


# --------------------------------------------------------------------- 工具 7


@mcp_tool(name="ledger_balance", description=_DESC_BALANCE)
def ledger_balance(
    ctx: Context,
    book_id: int,
    period_code: Optional[str] = None,
    period_id: Optional[int] = None,
    from_period_code: Optional[str] = None,
    subject_scope: str = "all",
    include_unposted: bool = False,
) -> LedgerBalanceResult:
    """科目余额表（与界面同一份实现）。"""
    tc = current_tool_context(ctx)
    # 这一行**本身就是闸门**：require_book 先查账套授权（没有 ⇒ 403），
    # 再查账套存在（没有 ⇒ 404）。它不返回需要用的东西，所以不接返回值。
    require_book(tc, book_id)
    db = tc.db

    notes: list[str] = []
    if period_code:
        period = find_period_by_code(db, book_id, period_code)
    elif period_id is not None:
        period = db.get(Period, int(period_id))
        if period is None or period.book_id != book_id:
            raise McpNotFoundError("会计期间不存在")
    else:
        period = current_period(db, book_id)
        if period is None:
            raise McpBadRequest("该账套还没有会计期间")
        notes.append(f"未指定期间，取账套当前在记的那一期 {period.code}。")

    from_period = None
    if from_period_code:
        from_period = find_period_by_code(db, book_id, from_period_code)
        check_range(from_period, period)

    # **枚举参数要校验，不能让它落到「值错了就当 all」。**
    # 以前非法值被悄悄当成 all，调用方拿到一份多行的表还以为是自己要的。
    subject_scope = check_enum(subject_scope, ("all", "top", "leaf"), "subject_scope")

    notes.append(
        "余额表走的是界面同一份实现（ledger/queries.balance_sheet_of_accounts），"
        "所以同一期间的同一行数字与界面必然一致。")
    notes.append(
        "side 是期末余额所在的借贷方（借/贷/平），按符号判定，不是按科目自然"
        "方向反推；direction 才是科目的自然方向。")
    notes.append(
        "试算合计（check）按明细级科目算，不随 subject_scope 档位变化。")
    notes.append(
        "check 里的合计**恒按末级科目（leaf）算**，与 subject_scope 无关 —— "
        "subject_scope 只决定 rows 里列不列父科目行。所以「all」档位下"
        "**显示行之和 ≠ 合计**（父科目行不计入合计）；每一行的 "
        "included_in_totals 标明它计不计入。")
    if from_period is not None:
        notes.append(
            f"本次是**区间查询**（{from_period.code} ~ {period.code}）："
            f"期初取 {from_period.code} 那一期的期初余额，与单期查询同一口径；"
            f"本期借/贷列装的是**区间**发生额。")

    # 科目表：区间涉及哪几个年度就取哪几个年度的并集 —— 与接口
    # ``GET /api/books/{id}/ledger/balance`` 完全同一句取法。
    subjects = subjects_for_periods(
        db, book_id, [from_period, period] if from_period else [period])
    rows, check = balance_sheet_of_accounts(
        db, book_id, subjects, period, include_unposted,
        from_period=from_period)
    rows = filter_balance_rows(rows, subject_scope)

    natural = {s.id: s for s in subjects}
    out_rows = []
    for row in rows:
        subject = natural.get(row["subject_id"])
        out_rows.append({
            "code": row["code"],
            "name": row["name"],
            "category": row["category"],
            "category_name": CATEGORY_CN.get(row["category"] or "",
                                            row["category"]),
            "direction": (subject.direction if subject is not None else None),
            "opening": money_str(row["opening"]),
            "period_debit": money_str(row["period_debit"]),
            "period_credit": money_str(row["period_credit"]),
            "ytd_debit": money_str(row["ytd_debit"]),
            "ytd_credit": money_str(row["ytd_credit"]),
            "closing": money_str(row["closing"]),
            # 这一列就是 ``display_side`` 的结果（引擎按符号判定），
            # **不是** subject.direction 的反推 —— 早期版本按自然方向判，
            # 会把「短期借款 −1000」显示成「借 1000」。
            "side": row["direction"],
            # **这一行计不计入 check 合计**。合计恒按末级科目算（否则
            # 父级汇总行会重复计数），所以父科目行是 false ——
            # 「显示行之和 ≠ 合计」这件事必须显式可见，否则调用方
            # 手动加总会得到一个偏大的数，还以为账不平。
            "included_in_totals": bool(row.get("is_leaf")),
        })

    out_check = {}
    for key, value in check.items():
        out_check[key] = money_str(value) if isinstance(value, Decimal) else value

    coverage = voucher_coverage(
        db, book_id,
        [p for p in (from_period, period) if p is not None],
        include_unposted=include_unposted)
    cov_note = coverage_note(coverage)
    if cov_note:
        notes.insert(0, cov_note)

    return envelope(LedgerBalanceResult, 
        caliber="ledger", notes=notes, book_id=book_id, year=period.year,
        period=period.code, rows=out_rows, check=out_check,
        totals_scope="leaf", coverage=coverage)


# --------------------------------------------------------------------- 注册


def register(server) -> None:
    """把本模块的工具注册到 MCP 服务上。"""
    server.add_tool(search_vouchers, name="search_vouchers",
                    description=_DESC_SEARCH)
    server.add_tool(latest_payment, name="latest_payment",
                    description=_DESC_LATEST_PAYMENT)
    server.add_tool(check_period_payments, name="check_period_payments",
                    description=_DESC_CHECK_PERIOD)
    server.add_tool(ledger_balance, name="ledger_balance",
                    description=_DESC_BALANCE)
