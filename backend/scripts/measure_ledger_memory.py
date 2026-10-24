#!/usr/bin/env python3
"""量一量账簿取数路径的**瞬时内存**（2026-10-24 性能审计）。

用法：
    .venv/bin/python scripts/measure_ledger_memory.py

打的是 ``detail_ledger_book``（账簿成册那条路）—— 它是**最重的一条**：
把整本账的分录一次性取回来再按科目切。屏幕上那本明细账（``detail_ledger``）
是它的子集，所以这条路的数字就是屏幕那条路的上界。

量法用 ``tracemalloc``：它量的是 **Python 分配器**上的峰值，与 CPython
的 obmalloc 无关但与「对象活多久」有关 —— 正好是我们要的那件事
（ORM 实体活到函数结束，列元组也一样，但**每个有多大**差 6 倍）。
"""
from __future__ import annotations

import gc
import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.ledger import queries  # noqa: E402
from app.models import Book, Period  # noqa: E402


def main() -> int:
    book_name = sys.argv[1] if len(sys.argv) > 1 else "压测账套01"
    db = SessionLocal()
    try:
        book = db.scalar(select(Book).where(Book.name == book_name))
        if book is None:
            print(f"找不到账套「{book_name}」，先用 seed_perf.py --profile medium 造数")
            return 1
        period = db.scalar(
            select(Period).where(Period.book_id == book.id).order_by(Period.ord.desc()).limit(1))
        n_entries = db.scalar(
            select(__import__("sqlalchemy").func.count()).select_from(
                __import__("app.models", fromlist=["Entry"]).Entry).where(
                __import__("app.models", fromlist=["Entry"]).Entry.book_id == book.id)) or 0
        print(f"账套「{book_name}」 账套 id={book.id}  期间={period.code}  分录总数={n_entries:,}")

        groups = queries.detail_ledger_groups(db, book.id, period.year)
        leaves = sum(len(g.get("leaves") or []) for g in groups)
        print(f"明细账册页单位 {len(groups)} 个，覆盖末级科目 {leaves} 个")

        gc.collect()
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        rows = queries.detail_ledger_book(db, book.id, groups, period, period,
                                          include_unposted=False)
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        print(f"出行 {len(rows):,} 行")
        print(f"峰值内存 {peak / 1024 / 1024:.1f} MB"
              f"   结束时仍持有 {(current - before) / 1024 / 1024:.1f} MB")
        print(f"平均每行 {(peak - before) / max(1, len(rows)):.0f} 字节")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
