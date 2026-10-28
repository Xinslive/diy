"""补回被误收回的「已入账」标记。

**背景**：2026-10-28 修的缺陷 —— 用户点「可改科目」把现金侧搬到账户上之后，
``plan_vouchers`` 的去重扫描（±7 天）扫不到那张凭证（跨期 16~30 天的候选
本来就是靠用户人工确认才敢用的），于是把「已入账」标记收了回去。行状态从
「已入账」退回「待确认科目」。

**而且退不回去**：凭证已经在账户上了，候选清单里不会再出现它（「可改科目」
的候选只找**别的**现金科目上的分录），用户只剩手动指定对方科目再制一张凭证
这条路 —— 同一笔钱记两遍。生产上就是这样：statement 1 的 seq=26
（2026-09-24，2400.00 支出，凭证 记0001 在 2026-09-08，相差 16 天）。

**这个脚本按审计日志把标记补回去。**

为什么用审计日志，而不是「扫一遍同额同向的分录」：同额在现实里不稀有 ——
生产实测有一行 10.00 的支出，账上能扫出 **49 张**同额凭证。扫出来的那个
「最像的」是巧合，不是事实；而审计日志记的是**用户真的动过手**：凭证的某条
分录的科目从 A 改成了 B。这就是事实。

**判据四件套**（全都要满足才动）：

1. 审计日志里有一条 ``voucher`` / ``UPDATE``，前后快照的同一行分录**科目
   变了**：变之前是现金科目（1001 / 1002 / 1012），变之后是某个对账单绑定的
   账户；
2. 那条分录的金额、方向与某一行流水**完全一致**（改科目不该动金额与方向，
   动了就不认）；
3. 凭证日期与流水日期相差不超过 ``EXTENDED_WINDOW_DAYS``（30 天）——
   这正是界面上「可改科目」候选窗口的宽度，用户能选到的也就这个范围；
4. 这样的流水行**有且只有一行**（多行命中就是说不清，不动，报告里列出来）。

**默认只报告不改数据。** 确认无误后再加 ``--apply``。

跑法::

    # 先看（不改数据）
    backend/.venv/bin/python tools/repair_reclassified_marks.py

    # 确认无误后再写
    backend/.venv/bin/python tools/repair_reclassified_marks.py --apply

**跑在生产机上时**要连到生产的库（在 ``backend/`` 目录下用生产自己的
``.venv`` 跑，配置从生产的环境变量来）::

    cd /www/wwwroot/acct.xins.live/backend
    .venv/bin/python ../../../tools/repair_reclassified_marks.py

**别加 ``--dry-run``。** 这个参数**不存在** —— 默认就是「只报告不改数据」，
加上去 argparse 直接退出码 2（unrecognized arguments）。要写库才加 ``--apply``。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from sqlalchemy import select  # noqa: E402

from app.bankstatement.matching import EXTENDED_WINDOW_DAYS  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models import AuditLog, Subject, Voucher  # noqa: E402
from app.models.bankstatement import (  # noqa: E402
    DIRECTION_IN,
    DIRECTION_OUT,
    STATUS_IGNORED,
    STATUS_POSTED,
    BankStatement,
    BankStatementRow,
)

#: 现金类科目的编码前缀（与服务层 CASH_SUBJECT_PREFIXES 一致）。
CASH_PREFIXES = ("1001", "1002", "1012")


def _is_cash(subject: Subject | None) -> bool:
    return bool(subject) and any(subject.code.startswith(p) for p in CASH_PREFIXES)


def _entry_direction(row_direction: str) -> str:
    """银行账的方向与会计凭证相反：流水 IN（收钱）= 现金增加 = 借方。"""
    return "DEBIT" if row_direction == DIRECTION_IN else "CREDIT"


def _snapshot_entries(snapshot: object) -> dict[int, dict]:
    if not isinstance(snapshot, dict):
        return {}
    out: dict[int, dict] = {}
    for item in snapshot.get("entries") or []:
        if not isinstance(item, dict) or "line_no" not in item:
            continue
        try:
            out[int(item["line_no"])] = item
        except (TypeError, ValueError):
            continue
    return out


def find_moved_cash_sides(db, book_id: int | None = None) -> list[dict]:
    """审计日志里「现金侧被搬到某个账户上」的事实清单。

    返回 ``[{"voucher_id", "account_subject_id", "amount", "direction",
    "voucher_date", "audit_id", "from_code", "to_code"}, ...]``。
    """
    stmt = select(AuditLog).where(
        AuditLog.entity == "voucher",
        AuditLog.action == "UPDATE",
    ).order_by(AuditLog.id)
    if book_id is not None:
        stmt = stmt.where(AuditLog.book_id == book_id)

    facts: list[dict] = []
    for log in db.scalars(stmt).all():
        before = _snapshot_entries(log.before)
        after = _snapshot_entries(log.after)
        if not before or not after:
            continue
        for line_no, a_entry in after.items():
            b_entry = before.get(line_no)
            if not b_entry:
                continue
            b_sid = int(b_entry.get("subject_id") or 0)
            a_sid = int(a_entry.get("subject_id") or 0)
            if not b_sid or not a_sid or b_sid == a_sid:
                continue
            # **改科目不动金额与方向** —— 动了就说明这是别的编辑，不认。
            if str(b_entry.get("direction")) != str(a_entry.get("direction")):
                continue
            if Decimal(str(b_entry.get("amount") or 0)) != Decimal(
                    str(a_entry.get("amount") or 0)):
                continue
            old_subject = db.get(Subject, b_sid)
            new_subject = db.get(Subject, a_sid)
            if not _is_cash(old_subject) or not _is_cash(new_subject):
                continue
            facts.append({
                "audit_id": log.id,
                "voucher_id": int(log.entity_id),
                "account_subject_id": a_sid,
                "from_code": old_subject.code,
                "to_code": new_subject.code,
                "amount": Decimal(str(a_entry.get("amount") or 0)),
                "direction": str(a_entry.get("direction")),
                "voucher_date": log.after.get("voucher_date"),
            })
    return facts


def find_rows(db, fact: dict, account_subject_id: int) -> list[BankStatementRow]:
    """与这条事实对得上的流水行（还没被标成已入账的）。"""
    rows = db.scalars(select(BankStatementRow).where(
        BankStatementRow.status != STATUS_POSTED,
    )).all()
    hits: list[BankStatementRow] = []
    for row in rows:
        if row.amount is None or row.direction not in (DIRECTION_IN, DIRECTION_OUT):
            continue
        if Decimal(str(row.amount)) != fact["amount"]:
            continue
        if _entry_direction(row.direction) != fact["direction"]:
            continue
        statement = db.get(BankStatement, row.statement_id)
        if statement is None or statement.account_subject_id != account_subject_id:
            continue
        if row.tx_date is None or not fact["voucher_date"]:
            continue
        try:
            vdate = date.fromisoformat(str(fact["voucher_date"])[:10])
        except ValueError:
            continue
        if abs((vdate - row.tx_date).days) > EXTENDED_WINDOW_DAYS:
            continue
        hits.append(row)
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", type=int, default=None,
                        help="只处理这个账套（默认全部）")
    parser.add_argument("--apply", action="store_true",
                        help="真的写库（默认只报告）")
    args = parser.parse_args(argv)

    planned: list[tuple[BankStatementRow, int, dict]] = []
    ambiguous: list[tuple[dict, list[BankStatementRow]]] = []

    with SessionLocal() as db:
        facts = find_moved_cash_sides(db, args.book)
        print(f"审计日志里「现金侧被搬到账户上」的事实：{len(facts)} 条")
        for fact in facts:
            voucher = db.get(Voucher, fact["voucher_id"])
            vno = voucher.voucher_no if voucher else "?"
            vdate = voucher.voucher_date if voucher else None
            if fact["voucher_date"]:
                vdate = fact["voucher_date"]
            print(f"  · 凭证 {fact['voucher_id']} {vno} {vdate} "
                  f"{fact['from_code']} → {fact['to_code']} "
                  f"{fact['direction']} {fact['amount']}（审计 {fact['audit_id']}）")
            hits = find_rows(db, fact, fact["account_subject_id"])
            if len(hits) == 1:
                row = hits[0]
                statement = db.get(BankStatement, row.statement_id)
                print(f"      → 对应流水：对账单 {statement.id} seq={row.seq} "
                      f"{row.tx_date} {row.amount} {row.direction} "
                      f"（当前 status={row.status}）")
                planned.append((row, fact["voucher_id"], fact))
            elif not hits:
                print("      → 没有对得上的流水行（可能已经标成已入账了）")
            else:
                print(f"      → 对应流水有 {len(hits)} 行，说不清，不动：")
                for row in hits:
                    print(f"         对账单 {row.statement_id} seq={row.seq} "
                          f"{row.tx_date} {row.amount} {row.direction}")
                ambiguous.append((fact, hits))

        print()
        print(f"要补的标记：{len(planned)} 条；说不清的：{len(ambiguous)} 条")
        if not args.apply:
            print("（只报告，没写库。要写就加 --apply）")
            return 0
        if not planned:
            print("没有要补的，不写。")
            return 0

        backup = [{
            "row_id": row.id,
            "statement_id": row.statement_id,
            "seq": row.seq,
            "status": row.status,
            "matched_voucher_id": row.matched_voucher_id,
            "plan_status": row.plan_status,
            "new_matched_voucher_id": voucher_id,
        } for row, voucher_id, _ in planned]
        backup_path = Path("reclassified_marks_backup.json")
        backup_path.write_text(
            json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"改前快照写到 {backup_path}")

        for row, voucher_id, _ in planned:
            row.status = STATUS_POSTED
            row.matched_voucher_id = voucher_id
        db.commit()
        print(f"已补回 {len(planned)} 条标记。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
