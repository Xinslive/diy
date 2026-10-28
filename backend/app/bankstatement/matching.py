"""发票 ↔ 流水 的结算科目匹配。

**这张发票的钱是从哪个账户收/付的？** 发票上不写 —— 发票只写「买了什么、
多少钱」，不写「从哪个银行账户付的」。而对账单知道。所以流程是：

    发票（金额 + 方向） → 流水（金额 + 方向） → 流水所属的现金科目
                          → 这张发票的结算科目

**匹配口径是「金额相等 + 收支方向相反对」。** 不用日期窗口：发票与流水
之间本来就会差几天（票开了几天才付款、货款到了才开票），而**金额相等且
方向一致**这件事本身已经足够稀有。

不唯一时的处理（**一律回落，绝不猜**）：

- **多个账户同额** → 回落配置科目，附一句「多账户同额，需人工指定」；
- **流水已被别的发票占用** → 回落配置科目（那一行只能被一张发票占）；
- **没有命中** → 回落配置科目。

**一行流水只被一张发票占用**，同批次内按页序先到先得 —— 同一笔钱
不可能既是 A 发票的结算又是 B 发票的结算。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.bankstatement.caliber import CASH_SUBJECT_PREFIXES, account_subject_code
from app.models import BankStatement, BankStatementRow, Entry, Period, Subject, Voucher
from app.models.bankstatement import (
    DIRECTION_IN,
    DIRECTION_OUT,
    STATUS_OPEN,
)

#: 发票类型 → 流水方向。销项是「收钱」，找**收入**行；进项是「付钱」，
#: 找**支出**行。
DIRECTION_FOR_INVOICE = {
    "OUTPUT": DIRECTION_IN,
    "INPUT": DIRECTION_OUT,
}


#: 流水 ↔ 账上分录的**主匹配窗口**：凭证日期离流水日期不超过这么多天，
#: 就当是同一笔（改科目时直接给，不必多问一句）。
MATCH_WINDOW_DAYS = 7

#: **跨期提示窗口**：日期差超过主窗口、但不超过这个天数的分录，**照样列出来**
#: 作为候选，只是标出日期差、让用户人工确认是不是同一笔。
#:
#: 为什么要有这一层：银行流水与会计记账的日期**本来就会差** —— 跨月入账、
#: 月末才记账、票到才付款，都是常事。按 7 天一刀切，等于把「这笔钱记错在
#: 1001 上」的那类候选整个藏起来，用户在界面上永远看不到「可改科目」，
#: 于是又去制一张新凭证 —— 同一笔钱记两遍。
#:
#: 为什么是 30 天：跨月（28 天左右）是最常见的那种差，再往上就明显不是
#: 同一笔了 —— 生产实测：同额同向的候选在 ±7 天 0 条、±30 天 3 条、
#: ±60 天 4 条、±90 天 6 条（73 天、87 天那两条几乎肯定是不同的钱）。
#: 窗口越大噪音越多，而「同额」在现实里一点都不稀有。
#:
#: **这是口径，不是事实** —— 改这个值会改变界面上出现多少候选。
EXTENDED_WINDOW_DAYS = 30


def direction_for_invoice(invoice_type: str) -> Optional[str]:
    return DIRECTION_FOR_INVOICE.get(invoice_type)


def candidates_for(db: Session, book_id: int, amount: Decimal,
                   direction: str) -> list[dict[str, Any]]:
    """金额与方向都对得上的流水行。

    只有仍为 ``OPEN`` 且没有被发票占用的行才在这里 —— 已入账的流水不能
    因为旧的预览候选再次被认领。
    """
    if direction not in (DIRECTION_IN, DIRECTION_OUT):
        return []
    rows = db.execute(
        select(BankStatementRow, BankStatement, Subject)
        .join(BankStatement, BankStatement.id == BankStatementRow.statement_id)
        # **左连接**：那一年的科目行没了也要把候选列出来 ——
        # 那时「这份流水属于哪个账户」只剩快照里那一份线索。
        .outerjoin(Subject, Subject.id == BankStatement.account_subject_id)
        .where(
            BankStatementRow.book_id == book_id,
            BankStatementRow.direction == direction,
            BankStatementRow.amount == amount,
            BankStatementRow.status == STATUS_OPEN,
            BankStatementRow.matched_invoice_record_id.is_(None),
        )
        .order_by(BankStatementRow.tx_date, BankStatementRow.id)
    ).all()
    return [{
        "row_id": row.id,
        "seq": row.seq,
        "tx_date": row.tx_date.isoformat() if row.tx_date else "",
        "summary": row.summary,
        "counterparty_name": row.counterparty_name,
        "statement_id": statement.id,
        "filename": statement.filename,
        "account_subject_id": statement.account_subject_id,
        # **活科目优先，快照兜底**（理由见 caliber.account_subject_code）。
        "account_subject_code": account_subject_code(subject, statement),
        "account_name": statement.account_name or statement.account_no,
        "bank_name": statement.bank_name,
    } for row, statement, subject in rows]


def pick(candidates: Iterable[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], str]:
    """从候选里挑一个，**挑不出来就说清楚为什么**。

    返回 ``(选中的那条, 提示语)``。提示语为空表示干净命中。

    判据是**账户**而不是行：同一个账户下有两条同额流水时，结算科目
    （哪个账户）是确定的，具体记哪一条不影响科目 —— 所以这不算歧义。
    真正的歧义是「两个不同账户各有一笔同额的钱」，那时候必须人来定。
    """
    items = list(candidates)
    if not items:
        return None, ""
    accounts = {item["account_subject_id"] for item in items}
    if len(accounts) > 1:
        names = "、".join(sorted({
            item.get("account_subject_code") or item.get("account_name") or ""
            for item in items
        } - {""}))
        return None, f"多账户同额（{names}），需人工指定"
    return items[0], ""


def release_claims(db: Session, record_ids: Iterable[int]) -> None:
    """把某些发票记录占用的流水**放掉**。

    什么时候要放：重算预览之前。用户在预览里改了发票的金额或类型之后，
    原来占着的那条流水应当重新变成「没人用」——
    否则改完之后那笔流水会一直显示「已被发票占用」，而真正该占它的
    发票（金额改过的那张）反而匹配不上。

    **已提交的发票不放**：它们的凭证已经在账上，放掉会让同一条流水
    再被一张新发票认领一次。
    """
    ids = [int(i) for i in record_ids if i]
    if not ids:
        return
    db.execute(
        update(BankStatementRow)
        .where(BankStatementRow.matched_invoice_record_id.in_(ids))
        .values(matched_invoice_record_id=None)
    )


def claim(db: Session, row_id: int, record_id: int) -> bool:
    """把一条仍开放的流水标记为「被这张发票占用」。

    ``candidates_for`` 与真正写入之间可能夹着一次制证请求，所以这里必须
    再做一次条件更新：如果流水已经入账或被别的发票占用，认领失败而不是
    把旧候选重新写成「属于这张发票」。
    """
    result = db.execute(
        update(BankStatementRow)
        .where(
            BankStatementRow.id == row_id,
            BankStatementRow.status == STATUS_OPEN,
            BankStatementRow.matched_invoice_record_id.is_(None),
        )
        .values(matched_invoice_record_id=int(record_id))
    )
    return result.rowcount == 1


# ----------------------------------------------------------------------
# 流水 ↔ 账上已有的现金分录（改科目用）
# ----------------------------------------------------------------------

def _cash_entries(db: Session, book_id: int, lo: date, hi: date) -> list[Any]:
    """现金科目上的分录（连同凭证与期间的信息），一次读完。

    **只取现金科目**：改科目的意思是「把记在 1001 上的那笔挪到 1002」，
    而记在费用科目上的分录跟银行流水没有「同一笔钱」这层关系 ——
    那个方向是**对方科目**的事，流水这一侧判不了。
    """
    return db.execute(
        select(
            Entry.id, Entry.voucher_id, Entry.subject_id, Entry.direction,
            Entry.amount, Entry.summary, Entry.line_no,
            Voucher.voucher_no, Voucher.voucher_date, Voucher.status,
            Subject.code, Subject.name,
            Period.year, Period.month, Period.locked,
        )
        .join(Voucher, Voucher.id == Entry.voucher_id)
        .join(Subject, Subject.id == Entry.subject_id)
        .join(Period, Period.id == Voucher.period_id)
        .where(
            Voucher.book_id == book_id,
            Entry.book_id == book_id,
            or_(*[Subject.code.startswith(prefix)
                  for prefix in CASH_SUBJECT_PREFIXES]),
            Voucher.voucher_date >= lo,
            Voucher.voucher_date <= hi,
        )
    ).all()


def reclassify_candidates(db: Session, book_id: int, statement: BankStatement,
                          rows: Iterable[BankStatementRow],
                          window_days: int = MATCH_WINDOW_DAYS,
                          extended_window_days: int = EXTENDED_WINDOW_DAYS,
                          ) -> dict[int, list[dict[str, Any]]]:
    """一批流水行 → 每行的「账上可能对应的分录」。

    返回 ``{行 id: [候选...]}``。没有候选的行不在返回里。

    **判据四件套（与「已入账」那套同源，但对象不同）**：同一个现金科目族
    （1001/1002/1012）、金额相等、**现金侧方向对应**、凭证日期在流水日
    ±N 天内。

    与 ``_voucher_matches`` 的区别只有一处，但那一处是关键：

    - ``_voucher_matches`` 找的是**这个账户上**的分录 —— 用来判断
      「这笔已经入过账了，别再制一张」；
    - 这里找的是**别的现金科目上**的分录（1001，而这份对账单绑的是 1002）
      —— 用来判断「账上已经有一笔记在 1001 上的钱，跟这一行是同一笔，
      该把它挪过来，而不是再造一张凭证」。

    **同一个账户上的不算候选**：那种情况由「已入账」那条路处理，
    界面上它显示的是「已入账」，而不是「可以改科目」。

    **跨期的候选要列出来，但标出来让人工确认。** 见 ``EXTENDED_WINDOW_DAYS``。
    """
    dated = [row for row in rows
             if row.tx_date is not None and row.amount is not None
             and row.amount > 0 and row.direction in (DIRECTION_IN, DIRECTION_OUT)]
    if not dated:
        return {}
    lo = min(row.tx_date for row in dated) - timedelta(days=extended_window_days)
    hi = max(row.tx_date for row in dated) + timedelta(days=extended_window_days)
    entries = _cash_entries(db, book_id, lo, hi)

    # 按「金额 + 现金侧方向」分桶：一次查询出全部候选，逐行在桶里取。
    buckets: dict[tuple[str, str], list[Any]] = {}
    for entry in entries:
        buckets.setdefault(
            (str(Decimal(str(entry.amount))), entry.direction), []).append(entry)

    result: dict[int, list[dict[str, Any]]] = {}
    for row in dated:
        # **银行账的方向与会计凭证相反**：流水 IN（收钱）= 现金增加 = 借方。
        entry_direction = "DEBIT" if row.direction == DIRECTION_IN else "CREDIT"
        items: list[dict[str, Any]] = []
        for entry in buckets.get((str(Decimal(str(row.amount))), entry_direction), []):
            if entry.subject_id == statement.account_subject_id:
                continue
            days_diff = abs((entry.voucher_date - row.tx_date).days)
            if days_diff > extended_window_days:
                continue
            items.append({
                "entry_id": int(entry.id),
                "voucher_id": int(entry.voucher_id),
                "voucher_no": entry.voucher_no,
                "voucher_date": entry.voucher_date.isoformat(),
                "voucher_status": entry.status,
                "line_no": int(entry.line_no),
                "subject_id": int(entry.subject_id),
                "subject_code": entry.code,
                "subject_name": entry.name,
                "amount": str(entry.amount),
                "direction": entry.direction,
                "summary": entry.summary or "",
                # ``Period.code`` 是**属性**不是列（由 year/month 拼出来），
                # 所以这里自己拼一份，格式与它逐字一致。
                "period_code": f"{entry.year}-{entry.month:02d}",
                "locked": bool(entry.locked),
                # **跨期提示**：日期差多少天，以及它算不算「就在这个窗口里」。
                # 窗口外的候选照样列出来，但界面上要让人工确认是不是同一笔 ——
                # 跨月入账（9 月的流水、8 月底记的账）是常事，按 7 天一刀切
                # 就等于把「记错在 1001 上」的那种钱藏起来，用户永远找不到。
                "days_diff": days_diff,
                "in_window": days_diff <= window_days,
            })
        if items:
            # 窗口内的排前面；窗口外的按「差得少」排 —— 最像的那笔在最上面。
            items.sort(key=lambda item: (not item["in_window"], item["days_diff"],
                                         item["voucher_date"], item["voucher_id"]))
            result[row.id] = items
    return result


def reclassify_block_reason(entry: dict[str, Any]) -> str:
    """这条候选为什么改不了（能改就是空串）。

    **已记账的凭证动不得** —— 库层护栏 ``fn_entry_lock_guard`` 挡着，
    应用层 ``update_voucher`` 也挡着。已结账的期间同理。
    """
    if entry.get("voucher_status") == "POSTED":
        return "凭证已记账，不能改（先到「期末结账」反结账）"
    if entry.get("locked"):
        return f"{entry.get('period_code') or ''} 已结账，不能改".strip()
    return ""
