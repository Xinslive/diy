"""Shared-card live adapter; bundled into the signed plugin by the build tool.

Only calls the host's scoped public API. No SQL, network, files or host imports.
"""
from datetime import datetime, timezone
from uuid import uuid4

RECON_TABLE = "plugin_275b391c351a_recon"


def _recon_rows(ctx, card_id, books=None):
    """这张卡的核对记录 —— **跨账套一次读完**。

    核对组天然跨账套：一个核对组只写**一本账**，但**读**要读全部账套
    （撤销要找到那一组、stale 判定要看到全部组）。
    """
    return _read_across(ctx, RECON_TABLE, books=books, where={"card_id": int(card_id)})


def _signed_balance(row):
    value = _decimal(row.get("balance"))
    if value is None:
        return None
    return -value if row.get("direction") in ("贷", "CREDIT") else value


def _entry_signature(entry):
    return {key: entry.get(key) for key in (
        "id", "book_id", "voucher_id", "voucher_no", "voucher_date", "period",
        "subject_code", "amount_cents", "direction", "summary", "voucher_status")}


def _bank_signature(tx):
    return {key: tx.get(key) for key in (
        "id", "card_id", "tx_date", "tx_time", "direction", "amount_cents", "balance_cents")}


def _group_view(group, active=True, stale=False):
    """核对组给页面的形状 —— 与工作台 ``groups`` 里的那一份同款。

    页面在「确认核对」之后**不再重拉整个工作台**（那份要 0.9 MB、
    一次往返一秒多），改用这一份直接打补丁；所以这里的字段必须
    和工作台里的完全一致，否则页面上的核对状态会和工作台对不上。
    """
    return {**group, "id": group["root_id"], "active": active, "stale": stale}


def _target_book_hint(payload):
    """从客户端送来的分录快照里取出「这次核对落在哪本账」的提示。

    **这是提示，不是依据**：真正判定靠服务器重算那本账的分录 ——
    提示指向的账里找不到这些分录就一律 409，页面上选得到、服务器认不出
    的分录照样写不进去。

    快照缺失、或者快照本身指向好几本账，就返回 ``None``：那就不缩小范围，
    按从前的方式把全部账套重算一遍（行为一模一样，只是慢一点）。
    """
    snapshot = payload.get("entry_snapshot")
    if not isinstance(snapshot, list) or not snapshot:
        return None
    books = {item.get("book_id") for item in snapshot if isinstance(item, dict)}
    books.discard(None)
    if len(books) != 1:
        return None
    book_id = books.pop()
    return book_id if type(book_id) is int else None


