"""整理凭证：断号检查、重新排序、重新编号。

会计软件里「整理凭证」通常包含三件事，本模块把它们拆成可预览的两步操作：

1. ``GAPS``   —— 只体检：报告断号、空号、号段异常，不改任何数据。
2. ``RENUM``  —— 保持当前顺序，把号段压紧成 0001..N（补断号）。
3. ``SORT_DATE`` / ``SORT_DATE_DESC`` —— 先按日期排序，再压紧编号。

设计取舍
--------
* **两阶段返回**：``plan_organize`` 只算不改，``apply_organize`` 才落库。
  财务改号影响已经打印出来的纸质凭证，必须先看清差异再确认。
* **临时号避让**：``uq_voucher_period_no`` 是即时唯一约束，直接改号会在交换
  （记0001 <-> 记0002）时撞库。因此先整体写到临时号并 flush，再写最终号。
* **系统凭证不参与**：结转损益凭证（source=CARRY_FORWARD）由月末任务生成，
  固定排在最后，重排时保持不动。
* **按期间分组**：整理范围可以是单个期间，也可以是整个账套（自动按期间分组），
  不会把凭证号排到别的期间去。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log, voucher_snapshot
from app.accounting.util import period_label
from app.models import Period, SysUser, Voucher

# 凭证号形如「记0001」，前缀是 1~2 个汉字，序号是数字
NO_PATTERN = re.compile(r"^(?P<prefix>[^\d]{1,4})(?P<seq>\d+)$")
PARK_PREFIX = "∥"  # ∥ 临时占位前缀，保证永不与真实凭证号冲突

STRATEGIES = ("GAPS", "RENUM", "SORT_DATE", "SORT_DATE_DESC")


@dataclass
class Change:
    """一张凭证的改号前后对照。"""

    voucher_id: int
    old_no: str
    new_no: str
    voucher_date: str
    status: str
    source: str

    @property
    def changed(self) -> bool:
        return self.old_no != self.new_no


@dataclass
class PeriodPlan:
    period_id: int
    label: str
    changes: list[Change] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "period_id": self.period_id,
            "period": self.label,
            "changes": [
                {
                    "voucher_id": c.voucher_id,
                    "old_no": c.old_no,
                    "new_no": c.new_no,
                    "voucher_date": c.voucher_date,
                    "status": c.status,
                    "source": c.source,
                    "changed": c.changed,
                }
                for c in self.changes
            ],
            "gaps": self.gaps,
            "skipped": self.skipped,
            "changed_count": sum(1 for c in self.changes if c.changed),
        }


# --------------------------------------------------------------------- 解析


def split_no(voucher_no: str) -> tuple[str, int]:
    """拆出 (前缀, 序号)。解析不了时返回 (原串, 0) 以便原样保留。"""
    m = NO_PATTERN.match((voucher_no or "").strip())
    if not m:
        return (voucher_no or "").strip(), 0
    return m.group("prefix"), int(m.group("seq"))


def _sort_key(voucher: Voucher, strategy: str):
    if strategy == "SORT_DATE_DESC":
        return (-voucher.voucher_date.toordinal(), voucher.id)
    if strategy == "SORT_DATE":
        return (voucher.voucher_date.toordinal(), voucher.id)
    return (voucher.id,)  # RENUM 保持当前存储顺序


# --------------------------------------------------------------------- 体检


def check_gaps(db: Session, period: Period, include_system: bool = True) -> dict:
    """单期间断号检查。不改数据。"""
    stmt = select(Voucher).where(Voucher.period_id == period.id)
    if not include_system:
        stmt = stmt.where(Voucher.source != "CARRY_FORWARD")
    rows = list(db.scalars(stmt.order_by(Voucher.id)).all())

    parsed = [(v, *split_no(v.voucher_no)) for v in rows]
    with_seq = [(v, p, s) for v, p, s in parsed if s > 0]

    prefixes = {p for _, p, _ in with_seq}
    gaps: list[str] = []
    dup: list[str] = []
    unparsed: list[str] = []

    for v, p, s in parsed:
        if s == 0:
            unparsed.append(v.voucher_no)

    if prefixes:
        _, prefix, top_seq = max(with_seq, key=lambda t: t[2])
        used = {s for _v, p, s in with_seq if p == prefix}
        missing = [n for n in range(1, top_seq + 1) if n not in used]
        gaps = [f"{prefix}{n:04d}" for n in missing]
        seen: set[str] = set()
        for v, p, s in with_seq:
            if p != prefix:
                continue
            if v.voucher_no in seen:
                dup.append(v.voucher_no)
            seen.add(v.voucher_no)

    return {
        "period": period_label(period.year, period.month),
        "total": len(rows),
        "prefixes": sorted(prefixes),
        "gaps": gaps,
        "duplicates": dup,
        "unparsed": unparsed,
        "clean": not (gaps or dup or unparsed or len(prefixes) > 1),
    }


# --------------------------------------------------------------------- 计划


def plan_organize(
    db: Session,
    *,
    book_id: int,
    period_id: int | None = None,
    strategy: str = "GAPS",
    include_system: bool = False,
) -> list[PeriodPlan]:
    """算出整理方案但不落库。``period_id`` 为空表示整个账套。"""
    if strategy not in STRATEGIES:
        raise ValueError(f"未知的整理方式：{strategy}")

    period_stmt = select(Period).where(Period.book_id == book_id).order_by(Period.ord)
    if period_id is not None:
        period_stmt = period_stmt.where(Period.id == period_id)
    periods = list(db.scalars(period_stmt).all())
    if not periods:
        raise ValueError("没有可整理的会计期间")

    plans: list[PeriodPlan] = []
    for period in periods:
        plan = PeriodPlan(period_id=period.id, label=period_label(period.year, period.month))
        plan.gaps = check_gaps(db, period)["gaps"]

        stmt = select(Voucher).where(Voucher.period_id == period.id)
        if not include_system:
            stmt = stmt.where(Voucher.source != "CARRY_FORWARD")
        rows = list(db.scalars(stmt).all())

        if strategy == "GAPS":
            for v in rows:
                plan.changes.append(
                    Change(v.id, v.voucher_no, v.voucher_no,
                           v.voucher_date.isoformat(), v.status, v.source)
                )
            plans.append(plan)
            continue

        prefix = _dominant_prefix(rows)
        ordered = sorted(rows, key=lambda v: _sort_key(v, strategy))
        for i, v in enumerate(ordered, start=1):
            plan.changes.append(
                Change(v.id, v.voucher_no, f"{prefix}{i:04d}",
                       v.voucher_date.isoformat(), v.status, v.source)
            )

        kept = _pinned_system(db, period)
        for v, _p, _s in ((v, *split_no(v.voucher_no)) for v in rows):
            if v.id in kept and v.source == "CARRY_FORWARD":
                plan.skipped.append(v.voucher_no)

        plans.append(plan)

    return plans


def _dominant_prefix(rows: list[Voucher]) -> str:
    """取出现次数最多的前缀，保证重编号后仍是原来的凭证字。"""
    counts: dict[str, int] = {}
    for v in rows:
        p, _ = split_no(v.voucher_no)
        counts[p] = counts.get(p, 0) + 1
    if not counts:
        return "记"
    return max(counts.items(), key=lambda kv: (kv[1], kv[0] == "记"))[0]


def _pinned_system(db: Session, period: Period) -> set[int]:
    """系统凭证（结转损益）永远排在人工凭证之后，且不参与重编号。"""
    return set(
        db.scalars(
            select(Voucher.id).where(
                Voucher.period_id == period.id, Voucher.source == "CARRY_FORWARD"
            )
        ).all()
    )


# --------------------------------------------------------------------- 执行


def apply_organize(
    db: Session,
    *,
    book_id: int,
    user: SysUser,
    period_id: int | None = None,
    strategy: str = "RENUM",
    include_system: bool = False,
) -> dict:
    """落库整理。返回每期的实际改动张数。"""
    if strategy == "GAPS":
        raise ValueError("GAPS 只是体检，不执行改号")

    plans = plan_organize(
        db, book_id=book_id, period_id=period_id, strategy=strategy, include_system=include_system
    )

    locked = db.execute(
        select(Period.year, Period.month).where(
            Period.id.in_([p.period_id for p in plans]), Period.locked.is_(True)
        )
    ).all()
    if locked:
        raise ValueError(
            "会计期间 " + ", ".join(period_label(y, m) for y, m in locked) + " 已结账，不能整理凭证"
        )

    result: list[dict] = []
    total_changed = 0

    for plan in plans:
        before = {
            c.voucher_id: c.old_no
            for c in plan.changes
            if c.changed
        }
        if before:
            _renumber(db, plan)
            total_changed += len(before)
        result.append(plan.to_dict())

    db.flush()
    log(
        db,
        action="ORGANIZE",
        entity="voucher",
        entity_id=str(period_id or "ALL"),
        user=user,
        book_id=book_id,
        detail=f"整理凭证（{strategy}）：改号 {total_changed} 张",
        before={"old": {str(k): v for k, v in before.items()}} if before else None,
        after={"count": total_changed},
    )
    db.commit()

    return {"strategy": strategy, "total_changed": total_changed, "periods": result}


def _renumber(db: Session, plan: PeriodPlan) -> None:
    """两阶段改号，避开即时唯一约束。"""
    targets = [c for c in plan.changes if c.changed]
    if not targets:
        return

    # 阶段一：整体挪到 ∥<id>，这些值与任何真实凭证号都不冲突
    for c in targets:
        v = db.get(Voucher, c.voucher_id)
        if v is None:
            raise ValueError(f"凭证不存在（可能已被删除）：{c.voucher_id}")
        v.voucher_no = f"{PARK_PREFIX}{v.id}"
    db.flush()

    # 阶段二：写最终号，此时序列里已经没有旧号
    for c in targets:
        v = db.get(Voucher, c.voucher_id)
        v.voucher_no = c.new_no
    db.flush()
