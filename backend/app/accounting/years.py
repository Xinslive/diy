"""年度作用域：**(账套, 年度)** 这一对才是真正的记账单位。

背景
----
账套（``Book``）回答「这是谁家的哪本账」，年度（``BookYear``）回答
「这一年这本账长什么样」。**科目表、期初余额、初始化进度都是年度的属性。**
2026 年和 2027 年各有各的科目表 —— 就像挂在同一个账套名下的两个独立账套。

为什么会有这个模块
------------------
「这个科目属于哪一年」这件事，全系统有**十几处**要回答：取科目表、建凭证、
录期初、算账、算报表、打印、发票制证、插件接口……每处各写一遍
``Subject.book_id == X AND Subject.year == Y``，迟早有一处漏 —— 而漏掉的那一处
恰好就是「2027 年的凭证挂到了 2026 年的科目上」这个入口。

所以口径收在这里：**一个函数回答「这个年度的科目表」**，其余地方都调它。

依赖方向
--------
``api → accounting → ledger → models``。本模块只依赖 ``models``，
不 import ``accounting.periods``（那会成环 —— ``periods`` 要用本模块
建年度账套行）。需要 ``current_accounting_year`` 时在函数体内惰性 import。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Book, BookYear, Period, PeriodOpening, Subject


def ensure_book_year(db: Session, book: Book, year: int) -> BookYear:
    """取某个年度的年度账套行，**没有就建一行**。

    「建」这一步是幂等的：``uq_book_year`` 保证同一个 (账套, 年度) 只会有一行。
    并发建年度时靠这个唯一约束兜底 —— 撞了就当成「另一方已经建好了」，
    与 ``periods.ensure_periods`` 同一个套路。

    规则从账套继承（``book.subject_code_rule``）：账套级的是**默认**，
    有效值在年度上（见 ``BookYear.subject_code_rule`` 的注释）。
    """
    row = db.scalar(
        select(BookYear).where(BookYear.book_id == book.id, BookYear.year == year)
    )
    if row is not None:
        return row
    created = BookYear(
        book_id=book.id, year=year, subject_code_rule=book.subject_code_rule
    )
    db.add(created)
    db.flush()
    return created


def book_year_of(db: Session, book_id: int, year: int) -> Optional[BookYear]:
    """取年度账套行，没有就是 ``None``（只读路径用，不建）。"""
    return db.scalar(
        select(BookYear).where(BookYear.book_id == book_id, BookYear.year == year)
    )


def subjects_of(db: Session, book_id: int, year: int,
                include_inactive: bool = True) -> list[Subject]:
    """某个年度的**科目表**。

    默认**含停用科目**：停用的语义是「不让再选它」，不是「删掉它」——
    老凭证还指着它，账还得算得出来、还得看得见。过滤掉的话，历史余额
    在界面上直接消失，用户会以为账丢了。写路径另有闸门
    （``accounting/vouchers.py`` 建凭证时校验科目可用），这里是只读展示。
    """
    stmt = select(Subject).where(Subject.book_id == book_id, Subject.year == year)
    if not include_inactive:
        stmt = stmt.where(Subject.active.is_(True))
    return list(db.scalars(stmt.order_by(Subject.code)).all())


def subject_by_code(db: Session, book_id: int, year: int, code: str) -> Optional[Subject]:
    """按编码取某年度的科目。"""
    return db.scalar(
        select(Subject).where(
            Subject.book_id == book_id, Subject.year == year, Subject.code == code
        )
    )


def resolve_year(db: Session, book: Book, year: Optional[int]) -> int:
    """把「用户指的年度」解析成一个具体的年度。

    没指定时用**这个账套当前在记的那一年**（最近一期有凭证的期间所在的年度，
    一张凭证都没有时退回账套的启用年度）—— 与
    ``accounting.periods.current_accounting_year`` 同一个口径。

    为什么不用服务器日历年：系统的会计年度是账套的属性，而 ``CURRENT_DATE``
    是服务器当前日期加上连接会话时区（本项目把时区钉死成 UTC）。于是每年
    1 月 1 日的 8 小时窗口里，北京时间已经跨年、UTC 还没跨年；
    给一本 2026 年建的账补录 2025 年的分录时，「本年」算的也是错的年份。
    """
    if year is not None:
        return int(year)
    from app.accounting.periods import current_accounting_year

    return current_accounting_year(db, book)


def year_of_period(db: Session, period_ord: int, book_id: int) -> Optional[int]:
    """由「账套内连续期间序号」反查年度。

    ``ord`` 是跨年度连续的，所以查表是唯一可靠的办法（不能靠算术反推：
    启用月不是 1 月时算术要拿 ``start_month`` 修正，容易写错一处）。
    """
    from app.models import Period

    return db.scalar(
        select(Period.year).where(
            Period.book_id == book_id, Period.ord == period_ord
        )
    )


def copy_chart(db: Session, book_id: int, from_year: int, to_year: int) -> int:
    """把某年度的科目表**整份复制**到另一年度，返回复制的科目数。

    **为什么要复制而不是共用。** 共享意味着两个年度的科目是同一批行 ——
    那就回到了「改 2027 的科目会改到 2026」的老问题，年度隔离等于没做。

    **按编码配对，不按 id。** 复制的正是「同一套账的下一年度」，
    编码是唯一稳定的对应关系（新行是新 id，id 之间没有任何对应关系）。

    父指针要**重连到新行**：新行有新的 id，父指针必须指向新行里同编码的那一个，
    否则科目树在新年度是断的（父节点指向别的年度的科目）。

    调用方（年度结转）保证目标年度**还没有科目表**时才会调这里；
    目标年度已经有自己的科目表时**不复制**——那是用户自己导入的，
    覆盖它等于把用户刚做的工作抹掉。
    """
    # **目标年度的年度账套行必须先建出来**（``fk_subject_book_year``）。
    #
    # 组合外键 ``(book_id, year) → book_year`` 钉死了：科目行所属的年度必须
    # 先在年度账套里登记过。顺序反过来（先插科目、最后才建年度行）就会当场
    # 撞外键 —— 而报错信息是「insert or update on table subject violates
    # foreign key constraint fk_subject_book_year」，报的是科目表，指向的
    # 却是年度账套没登记，读的人要在两个模型之间来回猜。
    #
    # 所以这里先建年度行，再插科目。
    book_year = ensure_book_year(db, db.get(Book, book_id), to_year)

    src = subjects_of(db, book_id, from_year)
    if not src:
        return 0

    by_code = {s.code: s for s in src}
    created: dict[str, Subject] = {}
    count = 0
    # 按级次从浅到深建：父级必须先有 id，子级才能指过去。
    for level in range(1, max(s.level for s in src) + 1):
        for s in src:
            if s.level != level:
                continue
            new = Subject(
                book_id=book_id,
                year=to_year,
                code=s.code,
                name=s.name,
                category=s.category,
                direction=s.direction,
                parent_id=None,
                level=s.level,
                is_leaf=s.is_leaf,
                carry_forward=s.carry_forward,
                qty_calc=s.qty_calc,
                aux_calc=s.aux_calc,
                fx_calc=s.fx_calc,
                sort_no=s.sort_no,
                active=s.active,
                ext_attrs=dict(s.ext_attrs or {}),
            )
            db.add(new)
            created[s.code] = new
            count += 1
        db.flush()
        # 本级建完再连父指针：父级在上一轮已经 flush 过，id 有了。
        for s in src:
            if s.level != level or not s.parent_id:
                continue
            parent = by_code.get(
                next((p.code for p in src if p.id == s.parent_id), "")
            )
            if parent is None:
                continue
            created[s.code].parent_id = created[parent.code].id
        db.flush()

    # **第 1 步（确定科目）对目标年度就是「已完成」。**
    #
    # 初始化状态挂在**年度**上（``book_year.init_subjects_at``，迁移 0032）：
    # 这一年已经有自己的科目表了，它就是这一年的科目表 —— 这一步做完了。
    # 不标的话，向导会一直催「2027 年度还没完成初始化第 1 步」，
    # 而期初接口也会以同一条理由把这一年的期初挡下来（409）——
    # **明明有一张现成的科目表**。
    from app.accounting.initialization import mark_subjects_ready

    mark_subjects_ready(db, book_year, "CHART")
    return count


# --------------------------------------------------------------- 期初按编码搬家


def openings_of(db: Session, book_id: int,
                year: int) -> list[tuple[int, str, Decimal]]:
    """某个年度的期初快照，按 ``(period_id, 科目编码, 金额)`` 列出来。

    **要的是编码，不是科目 id。** 覆盖导入会把本年度的科目整批删掉重建 ——
    新行是新 id，原来那条期初快照指着的老 id 就不存在了。而期初的归属是
    **编码**的事，与它挂在哪一行科目上无关：1002 的期初就是 1002 的期初，
    哪怕下一张科目表里 1002 换了另一个 id。

    返回编码而不是 id，是为了让「搬到哪张表上」这件事由调用方按编码决定 ——
    编码对不上时可以拒绝，而不是默默搬到错的科目上。
    """
    rows = db.execute(
        select(PeriodOpening.period_id, Subject.code, PeriodOpening.amount)
        .join(Subject, Subject.id == PeriodOpening.subject_id)
        .join(Period, Period.id == PeriodOpening.period_id)
        .where(Period.book_id == book_id, Period.year == year)
    ).all()
    return [(r.period_id, r.code, r.amount) for r in rows]


def detach_openings(db: Session, book_id: int, year: int) -> int:
    """把某个年度的期初快照**摘下来**（删掉），返回摘下的条数。

    为什么是**摘下来**而不是就地改：``period_opening.subject_id`` 的外键是
    **RESTRICT**（审计 P1-05 / P1-10 特意选的就是 RESTRICT 而不是 CASCADE ——
    「只被期初引用、没有凭证引用」的科目一删，期初行跟着消失，余额从 0
    变成 -100，没有任何报错）。科目要删，期初就必须先走。

    而 ``fn_opening_guard`` 又挡 UPDATE（「期初快照不可直接修改」），
    所以只能删了重建。
    """
    result = db.execute(
        delete(PeriodOpening).where(
            PeriodOpening.period_id.in_(
                select(Period.id).where(
                    Period.book_id == book_id, Period.year == year)
            )
        )
    )
    return int(result.rowcount or 0)


def opening_code_gaps(snapshots: list[tuple[int, str, Decimal]],
                      codes: set[str]) -> list[dict[str, Any]]:
    """期初里有、而新科目表里**没有**的编码 —— 搬不过去的那些。

    **只有金额非零的才算问题。** 零余额的编码在新表里消失了，没有数字要搬，
    丢掉它不改变任何金额（与年度结转「余额为零不对不上不拦」同一口径）。

    非零的搬不过去就必须**拒绝**：静默丢弃的后果是「钱没了，而借贷仍然平衡，
    系统自检不会报错」—— 这正是这套系统反复强调的头号风险。
    """
    gaps: dict[str, Decimal] = {}
    for _period_id, code, amount in snapshots:
        if code in codes:
            continue
        if amount == 0:
            continue
        gaps[code] = gaps.get(code, Decimal("0")) + amount
    return [
        {"code": code, "amount": amount}
        for code, amount in sorted(gaps.items())
    ]


def attach_openings(db: Session, book_id: int,
                    snapshots: list[tuple[int, str, Decimal]],
                    by_code: dict[str, Subject]) -> int:
    """把摘下来的期初快照按编码**挂到新科目上**，返回挂回去的条数。

    编码在新表里找得到的挂回去；找得到但金额是零的也挂（保持快照完整）；
    找**不到**的跳过 —— 调用方必须先用 ``opening_code_gaps`` 把非零的挡在
    外面，到这里还出现就意味着有金额要丢。
    """
    written = 0
    for period_id, code, amount in snapshots:
        subject = by_code.get(code)
        if subject is None:
            continue
        db.add(PeriodOpening(
            book_id=book_id,
            period_id=period_id,
            subject_id=subject.id,
            amount=amount,
        ))
        written += 1
    if written:
        db.flush()
    return written


__all__ = [
    "attach_openings", "book_year_of", "copy_chart", "detach_openings",
    "ensure_book_year", "opening_code_gaps", "openings_of", "resolve_year",
    "subject_by_code", "subjects_of", "year_of_period",
]