def _live_workspace(ctx, card_id, only_book=None, with_ledger=True):
    """取这张卡的工作台模型。

    ``only_book`` —— 只算**这一本账**的账面侧（明细账、余额锚点、分录匹配）。
    一次核对只写一本账，其余账套的明细账读回来既写不进去、也校验不到，
    白读六倍时间（实测七本账要 580 ms，只读一本 80 ms）。

    ``with_ledger=False`` —— 账面侧整段不算（只需要银行侧时用）。
    这时核对组的「来源已变化」判定只看银行侧 —— 账面侧没读，
    就没有「分录变没变」的依据，不能凭空说它变了。
    """
    card = _card(ctx, card_id)
    books = _books(ctx)
    members = [m for m in _members(ctx, card_id, books) if m.get("enabled", True)]
    book_ids = {m["book_id"] for m in members}
    visible_books = [dict(b) for b in books if b["id"] in book_ids]
    # 对账单、归属、流水行：**跨账套各读一次**。从前是「按账套读一遍再合并」，
    # 读几次随账套数涨；这些表**总共只有一百多行**，读一百多遍同一批数据
    # （见 _read_across）。
    statements = _read_across(ctx, STMT_TABLE, books=books, where={"card_id": int(card_id)})
    allocations = _read_across(ctx, ALLOC_TABLE, books=books, where={"card_id": int(card_id)})
    rows = _stmt_rows_many(ctx, statements, books)
    if not rows:
        return {"empty": True, "card": {"id": card["id"], "name": card["name"]},
                "members": members, "books": visible_books, "imports": []}
    days = sorted(_text(r.get("tx_date"))[:10] for r in rows if r.get("tx_date"))
    if not days:
        _fail("对账单没有可识别的银行日期，请在原始资料中校正解析。")
    lo = previous_month(days[0][:7])
    hi = days[-1][:7]
    lo_index = int(lo[:4]) * 12 + int(lo[5:]) - 1
    hi_index = int(hi[:4]) * 12 + int(hi[5:]) - 1
    labels = ["%04d-%02d" % (n // 12, n % 12 + 1) for n in range(lo_index, hi_index + 1)]
    # 账簿覆盖哪些月份，由**银行流水覆盖的区间**决定，不由「哪几本账这一年
    # 有余额锚点」决定。否则「只算被选中那本账」的核对路径与「算全部账套」的
    # 工作台会给出不同的分录清单 —— 页面选得到的分录，服务器却认不出来。
    month_gate = sorted(set(labels) | {previous_month(labels[0])})
    entries, anchors, errors = [], [], []
    for book in visible_books:
        bid = book["id"]
        if not with_ledger or (only_book is not None and bid != only_book):
            continue
        periods = ctx.period_list(bid)
        by_month = {_month_label(int(p["year"]), int(p["month"])): p for p in periods}
        if periods:
            first = min(periods, key=lambda p: p["ord"])
            book.update(start_year=first["year"], start_month=first["month"])
        roots = sorted({m["subject_code"] for m in members if m["book_id"] == bid})
        roots = [code for code in roots if not any(code != other and code.startswith(other) for other in roots)]
        for year in sorted({int(m[:4]) for m in labels}):
            wanted = [label for label in labels if label.startswith(str(year)) and label in by_month]
            if not wanted:
                continue
            try:
                subjects = ctx.subject_read(bid, year)
                leaf_codes = [s["code"] for s in subjects if s.get("is_leaf") and any(s["code"].startswith(code) for code in roots)]
                if not leaf_codes:
                    errors.append({"book_id": bid, "year": year, "error": "登记范围没有可读的末级科目"})
                    continue
                result = ctx.detail_ledger_read(bid, leaf_codes, by_month[wanted[0]]["id"],
                                                by_month[wanted[-1]]["id"], include_unposted=True)
                leaf_anchors = []
                for subject in result.get("subjects", []):
                    if not subject.get("ok"):
                        errors.append({"book_id": bid, "year": year, "error": subject.get("error")})
                        continue
                    running = None
                    for index, row in enumerate(subject.get("rows", [])):
                        kind = row.get("kind")
                        if kind == "OPENING":
                            running = _signed_balance(row)
                        elif kind == "MONTH_TOTAL":
                            leaf_anchors.append({"book_id": bid, "subject_code": subject["code"],
                                "month": row["period"], "opening": _money(running),
                                "debit": _money(row.get("debit")), "credit": _money(row.get("credit")),
                                "closing": _money(_signed_balance(row)), "status": "OK"})
                            running = _signed_balance(row)
                        elif kind == "ENTRY":
                            debit, credit = _decimal(row.get("debit")), _decimal(row.get("credit"))
                            side = "DEBIT" if debit and not credit else "CREDIT" if credit and not debit else "UNKNOWN"
                            amount = debit if side == "DEBIT" else credit if side == "CREDIT" else None
                            entries.append({"id": -(len(entries) + 1), "book_id": bid,
                                "voucher_id": row.get("voucher_id"), "voucher_no": row.get("voucher_no") or "",
                                "voucher_date": row.get("voucher_date"), "voucher_status": row.get("status"),
                                "year": year, "month": int(row["period"][5:]), "period": row["period"],
                                "subject_code": row.get("subject_code"), "subject_name": row.get("subject_name"),
                                "summary": row.get("summary") or "", "direction": side, "amount": _money(amount),
                                "line_no": index, "reference_ok": False})
                for root in roots:
                    for month in wanted:
                        parts = [a for a in leaf_anchors if a["month"] == month and a["subject_code"].startswith(root)]
                        expected = [code for code in leaf_codes if code.startswith(root)]
                        if len(parts) != len(expected) or not parts or any(cents(a.get(k)) is None for a in parts for k in ("opening", "debit", "credit", "closing")):
                            continue
                        anchors.append({"book_id": bid, "subject_code": root, "month": month, "status": "OK",
                            **{k: money(sum(cents(a[k]) for a in parts)) for k in ("opening", "debit", "credit", "closing")}})
            except HTTPException as exc:
                errors.append({"book_id": bid, "year": year, "error": _detail(exc)})
        # Resolve actual Entry IDs through the host's existing read capability.
        # Buckets with indistinguishable voucher-side lines remain non-confirmable.
        local = [e for e in entries if e["book_id"] == bid and cents(e["amount"]) is not None and cents(e["amount"]) > 0]
        used_entry_ids = set()
        for start in range(0, len(local), 4000):
            batch = local[start:start + 4000]
            if not batch:
                continue
            matched = ctx.entry_match(bid, roots, [{"id": -e["id"], "seq": n, "tx_date": e["voucher_date"],
                "direction": "IN" if e["direction"] == "DEBIT" else "OUT", "amount": e["amount"]} for n, e in enumerate(batch)],
                date_from=min(e["voucher_date"] for e in batch), date_to=max(e["voucher_date"] for e in batch), primary_days=1, review_days=1)
            pools = defaultdict(list)
            input_map = {-e["id"]: e for e in batch}
            for pair in matched.get("pairs", []):
                if int(pair.get("days_diff") or 0) != 0:
                    continue
                side = input_map.get(pair["row_id"], {}).get("direction")
                key = (int(pair["voucher_id"]), str(pair["voucher_date"]), cents(pair["amount"]), side)
                pools[key].append(int(pair["entry_id"]))
            for entry in batch:
                key = (int(entry["voucher_id"]), str(entry["voucher_date"]), cents(entry["amount"]), entry["direction"])
                same = [e for e in local if (int(e["voucher_id"]), str(e["voucher_date"]), cents(e["amount"]), e["direction"]) == key]
                candidates = pools.get(key, [])
                if len(same) == 1 and len(candidates) == 1 and candidates[0] not in used_entry_ids:
                    entry["id"] = candidates[0]
                    entry["reference_ok"] = True
                    used_entry_ids.add(candidates[0])
    # Aggregate disjoint registered subject roots once per book. The view shows a
    # single book balance even when that card uses several leaf accounts there.
    combined, projection_members = [], []
    for book in visible_books:
        bid = book["id"]
        roots = sorted({m["subject_code"] for m in members if m["book_id"] == bid})
        roots = [code for code in roots if not any(code != other and code.startswith(other) for other in roots)]
        projection_members.append({"book_id": bid, "card_id": int(card_id), "subject_code": "", "enabled": True})
        for month in labels:
            parts = [a for a in anchors if a["book_id"] == bid and a["month"] == month]
            row = {"book_id": bid, "subject_code": "", "month": month, "status": "NO_PERIOD"}
            if len(parts) == len(roots) and parts:
                row.update(status="OK", **{k: money(sum(cents(a[k]) for a in parts)) for k in ("opening", "debit", "credit", "closing")})
            combined.append(row)
    snapshot = _jsonable({"cards": [card], "books": visible_books, "members": projection_members,
        "statements": statements, "rows": rows, "allocations": allocations, "entries": entries,
        "ledger_anchors": combined, "ledger_months": month_gate,
        "captured_at": datetime.now(timezone.utc).isoformat(), "source": "宿主实时账簿与银行原件"})
    model = build_model(snapshot, int(card_id))
    model["members"] = _jsonable(members)
    model["ledger"]["read_errors"] = errors
    if errors:
        model["ledger"]["summary"]["chain_ok"] = False
    records = _recon_rows(ctx, card_id, books)
    overrides = {}
    for row in sorted([r for r in records if r["kind"] == "OWNER"], key=lambda r: r["id"]):
        overrides[row["payload"]["tx_id"]] = row["payload"]
    for tx in model["transactions"]:
        chosen = overrides.get(tx["id"])
        if chosen:
            tx["ownership"] = "MANUAL"
            tx["owner_book_id"] = chosen["book_id"]
            tx["owner_override"] = chosen
    tx_map = {tx["id"]: tx for tx in model["transactions"]}
    entry_map = {e["id"]: e for e in model["entries"]}
    headers = {r["group_key"]: r for r in records if r["kind"] == "GROUP"}
    groups, history = [], []
    for row in records:
        if row["kind"] != "HISTORY":
            continue
        group = dict(row["payload"])
        group.update(id=group["root_id"], active=row["group_key"] in headers)
        history.append(group)
        if not group["active"]:
            continue
        valid = all(tx_map.get(t["id"]) and _bank_signature(tx_map[t["id"]]) == t
                    and tx_map[t["id"]].get("owner_book_id") in (None, group["book_id"])
                    and tx_map[t["id"]]["ownership"] != "CONFLICT" for t in group["bank_snapshot"]) \
            and (not with_ledger
                 or all(entry_map.get(e["id"]) and _entry_signature(entry_map[e["id"]]) == e
                        for e in group["entry_snapshot"]))
        group["stale"] = not valid
        groups.append(group)
    model.update(groups=groups, history=history,
        can_write_books=[b["id"] for b in books if b.get("can_write")], empty=False)
    model["summary"]["owner_conflicts"] = sum(t["ownership"] == "CONFLICT" for t in model["transactions"])
    return model


def _protect_statement_matches(ctx, stmt, books):
    raw_ids = {r["id"] for r in _stmt_rows(ctx, stmt["id"], books)}
    for record in _recon_rows(ctx, stmt["card_id"], books):
        if record["kind"] == "CLAIM" and record.get("bank_row_id") in raw_ids:
            _fail("这份原件仍有有效核对关系，请先在对账工作台撤销后再删除或重新解析。", 409)


def register_workspace(registrar, ctx):
    def workspace(card_id: int):
        _require_caps()
        return _live_workspace(ctx, card_id)

    def set_owner(card_id: int, payload: dict):
        # 指定归属只看银行侧（这笔交易归哪本账），账面侧整段不必算。
        model = _live_workspace(ctx, card_id, with_ledger=False)
        try:
            bid = int(payload.get("book_id") or 0)
        except (TypeError, ValueError):
            _fail("请选择有效的成员账套。")
        if bid not in model.get("can_write_books", []) or bid not in {m["book_id"] for m in model["members"]}:
            _fail("请选择有写权限的成员账套。", 403)
        tx = next((t for t in model["transactions"] if t["id"] == payload.get("tx_id")), None)
        if not tx:
            _fail("交易不存在或来源已改变，请刷新。", 409)
        if tx.get("has_legacy_voucher") or any(tx["id"] in g["bank"] for g in model["groups"]):
            _fail("这笔交易已有凭证或核对关系，请先核实并撤销关联。", 409)
        ctx.data.insert(RECON_TABLE, {"card_id": int(card_id), "kind": "OWNER", "group_key": str(uuid4()),
            "payload": {"tx_id": tx["id"], "book_id": bid, "prior": tx["allocation_evidence"]}}, book_id=bid)
        ctx.audit_write("sharedcard_owner_confirmed")
        return {"ok": True}

    def confirm(card_id: int, payload: dict):
        # 一次核对只写**一本账**。把七本账的明细账全读一遍，六本的结果
        # 既写不进去也校验不到 —— 白读六倍时间。客户端送来的分录快照
        # 只当提示用（见 _target_book_hint），真正的判定仍是服务器自己
        # 重算出来的那本账。
        model = _live_workspace(ctx, card_id, only_book=_target_book_hint(payload))
        tx_map = {t["id"]: t for t in model.get("transactions", [])}
        entry_map = {e["id"]: e for e in model.get("entries", [])}
        tx_ids, entry_ids = payload.get("bank"), payload.get("entries")
        if not isinstance(tx_ids, list) or not isinstance(entry_ids, list) or not tx_ids or not entry_ids \
                or len(tx_ids) + len(entry_ids) > 5000 \
                or any(not isinstance(k, str) for k in tx_ids) \
                or any(type(k) is not int for k in entry_ids) \
                or len(set(tx_ids)) != len(tx_ids) or len(set(entry_ids)) != len(entry_ids):
            _fail("请选择不重复的银行交易与账面分录。")
        bank = [tx_map.get(key) for key in tx_ids]
        entries = [entry_map.get(key) for key in entry_ids]
        if not all(bank) or not all(entries) or any(not e.get("reference_ok") for e in entries):
            _fail("来源已变化或无法取得唯一分录标识，请刷新后重选。", 409)
        bid = entries[0]["book_id"]
        if bid not in model["can_write_books"]:
            _fail("没有这本账的写权限。", 403)
        if any(e["book_id"] != bid for e in entries):
            _fail("一个核对组只能归到一本账。")
        if any(t["ownership"] == "CONFLICT" or t.get("has_legacy_voucher") or t.get("owner_book_id") not in (None, bid) for t in bank):
            _fail("请先核实银行交易的归属冲突或既有凭证。", 409)
        direction = bank[0]["direction"]
        if direction not in ("IN", "OUT") or any(t["direction"] != direction for t in bank) \
                or any(e["direction"] != ("DEBIT" if direction == "IN" else "CREDIT") for e in entries):
            _fail("收入和支出必须分开核对，不能净额抵平。")
        if any(t["amount_cents"] is None or t["amount_cents"] <= 0 for t in bank + entries):
            _fail("核对金额必须明确且大于零。")
        amount = sum(t["amount_cents"] for t in bank)
        if amount != sum(e["amount_cents"] for e in entries):
            _fail("两侧金额不相等，不能确认。")
        if payload.get("bank_snapshot") != [_bank_signature(t) for t in bank] \
                or payload.get("entry_snapshot") != [_entry_signature(e) for e in entries]:
            _fail("所选数据在操作期间发生变化，请刷新核实后重选。", 409)
        # One statement inserts header, immutable evidence and every occupancy row.
        # Negative UUID-derived header IDs cannot collide with the positive sequence.
        key = str(uuid4())
        root_id = -int(key.replace("-", "")[:13], 16)
        group = {"root_id": root_id, "key": key, "book_id": bid, "bank": tx_ids, "entries": entry_ids,
            "amount_cents": amount, "direction": direction, "created_at": datetime.now(timezone.utc).isoformat(),
            "bank_snapshot": [_bank_signature(t) for t in bank], "entry_snapshot": [_entry_signature(e) for e in entries]}
        records = [{"id": root_id, "kind": "GROUP", "root_id": None, "bank_row_id": None, "entry_id": None, "payload": {}},
                   {"id": None, "kind": "HISTORY", "root_id": None, "bank_row_id": None, "entry_id": None, "payload": group}]
        # Explicit NULL id would bypass the default. Allocate every row ID from UUIDs.
        for tx in bank:
            for source in tx["sources"]:
                records.append({"id": None, "kind": "CLAIM", "root_id": root_id, "bank_row_id": source["row_id"], "entry_id": None, "payload": {}})
        for entry in entries:
            records.append({"id": None, "kind": "CLAIM", "root_id": root_id, "bank_row_id": None, "entry_id": entry["id"], "payload": {}})
        for record in records:
            if record["id"] is None:
                record["id"] = -int(uuid4().hex[:13], 16)
            record.update(card_id=int(card_id), group_key=key)
        try:
            ctx.data.insert_many(RECON_TABLE, records, book_id=bid)
        except HTTPException:
            _fail("所选银行交易或分录已被另一核对组占用；本次整组未写入，请刷新。", 409)
        ctx.audit_write("sharedcard_group_confirmed")
        # 把这一组原样交回页面：页面在确认之后**不再重拉整个工作台**
        # （那份 0.9 MB，一次往返一秒多），直接用这份打补丁。
        return {"id": root_id, "ok": True, "group": _group_view(group)}

    def revoke(card_id: int, group_id: int):
        records = _recon_rows(ctx, card_id, _books(ctx))
        header = next((r for r in records if r["kind"] == "GROUP" and r["id"] == int(group_id)), None)
        if not header:
            _fail("此核对组已撤销或不存在。", 409)
        history = next((r for r in records if r["kind"] == "HISTORY"
                        and r.get("group_key") == header.get("group_key")), None)
        ctx.data.delete(RECON_TABLE, header["id"], book_id=header["book_id"])
        ctx.audit_write("sharedcard_group_revoked")
        revoked = None
        if history:
            revoked = {**dict(history["payload"]), "id": int(group_id), "active": False}
        return {"ok": True, "group": revoked}

    registrar.get("/cards/{card_id}/workspace", workspace)
    registrar.post("/cards/{card_id}/ownership", set_owner)
    registrar.post("/cards/{card_id}/reconciliations", confirm)
    registrar.add("/cards/{card_id}/reconciliations/{group_id}", revoke, ["DELETE"])
