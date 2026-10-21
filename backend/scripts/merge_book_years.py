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
    """找出需要合并的 (主账套, 待并入的账套列表)。主账套 = 年度最早的那个。

    连年链（P2-04）：原来的判据是 ``other.start_year == main.start_year + 1``，
    只认**紧接主账套的下一年**。于是 2026 → 2027 → 2028 这样的链会被切成
    两组：2026 收到 2027，2028 变成一个只跟 2027 挨着但 2027 已被消费掉的
    孤儿，**永远并不进 2026**。三年以上的老数据就是这么漏的。

    现在按「年度连续 + 命名规则」把整条链收进同一组：从主账套出发，
    年份逐个往上找，找到就接着往下游推进。
    """
    books = list(db.scalars(select(Book).order_by(Book.start_year, Book.id)).all())
    groups: list[tuple[Book, list[Book]]] = []
    consumed: set[int] = set()

    def _matches(main: Book, other: Book) -> bool:
        # 名字规则：系统自动生成的下一年度账套叫 ``{原名} {年份}``。
        # 逐年推进时比对的是**最初那个**主账套的名字，不能用中间那年的名字 ——
        # 否则 2028 的「XX 2027」式命名链会在这里断掉。
        same_name = other.name == main.name or other.name == f"{main.name} {other.start_year}"
        return other.company_name == main.company_name and same_name

    for main in books:
        if main.id in consumed:
            continue
        donors: list[Book] = []
        year = main.start_year + 1
        while True:
            nxt = next(
                (b for b in books
                 if b.id not in consumed and b.start_year == year and _matches(main, b)),
                None,
            )
            if nxt is None:
                break
            donors.append(nxt)
            consumed.add(nxt.id)
            year += 1
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


def _ensure_book_year(db, book: Book, year: int) -> None:
    """主账套必须**登记过**这个年度。

    期间与科目都有组合外键钉在 ``book_year(book_id, year)`` 上
    （迁移 0032），库里不可能出现「主账套的 2027 年」却没有那一行。
    """
    from app.models import BookYear

    exists = db.scalar(
        select(BookYear).where(BookYear.book_id == book.id, BookYear.year == year)
    )
    if exists is None:
        db.add(BookYear(book_id=book.id, year=year,
                        subject_code_rule=book.subject_code_rule))
        db.flush()


def _subject_remap(db, donor: Book, main: Book) -> dict[int, int]:
    """从账套科目 id → 主账套科目 id。**按（年度, 编码）配对。**

    为什么是（年度, 编码）而不是只按编码：科目表每个年度各有一份
    （迁移 0032），2026 的 1001 与 2027 的 1001 是**两行不同的记录**。
    只按编码配对，2027 年的科目会被错指到 2026 的那一行上 ——
    于是 2027 年的期初挂到了 2026 年的科目上，账直接串了。

    两种情形：

    * 主账套**已经有**那一年的科目表 → 按编码配对到主账套**同一年度**的
      同一编码；找不到同编码就**中止**（静默丢掉一条期初等于静默改数）。
    * 主账套**还没有**那一年的科目表 → 从账套那张表**整体搬过来**
      （``book_id`` 改成主账套，年度不变）。id 不变，remap 是恒等映射。
      搬过来之后主账套就有了那一年的科目表 —— 这正是新模型的形状。
    """
    remap: dict[int, int] = {}
    missing: list[str] = []
    donor_subjects = list(
        db.scalars(select(Subject).where(Subject.book_id == donor.id)).all()
    )
    by_year: dict[int, list[Subject]] = {}
    for s in donor_subjects:
        by_year.setdefault(s.year, []).append(s)

    for year in sorted(by_year):
        _ensure_book_year(db, main, year)
        main_by_code = {
            s.code: s for s in db.scalars(
                select(Subject).where(
                    Subject.book_id == main.id, Subject.year == year)).all()
        }
        if not main_by_code:
            # 主账套还没有这一年的科目表 → 整张搬过来。
            # 父指针不用改：被搬的父子一起搬，关系原样保留。
            for s in by_year[year]:
                s.book_id = main.id
                remap[s.id] = s.id
            db.flush()
            continue
        for s in by_year[year]:
            hit = main_by_code.get(s.code)
            if hit is None:
                missing.append(f"{year} 年 {s.code} {s.name}")
                continue
            remap[s.id] = hit.id
    if missing:
        # 静默丢掉一条期初等于静默改数 —— 比合并不下去严重得多。
        raise SystemExit(
            f"账套 #{donor.id} 有 {len(missing)} 个科目在主账套 #{main.id} 里找不到同编码科目：\n"
            "  " + "\n  ".join(missing[:20])
            + "\n请先把两边科目表对齐再合并（不要靠这里硬凑）。"
        )
    return remap


