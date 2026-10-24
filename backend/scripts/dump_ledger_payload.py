#!/usr/bin/env python3
"""把账簿接口的响应**逐字**落盘，用来比对改动前后是否完全一致。

    .venv/bin/python scripts/dump_ledger_payload.py /tmp/after.json

用法：改代码前后各跑一次，``diff`` 两个文件必须是空的。
比对的是**响应体本身**（不是解析后的对象）—— 少一个字段、多一个 null、
键序变了，都会在这里露出来。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
USER = "perf_probe"
PASSWORD = "perf-probe-password-2026"
BOOK = "压测账套01"


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ledger_dump.json")
    c = httpx.Client(base_url=BASE, timeout=600)
    token = c.post("/api/auth/login", json={"username": USER, "password": PASSWORD}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    books = c.get("/api/books", headers=h).json()
    book_id = next(b["id"] for b in books if b["name"] == BOOK)

    subjects = c.get(f"/api/books/{book_id}/subjects", params={"year": 2026}, headers=h).json()

    def walk(nodes):
        out_ = []
        for n in nodes:
            kids = n.get("children") or []
            out_ += walk(kids)
            if not kids:
                out_.append(n)
        return out_

    leaves = walk(subjects)
    periods = c.get(f"/api/books/{book_id}/periods", headers=h).json()
    rows = periods if isinstance(periods, list) else periods["periods"]
    p2026 = [r["id"] for r in rows if r["year"] == 2026]
    p2027 = [r["id"] for r in rows if r["year"] == 2027]

    payloads = {}
    for year, pid in ((2026, p2026[-1]), (2027, p2027[-1])):
        payloads[f"detail-1-{year}"] = c.get(
            f"/api/books/{book_id}/ledger/detail",
            params={"subject_id": leaves[0]["id"], "period_id": pid}, headers=h).content
        payloads[f"balance-{year}"] = c.get(
            f"/api/books/{book_id}/ledger/balance",
            params={"period_id": pid}, headers=h).content
        payloads[f"general-{year}"] = c.get(
            f"/api/books/{book_id}/ledger/general",
            params={"subject_id": leaves[0]["id"], "period_id": pid}, headers=h).content

    blob = json.dumps({k: v.decode("utf-8") for k, v in payloads.items()},
                      ensure_ascii=False, sort_keys=True, indent=1)
    out.write_text(blob, encoding="utf-8")
    for k, v in payloads.items():
        print(f"{k:16} {len(v):>9,} B  sha256={hashlib.sha256(v).hexdigest()[:16]}")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
