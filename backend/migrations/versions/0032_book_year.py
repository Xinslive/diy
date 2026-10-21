"""年度账套（book_year）：把科目表从**账套级**下沉到**年度级**。

Revision ID: 0032_book_year
Revises: 0031_subject_guard_year
Create Date: 2026-10-21

**这一条改的是什么。**

旧模型里科目表是账套级的：``subject`` 只有 ``book_id``，唯一键
``(book_id, code)``，整个账套一份。于是：

* 2026 年和 2027 年**共用**同一批科目行；
* 「覆盖导入科目表」是「删掉账套的全部科目再重建」；
* 于是「2026 年记了账 → 2027 年导入科目表被拒」成了必然 ——
  闸门是账套级计数（``initialization.subject_import_state``），
  而它这么写不是作者忘了加年，是**当时只能这么写**：真放行 2027 的导入，
  删掉的就是 2026 正在用的那批科目行，2026 的凭证全部指向被删的科目
  （``entry.subject_id`` 外键），账当场烂掉。

新模型把科目表下沉到年度：``subject.year`` + 唯一键
``(book_id, year, code)``，**每个年度一份独立的科目表**。
2026 和 2027 的「1002 银行存款」是两行不同的记录、两个 id。

**「年度账套」（``book_year``）是什么。** 账套回答「这是谁家的哪本账」，
年度账套回答「这一年这本账长什么样」—— 科目表、期初余额、初始化进度
都是**年度**的属性。账套本身仍然只有一个：名字、单位、授权、
账套列表里的一行。权限仍是账套级的。

**存量数据怎么归位。**

1. 每个账套的每个年度建一行 ``book_year``。
2. 原来那份科目表归给**锚年**：账套里**有业务数据的最早年度**
   （有分录或期初快照），一张都没记的退回账套的启用年度。
   —— 科目必须跟着「真正在用它的那些分录」走，否则那些分录会指向
   别的年度的科目。
3. 其余年度**克隆**一份科目表（原样复制，父指针重连到新行），
   并把该年度的分录、期初快照**重指**到克隆出来的行。
4. **把跨年的期初承接补上。** 旧模型里期初是推导值，跨年自动滚
   （引擎从账套首期一路推下去）。新模型里跨年是硬边界，
   期初必须是一条显式快照 —— 否则「次年 1 月的期初」会变成 0，
   而这**不是**新模型的语义，是数据搬家的副作用：账面数字会变，
   而且变的是没人看见的地方。
   所以这里按年度顺序把上一年的期末余额写成下一年的期初快照
   （就是年度结转干的那件事，用**同一份引擎**算，数字与旧模型逐分对齐）。

**为什么第 4 步要按顺序。** 第 N 年的期初来自第 N-1 年的期末，
而第 N-1 年的期末又要看第 N-1 年的期初 —— 顺序反了会算出 0。
所以从锚年开始一年一年往下推。

**降级。** 这一条**不回退**：旧模型是「已知不满足需求」的那一个
（用户要的正是「每个年度有自己的科目表」），回退等于把已知缺陷装回去。
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import func, select, text

revision = "0032_book_year"
down_revision = "0031_subject_guard_year"
branch_labels = None
depends_on = None


def _ddl() -> None:
    """建 ``book_year`` 表、给 ``subject`` 加 ``year`` 列。

    纯 DDL，不动数据 —— 数据搬运在 ``_migrate_data`` 里。
    """
    op.execute("""
        CREATE TABLE IF NOT EXISTS book_year (
            id          serial PRIMARY KEY,
            book_id     integer NOT NULL REFERENCES book(id) ON DELETE CASCADE,
            year        integer NOT NULL,
            subject_code_rule varchar(32) NOT NULL DEFAULT '4-2-2-2',
            init_subjects_at   TIMESTAMP WITH TIME ZONE,
            init_subjects_source varchar(16),
            init_openings_at   TIMESTAMP WITH TIME ZONE,
            init_openings_source varchar(16),
            created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            updated_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            CONSTRAINT uq_book_year UNIQUE (book_id, year)
        )
    """)
    op.execute("ALTER TABLE subject ADD COLUMN IF NOT EXISTS year integer")


def _drop_legacy_code_key() -> None:
    """把旧唯一键 ``(book_id, code)`` 拆掉 —— **必须早于克隆**。

    旧键的语义是「一个账套一份科目表」：同一个账套里编码不能重复。
    而新模型下同编码要**按年度各有一份**（2026 的 1001 与 2027 的 1001
    是两行、两码）。克隆科目表干的就是「在同一账套里插同编码的新行」——
    旧键还在的话这一步必然撞 UniqueViolation。

    换键本身在 ``_finalize`` 里做（加新键 ``(book_id, year, code)``）。
    但**删旧键必须提前**：新键要等数据归位完才能加 —— 加早了会把
    「还没归位的行」也算进唯一性判定（那时 ``year`` 还是 NULL）。

    同一事务内完成：迁移失败回滚，旧键原样还在。
    """
    op.execute("ALTER TABLE subject DROP CONSTRAINT IF EXISTS uq_subject_book_code")


def _set_trigger_enabled(table: str, trigger: str, enabled: bool) -> None:
    """开/关一条**普通**触发器。

    ``ALTER TABLE ... DISABLE TRIGGER`` 没有 ``IF EXISTS`` 这种写法 ——
    触发器不存在时它直接报错。而迁移必须能在「这条触发器还没装上」的
    库上跑（从 0001 起重放整条链时，0001 不装它），所以先问一句。

    判存在用 ``pg_trigger`` 配 ``to_regclass``：后者走的是与
    ``ALTER TABLE <table>`` **同一套 search_path 解析**，两边的
    ``entry`` 必然是同一张表（迁移测试用独立 schema 当「另一个数据库」，
    正是在那儿踩过 ``information_schema`` 看得见同名表的坑）。
    """
    exists = op.get_bind().execute(
        text("SELECT 1 FROM pg_trigger "
             "WHERE tgrelid = to_regclass(:t) AND tgname = :n AND NOT tgisinternal"),
        {"t": table, "n": trigger}).scalar()
    if not exists:
        return
    op.execute(
        f"ALTER TABLE {table} {'ENABLE' if enabled else 'DISABLE'} TRIGGER {trigger}")


def _migrate_data() -> None:
    """把存量数据归到年度上：登记年度、克隆科目表、重指分录与期初、补跨年期初。"""
    from app.core.db import SessionLocal

    # **先拆旧唯一键**，理由见 ``_drop_legacy_code_key``。
    _drop_legacy_code_key()

    db = SessionLocal(bind=op.get_bind())
    try:
        from app.models import Book

        for book in list(db.scalars(select(Book)).all()):
            _migrate_book(db, book)
        db.commit()
    finally:
        db.close()


def _migrate_book(db, book) -> None:
    from app.models import BookYear, Period, PeriodOpening, Subject, Voucher

    years = [
        int(y) for y in db.scalars(
            select(Period.year).where(Period.book_id == book.id).distinct()
            .order_by(Period.year)
        ).all()
    ]
    if not years:
        # 没有会计期间的账套（建了没初始化）：给它登记启用年度，
        # 否则它的科目表无处安放。
        years = [int(book.start_year)]

    # ---- 1. 登记年度账套行 ----
    for year in years:
        exists = db.scalar(
            select(BookYear).where(BookYear.book_id == book.id, BookYear.year == year)
        )
        if exists is None:
            db.add(BookYear(book_id=book.id, year=year,
                            subject_code_rule=book.subject_code_rule))
    db.flush()

    # ---- 1b. 已经搬过一次了吗？----
    #
    # **这一条迁移必须能重复执行。** 仓库里 0005 就立过这条纪律
    # （``test_0005_重复执行不改变结构``），而迁移链的测试更狠：
    # 把 ``alembic_version`` 打回 0004 再重跑一遍整条链 —— 于是每一条
    # revision 都要经得起「在同一份数据上再跑一次」。
    #
    # 判据是**科目表有没有归位**：``subject.year`` 刚加时全是 NULL，
    # 搬完之后没有 NULL 的了。第二次跑就停在这里，
    # 不会把已经克隆出来的 2027 科目表又搬回 2026（那会撞唯一键）。
    unassigned = db.scalar(
        select(func.count(Subject.id)).where(
            Subject.book_id == book.id, Subject.year.is_(None))
    ) or 0
    if not unassigned:
        return

    # ---- 2. 锚年 = 真正在用这份科目表的年度 ----
    voucher_years = {
        int(y) for y in db.scalars(
            select(Period.year).join(Voucher, Voucher.period_id == Period.id)
            .where(Period.book_id == book.id).distinct()
        ).all()
    }
    opening_years = {
        int(y) for y in db.scalars(
            select(Period.year).join(PeriodOpening, PeriodOpening.period_id == Period.id)
            .where(PeriodOpening.book_id == book.id).distinct()
        ).all()
    }
    data_years = voucher_years | opening_years
    anchor = min(data_years) if data_years else years[0]

    # ---- 3. 原科目表归锚年 ----
    #
    # 只取**还没归位**的（year IS NULL）—— 不是「这个账套的全部科目」。
    # 上一次跑剩下的半截（year 一半有值一半没值）时，已经归位的那半
    # 不该再被搬一次。
    originals = list(db.scalars(
        select(Subject).where(
            Subject.book_id == book.id, Subject.year.is_(None))
        .order_by(Subject.level, Subject.code)
    ).all())
    for s in originals:
        s.year = anchor
    db.flush()

    # ---- 4. 其余年度克隆一份 ----
    for year in years:
        if year == anchor:
            continue
        _clone_chart(db, book, originals, year)
    db.flush()

    # ---- 5. 初始化状态归位 ----
    _move_init_state(db, book, years, anchor, data_years)

    # ---- 6. 补跨年期初（顺序推） ----
    _carry_openings(db, book, years)


def _clone_chart(db, book, originals, year: int) -> None:
    """把原科目表克隆到某个年度，并把该年度的分录/期初重指到克隆行。"""
    from app.models import Entry, Period, PeriodOpening, Subject

    if not originals:
        return
    by_id = {s.id: s for s in originals}
    # 按级次从浅到深建：父级必须先有 id，子级才能指过去。
    created: dict[str, Subject] = {}
    for level in range(1, max(s.level for s in originals) + 1):
        for src in originals:
            if src.level != level:
                continue
            new = Subject(
                book_id=book.id,
                year=year,
                code=src.code,
                name=src.name,
                category=src.category,
                direction=src.direction,
                parent_id=None,
                level=src.level,
                is_leaf=src.is_leaf,
                carry_forward=src.carry_forward,
                qty_calc=src.qty_calc,
                aux_calc=src.aux_calc,
                fx_calc=src.fx_calc,
                sort_no=src.sort_no,
                active=src.active,
                ext_attrs=dict(src.ext_attrs or {}),
            )
            db.add(new)
            created[src.code] = new
        db.flush()
        for src in originals:
            if src.level != level or not src.parent_id:
                continue
            parent_src = by_id.get(src.parent_id)
            if parent_src is None:
                continue
            created[src.code].parent_id = created[parent_src.code].id
        db.flush()

    # ---- 重指该年度的分录 ----
    #
    # 「已结账期间禁写」那条触发器挡的是**改业务数据**，而这里搬的是
    # **引用**（subject_id 从旧行指向同编码的新行），一个金额都不改。
    # 触发器看不见这个区别，所以迁移期间临时关掉它，搬完立刻装回去。
    #
    # **只在真要改分录时才关。** 关了再开是一次 ALTER TABLE，而 ALTER
    # TABLE 要求这张表上没有挂起的延迟触发器事件 —— 分录一改，延迟到
    # 提交时才判的余额校验就排队了。所以顺序是：改完先结算（SET
    # CONSTRAINTS ALL IMMEDIATE），再把触发器装回去。
    # 没有分录要改的时候（本迁移的常态：次年没有凭证）就一步都别动 ——
    # 「没打算改就不去改触发器」比「改了再改回来」少一个失败点。
    ords = list(db.scalars(
        select(Period.ord).where(Period.book_id == book.id, Period.year == year)
    ).all())
    rows = []
    if ords:
        rows = db.execute(
            select(Entry.id, Entry.subject_id)
            .where(Entry.book_id == book.id, Entry.period_ord.in_(ords))
        ).all()
    if rows:
        _set_trigger_enabled("entry", "trg_entry_lock_guard", False)
        try:
            for entry_id, subject_id in rows:
                src = by_id.get(subject_id)
                hit = created.get(src.code) if src is not None else None
                if hit is None:
                    continue
                db.execute(
                    text("UPDATE entry SET subject_id = :sid WHERE id = :eid"),
                    {"sid": hit.id, "eid": entry_id},
                )
            db.flush()
            # 结算挂起的延迟事件：余额校验是「延迟到提交时才判」的，
            # 而 ALTER TABLE 不接受还有未决事件的表。这里结算等于把
            # 校验提前到现在做 —— 金额一个没动，平不平与迁移前同解。
            db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        finally:
            _set_trigger_enabled("entry", "trg_entry_lock_guard", True)

    # ---- 重指该年度的期初快照 ----
    #
    # **只能「删了重建」，不能就地 UPDATE。** ``period_opening`` 上有
    # 一条 ``trg_opening_guard``（BEFORE UPDATE）：期初是快照，
    # 不可直接修改，要改只能走反结账流程重算。UPDATE 会被它当场挡下
    # （check_violation「期初快照不可直接修改」）。
    #
    # 应用层改期初走的是同一条路：``years.detach_openings`` 把快照
    # 摘下来、换成新科目表、``years.attach_openings`` 再按编码挂回去。
    # 这里与它对齐：删掉旧的、按新归属重新插入。
    #
    # 归属按**编码**算：期初属于「某个编码在某个年度的余额」，与它
    # 挂在哪一行科目上无关 —— 2022 年 1001 的期初，该挂在 2022 年
    # 那张表里的 1001 上。
    opening_rows = db.execute(
        select(PeriodOpening.id, PeriodOpening.period_id,
               PeriodOpening.subject_id, PeriodOpening.amount,
               PeriodOpening.created_at)
        .join(Period, Period.id == PeriodOpening.period_id)
        .where(Period.book_id == book.id, Period.year == year)
    ).all()
    for opening_id, period_id, subject_id, amount, created_at in opening_rows:
        src = by_id.get(subject_id)
        hit = created.get(src.code) if src is not None else None
        if hit is None:
            # 编码对不上（不该发生：克隆是整张表照搬的）——
            # 保留原样，绝不静默丢掉一条有金额的期初。
            continue
        db.execute(
            text("DELETE FROM period_opening WHERE id = :oid"), {"oid": opening_id})
        db.execute(
            text("""
                INSERT INTO period_opening
                    (book_id, period_id, subject_id, amount, created_at, updated_at)
                VALUES (:b, :p, :s, :a, :c, now())
            """),
            {"b": book.id, "p": period_id, "s": hit.id,
             "a": amount, "c": created_at},
        )
    db.flush()


def _move_init_state(db, book, years, anchor: int, data_years: set[int]) -> None:
    """把账套级的初始化状态搬到年度上。

    * 锚年：原样搬过去（那就是「这套账初始化到哪一步了」的答案）。
    * 其余年度：科目表是继承来的（CHART）；有期初快照的（第 2 步 IMPORT），
      没有的留空 —— 空年度的期初该由用户自己确认或录入，
      迁移不替他们做这个决定（留空正是「待初始化」该有的样子）。
    """
    from app.models import BookYear

    # 账套级那两列在 ORM 里已经拿掉了（状态搬到年度上了），
    # 这里用原生 SQL 读一次 —— 它是这一条迁移要搬走的最后一份数据。
    book_row = db.execute(text(
        "SELECT init_subjects_at, init_subjects_source, "
        "init_openings_at, init_openings_source FROM book WHERE id = :b"
    ), {"b": book.id}).mappings().first()
    book_init = dict(book_row) if book_row else {}

    for year in years:
        row = db.scalar(
            select(BookYear).where(BookYear.book_id == book.id, BookYear.year == year)
        )
        if row is None:
            continue
        if year == anchor:
            row.init_subjects_at = book_init.get("init_subjects_at")
            row.init_subjects_source = book_init.get("init_subjects_source")
            row.init_openings_at = book_init.get("init_openings_at")
            row.init_openings_source = book_init.get("init_openings_source")
        else:
            if row.init_subjects_at is None:
                row.init_subjects_at = book_init.get("init_subjects_at")
                row.init_subjects_source = book_init.get("init_subjects_source") or "CHART"
            if year in data_years and row.init_openings_at is None:
                row.init_openings_at = book_init.get("init_openings_at")
                row.init_openings_source = book_init.get("init_openings_source") or "IMPORT"
    db.flush()


def _carry_openings(db, book, years) -> None:
    """把「次年 1 月的期初 = 上年 12 月的期末」补成显式快照。

    旧模型里期初是推导值，跨年自动滚；新模型里跨年是硬边界，
    期初必须是一条快照。**不补的话，搬完家之后次年的期初会变成 0** ——
    账面数字变了，而且变在没人看见的地方。

    用**引擎**算，而不是自己写一套求和：引擎是全系统唯一的算账逻辑
    （铁律 2），它算出来的数与旧模型逐分一致 —— 旧模型也是它算的。
    """
    from app.accounting.years import subjects_of
    from app.ledger.engine import LedgerEngine
    from app.models import Period, PeriodOpening

    ordered = sorted(int(y) for y in years)
    if len(ordered) < 2:
        return

    for idx, year in enumerate(ordered):
        if idx == 0:
            continue
        prev_year = ordered[idx - 1]
        first = db.scalar(
            select(Period).where(Period.book_id == book.id, Period.year == year)
            .order_by(Period.ord).limit(1)
        )
        if first is None:
            continue
        # 已经有快照的（老模型里年度结转写下的，或用户手工录的）不动。
        has_snapshot = db.scalar(
            select(func.count(PeriodOpening.id)).where(PeriodOpening.period_id == first.id)
        )
        if has_snapshot:
            continue
        prev_last = db.scalar(
            select(Period).where(Period.book_id == book.id, Period.year == prev_year)
            .order_by(Period.ord.desc()).limit(1)
        )
        if prev_last is None:
            continue
        src_subjects = subjects_of(db, book.id, prev_year)
        dst_subjects = {s.code: s for s in subjects_of(db, book.id, year)}
        if not src_subjects or not dst_subjects:
            continue
        # 引擎是**只读**的：写完快照要另起一个实例，否则拿到的是写前的数。
        eng = LedgerEngine(db, book.id)
        fmap = eng.compute([s.id for s in src_subjects], prev_last.ord)
        for s in src_subjects:
            amount = fmap[s.id].closing
            if amount == 0:
                continue
            dst = dst_subjects.get(s.code)
            if dst is None:
                # 编码对不上：新年度的科目表里没有这个编码。
                # 迁移阶段**跳过**（不静默丢弃，只是不搬），
                # 由用户在界面上看到差额后自己处理。
                continue
            db.add(PeriodOpening(book_id=book.id, period_id=first.id,
                                 subject_id=dst.id, amount=amount))
        db.flush()


def _add_constraint_if_missing(table: str, name: str, definition: str) -> None:
    """约束不存在才加 —— **重复执行不变**。

    ``ALTER TABLE ... ADD CONSTRAINT`` 没有 ``IF NOT EXISTS``，
    同一条迁移再跑一次就是一条 ``DuplicateTable``，整条链断在半路。
    迁移链的测试会把 ``alembic_version`` 打回 0004 再重跑整条链
    （见 ``tests/test_migration_guardrails.py``），所以每一条 revision
    都得受得住「在同一份数据上再跑一次」。

    判存在用 ``pg_constraint`` 配 ``to_regclass``：后者走的是与
    ``ALTER TABLE <table>`` **同一套 search_path 解析**，两边的
    ``subject`` 必然是同一张表 —— 不用 ``information_schema`` 是因为
    它看得见所有 schema 下同名但不同样的表（迁移测试用独立 schema
    当「另一个数据库」，正是在那儿踩过）。
    """
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = to_regclass('{table}') AND conname = '{name}'
            ) THEN
                EXECUTE 'ALTER TABLE {table} ADD CONSTRAINT {name} {definition}';
            END IF;
        END $$;
    """)


