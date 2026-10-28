"""存疑凭证：账上有、对账单里找不到对应流水的那些凭证。

**这一页是流水对账的另一半。**

流水明细页那条路是「对账单上有、账上没有」→ 生成凭证。
这一页是反过来的：「账上有、对账单上没有」→ 那是**错账** ——
钱记在了不该记的地方，或者根本不该记。

判据（用户定的，2026-10-28）：

    该账户**在这份对账单覆盖期间内**的现金分录，在这份对账单的流水行里
    找不到「金额相等 + 方向对应 + 日期在窗口内」的那一行。

**为什么限定在期间内。** 期间外（上一期/下一期）的分录不在这份对账单的
覆盖范围里 —— 用户没传那个月的对账单，说明不了什么。把它们算成「疑问」
只会把清单淹掉。生产实测：账套只传了 2026-09 的对账单，若不限定期间，
十七张「疑问凭证」里有十二张是八月的账 —— 那些不是错账，是**没传对账单**。

**为什么只查绑了对账单的账户。** 没有对账单就没有「对不对得上」这件事。
1001 库存现金如果没绑对账单，它上面的分录无从判定 —— 那不叫疑问，
那叫「没资料」。

**为什么内部转账不算疑问。** 钱在自家账户之间挪（1001 现金 ↔ 1002 银行）：
银行流水里本来就没有这一行 —— 它不是错账，只是没走银行。

**只查现金科目。** 记在费用科目上的分录跟银行流水没有「同一笔钱」这层
关系 —— 那个方向是**对方科目**的事，流水这一侧判不了（与
:mod:`app.bankstatement.matching` 里 ``_cash_entries`` 同一个口径）。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.organize import CASH_SUBJECT_PREFIXES
from app.models import BankStatement, BankStatementRow, Entry, Subject, Voucher
from app.models.bankstatement import DIRECTION_IN, DIRECTION_OUT

#: 匹配窗口（天）。**银行账与会计记账的日期本来就会差** —— 月末才记账、
#: 票到才付款，都是常事。按一天不差去卡，等于把「记错日期」的那类
#: 错账整个藏起来。
#:
#: **窗口不小于对账单期间本身**：期间内的分录与期间内的流水行，两者的
#: 日期差最坏也就差一个期间那么长。期间比 30 天长的对账单（有些银行一
#: 次给三个月），按 30 天卡会把「期初那一笔」判成对不上。
MATCH_WINDOW_DAYS = 30

#: **邻期窗口**：期间之外这么多天以内的分录，本页**不判定**、但要报个数 ——
#: 用户看得见「那几个月还有 N 笔没说法」，而不是以为它们不存在。
NEAR_PERIOD_DAYS = 30

#: 凭证状态：已记账与未记账的凭证都算数（用户往往先录凭证再对账）。
VOUCHER_STATUSES = ("POSTED", "UNPOSTED")


def _amount_key(value: Any) -> str:
    """金额的匹配键。

    **统一成两位小数的字符串再比。** 同一笔钱在流水行与分录两处的
    字符串化结果可能不一样（"3984.0" 与 "3984.00"），直接比字符串会漏配
    —— 漏配的代价是凭空多出一张「存疑凭证」。
    """
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def _cash_direction(row_direction: str) -> str:
    """流水方向 → 现金侧方向。

    **银行账与会计凭证的方向是相反的**：流水 IN（收钱）= 现金增加 = 借方。
    """
    return "DEBIT" if row_direction == DIRECTION_IN else "CREDIT"


def _rows_of(db: Session, statement_id: int) -> list[BankStatementRow]:
    return list(db.scalars(
        select(BankStatementRow)
        .where(BankStatementRow.statement_id == statement_id)
        .order_by(BankStatementRow.seq)).all())


def _index_rows(rows: list[BankStatementRow]
                ) -> dict[tuple[str, str], list[BankStatementRow]]:
    """流水行按「(金额, 现金侧方向)」分桶。

    **方向未知、金额为零、没有日期的行不进桶** —— 它们没法与分录配对。
    """
    buckets: dict[tuple[str, str], list[BankStatementRow]] = {}
    for row in rows:
        if row.tx_date is None or row.amount is None or row.amount <= 0:
            continue
        if row.direction not in (DIRECTION_IN, DIRECTION_OUT):
            continue
        buckets.setdefault(
            (_amount_key(row.amount), _cash_direction(row.direction)),
            []).append(row)
    return buckets


def _entries_on_account(db: Session, book_id: int, subject_id: int,
                        lo: date, hi: date) -> list[tuple[Entry, Voucher]]:
    """某个现金科目上、凭证日期落在 [lo, hi] 里的分录（连同凭证）。"""
    return list(db.execute(
        select(Entry, Voucher)
        .join(Voucher, Voucher.id == Entry.voucher_id)
        .where(
            Voucher.book_id == book_id,
            Entry.subject_id == subject_id,
            Voucher.voucher_date >= lo,
            Voucher.voucher_date <= hi,
            Voucher.status.in_(VOUCHER_STATUSES),
        )
    ).all())


def _internal_transfer_vouchers(db: Session, voucher_ids: set[int]) -> set[int]:
    """哪些凭证是**内部转账**（现金侧不止一个科目）。

    钱在自家账户之间挪（1001 现金 ↔ 1002 银行）：银行流水里本来就没有
    这一行 —— 它不是错账，只是没走银行。

    **一次查完，不一条一条查** —— 候选可能有几十张，逐条查就是 N+1。
    """
    if not voucher_ids:
        return set()
    rows = db.execute(
        select(Entry.voucher_id, Subject.code)
        .join(Subject, Subject.id == Entry.subject_id)
        .where(Entry.voucher_id.in_(voucher_ids))
    ).all()
    cash_by_voucher: dict[int, set[int]] = {}
    codes: dict[int, str] = {}
    for voucher_id, code in rows:
        if code.startswith(CASH_SUBJECT_PREFIXES):
            cash_by_voucher.setdefault(voucher_id, set()).add(code)
    return {vid for vid, codes_set in cash_by_voucher.items()
            if len(codes_set) >= 2}


def _clues(rows: list[BankStatementRow], entry: Entry,
           voucher_date: date) -> list[dict[str, Any]]:
    """给这条存疑分录找**线索** —— 它最可能错在哪。

    **没有线索的「疑问」是没用的疑问。** 用户看到「找不到对应流水」不知道
    该去改什么；看到「金额对得上，但流水日期差 40 天」就知道该看日期。

    两种线索：

    * ``AMOUNT`` —— **金额对得上、日期对不上**（最常见：记账晚了）；
    * ``DATE`` —— **日期附近有流水、金额对不上**（金额记错了）。
    """
    out: list[dict[str, Any]] = []
    key = (_amount_key(entry.amount), entry.direction)
    same_amount = [r for r in rows
                   if r.tx_date is not None
                   and _amount_key(r.amount) == key[0]
                   and _cash_direction(r.direction) == key[1]]
    if same_amount:
        best = min(same_amount,
                   key=lambda r: abs((r.tx_date - voucher_date).days))
        out.append({
            "kind": "AMOUNT",
            "seq": best.seq,
            "tx_date": best.tx_date.isoformat(),
            "amount": str(best.amount),
            "direction": best.direction,
            "days_diff": abs((best.tx_date - voucher_date).days),
            "summary": best.summary or "",
            "counterparty_name": best.counterparty_name or "",
        })
    # 同日期附近、**金额不同**的流水 —— 说了「这天有一笔，但不是这笔」。
    near = [r for r in rows
            if r.tx_date is not None
            and _amount_key(r.amount) != key[0]
            and abs((r.tx_date - voucher_date).days) <= 3]
    if near:
        best = min(near, key=lambda r: abs((r.tx_date - voucher_date).days))
        out.append({
            "kind": "DATE",
            "seq": best.seq,
            "tx_date": best.tx_date.isoformat(),
            "amount": str(best.amount),
            "direction": best.direction,
            "days_diff": abs((best.tx_date - voucher_date).days),
            "summary": best.summary or "",
            "counterparty_name": best.counterparty_name or "",
        })
    return out


def _reason(clues: list[dict[str, Any]]) -> str:
    """一句话说清「为什么可疑」—— 页面上直接显示这句。"""
    by_kind = {c["kind"]: c for c in clues}
    if "AMOUNT" in by_kind:
        c = by_kind["AMOUNT"]
        return (f"金额对得上，但流水那笔在 {c['tx_date']}，"
                f"差 {c['days_diff']} 天")
    if "DATE" in by_kind:
        c = by_kind["DATE"]
        return (f"{c['tx_date']} 前后有一笔流水，金额是 {c['amount']}，"
                f"对不上")
    return "这份对账单的流水里没有同金额、也没有同日期附近的记录"


def _one_statement(db: Session, book_id: int,
                   statement: BankStatement) -> dict[str, Any]:
    """一份对账单 → 它那个账户上的存疑凭证。"""
    rows = _rows_of(db, statement.id)
    buckets = _index_rows(rows)
    account = db.get(Subject, statement.account_subject_id)

    # **没有期间就没有判定的那把尺子** —— 期间可空（理论上），这时候
    # 一律判「不判定」，而不是拿一个空区间去比（那会把整本账都算成疑问）。
    if statement.period_start is None or statement.period_end is None:
        return {
            "statement_id": statement.id,
            "account_subject_id": statement.account_subject_id,
            "account_code": (account.code if account else ""),
            "account_name": (account.name if account else ""),
            "period_start": "",
            "period_end": "",
            "period_text": "",
            "row_count": len(rows),
            "entry_count": 0,
            "matched_count": 0,
            "suspicious_count": 0,
            "internal_transfer_count": 0,
            "near_period_uncounted": 0,
            "vouchers": [],
        }

    period_days = (statement.period_end - statement.period_start).days
    window = max(MATCH_WINDOW_DAYS, period_days)

    entries = _entries_on_account(
        db, book_id, statement.account_subject_id,
        statement.period_start, statement.period_end)

    matched = 0
    candidates: list[tuple[Entry, Voucher]] = []
    for entry, voucher in entries:
        key = (_amount_key(entry.amount), entry.direction)
        hit = None
        for row in buckets.get(key, []):
            if abs((row.tx_date - voucher.voucher_date).days) <= window:
                hit = row
                break
        if hit is not None:
            matched += 1
        else:
            candidates.append((entry, voucher))

    internal_ids = _internal_transfer_vouchers(
        db, {voucher.id for _, voucher in candidates})
    internal = [pair for pair in candidates if pair[1].id in internal_ids]
    real = [pair for pair in candidates if pair[1].id not in internal_ids]

    # 邻期（期间之外、但离得不远）的分录：**不判定**，只报个数 ——
    # 用户看得见「那几个月还有几笔没说法」，而不是以为它们不存在。
    near_uncounted = 0
    if statement.period_start and statement.period_end:
        lo = statement.period_start - timedelta(days=NEAR_PERIOD_DAYS)
        hi = statement.period_end + timedelta(days=NEAR_PERIOD_DAYS)
        near = _entries_on_account(db, book_id, statement.account_subject_id,
                                   lo, hi)
        for entry, voucher in near:
            if statement.period_start <= voucher.voucher_date <= statement.period_end:
                continue
            key = (_amount_key(entry.amount), entry.direction)
            if any(abs((row.tx_date - voucher.voucher_date).days) <= window
                   for row in buckets.get(key, [])):
                continue
            near_uncounted += 1

    vouchers: list[dict[str, Any]] = []
    for entry, voucher in sorted(real, key=lambda p: (p[1].voucher_date,
                                                       p[1].voucher_no)):
        clues = _clues(rows, entry, voucher.voucher_date)
        vouchers.append({
            "voucher_id": voucher.id,
            "voucher_no": voucher.voucher_no,
            "voucher_date": voucher.voucher_date.isoformat(),
            "voucher_status": voucher.status,
            "entry_id": entry.id,
            "line_no": entry.line_no,
            "subject_id": entry.subject_id,
            "amount": str(entry.amount),
            "direction": entry.direction,
            "summary": entry.summary or "",
            "reason": _reason(clues),
            "clues": clues,
        })

    return {
        "statement_id": statement.id,
        "account_subject_id": statement.account_subject_id,
        "account_code": (account.code if account else ""),
        "account_name": (account.name if account else ""),
        "period_start": statement.period_start.isoformat() if statement.period_start else "",
        "period_end": statement.period_end.isoformat() if statement.period_end else "",
        "period_text": (f"{statement.period_start} ~ {statement.period_end}"
                        if statement.period_start and statement.period_end else ""),
        "row_count": len(rows),
        "entry_count": len(entries),
        "matched_count": matched,
        "suspicious_count": len(vouchers),
        "internal_transfer_count": len(internal),
        "near_period_uncounted": near_uncounted,
        "vouchers": vouchers,
    }


def suspicious_vouchers(db: Session, book_id: int) -> dict[str, Any]:
    """账套里所有「存疑凭证」。

    返回按**对账单**分组的结构 —— 一份对账单 = 一个账户 + 一段期间，
    期间就是判定的那把尺子，所以按它分组才说得清「这一笔凭什么可疑」。
    """
    statements = db.scalars(
        select(BankStatement)
        .where(BankStatement.book_id == book_id)
        .order_by(BankStatement.id)).all()
    groups = [_one_statement(db, book_id, st) for st in statements]
    return {
        "groups": groups,
        "total_suspicious": sum(g["suspicious_count"] for g in groups),
        "total_internal_transfer": sum(g["internal_transfer_count"] for g in groups),
        "total_near_period_uncounted": sum(g["near_period_uncounted"] for g in groups),
    }
