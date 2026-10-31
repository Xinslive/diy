"""用只读生产快照预演共享卡的连续流水模型，不连接或修改数据库。

原始行始终保留；跨文件复用只发生在预览投影中。归属记录不是核对凭据，
同额候选也不是已核对。本工具不做子集凑数、不生成凭证、不落业务状态。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, time
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re


def cents(value):
    """金额按分比较；缺数或超出分币精度时拒绝伪造一个金额。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number * 100 != (number * 100).to_integral_value():
        return None
    return int(number * 100)


def money(value):
    return None if value is None else f"{Decimal(value) / 100:.2f}"


def previous_month(month):
    year, month_number = map(int, month.split("-"))
    index = year * 12 + month_number - 2
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def compatible_names(left, right):
    """完整户名与脱敏户名可以互为来源；两个不同的完整户名不静默合并。"""
    left, right = str(left or "").strip(), str(right or "").strip()
    if left == right:
        return bool(left)
    for masked, other in ((left, right), (right, left)):
        if "*" not in masked and "＊" not in masked:
            continue
        pattern = "^" + ".*".join(re.escape(p) for p in re.split(r"[*＊]+", masked)) + "$"
        if re.fullmatch(pattern, other) and re.sub(r"[*＊]", "", masked):
            return True
    return False


def identity(row, card_id, currency):
    """无银行流水号时的保守复用候选：卡、币种、秒级时间、方向、金额、余额。"""
    amount, balance = cents(row.get("amount")), cents(row.get("balance"))
    try:
        day = date.fromisoformat(str(row.get("tx_date") or ""))
        clock = time.fromisoformat(str(row.get("tx_time") or ""))
    except ValueError:
        return None
    if len(str(row.get("tx_time") or "")) < 8 or amount is None or amount <= 0 \
            or balance is None or row.get("direction") not in ("IN", "OUT"):
        return None
    return (card_id, currency, day.isoformat(), clock.isoformat(),
            row["direction"], amount, balance)


def order_transactions(transactions):
    """同一秒有多笔时按余额前后关系排序，不按原行号或金额猜顺序。"""
    buckets = defaultdict(list)
    for tx in transactions:
        buckets[(tx["tx_date"] or "9999", tx["tx_time"] or "")].append(tx)
    ordered, issues, previous = [], [], None
    for _, bucket in sorted(buckets.items()):
        remaining = sorted(bucket, key=lambda tx: tx["id"])
        if len(remaining) > 1:
            wanted = previous
            if wanted is None or not any(tx["before_cents"] == wanted for tx in remaining):
                ends = {tx["balance_cents"] for tx in remaining if tx["balance_cents"] is not None}
                starts = [tx for tx in remaining if tx["before_cents"] is not None
                          and tx["before_cents"] not in ends]
                wanted = starts[0]["before_cents"] if len(starts) == 1 else None
            picked = []
            while remaining:
                choices = [tx for tx in remaining if wanted is not None
                           and tx["before_cents"] == wanted]
                if len(choices) != 1:
                    issues.append({"kind": "ambiguous_order", "transactions": [tx["id"] for tx in remaining]})
                    picked.extend(remaining)
                    break
                tx = choices[0]
                picked.append(tx)
                remaining.remove(tx)
                wanted = tx["balance_cents"]
            bucket = picked
        else:
            bucket = remaining
        ordered.extend(bucket)
        previous = bucket[-1]["balance_cents"]
    return ordered, issues