def _finalize() -> None:
    """收口：NOT NULL、唯一键、组合外键、删掉账套级的初始化列。"""
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM subject WHERE year IS NULL) THEN
                RAISE EXCEPTION 'subject.year 仍有空值，拒绝收口';
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE subject ALTER COLUMN year SET NOT NULL")
    op.execute("ALTER TABLE subject DROP CONSTRAINT IF EXISTS uq_subject_book_code")
    _add_constraint_if_missing(
        "subject", "uq_subject_book_year_code", "UNIQUE (book_id, year, code)")
    op.execute("DROP INDEX IF EXISTS ix_subject_book_tree")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_subject_book_year_tree
            ON subject (book_id, year, parent_id, sort_no)
    """)
    # 组合外键：科目与期间都只能落在**已登记的年度**上。
    _add_constraint_if_missing(
        "period", "fk_period_book_year",
        "FOREIGN KEY (book_id, year) REFERENCES book_year (book_id, year) "
        "ON DELETE CASCADE")
    _add_constraint_if_missing(
        "subject", "fk_subject_book_year",
        "FOREIGN KEY (book_id, year) REFERENCES book_year (book_id, year) "
        "ON DELETE CASCADE")
    # 账套级的初始化状态已经搬到年度上，这两列不再有人写。
    op.execute("ALTER TABLE book DROP COLUMN IF EXISTS init_subjects_source")
    op.execute("ALTER TABLE book DROP COLUMN IF EXISTS init_subjects_at")
    op.execute("ALTER TABLE book DROP COLUMN IF EXISTS init_openings_source")
    op.execute("ALTER TABLE book DROP COLUMN IF EXISTS init_openings_at")


def upgrade() -> None:
    _ddl()
    _migrate_data()
    _finalize()


def downgrade() -> None:
    """**不回退。** 理由见模块 docstring：旧模型是「已知不满足需求」的那一个。

    真的回退要丢数据（年度之间拆开的科目表没法自动合回去，
    而合并脚本当初就是因为「有凭证的账套合并要重排 ord、搬期初，
    风险远大于收益」而直接拒绝合并的）。所以这里什么都不做。
    """
    pass