def _move_periods(db, donor: Book, main: Book) -> None:
    """把从账套的会计期间搬进主账套，ord 按主账套的起始年度重算。

    顺带把期初行的 ``subject_id`` 改指到主账套的同名科目上（审计 P2-04）。
    这一步原来**漏了**，而它是这个脚本在真实数据上必然失败的原因：

    ``period_opening.subject_id`` 上的外键是 **RESTRICT**（models/book.py
    的注释里记着这条外键的分歧史），而 ``apply`` 的下一步就是
    ``delete(Subject).where(book_id == donor.id)``。原来这里只改了
    ``period_id`` / ``book_id``，``subject_id`` 还指着将从账套 —— 于是删科目
    时必然撞外键。

    跨年账套存在的意义就是搬期初，所以这个脚本恰好**只在它唯一要处理的
    那种数据上稳定失败**。事务是完整的，所以从不丢数据，只是可靠地中止。
    对照 scripts/seed_demo.py:99-101 —— 那里是先删 PeriodOpening 再删
    Subject，顺序是对的。
    """
    remap = _subject_remap(db, donor, main)

    existing = {
        (p.year, p.month): p
        for p in db.scalars(select(Period).where(Period.book_id == main.id)).all()
    }
    for p in db.scalars(select(Period).where(Period.book_id == donor.id)
                        .order_by(Period.ord)).all():
        # 组合外键：期间必须挂在一个**已登记的**年度账套上（迁移 0032）
        _ensure_book_year(db, main, p.year)
        target = existing.get((p.year, p.month))
        if target is None:
            p.book_id = main.id
            p.ord = period_ord(p.year, p.month, main.start_year)
            db.flush()
            _repoint_openings(db, p.id, p.id, main.id, remap, donor, keep_period_id=p.id)
            existing[(p.year, p.month)] = p
            continue
        # 同月同期已存在（重复数据）：只搬期初快照，保留已有的那个期间。
        # 两边都有同一个科目的期初时，**以主账套已有的为准** —— 它才是这份
        # 合并里被保留下来的那一份，从账套那份是重复。
        _repoint_openings(db, p.id, target.id, main.id, remap, donor, keep_period_id=target.id)
        db.execute(delete(Period).where(Period.id == p.id))
        db.flush()


def _repoint_openings(db, from_period_id: int, to_period_id: int, to_book_id: int,
                      remap: dict[int, int], donor: Book, *, keep_period_id: int) -> None:
    """把 ``period_opening`` 指向从账套的科目改成主账套的同名科目。

    **只能删+插，不能 UPDATE。** 库层有一条 ``trg_opening_guard``
    （db_init.sql:449）对 ``period_opening`` 的**任何** UPDATE 直接抛
    check_violation：「期初快照不可直接修改，请通过反结账流程重算」。
    那条守卫是对的 —— 期初是快照，只能重算不能就地改。所以这里走
    「删掉旧的、按新归属重新插入」，与「反结账流程重算」同一条语义。
    （一开始写成 UPDATE 的结果是脚本直接撞这条守卫报错。）

    同 ``(period_id, subject_id)`` 撞上时**跳过从账套那份**，不报错也
    **不相加**：相加会把余额凭空翻倍，那比重复更难被发现。保留主账套已有的
    那份，因为它才是合并后留下来的那一份。
    """
    rows = db.execute(
        select(PeriodOpening).where(PeriodOpening.period_id == from_period_id)
    ).scalars().all()
    if keep_period_id == from_period_id:
        # **期间整体搬过去**（目标期间就是它自己）：这几行就是「要改指的
        # 那些行」，不能拿它们当「已存在、跳过」的依据 ——
        # 拿自己当重复项，结果是每一条期初都被跳过，然后被下面的 delete
        # 删掉：期初整条丢失，而且没有任何报错。
        #
        # 这在科目表按年度分开之后才露出来：新模型下主账套**没有**那一年
        # 的科目表时，从账套那张表整体搬过来，remap 是**恒等映射** ——
        # 新旧 subject_id 相同，于是每一条都撞上「重复」。
        present: set[int] = set()
    else:
        present = {
            r[0] for r in db.execute(
                select(PeriodOpening.subject_id).where(
                    PeriodOpening.period_id == keep_period_id)
            ).all()
        }
    to_insert: list[PeriodOpening] = []
    for row in rows:
        new_subject = remap.get(row.subject_id)
        if new_subject is None:
            # _subject_remap 已经拦过「主账套没有同编码科目」，走到这里说明
            # remap 与实际数据对不上。宁可中止也不要留下指向空处的行。
            raise SystemExit(
                f"账套 #{donor.id} 的期初行 {row.id} 指向一个无法映射的科目 "
                f"(subject_id={row.subject_id})，已中止合并。")
        if new_subject in present:
            continue          # 重复数据：保主账套那份，从账套这份直接不要了
        present.add(new_subject)
        to_insert.append(PeriodOpening(
            period_id=to_period_id, book_id=to_book_id,
            subject_id=new_subject, amount=row.amount,
        ))

    if rows:
        db.execute(delete(PeriodOpening).where(
            PeriodOpening.period_id == from_period_id))
    for row in to_insert:
        db.add(row)
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
