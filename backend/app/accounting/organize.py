"""整理凭证：重新排序、重新编号。

整理只做两件事，方式由用户在页面上选：

1. ``RENUM``  —— 保持当前顺序，把号段压紧成 001..N（补断号）。
2. ``SORT_DATE`` —— 先按日期升序排，再压紧编号。

设计取舍
--------
* **两阶段返回**：``plan_organize`` 只算不改，``apply_organize`` 才落库。
  财务改号影响已经打印出来的纸质凭证，必须先看清差异再确认。
* **临时号避让**：``uq_voucher_period_no`` 是即时唯一约束，直接改号会在交换
  （记001 <-> 记002）时撞库。因此先整体写到临时号并 flush，再写最终号。
* **系统凭证一起排，但永远垫底**：结转损益（source=CARRY_FORWARD）由月末任务
  生成，跟手工凭证抢号会让期末那几张看起来像随手记的。所以排序键把
  CARRY_FORWARD 放在最高优先级（排在最后），编号也就落在最后两个号。
* **按期间分组**：整理范围可以是单个期间，也可以是整个账套（自动按期间分组），
  不会把凭证号排到别的期间去。
* **体检不占一个入口**：``check_period`` 单独提供，给凭证列表页做顶部提醒、
  也给记账/结账当闸门，不再作为一种「整理方式」让用户去选。
* **已记账的不动**：已记账凭证占着的号原样保留，剩下的凭证只在空出来的号里
  重新发号 —— 否则改到一半撞上「这张已经记账了」，账就死在一半。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log, voucher_snapshot
from app.accounting.util import constraint_name, period_label
from app.models import Period, SysUser, Voucher

# 凭证号形如「记001」，前缀是 1~2 个汉字，序号是数字（补零到三位）
NO_PATTERN = re.compile(r"^(?P<prefix>[^\d]{1,4})(?P<seq>\d+)$")
PARK_PREFIX = "∥"  # ∥ 临时占位前缀，保证永不与真实凭证号冲突
# 与 accounting/vouchers.py 同一份判据：撞号要认约束，不能「凡 IntegrityError
# 都当撞号」（util.constraint_name 的注释里写了为什么）。
_NO_UNIQUE = "uq_voucher_period_no"

STRATEGIES = ("RENUM", "SORT_DATE")


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
    """排序键。最高一位永远是「是不是结转损益」：1 排最后。

    月末的结转凭证是系统生成的，收入类、成本费用类各一张，落在最后两个号上，
    一眼就能在号段末尾认出期末那几张，不必去翻摘要。
    """
    tail = 1 if voucher.source == "CARRY_FORWARD" else 0
    if strategy == "SORT_DATE":
        return (tail, voucher.voucher_date.toordinal(), voucher.id)
    return (tail, voucher.id)  # RENUM 保持当前存储顺序


# --------------------------------------------------------------------- 体检


def _time_order_issues(by_seq: list[tuple[Voucher, str, int]]) -> list[dict]:
    """按凭证号排一遍，看日期是不是也一路往后走。

    记账是按时间顺序记的，凭证号就是时间顺序的编号。号大的那张日期反而更早，
    说明这个月是「先记了后面的，又回头补了前面的」—— 号段乱了，纸质凭证按号
    装订也对不上，月底不能就这么封账。
    """
    issues: list[dict] = []
    prev: Voucher | None = None
    for v, _prefix, _seq in by_seq:
        if prev is not None and v.voucher_date < prev.voucher_date:
            issues.append({
                "voucher_no": v.voucher_no,
                "voucher_date": v.voucher_date.isoformat(),
                "prev_no": prev.voucher_no,
                "prev_date": prev.voucher_date.isoformat(),
            })
        prev = v
    return issues


def check_period(db: Session, period: Period) -> dict:
    """一个期间的「整理就绪」体检：断号 + 时序。全程只读。

    这两项任一不干净，这个月就不该记账、也不该结账：号段有洞或者号序跟日期
    对不上，纸质凭证按号装订就跟系统对不上，月底封了账再想改就只能作废重记。

    结转损益算不算断号/时序？算。它照样占着号，缺了就是缺了。
    """
    rows = list(db.scalars(
        select(Voucher).where(Voucher.period_id == period.id).order_by(Voucher.id)
    ).all())

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
        gaps = [f"{prefix}{n:03d}" for n in missing]
        seen: set[str] = set()
        for v, p, s in with_seq:
            if p != prefix:
                continue
            if v.voucher_no in seen:
                dup.append(v.voucher_no)
            seen.add(v.voucher_no)

    by_seq = sorted(with_seq, key=lambda t: t[2])
    out_of_order = _time_order_issues(by_seq)
    posted = [v.voucher_no for v in rows if v.status == "POSTED"]

    blocking: list[str] = []
    if gaps:
        blocking.append(f"断号 {len(gaps)} 个：{'、'.join(gaps)}")
    if dup:
        blocking.append(f"凭证号重复：{'、'.join(sorted(set(dup)))}")
    if unparsed:
        blocking.append(f"凭证号不是「凭证字+数字」：{'、'.join(unparsed)}")
    if len(prefixes) > 1:
        blocking.append(f"混用了多个凭证字：{'、'.join(sorted(prefixes))}")
    if out_of_order:
        blocking.append(
            f"时序不对 {len(out_of_order)} 处："
            + "、".join(f"{i['prev_no']}({i['prev_date']}) → {i['voucher_no']}({i['voucher_date']})"
                        for i in out_of_order[:3])
            + ("…" if len(out_of_order) > 3 else "")
        )

    return {
        "period_id": period.id,
        "period": period_label(period.year, period.month),
        "total": len(rows),
        "prefixes": sorted(prefixes),
        "gaps": gaps,
        "duplicates": dup,
        "unparsed": unparsed,
        "time_out_of_order": out_of_order,
        "posted": posted,
        "blocking": blocking,
        "clean": not blocking,
    }


# --------------------------------------------------------------------- 计划


def plan_organize(
    db: Session,
    *,
    book_id: int,
    period_id: int | None = None,
    strategy: str = "SORT_DATE",
) -> list[PeriodPlan]:
    """算出整理方案但不落库。``period_id`` 为空表示整个账套。

    结转损益不再有「参不参与」的开关：它一定参与，而且一定排在最后。
    """
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
        rep = check_period(db, period)
        plan = PeriodPlan(period_id=period.id, label=period_label(period.year, period.month))
        plan.gaps = rep["gaps"]

        # 「按当前顺序重新编号」的前提是当前顺序本身就对。
        # 时序不对还照着当前顺序压紧号，等于把乱的号段原样固化成整齐的乱。
        if strategy == "RENUM" and rep["time_out_of_order"]:
            raise ValueError(
                f"{rep['period']} 有 {len(rep['time_out_of_order'])} 处凭证号与日期不同序，"
                "不能「按当前顺序重新编号」；请改用「按日期升序重排并编号」先排好时序"
            )

        rows = list(db.scalars(
            select(Voucher).where(Voucher.period_id == period.id)
        ).all())

        # 已记账的凭证不动：它们占着的号原样留着，其余凭证只在空出来的号里发号。
        # 直接把已记账的排除在外再压紧成 1..N，N 一多半就会踩到它们占的号上。
        movable = [v for v in rows if v.status != "POSTED"]
        reserved = {_seq_of(v.voucher_no) for v in rows if v.status == "POSTED"}

        prefix = _dominant_prefix(rows)
        ordered = sorted(movable, key=lambda v: _sort_key(v, strategy))
        n = 0
        for v in ordered:
            n += 1
            while n in reserved:
                n += 1
            plan.changes.append(
                Change(v.id, v.voucher_no, f"{prefix}{n:03d}",
                       v.voucher_date.isoformat(), v.status, v.source)
            )
        for v in rows:
            if v.status == "POSTED":
                plan.changes.append(
                    Change(v.id, v.voucher_no, v.voucher_no,
                           v.voucher_date.isoformat(), v.status, v.source)
                )

        # 交给页面的顺序 = 整理完之后的号段顺序，而不是「先排可动的、最后补已记账的」。
        # 这两件事在混合场景下不是一回事：整月的凭证都记完账时，可动的那批是空的，
        # 页面拿到的就成了数据库的原始存储顺序 —— 日期是乱的、结转损益夹在中间，
        # 看着像整理排错了，其实只是列表没排。结转损益仍旧压在最末尾。
        plan.changes.sort(key=lambda c: (
            1 if c.source == "CARRY_FORWARD" else 0, _seq_of(c.new_no)
        ))

        plans.append(plan)

    return plans


def _seq_of(voucher_no: str) -> int:
    _prefix, seq = split_no(voucher_no)
    return seq


def _dominant_prefix(rows: list[Voucher]) -> str:
    """取出现次数最多的前缀，保证重编号后仍是原来的凭证字。"""
    counts: dict[str, int] = {}
    for v in rows:
        p, _ = split_no(v.voucher_no)
        counts[p] = counts.get(p, 0) + 1
    if not counts:
        return "记"
    return max(counts.items(), key=lambda kv: (kv[1], kv[0] == "记"))[0]


def assert_tidy(db: Session, periods: list[Period]) -> None:
    """记账 / 结账前的闸门：这个月还没整理干净，就别让它变成不可翻的历史。

    断号、时序不对的月份一旦记了账、封了账，纸质凭证就再也对不上了 ——
    而那张纸已经打印出去、装订进账簿了。所以闸门放在这里，而不是等到月底才发现。
    """
    bad: list[str] = []
    for p in periods:
        if p.locked:
            continue
        rep = check_period(db, p)
        if rep["blocking"]:
            bad.append(f"{rep['period']}　" + "；".join(rep["blocking"]))
    if bad:
        raise ValueError(
            "凭证还没整理干净，不能记账、也不能结账：\n"
            + "\n".join(bad)
            + "\n请先到「整理凭证」把号排好再来。"
        )


# --------------------------------------------------------------------- 执行


def apply_organize(
    db: Session,
    *,
    book_id: int,
    user: SysUser,
    period_id: int | None = None,
    strategy: str = "SORT_DATE",
) -> dict:
    """落库整理。返回每期的实际改动张数。"""
    plans = plan_organize(db, book_id=book_id, period_id=period_id, strategy=strategy)

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

    # P3-11：改号全程无锁。计划是「读一遍算一遍」，落库是「写两遍」，
    # 中间没有任何东西挡住并发的第二个整理 —— 两边会按**各自的**快照算出
    # 同一套目标号，然后后提交的那个撞上 ``uq_voucher_period_no``。
    #
    # DB 侧有唯一约束兜底，所以不会产生脏数据（这是「翻成人话」而不是
    # 「修数据错」的原因）；但撞出来的是一条裸的 IntegrityError，
    # 走全局处理器只得到 500，用户看不出是自己刚点过整理。
    #
    # 这里锁**期间行**，与 periods.py 的 ``ensure_periods`` 同一套做法
    # （它锁账套行）。锁粒度选期间而不是账套：整理是逐期发号的，
    # 锁期间就够把「同一个期间的号段」串行化，不同期间可以并行。
    # 锁顺序按 ``ord`` 升序 —— 全项目行锁只有这两处，顺序一致才不会成环。
    lock_ids = [p.period_id for p in plans if any(c.changed for c in p.changes)]
    if lock_ids:
        db.execute(
            select(Period.id)
            .where(Period.id.in_(lock_ids))
            .order_by(Period.ord)
            .with_for_update()
        )

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
    # 阶段二：写最终号，此时序列里已经没有旧号
    #
    # 两段都包在 savepoint 里（P3-11）：IntegrityError 会把整个事务标记为失败，
    # 而 ``apply_organize`` 逐期改号 —— 一个期间撞了不该把前面几期已经改好的
    # 号一起废掉。撞号（唯一约束兜底生效）翻成 ValueError；不是撞号的
    # 数据库错误原样抛出，不替它编理由。
    try:
        with db.begin_nested():
            for c in targets:
                v = db.get(Voucher, c.voucher_id)
                if v is None:
                    raise ValueError(f"凭证不存在（可能已被删除）：{c.voucher_id}")
                v.voucher_no = f"{PARK_PREFIX}{v.id}"
            db.flush()

            for c in targets:
                v = db.get(Voucher, c.voucher_id)
                v.voucher_no = c.new_no
            db.flush()
    except IntegrityError as exc:
        # savepoint 回滚了，数据库里的号没动，但会话里的对象还带着改过的值 ——
        # expire 掉，否则调用方接着读到的就是一份「内存里已经改了、库里没改」的
        # 凭证号，日志与返回值会互相打架。
        db.expire_all()
        if constraint_name(exc) != _NO_UNIQUE and _NO_UNIQUE not in str(
                getattr(exc, "orig", exc)):
            raise
        raise ValueError(
            f"改号失败：{plan.label} 的目标凭证号已被另一条记录占用"
            f"（可能刚被别的操作改过）。请刷新后重试。") from exc
