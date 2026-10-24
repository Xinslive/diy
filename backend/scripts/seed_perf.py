#!/usr/bin/env python
"""性能压测数据生成器 —— 按可调规模造出账套、科目、期间与凭证。

    .venv/bin/python scripts/seed_perf.py --books 1 --vouchers 5000
    .venv/bin/python scripts/seed_perf.py --profile medium
    .venv/bin/python scripts/seed_perf.py --reset

**为什么单独写一个，不复用 ``seed_demo.py``。** 演示脚本的量级是「一个
物业账套 12 个月」，几百张凭证，人看一眼就完。而 ``性能审计报告`` 里
``0011`` 那条索引交叉点的结论是在 **59,580 行分录**上实测出来的 ——
用几百行的库复现那个结论，测到的是缓存命中，不是规划器的选择。

三档 profile 的分录量（**每账套**）：

===========  =================  ==========================
档位          分录数（每账套）   用途
===========  =================  ==========================
``small``    约 10,000          贴近演示账套，只验「跑得起来」
``medium``   约 60,000          **对齐 0011 实测的 59,580 行**
``large``    约 300,000         看规模效应与索引退化
===========  =================  ==========================

**批量插入是合法的，不是抄近道。** 下面三条护栏逐条核过：

1. ``trg_entry_balance`` 是 **DEFERRABLE INITIALLY DEFERRED** 约束触发器，
   借贷平衡在 **COMMIT 时**才逐张校验 —— 所以批量插完再提交同样受保护，
   不需要每插一条查一次。
2. ``trg_entry_sync`` 在 ``BEFORE INSERT`` 把 ``entry.book_id`` /
   ``period_ord`` 从凭证反规范化过来。这里仍然显式填上正确的值：
   触发器会覆盖，但显式填让脚本在**没有触发器的库**上也能跑（不会静默
   造出一份坏数据），代价可以忽略。
3. ``fn_voucher_guard`` 挡已结账期间。这里只往**未结账**期间写，
   并把凭证直接建成 ``POSTED``（已记账），这样账簿查询走的是生产里
   99% 的那条路径 —— 大量 ``UNPOSTED`` 会让 ``include_unposted``
   两种分支走岔，测出来的数没有意义。

刻意**不**走 ``svc.create_voucher()``：那是 5000 次服务层往返
（取号 + 若干 INSERT + 校验），造 30 万分录要跑十几分钟。本脚本是
**造数工具**，不是被测路径 —— 被测的写入路径由 ``loadtest.py`` 的
写场景去打。

幂等：同名账套已存在时先按 ``wipe_book`` 的规矩删干净重建（解锁期间 +
反记账凭证 + 删凭证，见 ``seed_demo.wipe_book``），所以反复跑不会堆积。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, insert, select  # noqa: E402

from app.accounting.periods import ensure_periods  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import (  # noqa: E402
    Book, Entry, Period, PeriodOpening, Subject, SysUser, UserBook, Voucher,
)
from app.seed.chart_of_accounts import install_chart  # noqa: E402

D = Decimal

PERF_USERNAME = "perf_probe"
PERF_PASSWORD = "perf-probe-password-2026"
BOOK_PREFIX = "压测账套"

# 每档：分录数 = vouchers × entries_per_voucher
PROFILES = {
    "small": {"vouchers": 2_500, "entries_per_voucher": 4},    # 10,000
    "medium": {"vouchers": 15_000, "entries_per_voucher": 4},   # 60,000
    "large": {"vouchers": 75_000, "entries_per_voucher": 4},    # 300,000
}

# 分批提交：一批 2000 张凭证。太大则 deferred 触发器队列在 commit 时
# 一次性涌出来，内存峰值难看；太小则 commit 本身的开销占比上来。
CHUNK = 2_000


def ensure_user(db) -> SysUser:
    """压测账号直接写库建，不走 /api/auth/register。

    ``registration_enabled`` 默认 False，注册接口是关着的 —— 压测
    造数若依赖注册接口，就得先把那道闸门打开，那是在生产配置上开口子。
    """
    user = db.scalar(select(SysUser).where(SysUser.username == PERF_USERNAME))
    if user is not None:
        return user
    user = SysUser(username=PERF_USERNAME, password_hash=hash_password(PERF_PASSWORD),
                   real_name="压测账号", active=True, is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def wipe_books(db, prefix: str) -> int:
    """清掉所有压测账套。

    **必须按规矩过**，不能关掉触发器硬删 —— 与 ``seed_demo.wipe_book``
    同源的约束：
      1. ``trg_voucher_delete_guard`` 拦已结账期间的删除 → 先解锁期间
      2. ``trg_posted_voucher_guard`` 拦已记账凭证的删除     → 先反记账

    反记账（POSTED → UNPOSTED）是触发器明确放行的唯一状态跳转。
    分录随凭证级联删除，不用单独处理。
    """
    books = db.scalars(select(Book).where(Book.name.like(f"{prefix}%"))).all()
    for b in books:
        db.execute(Period.__table__.update()
                   .where(Period.book_id == b.id).values(locked=False))
        db.execute(Voucher.__table__.update()
                   .where(Voucher.book_id == b.id, Voucher.status == "POSTED")
                   .values(status="UNPOSTED"))
        db.execute(Voucher.__table__.delete().where(Voucher.book_id == b.id))
        db.execute(PeriodOpening.__table__.delete().where(PeriodOpening.book_id == b.id))
        db.execute(Period.__table__.delete().where(Period.book_id == b.id))
        db.execute(Subject.__table__.delete().where(Subject.book_id == b.id))
        db.execute(UserBook.__table__.delete().where(UserBook.book_id == b.id))
        db.execute(Book.__table__.delete().where(Book.id == b.id))
    db.commit()
    return len(books)


def leaf_subject_ids(db, book_id: int, year: int | None = None) -> list[int]:
    """账套里的**末级**科目 id，按 id 升序。

    只取末级：明细账的取数口径就是末级科目（汇总科目上的钱来自它下面），
    拿汇总科目去压测会走另一条分支，测到的不是真实路径。

    ``year`` 不传时返回**所有年度**的末级科目；给了就只取那一张科目表。
    分录必须挂在本年度的科目上（``fn_entry_subject_year_guard``），所以
    造分录时是按凭证所属年度取的那一份。
    """
    stmt = select(Subject.id).where(Subject.book_id == book_id,
                                     Subject.is_leaf.is_(True))
    if year is not None:
        stmt = stmt.where(Subject.year == year)
    return list(db.scalars(stmt.order_by(Subject.id)).all())


def widen_chart(db, book_id: int, target: int, year: int) -> int:
    """把账套的科目表扩到 ``target`` 个，返回新增数。

    **为什么必须扩。** 标准科目表只有 61 个科目，而 ``性能审计报告``
    里 ``0011`` 那条索引交叉点的结论是在**生产量级（300~500 科目）**
    上得出的。这里补一条：**科目数本身就是交叉点的输入变量** ——
    叶子铺得越开，规划器在 ``ix_entry_subject_ord``（book_id, subject_id,
    period_ord）与 ``ix_entry_book_period`` 之间做选择时的统计信息
    越不一样。拿 61 个科目的库去复核一条 300~500 科目的结论，
    复核的不是那条结论。

    扩法：**只往非末级父科目下挂末级子科目**，逐个父科目轮转。
    这恰好绕开两道护栏（见 ``db_init.sql``）：
      * ``trg_subject_leaf_guard``：新增行 is_leaf=true 且自己没有下级 —— 放行
      * ``fn_subject_leaf_parent_guard``：父科目 is_leaf 必须为 false；
        轮转只选**已有下级**的父科目，天然满足

    编码走 9xxx 段，与标准表（1xxx~6xxx）不撞；``uq_subject_book_code``
    是 (book_id, code) 唯一，靠编码前缀天然避开。

    **科目表是按年度各有一份的（迁移 0032），所以新科目必须带上 year。**
    ``Subject.year`` 是 NOT NULL，唯一约束是 ``(book_id, year, code)``，
    库层还有 ``fn_subject_parent_year_guard()`` 盯着「父子必须在同一张
    科目表上」。这一列是 0032 之后才有的，而本函数是那之前写的 ——
    新科目不填 year 就会插进 NULL，被那条触发器当场打回：

        CheckViolation: 上级科目属于 2026 年度，与本科目（<NULL> 年度）
        不在同一张科目表上

    也就是说 ``--widen-to``（**默认值就是 320**）从 0032 之后一直跑不通，
    压测基线里那句「320 科目」根本不是这条路径造出来的。本函数因此按
    **父科目自己的 year** 落库 —— 不是按账套的 start_year，因为扩出来的
    科目要挂在哪个年度的父科目下，就属于哪一张科目表。

    ``year`` 参数指定**扩哪一张**（年度）科目表。``target`` 是那一张表
    的科目数目标，不是整本账的总数 —— 科目表本来就是每年各有一份，
    界面上 ``/subjects?year=…`` 取的也是那一份。
    """
    if target <= 0:
        return 0
    have = db.scalar(select(func.count()).select_from(Subject)
                     .where(Subject.book_id == book_id, Subject.year == year)) or 0
    need = target - have
    if need <= 0:
        return 0

    # 只挑「已经有下级」的父科目 —— is_leaf 由服务端按子级关系维护，
    # 这种父科目的 is_leaf 必然是 false，正好是能挂子科目的那批。
    parents = list(db.scalars(
        select(Subject).where(Subject.book_id == book_id, Subject.year == year,
                               Subject.is_leaf.is_(False))
        .order_by(Subject.id)
    ).all())
    if not parents:
        raise RuntimeError(f"{year} 年度科目表里没有可挂子科目的父科目，无法扩充科目表")

    # 类别与余额方向成对：资产/成本是借方，其余是贷方。
    cats = [("ASSET", "DEBIT"), ("PROFIT", "CREDIT"), ("COST", "DEBIT"),
            ("LIABILITY", "CREDIT"), ("EQUITY", "CREDIT")]
    rows = []
    for i in range(need):
        parent = parents[i % len(parents)]
        category, direction = cats[i % len(cats)]
        seq = i + 1
        rows.append({
            "book_id": book_id,
            # 跟着父科目走：新科目属于父科目所在的那一张（年度）科目表。
            # 不写这一列 → 触发器 fn_subject_parent_year_guard 报错。
            "year": parent.year,
            "code": f"9{seq:03d}",
            "name": f"压测扩展科目{seq}",
            "parent_id": parent.id,
            "level": (parent.level or 1) + 1,
            "category": category,
            "direction": direction,
            "is_leaf": True,
            "carry_forward": False,
            "qty_calc": False, "aux_calc": False, "fx_calc": False,
            "sort_no": seq,
            "active": True,
            "ext_attrs": {},
        })
        if len(rows) >= 1_000:
            db.execute(insert(Subject), rows)
            rows = []
    if rows:
        db.execute(insert(Subject), rows)
    db.commit()
    return need


def build_book(db, user: SysUser, name: str, year: int, years: int,
               widen_to: int = 0) -> Book:
    book = Book(company_name=f"{name}有限公司", name=name,
                start_year=year, start_month=1)
    db.add(book)
    db.flush()
    for y in range(year, year + years):
        ensure_periods(db, book, y)
        # **科目表每个年度各有一份（迁移 0032）**，所以每个铺了期间的年度
        # 都要装一张自己的科目表。只装 start_year 那一张的话，铺到下一年的
        # 凭证就没有科目可挂 —— 库层 ``fn_entry_subject_year_guard()`` 会报
        # 「分录的科目属于 2026 年度，不能记在 2027 年度的凭证上」。
        install_chart(db, book.id, y)
        added = widen_chart(db, book.id, widen_to, y)
        if added:
            print(f"  {y} 年度科目表扩充: +{added} 个（目标 {widen_to}）")
    db.add(UserBook(user_id=user.id, book_id=book.id, role="OWNER"))
    db.commit()
    return book


def seed_vouchers(db, book: Book, subjects_by_year: dict[int, list[int]],
                  periods: list[Period],
                  n_vouchers: int, entries_per: int, year: int) -> tuple[int, int]:
    """批量写入 n_vouchers 张已记账凭证，返回 (凭证数, 分录数)。

    **借贷必须平**，否则 ``trg_entry_balance`` 在 commit 时把整批打回。
    这里用最省事的写法：一半借一半贷、金额相同。

    分录数取偶数：奇数条无法在「同额对冲」下配平。与其在这里
    特判「最后一条补差」，不如让调用方传偶数并在下面 assert ——
    构造出来的**假平**数据会让账簿聚合的数对不上，排查成本远高于
    这里多一句断言。

    ``subjects_by_year`` 是「年度 → 该年度科目表里的末级科目 id」。
    **必须按年度分池**：科目表每个年度各有一份（迁移 0032），分录只能挂
    在本年度科目上（``fn_entry_subject_year_guard``）—— 拿 2026 的科目去记
    2027 的凭证，库层会直接打回。
    """
    assert entries_per % 2 == 0, "entries_per 必须是偶数，否则无法配平借贷"
    assert subjects_by_year, "账套里没有末级科目"
    empty = [y for y, ids in subjects_by_year.items() if not ids]
    assert not empty, f"这些年度没有末级科目可用：{empty}"

    n_vouchers = min(n_vouchers, len(periods) * 100_000)
    made = 0
    total_entries = 0
    half = entries_per // 2

    while made < n_vouchers:
        batch_n = min(CHUNK, n_vouchers - made)
        # 凭证号在账套内单调递增。库上的约束是 uq_voucher_period_no
        # （period_id + voucher_no 唯一），全局单调自然满足它。
        vrows = []
        for i in range(batch_n):
            idx = made + i
            period = periods[idx % len(periods)]
            month = period.month
            day = min(28, 1 + (idx % 28))
            vrows.append({
                "book_id": book.id,
                "period_id": period.id,
                "voucher_no": f"记-{idx + 1:08d}",
                "voucher_date": date(period.year, month, day),
                "status": "POSTED",
                "source": "MANUAL",
                "attachments": 0,
                "remark": f"压测凭证 {idx + 1}",
                "created_by": PERF_USERNAME,
                "posted_by": PERF_USERNAME,
                "ext_attrs": {},
            })
        db.execute(insert(Voucher), vrows)
        db.flush()

        vids = list(db.scalars(
            select(Voucher.id).where(Voucher.book_id == book.id,
                                     Voucher.voucher_no.in_([r["voucher_no"] for r in vrows]))
            .order_by(Voucher.voucher_no)
        ).all())
        if len(vids) != batch_n:
            db.rollback()
            raise RuntimeError(f"写入 {batch_n} 张凭证却只读回 {len(vids)} 个 id")

        ord_by_id = {p.id: p.ord for p in periods}
        erows = []
        for j, vid in enumerate(vids):
            period = periods[(made + j) % len(periods)]
            base = made + j
            amount = D("100.00") + D(base % 900)
            # 本张凭证所属年度的末级科目池 —— 科目表每年各有一份，
            # 挂错年度的科目会被 fn_entry_subject_year_guard 打回。
            pool = subjects_by_year[period.year]
            # 科目按全局序号轮转：让同一个科目在整库里均匀铺开，
            # 这样多科目筛选才能复现 0011 记的那个交叉点。
            for k in range(entries_per):
                erows.append({
                    "voucher_id": vid,
                    "book_id": book.id,           # trg_entry_sync 会覆盖，显式填只为可读
                    "period_ord": ord_by_id[period.id],
                    "line_no": k + 1,
                    "subject_id": pool[(base * entries_per + k) % len(pool)],
                    "direction": "DEBIT" if k < half else "CREDIT",
                    "amount": amount,
                    "summary": f"压测分录 {base}-{k + 1}",
                    "aux_ref": {},
                })
        db.execute(insert(Entry), erows)
        made += batch_n
        total_entries += len(erows)
        db.commit()

    return made, total_entries


def main() -> int:
    p = argparse.ArgumentParser(description="压测数据生成器")
    p.add_argument("--profile", choices=sorted(PROFILES), default="medium",
                   help="数据量档位（默认 medium ≈ 6 万分录/账套）")
    p.add_argument("--books", type=int, default=1, help="账套数（默认 1）")
    p.add_argument("--vouchers", type=int, default=None,
                   help="覆盖档位：每账套凭证数")
    p.add_argument("--entries-per-voucher", type=int, default=None,
                   help="覆盖档位：每张凭证分录数（必须为偶数）")
    p.add_argument("--years", type=int, default=2, help="铺几个年度的会计期间（默认 2）")
    p.add_argument("--year", type=int, default=2026, help="起始年度（默认 2026）")
    p.add_argument("--widen-to", type=int, default=320,
                   help="把科目表扩到多少个（默认 320，对齐审计提到的生产量级 300~500；"
                        "传 0 保持标准 61 个科目表）")
    p.add_argument("--reset", action="store_true", help="先删掉已有压测账套再生成")
    p.add_argument("--keep", action="store_true",
                   help="账套已存在时不删，直接跳过（用于反复造数时只补新账套）")
    args = p.parse_args()

    cfg = PROFILES[args.profile]
    n_vouchers = args.vouchers or cfg["vouchers"]
    entries_per = args.entries_per_voucher or cfg["entries_per_voucher"]

    if args.entries_per_voucher is not None and args.entries_per_voucher % 2 != 0:
        print("❌ --entries-per-voucher 必须是偶数（借贷要配平）")
        return 1

    db = SessionLocal()
    try:
        user = ensure_user(db)

        if args.reset:
            n = wipe_books(db, BOOK_PREFIX)
            print(f"已清理 {n} 个压测账套")
        if args.keep:
            existing = db.scalar(
                select(func.count()).select_from(Book).where(Book.name.like(f"{BOOK_PREFIX}%"))
            ) or 0
            if existing:
                print(f"已存在 {existing} 个压测账套，--keep 生效，跳过生成")
                return 0

        t_all = time.time()
        grand_v = grand_e = 0
        for b in range(args.books):
            name = f"{BOOK_PREFIX}{b + 1:02d}"
            t0 = time.time()
            book = build_book(db, user, name, args.year, args.years,
                              widen_to=args.widen_to)
            # **按年度分池**：科目表每个年度各有一份（迁移 0032），
            # 造分录时必须用凭证所属年度的那一份。
            subjects_by_year = {
                y: leaf_subject_ids(db, book.id, y)
                for y in range(args.year, args.year + args.years)
            }
            subjects = [sid for ids in subjects_by_year.values() for sid in ids]
            periods = list(db.scalars(
                select(Period).where(Period.book_id == book.id).order_by(Period.ord)
            ).all())
            t_build = time.time() - t0

            t1 = time.time()
            nv, ne = seed_vouchers(db, book, subjects_by_year, periods,
                                   n_vouchers, entries_per, args.year)
            t_seed = time.time() - t1
            grand_v += nv
            grand_e += ne

            size = db.scalar(select(func.pg_total_relation_size("entry"))) or 0
            print(f"账套 {name}: 科目 {len(subjects)} / 期间 {len(periods)} / "
                  f"凭证 {nv} / 分录 {ne} "
                  f"(建账 {t_build:.1f}s, 写入 {t_seed:.1f}s)")

        print(f"✅ 完成: {args.books} 个账套, {grand_v} 张凭证, {grand_e} 条分录, "
              f"耗时 {time.time() - t_all:.1f}s")
        print(f"   账号: {PERF_USERNAME} / {PERF_PASSWORD}")
        print(f"   账套名: {BOOK_PREFIX}01 … {BOOK_PREFIX}{args.books:02d}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