def build_ledger_line(snapshot, entries, members, books):
    """只投影宿主引擎的期初/发生额，不把筛选结果从零重新滚动。"""
    anchors = snapshot.get("ledger_anchors") or []
    if not anchors:
        return {"status": "UNAVAILABLE", "months": [], "bridges": [], "issues": [],
                "summary": {"chain_ok": False, "reason": "没有宿主账簿引擎的余额锚点"}}
    requested = {(m["book_id"], m["subject_code"]) for m in members}
    anchors = [dict(a) for a in anchors if (a["book_id"], a["subject_code"]) in requested]
    issues, bridges, periods, previous_by_book = [], [], [], {}
    for month in sorted({a["month"] for a in anchors}):
        current = [a for a in anchors if a["month"] == month]
        covered, absent, unopened = [], [], []
        for book_id, code in sorted(requested):
            found = [a for a in current if a["book_id"] == book_id and a["subject_code"] == code]
            book = books.get(book_id, {})
            start = f"{book.get('start_year', 0):04d}-{book.get('start_month', 1):02d}"
            if len(found) != 1 or found[0].get("status") != "OK":
                if month < start:
                    unopened.append(book_id)
                else:
                    absent.append(book_id)
                    issues.append({"kind": "ledger_anchor_missing", "book_id": book_id, "month": month})
                previous_by_book[book_id] = {"month": month, "status": "NOT_STARTED" if month < start else "MISSING"}
                continue
            anchor = found[0]
            values = {key: cents(anchor.get(key)) for key in ("opening", "debit", "credit", "closing")}
            if any(value is None for value in values.values()):
                absent.append(book_id)
                issues.append({"kind": "ledger_anchor_invalid", "book_id": book_id, "month": month})
                previous_by_book[book_id] = {"month": month, "status": "MISSING"}
                continue
            items = sorted([e for e in entries if e["book_id"] == book_id
                            and e["period"] == month and str(e["subject_code"]).startswith(code)],
                           key=lambda e: (e["voucher_date"], e["voucher_no"], e["line_no"], e["id"]))
            dr = sum(e["amount_cents"] for e in items if e["direction"] == "DEBIT" and e["amount_cents"] is not None)
            cr = sum(e["amount_cents"] for e in items if e["direction"] == "CREDIT" and e["amount_cents"] is not None)
            valid = values["opening"] + values["debit"] - values["credit"] == values["closing"] \
                and dr == values["debit"] and cr == values["credit"] \
                and all(e["amount_cents"] is not None and e["direction"] in ("DEBIT", "CREDIT") for e in items)
            if not valid:
                issues.append({"kind": "ledger_movement_mismatch", "book_id": book_id, "month": month,
                               "source_debit": money(values["debit"]), "entries_debit": money(dr),
                               "source_credit": money(values["credit"]), "entries_credit": money(cr)})
            previous = previous_by_book.get(book_id)
            if previous:
                if previous["status"] == "OK" and previous["closing_cents"] != values["opening"]:
                    issues.append({"kind": "ledger_opening_break", "book_id": book_id, "month": month,
                                   "previous_closing": money(previous["closing_cents"]),
                                   "current_opening": money(values["opening"])})
                elif previous["status"] == "NOT_STARTED" and month == start:
                    bridges.append({"kind": "BOOK_OPENING", "book_id": book_id, "month": month,
                                    "amount_cents": values["opening"], "amount": money(values["opening"]),
                                    "source": "宿主账簿引擎·账套启用期初，不是收支"})
                elif previous["status"] != "OK":
                    issues.append({"kind": "ledger_scope_unknown", "book_id": book_id, "month": month})
            running = values["opening"]
            for entry in items:
                if entry["amount_cents"] is None or entry["direction"] not in ("DEBIT", "CREDIT"):
                    entry["book_balance_cents"] = None
                    continue
                running += entry["amount_cents"] * (1 if entry["direction"] == "DEBIT" else -1)
                entry["book_balance_cents"] = running if valid else None
            covered.append({"book_id": book_id, "subject_code": code, "valid": valid,
                            **{f"{key}_cents": value for key, value in values.items()}})
            previous_by_book[book_id] = {"month": month, "status": "OK" if valid else "MISSING",
                                         "closing_cents": values["closing"]}
        complete = bool(covered) and not absent and all(a["valid"] for a in covered) and len(covered) + len(unopened) == len(requested)
        values = {key: sum(a[f"{key}_cents"] for a in covered) if complete else None
                  for key in ("opening", "debit", "credit", "closing")}
        ordered = sorted([e for e in entries if e["period"] == month],
                         key=lambda e: (e["voucher_date"], e["book_id"], e["voucher_no"], e["line_no"], e["id"]))
        running = values["opening"]
        for entry in ordered:
            if running is not None and entry["amount_cents"] is not None:
                running += entry["amount_cents"] * (1 if entry["direction"] == "DEBIT" else -1)
            else:
                running = None
            entry["aggregate_balance_cents"] = running
        if complete and running != values["closing"]:
            issues.append({"kind": "ledger_aggregate_mismatch", "month": month})
        periods.append({"month": month, "count": len(ordered), "complete": complete,
                        "coverage": len(covered), "expected": len(requested), "unopened": unopened,
                        "missing": absent, "books": covered,
                        **{key: money(value) for key, value in values.items()},
                        "entry_ids": [e["id"] for e in ordered]})
    complete = bool(periods) and all(p["complete"] for p in periods)
    summary = {"chain_ok": complete and not issues, "issues": len(issues),
               "opening": periods[0]["opening"] if periods else None,
               "closing": periods[-1]["closing"] if periods else None,
               "debit": money(sum(cents(p["debit"]) for p in periods)) if complete else None,
               "credit": money(sum(cents(p["credit"]) for p in periods)) if complete else None,
               "scope_opening": money(sum(b["amount_cents"] for b in bridges)),
               "entry_count": len(entries), "anchor_count": sum(len(p["books"]) for p in periods),
               "from_month": periods[0]["month"] if periods else None,
               "to_month": periods[-1]["month"] if periods else None}
    return {"status": "OK" if summary["chain_ok"] else "REVIEW", "months": periods,
            "bridges": bridges, "issues": issues, "summary": summary}


