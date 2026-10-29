"""流水 ↔ 账上分录的**一对一分配**。

**这一页是对账这一条线唯一的「谁跟谁是一笔」的裁判。**

流水明细页（``service.plan_vouchers``）问的是「这一行流水，账上有没有
对应的那一笔？」；存疑凭证页（``suspicious``）问的是反过来的一句：
「这一笔账，对账单里有没有对应的那一行？」。

**两个问题必须用同一把尺子**，而且答案必须**互不打架**：

- 尺子不一样 → 同一笔钱在流水页说「没入账」、在存疑页说「对得上」，
  用户两边都看，两边都看不懂（2026-10-29 用户报的那个问题就是这个）；
- 尺子一样但**不是一对一** → 一张凭证可以「对上」很多行流水，
  而账上明明只有一笔（用户报的第二条问题：四笔同额流水全显示「已入账」，
  账上其实只记了一笔）。

所以这一模块只做一件事：**把「流水行」与「账上分录」一一对上**，
对不上的两边各自留在「没对上」那一堆里。

**为什么「一对一」是硬要求。** 一笔账就是一笔钱。对账单上四笔金额相同
的支出，对应的是账上四笔（或者三笔 + 一笔没记）；一张凭证上的那一条
分录，对应的也只能是**一条**流水行 —— 同一笔钱不可能既是 A 行的结算
又是 B 行的结算。所以「一张凭证对上四行」在会计上就是错的：那四行里
有三行是**账上根本没记**的钱，界面上却说它们「已入账」，于是
「生成流水凭证」跳过它们，这三笔钱永远进不了账。

**三档窗口（这是口径，不是事实）**：

===========================  ==========================================
窗口                          判定
===========================  ==========================================
``≤ PRIMARY_WINDOW_DAYS``     **已入账** —— 自动判，不用问人
``≤ REVIEW_WINDOW_DAYS``      **待复核** —— 金额对得上、日期对不上
（两档之间）                  → 提醒人工复核（**不自动制证**）
``> REVIEW_WINDOW_DAYS``       **不是同一笔** —— 当作没这回事
===========================  ==========================================

**为什么中间要留一档「待复核」。** 银行流水记的是**实际收付日期**，
会计凭证记的常常是**发票日期**（票开了几天才付、货款到了才开票）——
两者差十天是常事。按一档切（差 7 天以上就不认），那笔钱在流水页上
是「没入账」（于是「生成流水凭证」又给它记一遍，**同一笔钱记两次**），
在存疑页上却因为窗口更宽而「对得上」（于是也不报）——
**两页都不报，它就从系统的视野里整个消失了**。

中间这一档就是给它一个落脚的地方：**报出来，让人判**。
判「是同一笔」→ 挂上那张凭证；判「不是同一笔」→ 放行，让它正常制证。

**为什么不是直接放宽成 30 天自动判「已入账」。** 那等于替用户认定
「日期差了 30 天也是同一笔」——而 30 天前的那笔钱很可能就是**另一笔**
（每月固定金额的房租、货款尤其容易撞）。日期差这么多时，人来判断
比机器猜要可靠，而且**猜错的代价是钱记两遍**。

**「已确认的关联」优先（``claims``）。** 用户在「可改科目」里人工
确认过的那一行，它与那张凭证的关联是**人的决定**，不许被下面那次
自动分配顶掉 —— 自动分配是启发式，人工的确认不是。

**但一张凭证只能被一个行认领。** 所以「已确认」也是排队认领：
几行都指着同一张凭证时，按**日期差最小**的那一行拿到它，其余的行
收回标记（它们本来就不该「已入账」——见上面「为什么一对一是硬要求」）。

**这一模块不查库。** 它只对**已经在内存里的两侧**做分配 ——
查库、取数、口径的边界都在 ``service`` 与 ``suspicious`` 那边。
这样这条规则可以单独测：给它两堆数据，它只回答「谁跟谁是一笔」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

#: **自动判「已入账」的窗口**（天）。
#:
#: 同一天同金额同科目的两笔款是常事（付两笔一样的运费），没有窗口就会
#: 把第二笔误判成重复，于是它永远进不了账。窗口取 7 天是因为发票与付款
#: 之间差几天是常态。
PRIMARY_WINDOW_DAYS = 7

#: **「待复核」的窗口**（天）—— 超过这个天数就当**不是同一笔**。
#:
#: 与「改科目」那条路上的跨期窗口（``matching.EXTENDED_WINDOW_DAYS``）
#: 与存疑凭证那条路上的窗口（``suspicious.MATCH_WINDOW_DAYS``）**同一个值**：
#: 这一条线上只有一把尺子，三处各用各的数正是「两页都不报」的成因。
#:
#: 为什么是 30 天：跨月（28 天左右）是最常见的那种差，再往上就明显不是
#: 同一笔了 —— 生产实测：同额同向的候选在 ±7 天 0 条、±30 天 3 条、
#: ±60 天 4 条、±90 天 6 条（73 天、87 天那两条几乎肯定是不同的钱）。
#: 窗口越大噪音越多，而「同额」在现实里一点都不稀有。
REVIEW_WINDOW_DAYS = 30

#: 银行账方向 → 会计现金侧方向。
#:
#: **银行账与会计凭证的方向是相反的**：流水 IN（收钱）= 账户余额增加 =
#: 现金增加 = 借方；流水 OUT（付钱）= 余额减少 = 贷方。
#:
#: 写成一张表而不是散落的 if：这条线上的每一处方向换算都要用它，
#: 写错一次就是「收入被当成支出」—— 而那种错在界面上完全看不出来。
CASH_DIRECTION = {"IN": "DEBIT", "OUT": "CREDIT"}


def cash_direction(row_direction: str) -> Optional[str]:
    """流水方向 → 现金侧方向。**认不出就返回 ``None``**（不猜）。"""
    return CASH_DIRECTION.get(row_direction)


def amount_key(value: object) -> str:
    """金额的匹配键：**统一成两位小数的字符串再比**。

    同一笔钱在流水行与分录两处的字符串化结果可能不一样
    （``"3984.0"`` 与 ``"3984.00"``），直接比字符串会漏配 ——
    漏配的代价是凭空多出一张「存疑凭证」，或者一笔明明已入账的流水
    被当成没入账。
    """
    if value is None:
        return ""
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except (ArithmeticError, ValueError):
        return str(value)


@dataclass(frozen=True)
class RowSide:
    """对账单上的**一条流水**（分配的一侧）。"""

    id: int
    tx_date: Optional[date]
    amount: Optional[Decimal]
    direction: str
    #: 排序用的稳定次序（一般是行号）。
    seq: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (amount_key(self.amount), cash_direction(self.direction) or "")


@dataclass(frozen=True)
class EntrySide:
    """账上的**一条分录**（分配的另一侧）。

    **分配的单位是「分录」而不是「凭证」**：一张凭证上可能有**好几条**
    金额相同、方向相同的分录（拆分的凭证），它们各自对应一条流水。
    按凭证分配会把「四条流水对一张有两个分录的凭证」算成「四笔都已入账」。
    """

    id: int
    voucher_id: int
    voucher_no: str
    voucher_date: date
    amount: Decimal
    #: DEBIT / CREDIT（**现金侧**方向）。
    direction: str

    @property
    def key(self) -> tuple[str, str]:
        return (amount_key(self.amount), self.direction)


@dataclass(frozen=True)
class Pair:
    """配成了一对的一行流水与一条分录。"""

    row_id: int
    entry_id: int
    voucher_id: int
    voucher_no: str
    voucher_date: date
    amount: Decimal
    days_diff: int
    #: 日期差在**主窗口**内（``≤ PRIMARY_WINDOW_DAYS``）。
    in_primary: bool
    #: 这一对是**用户已确认过的关联**（而不是自动配上的）。
    claimed: bool = False


@dataclass
class Allocation:
    """一次分配的结果。"""

    #: 行 id → 配对。
    pairs: dict[int, Pair] = field(default_factory=dict)
    #: 没有配上分录的行。
    unmatched_rows: list[RowSide] = field(default_factory=list)
    #: 没有配上流水行的分录。
    unmatched_entries: list[EntrySide] = field(default_factory=list)

    def pair_of(self, row_id: int) -> Optional[Pair]:
        return self.pairs.get(row_id)


def _gap(a: date, b: date) -> int:
    return abs((a - b).days)


def allocate(rows: Iterable[RowSide],
             entries: Iterable[EntrySide],
             *,
             primary_window: int = PRIMARY_WINDOW_DAYS,
             review_window: int = REVIEW_WINDOW_DAYS,
             claims: Optional[dict[int, int]] = None,
             rejected: Optional[dict[int, int]] = None,
             ) -> Allocation:
    """把「流水行」与「账上分录」一一对上。

    :param rows: 对账单上的流水行。
    :param entries: 这个账户上的分录（**现金侧**方向）。
    :param primary_window: 主窗口（天）。日期差在这个之内 → ``in_primary``。
    :param review_window: 复核窗口（天）。超出它就**不是同一笔**。
    :param claims: ``{行 id: 凭证 id}`` —— 用户已确认过的关联，**优先兑现**。
    :param rejected: ``{行 id: 凭证 id}`` —— 用户已判定「不是同一笔」的关联，
        分配时跳过（不再拿它去打扰用户）。
    :returns: :class:`Allocation`
    """
    claims = claims or {}
    rejected = rejected or {}

    row_list = list(rows)
    entry_list = list(entries)

    # ---- 分桶：同一笔钱的候选只可能落在同一个 (金额, 现金侧方向) 桶里 ----
    buckets: dict[tuple[str, str], list[EntrySide]] = {}
    for entry in entry_list:
        buckets.setdefault(entry.key, []).append(entry)

    # ---- 候选对：金额相等 + 方向对应 + 日期差在复核窗口内 ----
    candidates: list[tuple[int, RowSide, EntrySide]] = []
    for row in row_list:
        if row.tx_date is None or row.amount is None or row.amount <= 0:
            continue
        if cash_direction(row.direction) is None:
            continue
        rejected_voucher = rejected.get(row.id)
        for entry in buckets.get(row.key, []):
            # **用户否掉过的关联不再来打扰**：判「不是同一笔」之后，
            # 每次重算都还会把它捞出来 —— 用户会以为没生效。
            if rejected_voucher is not None and entry.voucher_id == rejected_voucher:
                continue
            gap = _gap(entry.voucher_date, row.tx_date)
            if gap > review_window:
                continue
            candidates.append((gap, row, entry))

    result = Allocation()
    taken_rows: set[int] = set()
    taken_entries: set[int] = set()

    def _take(gap: int, row: RowSide, entry: EntrySide, claimed: bool) -> None:
        taken_rows.add(row.id)
        taken_entries.add(entry.id)
        result.pairs[row.id] = Pair(
            row_id=row.id,
            entry_id=entry.id,
            voucher_id=entry.voucher_id,
            voucher_no=entry.voucher_no,
            voucher_date=entry.voucher_date,
            amount=entry.amount,
            days_diff=gap,
            in_primary=(gap <= primary_window),
            claimed=claimed,
        )

    # ---- 第一轮：兑现「用户已确认过的关联」 ----
    #
    # **人工确认优先于自动分配**：用户在「可改科目」里指认过哪张凭证，
    # 那就是人的决定，自动分配不许把它顶掉（顶掉了，界面上会从
    # 「已入账」变回「待处理」—— 用户明明刚确认过，看着却像没改成）。
    #
    # **这一轮不看日期窗口**：人工确认过的那笔，日期差多远都是真的
    # （跨期 16~30 天的候选改完之后，那张凭证在主窗口的扫描里根本
    # 看不见，标记当场被收回 —— 用户明明刚确认过，看着却像没改成）。
    #
    # **但一张凭证只被一个行认领**：几行都指着同一张凭证时，按**日期差
    # 最小**的那一行拿到它，其余的行收回标记 —— 它们本来就不该显示
    # 「已入账」（账上只有那一笔）。
    by_voucher: dict[int, list[EntrySide]] = {}
    for entry in entry_list:
        by_voucher.setdefault(entry.voucher_id, []).append(entry)

    def _claim_gap(row: RowSide, entry: EntrySide) -> int:
        if row.tx_date is None:
            return 10 ** 6
        return _gap(entry.voucher_date, row.tx_date)

    claimed_rows = [row for row in row_list if claims.get(row.id)]
    # 先按「到那张凭证的日期差」排序 → 最像的那一行先拿到。
    claimed_rows.sort(key=lambda row: (
        min((_claim_gap(row, entry)
             for entry in by_voucher.get(claims[row.id], [])
             if entry.key == row.key),
            default=10 ** 6),
        row.tx_date or date.min,
        row.id,
    ))
    for row in claimed_rows:
        claim = claims[row.id]
        best: Optional[tuple[int, EntrySide]] = None
        for entry in by_voucher.get(claim, []):
            # **必须是这一行对得上的那一条分录**：科目对（同一个账户）、
            # 方向对、金额对 —— 只是日期可能差得很远。
            if entry.key != row.key:
                continue
            if entry.id in taken_entries:
                continue
            gap = _claim_gap(row, entry)
            if best is None or (gap, entry.voucher_date, entry.id) < (
                    best[0], best[1].voucher_date, best[1].id):
                best = (gap, entry)
        if best is not None:
            _take(best[0], row, best[1], claimed=True)

    # ---- 第二轮：贪心补齐 ----
    #
    # **按日期差从小到大配**：差得少的最像同一笔，先把它们配掉。
    # 剩下的按稳定的次序（行日期、行 id、凭证日期、分录 id）排 ——
    # **同样的输入必须得到同样的结果**，不然同一份对账单两次重算
    # 会配出不同的结果，用户会以为系统在乱来。
    for gap, row, entry in sorted(
            candidates,
            key=lambda item: (item[0], item[1].tx_date or date.min, item[1].id,
                              item[2].voucher_date, item[2].id)):
        if row.id in taken_rows or entry.id in taken_entries:
            continue
        _take(gap, row, entry, claimed=False)

    result.unmatched_rows = [row for row in row_list if row.id not in taken_rows]
    result.unmatched_entries = [entry for entry in entry_list
                                 if entry.id not in taken_entries]
    return result
