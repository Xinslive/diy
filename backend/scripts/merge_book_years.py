"""把「一年一个账套」的旧数据合并成「一个账套多个年度」。

为什么需要这个脚本
------------------
早期模型里 book 一行 = （单位 + 账套名 + 年度），年度结转会**复制出下一个年度的
账套**：科目表复制一份、授权复制一份、期初余额再搬过去。于是同一个单位在列表里
变成两条：

    租金账套037569        2026  已结转
    租金账套037569 2027   2027  使用中

新模型里年度只是账套的一个属性，这些行应该合并成一条「租金账套037569」，
它同时拥有 2026 和 2027 的会计期间。

合并规则（刻意保守，宁可不合并也不能合错）
------------------------------------------
只有同时满足下面两条的账套才会被合并进主账套：

1. 单位名相同、账套名相同。系统自动生成的下一年度账套名字是
   ``{原名} {年份}``，所以额外接受这种「名字多一个年份后缀」的组合；
   人工起的名字不同一律不动。
2. 年度正好紧接（从账套的 start_year == 主账套的 start_year + 1）。

只动**没有凭证**的从账套 —— 有凭证的账套合并要重排 ord、搬期初，风险远大于收益，
脚本直接报错让人处理，而不是猜。

用法
----
    python scripts/merge_book_years.py            # 先看看会合并哪些
    python scripts/merge_book_years.py --apply    # 真的执行
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select, text  # noqa: E402

from app.core.db import SessionLocal, engine  # noqa: E402
from app.core.schema_version import assert_schema_ready  # noqa: E402
from app.models import Book, Period, PeriodOpening, Subject, UserBook  # noqa: E402
from app.models.book import period_ord  # noqa: E402

ROLE_RANK = {"VIEWER": 1, "ACCOUNTANT": 2, "OWNER": 3}

# 附属数据：有凭证的书表扔掉没问题，没有凭证的这些记录跟着一起扔
DEPENDENTS = ("audit_log", "report_result", "import_batch")


def _count(db, sql: str, **kw) -> int:
    return int(db.scalar(text(sql), kw) or 0)


def voucher_count(db, book_id: int) -> int:
    return _count(db, "select count(*) from voucher where book_id = :b", b=book_id)


def period_count(db, book_id: int) -> int:
    return _count(db, "select count(*) from period where book_id = :b", b=book_id)


def find_groups(db) -> list[tuple[Book, list[Book]]]:
    """找出需要合并的 (主账套, 待并入的账套列表)。主账套 = 年度最早的那个。"""
    books = list(db.scalars(select(Book).order_by(Book.start_year, Book.id)).all())
    groups: list[tuple[Book, list[Book]]] = []
    consumed: set[int] = set()

    for main in books:
        if main.id in consumed:
            continue
        donors = []
        for other in books:
            if other.id == main.id or other.id in consumed:
                continue
            same_name = other.name == main.name or other.name == f"{main.name} {other.start_year}"
            if (other.company_name == main.company_name and same_name
                    and other.start_year == main.start_year + 1):
                donors.append(other)
                consumed.add(other.id)
        if donors:
            consumed.add(main.id)
            groups.append((main, donors))
    return groups


def report(db, groups) -> None:
    if not groups:
        print("没有需要合并的账套，年度模型已经是「一个账套多个年度」。")
        return
    for main, donors in groups:
        print(f"主账套 #{main.id} 《{main.company_name} · {main.name}》{main.start_year} 年起")
        for d in donors:
            n_v = voucher_count(db, d.id)
            flag = "" if n_v == 0 else "   <- 有凭证，会被拒绝合并"
            print(f"  并入 #{d.id} 《{d.name}》{d.start_year} 年 "
                  f"{period_count(db, d.id)} 个期间 / {n_v} 张凭证{flag}")


def apply(db, groups) -> None:
    for main, donors in groups:
        for d in donors:
            n_v = voucher_count(db, d.id)
            if n_v:
                raise SystemExit(
                    f"账套 #{d.id}《{d.name}》还有 {n_v} 张凭证，自动合并不安全，请先人工处理。")
            _move_periods(db, d, main)
            _move_links(db, d, main)
            _drop_dependents(db, d)
            db.execute(delete(Subject).where(Subject.book_id == d.id))
            db.execute(delete(Book).where(Book.id == d.id))
            print(f"  已合并 #{d.id} -> #{main.id}")
    db.commit()


def _move_periods(db, donor: Book, main: Book) -> None:
    """把从账套的会计期间搬进主账套，ord 按主账套的起始年度重算。"""
    existing = {
        (p.year, p.month): p
        for p in db.scalars(select(Period).where(Period.book_id == main.id)).all()
    }
    for p in db.scalars(select(Period).where(Period.book_id == donor.id)
                        .order_by(Period.ord)).all():
        target = existing.get((p.year, p.month))
        if target is None:
            p.book_id = main.id
            p.ord = period_ord(p.year, p.month, main.start_year)
            db.flush()
            existing[(p.year, p.month)] = p
            continue
        # 同月同期已存在（重复数据）：只搬期初快照，保留已有的那个期间
        db.execute(
            text("update period_opening set period_id = :t, book_id = :b "
                 "where period_id = :p and book_id = :db"),
            {"t": target.id, "b": main.id, "p": p.id, "db": donor.id},
        )
        db.execute(delete(Period).where(Period.id == p.id))
        db.flush()


def _move_links(db, donor: Book, main: Book) -> None:
    """授权合并：同一个人两个账套都有权限时，保留权限更高的那个。"""
    have = {lk.user_id: lk for lk in
            db.scalars(select(UserBook).where(UserBook.book_id == main.id)).all()}
    for lk in db.scalars(select(UserBook).where(UserBook.book_id == donor.id)).all():
        cur = have.get(lk.user_id)
        if cur is None:
            lk.book_id = main.id
        elif ROLE_RANK.get(lk.role, 0) > ROLE_RANK.get(cur.role, 0):
            cur.role = lk.role
        else:
            db.execute(delete(UserBook).where(UserBook.id == lk.id))
    db.flush()


def _drop_dependents(db, donor: Book) -> None:
    for table in DEPENDENTS:
        db.execute(text(f"delete from {table} where book_id = :b"), {"b": donor.id})
    db.flush()


if __name__ == "__main__":
    apply_it = "--apply" in sys.argv
    # 只校验 schema 版本，**不**在这里迁移。合并年份要动的是数据，
    # 而表结构变不变由部署流程负责；让一个数据脚本顺手改生产库的表结构，
    # 出事时说不清是哪一步干的。
    assert_schema_ready()
    db = SessionLocal()
    try:
        groups = find_groups(db)
        report(db, groups)
        if apply_it and groups:
            print("\n开始合并：")
            apply(db, groups)
            print("合并完成。")
        elif groups:
            print("\n以上只是预演。确认无误后加 --apply 执行。")
    finally:
        db.close()
        engine.dispose()
