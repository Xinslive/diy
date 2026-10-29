"""流水 ↔ 账上分录的**一对一分配**：这个模块自己的规矩。

**这一份不碰数据库** —— 它只对已经在内存里的两侧做分配，回答一个问题：
「谁跟谁是一笔」。所以这些用例全是纯数据，测的正是规则本身。

**为什么「一对一」是硬要求**（2026-10-29 用户报的问题）：对账单上四笔金额
相同的支出，账上只记了一笔 —— 界面上却四条都显示「已入账」。那三笔是
**账上根本没记**的钱，于是「生成流水凭证」跳过它们，这三笔钱永远进不了账。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.bankstatement.allocate import (
    PRIMARY_WINDOW_DAYS,
    REVIEW_WINDOW_DAYS,
    EntrySide,
    RowSide,
    allocate,
    amount_key,
    cash_direction,
)

D = Decimal

#: 基准日 —— 用「基准日 ± N 天」而不是「某月某日 + N」，
#: 后者跨月就炸（9 月 20 日 + 31 天 = 10 月 51 日）。
BASE = date(2026, 9, 20)


def _row(rid: int, offset: int = 0, amount: str = "100.00",
         direction: str = "IN") -> RowSide:
    return RowSide(id=rid, tx_date=BASE + timedelta(days=offset), amount=D(amount),
                   direction=direction, seq=rid)


def _entry(eid: int, offset: int = 0, amount: str = "100.00",
           direction: str = "DEBIT", voucher_id: int = 100,
           voucher_no: str = "记0001") -> EntrySide:
    return EntrySide(id=eid, voucher_id=voucher_id, voucher_no=voucher_no,
                     voucher_date=BASE + timedelta(days=offset), amount=D(amount),
                     direction=direction)


# ----------------------------------------------------------------------
# 方向与金额
# ----------------------------------------------------------------------

def test_银行方向与会计方向相反():
    """流水 IN（收钱）= 现金增加 = 借方；OUT（付钱）= 贷方。"""
    assert cash_direction("IN") == "DEBIT"
    assert cash_direction("OUT") == "CREDIT"
    assert cash_direction("UNKNOWN") is None


def test_金额按两位小数归一():
    """``"3984.0"`` 与 ``"3984.00"`` 是同一笔钱 —— 不归一就会漏配。"""
    assert amount_key("3984.0") == amount_key("3984.00")
    assert amount_key(D("3984.00")) == "3984.00"
    assert amount_key(None) == ""


# ----------------------------------------------------------------------
# 一对一
# ----------------------------------------------------------------------

def test_四行同额一条分录_只配上一行():
    """**用户报的问题**：账上一笔，流水四笔同额 —— 只能配上一行。"""
    rows = [_row(1, 0, "500.00", "OUT"), _row(2, 0, "500.00", "OUT"),
            _row(3, 0, "500.00", "OUT"), _row(4, 0, "500.00", "OUT")]
    entries = [_entry(10, 20, "500.00", "CREDIT")]
    result = allocate(rows, entries)
    assert len(result.pairs) == 1, result.pairs
    assert len(result.unmatched_rows) == 3, "另外三行没配上是**没入账**，不是「已入账」"
    assert len(result.unmatched_entries) == 0


def test_一条分录配不上一行_那一行进未配上():
    """账上四笔、流水一行 —— 另外三笔是错账（存疑那一页要报出来）。"""
    rows = [_row(1, 20, "500.00", "OUT")]
    entries = [_entry(10, 20, "500.00", "CREDIT"),
               _entry(11, 20, "500.00", "CREDIT"),
               _entry(12, 20, "500.00", "CREDIT"),
               _entry(13, 20, "500.00", "CREDIT")]
    result = allocate(rows, entries)
    assert len(result.pairs) == 1
    assert len(result.unmatched_entries) == 3, (
        "账上多出来的三笔没报出来 —— 它们是错账")


def test_两行两笔_各配各的():
    rows = [_row(1, -10, "100.00"), _row(2, 0, "200.00")]
    entries = [_entry(10, -10, "100.00"), _entry(11, 0, "200.00")]
    result = allocate(rows, entries)
    assert len(result.pairs) == 2
    assert result.pairs[1].entry_id == 10
    assert result.pairs[2].entry_id == 11


def test_一张凭证两条相同分录_能配两行():
    """**分配的单位是「分录」不是「凭证」** —— 拆分凭证上有两条一样的分录，
    它们各自对应一条流水。按凭证分配就会把「两条流水」算成没配上。"""
    rows = [_row(1, -10, "100.00"), _row(2, -10, "100.00")]
    entries = [_entry(10, -10, "100.00", voucher_id=7),
               _entry(11, -10, "100.00", voucher_id=7)]
    result = allocate(rows, entries)
    assert len(result.pairs) == 2, "同一张凭证上的两条分录各自配一行"


# ----------------------------------------------------------------------
# 窗口
# ----------------------------------------------------------------------

def test_主窗口内_算已入账():
    rows = [_row(1, 0, "100.00")]
    entries = [_entry(10, PRIMARY_WINDOW_DAYS, "100.00")]
    result = allocate(rows, entries)
    assert result.pairs[1].in_primary is True


def test_主窗口外复核窗口内_不自动判已入账():
    """**用户报的问题**：日期差 10 天 —— 金额对得上，但不许自动判「已入账」。"""
    rows = [_row(1, 0, "100.00")]
    entries = [_entry(10, PRIMARY_WINDOW_DAYS + 3, "100.00")]
    result = allocate(rows, entries)
    assert result.pairs[1].in_primary is False
    assert result.pairs[1].days_diff == PRIMARY_WINDOW_DAYS + 3


def test_复核窗口外_不配对():
    """差得太远就是**另一笔钱**，不该报出来让人复核。"""
    rows = [_row(1, 0, "100.00")]
    entries = [_entry(10, REVIEW_WINDOW_DAYS + 1, "100.00")]
    result = allocate(rows, entries)
    assert not result.pairs
    assert len(result.unmatched_rows) == 1


# ----------------------------------------------------------------------
# 方向
# ----------------------------------------------------------------------

def test_方向对不上_不配对():
    """收入流水配借方分录；支出流水配贷方分录。反了就是钱记到反方向。"""
    rows = [_row(1, 20, "100.00", "OUT")]
    entries = [_entry(10, 0, "100.00", "DEBIT")]
    result = allocate(rows, entries)
    assert not result.pairs, "支出流水配上了借方分录 —— 方向反了"


# ----------------------------------------------------------------------
# 已确认的关联
# ----------------------------------------------------------------------

def test_已确认的关联优先兑现():
    """用户人工指认过的那张凭证，先给它 —— 启发式不许把它顶掉。"""
    rows = [_row(1, 0, "100.00")]
    entries = [_entry(10, 0, "100.00", voucher_id=7),
               _entry(11, 0, "100.00", voucher_id=8)]
    result = allocate(rows, entries, claims={1: 8})
    assert result.pairs[1].voucher_id == 8
    assert result.pairs[1].claimed is True


def test_已确认的关联不看日期窗口():
    """跨期 20 天的候选改完之后，那张凭证在主窗口里根本看不见 ——
    标记当场被收回，用户明明刚确认过却看着像没改成。"""
    rows = [_row(1, 0, "100.00")]
    entries = [_entry(10, 20, "100.00", voucher_id=7)]
    result = allocate(rows, entries, claims={1: 7})
    assert result.pairs[1].voucher_id == 7
    assert result.pairs[1].claimed is True


def test_几行都指着同一张凭证_只认最近的那一行():
    """**一张凭证只被一个行认领** —— 剩下的行本来就不该显示「已入账」。"""
    rows = [_row(1, -10, "500.00"), _row(2, 0, "500.00")]
    entries = [_entry(10, 0, "500.00", voucher_id=7)]
    result = allocate(rows, entries, claims={1: 7, 2: 7})
    assert len(result.pairs) == 1
    assert result.pairs[2].voucher_id == 7, "日期差最小的那一行才该拿到"


def test_否掉过的关联不再来打扰():
    """用户判过「不是同一笔」之后，重算不该又把它捞出来。"""
    rows = [_row(1, 0, "100.00")]
    entries = [_entry(10, 10, "100.00", voucher_id=7)]
    result = allocate(rows, entries, rejected={1: 7})
    assert not result.pairs


# ----------------------------------------------------------------------
# 确定性
# ----------------------------------------------------------------------

def test_同样的输入给出同样的结果():
    """**两次重算必须配出同一个结果** —— 不然用户会以为系统在乱来。"""
    rows = [_row(3, 0, "500.00"), _row(1, 0, "500.00"), _row(2, 1, "500.00")]
    entries = [_entry(11, 1, "500.00"), _entry(10, 0, "500.00")]
    first = allocate(rows, entries)
    second = allocate(rows, entries)
    assert {rid: p.entry_id for rid, p in first.pairs.items()} == \
           {rid: p.entry_id for rid, p in second.pairs.items()}
    # **日期差小的优先**：第 1 行（20 日）配 20 日那条，第 2 行配 21 日那条。
    assert first.pairs[1].entry_id == 10
    assert first.pairs[2].entry_id == 11


def test_缺日期缺金额方向未知的行不参与配对():
    rows = [RowSide(id=1, tx_date=None, amount=D("100.00"), direction="IN"),
            RowSide(id=2, tx_date=date(2026, 9, 20), amount=None, direction="IN"),
            RowSide(id=3, tx_date=date(2026, 9, 20), amount=D("100.00"),
                    direction="UNKNOWN")]
    entries = [_entry(10, 0, "100.00")]
    result = allocate(rows, entries)
    assert not result.pairs