def build_model(snapshot, card_id=None):
    cards = snapshot.get("cards") or []
    if card_id is None:
        if len(cards) != 1:
            raise ValueError("快照包含多张卡或没有卡，请明确 --card-id")
        card_id = cards[0]["id"]
    card = next((item for item in cards if item["id"] == card_id), None)
    if card is None:
        raise ValueError("快照中没有这张共享卡")
    card = {**{key: value for key, value in card.items() if key != "account_no"},
            "last4": str(card.get("last4") or card.get("account_no") or "")[-4:]}
    statements = {s["id"]: s for s in snapshot.get("statements", []) if s["card_id"] == card_id}
    rows = [r for r in snapshot.get("rows", []) if r["stmt_id"] in statements]
    if not rows:
        raise ValueError("这张卡的快照没有流水，不能建立资金连续性结论")
    members = [m for m in snapshot.get("members", []) if m["card_id"] == card_id and m.get("enabled", True)]
    book_ids = {m["book_id"] for m in members}
    books = {b["id"]: b for b in snapshot.get("books", []) if b["id"] in book_ids}
    allocations = defaultdict(list)
    for alloc in snapshot.get("allocations", []):
        if alloc["card_id"] == card_id:
            allocations[alloc["row_id"]].append(alloc)

    candidates = defaultdict(list)
    for row in rows:
        key = identity(row, card_id, card.get("currency", "CNY"))
        candidates[key if key is not None else ("raw", row["id"])].append(row)
    groups, collision_groups = [], []
    for group in candidates.values():
        distinct_sources = len({r["stmt_id"] for r in group}) == len(group)
        names_agree = all(compatible_names(a.get("cp_name"), b.get("cp_name"))
                          for index, a in enumerate(group) for b in group[index + 1:])
        if len(group) > 1 and (not distinct_sources or not names_agree):
            collision_groups.append([r["id"] for r in group])
            groups.extend([[row] for row in group])
        else:
            groups.append(group)

    transactions, row_to_transaction = [], {}
    for group in groups:
        group.sort(key=lambda row: row["id"])
        row = group[0]
        amount, balance = cents(row.get("amount")), cents(row.get("balance"))
        signed = amount * (1 if row["direction"] == "IN" else -1) \
            if amount is not None and row["direction"] in ("IN", "OUT") else None
        evidence = [alloc for alias in group for alloc in allocations[alias["id"]]]
        owners = {alloc["book_id"] for alloc in evidence}
        owner = next(iter(owners)) if len(owners) == 1 else None
        ownership = "CONFLICT" if len(owners) > 1 else (
            "MANUAL" if any(a["status"] == "ALLOCATED" for a in evidence) else (
                "PRESCREENED" if evidence else "UNASSIGNED"))
        tx_id = f"tx-{row['id']}"
        sources = [{"row_id": alias["id"], "statement_id": alias["stmt_id"],
                    "seq": alias["seq"], "filename": statements[alias["stmt_id"]]["filename"],
                    "counterparty": alias.get("cp_name") or "",
                    "legacy_match_months": statements[alias["stmt_id"]].get("match_months") or ""}
                   for alias in group]
        tx = {"id": tx_id, "card_id": card_id, "tx_date": row.get("tx_date"),
              "tx_time": row.get("tx_time"), "direction": row.get("direction"),
              "amount": money(amount), "amount_cents": amount,
              "balance": money(balance), "balance_cents": balance,
              "before_cents": None if balance is None or signed is None else balance - signed,
              "summary": row.get("summary") or "", "purpose": row.get("purpose") or "",
              "counterparty": row.get("cp_name") or "", "sources": sources,
              "ownership": ownership, "owner_book_id": owner,
              "owner_name": books.get(owner, {}).get("name", ""),
              "allocation_evidence": evidence,
              "has_legacy_voucher": any(a.get("status") == "VOUCHER_POSTED" for a in evidence),
              "problems": [problem for alias in group for problem in alias.get("problems", [])]}
        transactions.append(tx)
        row_to_transaction.update({alias["id"]: tx_id for alias in group})

    transactions, issues = order_transactions(transactions)
    if collision_groups:
        issues.extend({"kind": "identity_conflict", "rows": group} for group in collision_groups)
    previous = transactions[0]["before_cents"]
    opening = previous
    for tx in transactions:
        if identity({"tx_date": tx["tx_date"], "tx_time": tx["tx_time"],
                     "direction": tx["direction"], "amount": tx["amount"], "balance": tx["balance"]},
                    card_id, card.get("currency", "CNY")) is None:
            issues.append({"kind": "incomplete_bank_fields", "transaction": tx["id"]})
        if previous is not None and tx["before_cents"] is not None and previous != tx["before_cents"]:
            issues.append({"kind": "balance_gap", "transaction": tx["id"],
                           "previous_balance": money(previous), "next_before_balance": money(tx["before_cents"]),
                           "difference": money(tx["before_cents"] - previous)})
        previous = tx["balance_cents"]

    def total(items, direction):
        return sum(item["amount_cents"] for item in items
                   if item["direction"] == direction and item["amount_cents"] is not None)

    months = []
    for month in sorted({tx["tx_date"][:7] for tx in transactions if tx["tx_date"]}):
        items = [tx for tx in transactions if str(tx["tx_date"] or "").startswith(month)]
        months.append({"month": month, "count": len(items),
                       "income": money(total(items, "IN")), "expense": money(total(items, "OUT")),
                       "opening": money(items[0]["before_cents"]), "closing": items[-1]["balance"],
                       "first_date": items[0]["tx_date"], "last_date": items[-1]["tx_date"],
                       "overlap_count": sum(len(tx["sources"]) > 1 for tx in items),
                       "owned": sum(tx["owner_book_id"] is not None for tx in items)})

    # 账簿覆盖哪些月份：快照里显式给出的是**银行流水覆盖的区间**
    # （见 _live_workspace 的 month_gate）。这一项不能省 ——
    # 「只算被选中那本账」的核对路径与「算全部账套」的工作台
    # 必须给出同一份分录清单，否则页面选得到的分录服务器会认不出来。
    ledger_months = set(snapshot.get("ledger_months") or []) \
        | {m["month"] for m in months} | {previous_month(m["month"]) for m in months} \
        | {a["month"] for a in snapshot.get("ledger_anchors", [])}
    entries = []
    for entry in snapshot.get("entries", []):
        period = f"{entry['year']:04d}-{entry['month']:02d}" if "year" in entry and "month" in entry else entry["voucher_date"][:7]
        if entry["book_id"] not in book_ids or period not in ledger_months:
            continue
        if not any(entry["book_id"] == m["book_id"] and str(entry["subject_code"]).startswith(m["subject_code"]) for m in members):
            continue
        item = dict(entry)
        item.update({"book_name": books.get(entry["book_id"], {}).get("name", ""),
                     "period": period, "line_no": entry.get("line_no", 0),
                     "amount_cents": cents(entry["amount"]), "amount": money(cents(entry["amount"]))})
        entries.append(item)
    entry_to_transactions = defaultdict(list)
    for tx in transactions:
        month = previous_month(tx["tx_date"][:7]) if tx["tx_date"] else ""
        tx["suggested_ledger_month"] = month
        tx["candidate_entry_ids"] = [e["id"] for e in entries
            if e["period"] == month and e["amount_cents"] == tx["amount_cents"]
            and e["amount_cents"] is not None and e["amount_cents"] > 0
            and ((tx["direction"] == "IN" and e["direction"] == "DEBIT")
                 or (tx["direction"] == "OUT" and e["direction"] == "CREDIT"))]
        for entry_id in tx["candidate_entry_ids"]:
            entry_to_transactions[entry_id].append(tx["id"])
    for tx in transactions:
        tx["candidate_reused"] = any(len(entry_to_transactions[eid]) > 1 for eid in tx["candidate_entry_ids"])
    ledger = build_ledger_line(snapshot, entries, members, books)

    imports, seen = [], set()
    for statement in sorted(statements.values(), key=lambda s: (s.get("period_start") or "", s["id"])):
        source_rows = [r for r in rows if r["stmt_id"] == statement["id"]]
        tx_ids = {row_to_transaction[r["id"]] for r in source_rows}
        new_count, overlap_count = len(tx_ids - seen), len(tx_ids & seen)
        source_ordered = [tx for tx in transactions if tx["id"] in tx_ids]
        prior = [tx for tx in transactions if tx["id"] in seen]
        imports.append({"id": statement["id"], "filename": statement["filename"],
                        "raw_count": len(source_rows), "new_count": new_count, "overlap_count": overlap_count,
                        "existing_before": len(seen), "after_count": len(seen | tx_ids),
                        "period_start": statement.get("period_start"), "period_end": statement.get("period_end"),
                        "declared_match_months": statement.get("match_months") or "",
                        "opening_stated": statement.get("opening_balance"),
                        "closing_stated": statement.get("closing_balance"),
                        "last_observed_balance": source_ordered[-1]["balance"] if source_ordered else None,
                        "before_latest_date": prior[-1]["tx_date"] if prior else None,
                        "before_latest_balance": prior[-1]["balance"] if prior else None,
                        "new_ids": sorted(tx_ids - seen), "transaction_ids": sorted(tx_ids)})
        seen |= tx_ids

    active_allocations = [alloc for row in rows for alloc in allocations[row["id"]]]
    summary = {"raw_rows": len(rows), "transactions": len(transactions), "statements": len(statements),
               "members": len(members), "overlap_transactions": sum(len(tx["sources"]) > 1 for tx in transactions),
               "reused_source_rows": len(rows) - len(transactions),
               "masked_name_variants": sum(len({s["counterparty"] for s in tx["sources"]}) > 1 for tx in transactions),
               "legacy_allocations": len(active_allocations),
               "transactions_with_ownership_evidence": sum(bool(tx["allocation_evidence"]) for tx in transactions),
               "owned_transactions": sum(tx["owner_book_id"] is not None for tx in transactions),
               "manual_owned": sum(tx["ownership"] == "MANUAL" for tx in transactions),
               "prescreen_owned": sum(tx["ownership"] == "PRESCREENED" for tx in transactions),
               "owner_conflicts": sum(tx["ownership"] == "CONFLICT" for tx in transactions),
               "legacy_allocations_without_voucher": sum(not a.get("voucher_id") for a in active_allocations),
               "income": money(total(transactions, "IN")), "expense": money(total(transactions, "OUT")),
               "opening_inferred": money(opening), "closing_observed": transactions[-1]["balance"],
               "first_date": transactions[0]["tx_date"], "last_date": transactions[-1]["tx_date"],
               "chain_ok": not issues and opening is not None, "chain_issues": len(issues),
               "same_amount_candidate_rows": sum(bool(tx["candidate_entry_ids"]) for tx in transactions),
               "ledger_entries_in_preview": len(entries),
               "ledger_unposted": sum(e.get("voucher_status") != "POSTED" for e in entries)}
    return {"version": 2, "captured_at": snapshot.get("captured_at"), "source": snapshot.get("source"),
            "card": card, "books": list(books.values()), "members": members, "summary": summary,
            "months": months, "transactions": transactions, "entries": entries,
            "imports": imports, "issues": issues, "collision_groups": collision_groups, "ledger": ledger}


def write_private(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--card-id", type=int)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"), parse_float=Decimal)
    model = build_model(snapshot, args.card_id)
    payload = json.dumps(model, ensure_ascii=False, default=str, separators=(",", ":"))
    write_private(args.output, payload)
    if args.preview:
        template = Path(__file__).with_name("sharedcard-preview.html").read_text(encoding="utf-8")
        if template.count("__SHARED_CARD_DATA__") != 1:
            raise ValueError("预览模板的数据占位符必须有且只有一个")
        # JSON 的文本不能提前结束 script 元素，也不能变成可执行 HTML。
        safe_payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        write_private(args.preview, template.replace("__SHARED_CARD_DATA__", safe_payload))
    print(json.dumps(model["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
