"""凭证服务：新增 / 修改 / 删除 / 记账 / 反记账 / 查询 / 批量。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.accounting import organize as svc_organize
from app.accounting.organize import split_no
from app.accounting.audit_helper import log, voucher_snapshot
from app.accounting.util import find_period, money, period_label
from app.models import Book, Entry, Period, Subject, SysUser, Voucher


# --------------------------------------------------------------------- 编号


def next_voucher_no(db: Session, period: Period, prefix: str = "记") -> str:
    """期间内顺序编号。凭证号允许回收复用，因此取「当前最大值 + 1」即可。

    原来这行是 ``max(substring(voucher_no, 3))``，两个假设都不成立：

    **① 固定偏移 3。** ``substring(x, 3)`` 假设凭证字恰好 2 个汉字，
    从第 3 个字符起截。而 ``organize.NO_PATTERN`` 允许 1~4 个非数字字符
    作前缀，导入功能也允许用户自带凭证号。实测（修复前）：

        库里只有 银009   → 发出 记010（截断位置错了，序号对但前缀被吞）
        库里只有 记账凭证009 → 发出 记001

    **② max() 作用在字符串上。** PostgreSQL 对 text 的 max 是字典序，
    ``"099" > "1000"``。所以超过 999 张就回绕。实测（修复前）：

        库里只有 记100   → 发出 记001  ← 字典序 "00" 赢过 "100"
        库里只有 记199   → 发出 记100
        库里只有 记999   → 发出 记100

    两个缺陷叠在一起，探针 13 组里 10 组结果是错的。

    改成把该期间的号全取出来，用 ``organize.split_no`` 在**应用层**按
    整数求最大值：前缀按传入的 prefix 走，序号按数值比。
    """
    nos = db.scalars(
        select(Voucher.voucher_no).where(Voucher.period_id == period.id)
    ).all()
    # 只看**与本次前缀相同**的号。这不是额外的洁癖，organize 里
    # ``_check``/``_dominant_prefix`` 本来就是按前缀分组的：凭证字不同
    # 就是不同的号段，跨前缀比序号没有意义。
    #
    # 否则「记-2」会被解析成 (前缀「记-」, 序号 2)，把「记」的下一个号
    # 顶到 003 —— 用户看着 记001/记002 空着，发号却说 记003。
    # 同理「记-年结」这类解析不出序号的返回 0，本来就不该参与递增。
    seq = max((n for no in nos
               if (n := split_no(no)[1]) > 0 and split_no(no)[0] == prefix),
              default=0) + 1
    # 三位数够一个月的凭证用。四位（记0022）看着像系统自动加的零，反而难扫。
    return f"{prefix}{seq:03d}"


# --------------------------------------------------------------------- 插入位


def insert_slot(db: Session, anchor: Voucher) -> dict:
    """插入凭证要占的那个号：参照凭证自己的号，参照及以后的号整体顺延 +1。

    记001..记010，打开记006 插入 —— 存下去之后新凭证拿记006，
    原记006..记010 变成记007..记011。顺延按**号**算，不按日期：
    凭证号才是这个期间里的顺序，日期排出来的次序交给「整理凭证」管。

    这里只算不改。改号和建凭证必须落在同一个事务里：中途放弃一张空凭证，
    后面那叠号就该原封不动 —— 空凭证占着号、把别的凭证顶走，才是真事故。
    """
    prefix, seq = svc_organize.split_no(anchor.voucher_no)
    if seq == 0:
        raise ValueError(
            f"凭证号 {anchor.voucher_no} 不是「凭证字 + 序号」，插入凭证要照着它顺位，请先整理凭证"
        )
    rows = list(db.scalars(
        select(Voucher).where(Voucher.period_id == anchor.period_id)
    ).all())
    moved = []
    for v in rows:
        p, s = svc_organize.split_no(v.voucher_no)
        if p == prefix and s >= seq:
            moved.append({"id": v.id, "from": v.voucher_no, "to": f"{prefix}{s + 1:03d}"})
    moved.sort(key=lambda m: m["from"])
    return {"prefix": prefix, "seq": seq, "no": anchor.voucher_no, "moved": moved}


def _apply_shift(db: Session, anchor: Voucher) -> list[dict]:
    """把参照凭证及其后面的号整体 +1。两阶段改号，避开即时唯一约束。

    直接把 记006 改成 记007 会当场撞上原来那张记007 —— ``uq_voucher_period_no``
    是即时约束，不像平衡检查那样延迟到提交。所以先整体挪到 ∥<id> 临时号再写最终号，
    跟整理凭证用的是同一套路子。
    """
    plan = insert_slot(db, anchor)
    moved = plan["moved"]
    if not moved:
        return moved

    targets = []
    for m in moved:
        v = db.get(Voucher, m["id"])
        targets.append((v, m["to"]))
    for v, _new_no in targets:
        v.voucher_no = f"{svc_organize.PARK_PREFIX}{v.id}"
    db.flush()
    for v, new_no in targets:
        v.voucher_no = new_no
    db.flush()
    return moved


# --------------------------------------------------------------------- 校验


def side_totals(entries: list[dict]) -> tuple[Decimal, Decimal]:
    """凭证两栏的合计：**红字算到对面去**。

    贷方红字（负数）本质是借方，借方红字本质是贷方 —— 这是红字的本意，
    不是「贷方减了 300」。所以每栏只加正数：

        借方合计 = 借方正数 + 贷方红字（绝对值）
        贷方合计 = 贷方正数 + 借方红字（绝对值）

    跟「带符号的两边相等」完全等价（移项即可），所以拿它判平也对；
    但它是**给人看**的那个数：两栏都摆得下正数，一眼比得出大小。
    """
    dr = cr = Decimal("0")
    for e in entries:
        amt = money(e["amount"])
        if amt > 0:
            if e["direction"] == "DEBIT":
                dr += amt
            else:
                cr += amt
        elif amt < 0:
            if e["direction"] == "DEBIT":
                cr += -amt
            else:
                dr += -amt
    return dr, cr


# 系统自己生成的凭证：结转损益、年度结转。
# 它们搬的是**已经存在**的余额，不是「给这个科目记一笔新账」，
# 所以不受「停用科目不能新记」的闸门约束（审计 P2-08）。
SYSTEM_SOURCES = ("CARRY_FORWARD",)


def _validate_entries(db: Session, book_id: int, entries: list[dict],
                      allow_inactive: bool = False) -> None:
    if not entries:
        raise ValueError("凭证至少需要一条分录")
    dr = sum(money(e["amount"]) for e in entries if e["direction"] == "DEBIT")
    cr = sum(money(e["amount"]) for e in entries if e["direction"] == "CREDIT")
    if dr != cr:
        raise ValueError(f"借贷不平衡：借方 {dr} / 贷方 {cr}")
    # 负数是**红字冲销**，一张红字凭证借贷两边都是负的（-300 / -300），
    # 照样是平的、照样该存 —— 冲掉一张记错的凭证正是记账的日常。
    # 所以这里只挡「全零」，不挡负数。
    if dr == 0:
        raise ValueError("凭证金额不能是 0")

    subject_ids = {e["subject_id"] for e in entries}
    stmt = select(Subject.id, Subject.code, Subject.name, Subject.is_leaf).where(
        Subject.book_id == book_id,
        Subject.id.in_(subject_ids),
    )
    if not allow_inactive:
        stmt = stmt.where(Subject.active.is_(True))
    rows = db.execute(stmt).all()
    found = {r.id for r in rows}
    missing = subject_ids - found
    if missing:
        raise ValueError(f"科目不存在或已停用: {sorted(missing)}")

    # 末级科目这道关口放在服务端，不能只指望前端面板不给选：
    # 接口能被直接调、导入能绕、老的行早就挂上去了 —— 前端拦一遍，这里再拦一遍，
    # 才轮得到「账上不可能出现挂在上级科目上的分录」这句话。
    # 挂错了不是显示难看：明细账、科目余额表都按末级出账，这笔分录会在账上凭空消失。
    non_leaf = sorted(f"{r.code} {r.name}" for r in rows if not r.is_leaf)
    if non_leaf:
        raise ValueError(f"不是末级科目，请改到末级科目：{'、'.join(non_leaf)}")


# --------------------------------------------------------------------- 写操作


def create_voucher(
    db: Session,
    *,
    book_id: int,
    user: SysUser,
    voucher_date: date,
    entries: list[dict],
    remark: str = "",
    attachments: int = 0,
    source: str = "MANUAL",
    voucher_no: str | None = None,
    period_id: int | None = None,
    insert_after_id: int | None = None,
    commit: bool = True,
) -> Voucher:
    period = db.get(Period, period_id) if period_id else find_period(db, book_id, voucher_date)
    if period is None:
        raise ValueError("会计期间不存在")
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能新增凭证")

    # 插入凭证：先认下参照凭证。顺延跟建凭证在同一个事务里做完，
    # 半路退出（一个格子都没填）就不会动别人的号。
    anchor: Voucher | None = None
    if insert_after_id is not None:
        anchor = db.get(Voucher, insert_after_id)
        if anchor is None or anchor.book_id != book_id:
            raise ValueError("插入位置的参照凭证不存在")
        if anchor.period_id != period.id:
            where = db.get(Period, anchor.period_id)
            raise ValueError(
                f"只能插在同一个会计期间里：参照凭证 {anchor.voucher_no} 在 "
                f"{where.code if where else '别的期间'}，这张在 {period.code}"
            )

    payload = [
        {
            "subject_id": int(e["subject_id"]),
            "direction": e["direction"],
            "amount": money(e["amount"]),
            "summary": e.get("summary", "") or "",
            "aux_ref": e.get("aux_ref") or {},
        }
        for e in entries
    ]
    _validate_entries(db, book_id, payload, allow_inactive=source in SYSTEM_SOURCES)

    # 插入位上号沿用参照凭证的号（页面预填的就是它），后面的整体顺延。
    # 人要是自己改过号，以人填的为准 —— 顺位照做，占的是那个号。
    moved: list[dict] = []
    moving_ids: set[int] = set()
    if anchor is not None:
        moved = insert_slot(db, anchor)["moved"]
        moving_ids = {m["id"] for m in moved}
        no = voucher_no or insert_slot(db, anchor)["no"]
    else:
        no = voucher_no or next_voucher_no(db, period)

    # 页面上会预填下一个号（/vouchers/next），两个人同时开一张就会撞号。
    # 撞了就报人话 —— 数据库只会说「违反唯一约束 uq_voucher_period_no」，
    # 看到的人不知道该改哪个字段。
    #
    # 必须在顺位**之前**查一次：号被顶走是这一笔唯一不可逆的副作用，
    # 先顺位再发现撞号，事务虽然会回滚，可这一层代码不该依赖调用方记得回滚。
    # 顺位会把参照凭证的旧号腾出来，所以占用检查要把「待顺位的那几张」排除掉。
    taken = db.scalar(select(Voucher.id).where(
        Voucher.period_id == period.id, Voucher.voucher_no == no))
    if taken is not None and taken not in moving_ids:
        raise ValueError(f"凭证号 {no} 在 {period.code} 已经有一张了，请改一个号")

    if moved:
        moved = _apply_shift(db, anchor)

    voucher = Voucher(
        book_id=book_id,
        period_id=period.id,
        voucher_no=no,
        voucher_date=voucher_date,
        status="UNPOSTED",
        source=source,
        remark=remark,
        attachments=attachments,
        created_by=user.username,
    )
    db.add(voucher)
    db.flush()

    for i, e in enumerate(payload, start=1):
        db.add(Entry(voucher_id=voucher.id, line_no=i, **e))

    db.flush()
    if moved:
        # 号被顶走这件事必须留痕：事后有人问「记007 怎么变成这笔了」，
        # 操作日志里能一句话说清是老几号顺位过来的。
        log(
            db,
            action="RENAME",
            entity="voucher",
            entity_id=",".join(str(m["id"]) for m in moved),
            user=user,
            book_id=book_id,
            detail="插入凭证顺位：" + "、".join(f"{m['from']}→{m['to']}" for m in moved),
            before={"moved": [{k: str(v) for k, v in m.items()} for m in moved]},
        )
    log(
        db,
        action="CREATE",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=book_id,
        detail=f"新增凭证 {voucher.voucher_no}",
        after=voucher_snapshot(voucher),
    )
    if commit:
        db.commit()
        db.refresh(voucher)
    return voucher


def update_voucher(
    db: Session, voucher: Voucher, user: SysUser, payload: dict
) -> Voucher:
    before = voucher_snapshot(voucher)
    period = db.get(Period, voucher.period_id)
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能修改凭证")
    # 已记账 = 已进账簿，数字已经被总账/报表引用。改它必须先反记账，
    # 否则「报表里的数和凭证里写的数」会对不上，且没人知道差在哪一步。
    if voucher.status == "POSTED":
        raise ValueError(
            f"凭证 {voucher.voucher_no} 已记账，不能修改。请先在凭证列表里「反记账」再改。")

    if "voucher_date" in payload and payload["voucher_date"]:
        new_date = payload["voucher_date"]
        new_period = find_period(db, voucher.book_id, new_date)
        voucher.voucher_date = new_date
        voucher.period_id = new_period.id

    if "entries" in payload:
        entries = [
            {
                "subject_id": int(e["subject_id"]),
                "direction": e["direction"],
                "amount": money(e["amount"]),
                "summary": e.get("summary", "") or "",
                "aux_ref": e.get("aux_ref") or {},
            }
            for e in payload["entries"]
        ]
        _validate_entries(db, voucher.book_id, entries)
        voucher.entries.clear()
        db.flush()
        for i, e in enumerate(entries, start=1):
            voucher.entries.append(Entry(voucher_id=voucher.id, line_no=i, **e))
        # 改动内容后回到未记账，避免「已记账但内容变了」的脏状态
        voucher.status = "UNPOSTED"
        voucher.posted_at = None
        voucher.posted_by = None

    if "remark" in payload:
        voucher.remark = payload["remark"] or ""
    if "attachments" in payload:
        voucher.attachments = int(payload["attachments"] or 0)

    db.flush()
    log(
        db,
        action="UPDATE",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=voucher.book_id,
        detail=f"修改凭证 {voucher.voucher_no}",
        before=before,
        after=voucher_snapshot(voucher),
    )
    db.commit()
    db.refresh(voucher)
    return voucher


def delete_voucher(db: Session, voucher: Voucher, user: SysUser,
                   commit: bool = True) -> None:
    """删除凭证。因为编号可复用，必须把内容快照写进审计日志。

    ``commit=False`` 让调用方把多步操作合成一个事务（批量删除用它）。
    批量删到第 5 张失败时，前 4 张不该已经落在库里 ——
    用户收到的是 400「删失败」，而账上少了 4 张，且没人告诉他少了哪几张。
    """
    if voucher.status == "POSTED":
        raise ValueError(
            f"凭证 {voucher.voucher_no} 已记账，不能删除。请先在凭证列表里「反记账」再删。")
    snapshot = voucher_snapshot(voucher)
    log(
        db,
        action="DELETE",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=voucher.book_id,
        detail=f"删除凭证 {voucher.voucher_no}",
        before=snapshot,
    )
    db.delete(voucher)
    if commit:
        db.commit()


# --------------------------------------------------------------------- 记账


def _resolve_vouchers(db: Session, book_id: int, voucher_ids: list[int]) -> list[Voucher]:
    """把请求的 ID 列表解析成**确实属于 book_id**的凭证集合。

    为什么必须带 book_id 一起查：原来只有 ``Voucher.id.in_(ids)``，
    路由层拿到的 book 压根没往下传 —— 于是拿着 A 账套的接口就能把
    B 账套的凭证状态改掉（审计 P1-01，动态复现 HTTP 200）。
    凭证状态直接决定它算不算进余额表、总账和报表，这是跨账套数据完整性破口。

    为什么整体拒绝而不是「跳过越界的、成功的照样成功」：
    部分成功最坏 —— 用户以为 3 张都记上了，实际 2 张、而且报错被淹在
    「已记账 1 张」这种成功计数里。这里改成**一个不对就全批拒绝**。

    另外顺带解决 P1-08：原来查不到就直接访问 ``vouchers[0]``，
    传不存在的 ID → IndexError → HTTP 500。500 会掩盖真实原因
    （是不存在？是无权？还是服务端坏了？），也不符合 API 契约。
    """
    wanted = sorted(set(voucher_ids))
    rows = list(db.scalars(
        select(Voucher).where(Voucher.id.in_(wanted), Voucher.book_id == book_id)
    ).all())
    found = {v.id for v in rows}
    missing = [i for i in wanted if i not in found]
    if missing:
        raise ValueError(
            f"凭证不存在或不属于当前账套，已整批拒绝：{', '.join(str(i) for i in missing[:10])}"
            + ("…" if len(missing) > 10 else ""))
    return rows


def post_vouchers(db: Session, book_id: int, voucher_ids: list[int], user: SysUser,
                  commit: bool = True) -> int:
    """批量记账。``book_id`` 是**必填**的账套归属约束，不是可选参数。

    ``commit=False`` 让调用方把多步操作合成一个事务。
    """
    if not voucher_ids:
        return 0
    vouchers = _resolve_vouchers(db, book_id, voucher_ids)
    period_ids = {v.period_id for v in vouchers}
    rows = db.execute(select(Period).where(Period.id.in_(period_ids))).scalars().all() if period_ids else []

    locked = sorted(p.code for p in rows if p.locked)
    if locked:
        raise ValueError(f"会计期间 {', '.join(locked)} 已结账，不能记账")

    # 号段有洞、或者号序跟日期对不上：这个月就还不能记账。记了账这些凭证就锁死，
    # 而纸质的已经打印装订，月底对不上账只能整月作废重来。
    svc_organize.assert_tidy(db, rows)

    # **只有上一期已结账，本期才允许记账。**
    # 上一期没封账，本期凭证再去改上期已经变动的数字，账就对不回来了 ——
    # 这正是「先结账、再记下个月」这条规矩要挡住的事。
    # 账套的第一个期间没有上一期，不受此限制。
    prev_ords = {p.ord - 1 for p in rows if not p.locked}
    prev_rows = (
        db.execute(
            select(Period).where(
                Period.book_id.in_({p.book_id for p in rows}),
                Period.ord.in_(prev_ords),
            )
        ).scalars().all()
        if prev_ords
        else []
    )
    prev_map = {(p.book_id, p.ord): p for p in prev_rows}
    pending = {}
    for p in rows:
        if p.locked:
            continue
        prev = prev_map.get((p.book_id, p.ord - 1))
        if prev is not None and not prev.locked:
            pending[p.code] = prev.code
    if pending:
        detail = "；".join(f"{code} 需先结账 {prev}" for code, prev in sorted(pending.items()))
        raise ValueError(f"上一期尚未结账，不能记账：{detail}")

    count = 0
    for v in vouchers:
        if v.status == "POSTED":
            continue
        dr = sum(money(e.amount) for e in v.entries if e.direction == "DEBIT")
        cr = sum(money(e.amount) for e in v.entries if e.direction == "CREDIT")
        if dr != cr:
            raise ValueError(f"凭证 {v.voucher_no} 借贷不平衡，不能记账")
        v.status = "POSTED"
        v.posted_at = datetime.now(timezone.utc)
        v.posted_by = user.username
        count += 1
    db.flush()
    # book_id 用请求带来的，不取 vouchers[0].book_id：批次里的凭证经过
    # _resolve_vouchers 已保证同属一个账套，但审计记录的归属不该依赖
    # 「碰巧第一张是谁」这种实现细节。
    log(db, action="POST", entity="voucher", entity_id="batch", user=user,
        book_id=book_id, detail=f"记账 {count} 张凭证", after={"ids": voucher_ids})
    if commit:
        db.commit()
    return count


def unpost_vouchers(db: Session, book_id: int, voucher_ids: list[int], user: SysUser,
                    commit: bool = True) -> int:
    """批量反记账。归属约束与 post_vouchers 同一条，不能一边松一边紧。"""
    if not voucher_ids:
        return 0
    vouchers = _resolve_vouchers(db, book_id, voucher_ids)
    period_ids = {v.period_id for v in vouchers}
    locked = db.execute(
        select(Period.year, Period.month).where(Period.id.in_(period_ids), Period.locked.is_(True))
    ).all()
    if locked:
        codes = [period_label(y, m) for y, m in locked]
        raise ValueError(f"会计期间 {', '.join(codes)} 已结账，不能反记账")

    count = 0
    for v in vouchers:
        if v.status == "UNPOSTED":
            continue
        v.status = "UNPOSTED"
        v.posted_at = None
        v.posted_by = None
        count += 1
    db.flush()
    log(db, action="UNPOST", entity="voucher", entity_id="batch", user=user,
        book_id=book_id, detail=f"反记账 {count} 张凭证", before={"ids": voucher_ids})
    if commit:
        db.commit()
    return count


# --------------------------------------------------------------------- 查询


def query_vouchers(
    db: Session,
    book_id: int,
    *,
    from_period: Period | None = None,
    to_period: Period | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    subject_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Voucher], int]:
    stmt = select(Voucher).where(Voucher.book_id == book_id)

    if from_period:
        stmt = stmt.where(
            Voucher.period_id.in_(
                select(Period.id).where(Period.book_id == book_id, Period.ord >= from_period.ord)
            )
        )
    if to_period:
        stmt = stmt.where(
            Voucher.period_id.in_(
                select(Period.id).where(Period.book_id == book_id, Period.ord <= to_period.ord)
            )
        )
    if date_from:
        stmt = stmt.where(Voucher.voucher_date >= date_from)
    if date_to:
        stmt = stmt.where(Voucher.voucher_date <= date_to)
    if status:
        stmt = stmt.where(Voucher.status == status)
    if source:
        stmt = stmt.where(Voucher.source == source)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                Voucher.voucher_no.ilike(like),
                Voucher.remark.ilike(like),
                Voucher.id.in_(
                    select(Entry.voucher_id).where(Entry.summary.ilike(like))
                ),
            )
        )
    if subject_id:
        stmt = stmt.where(Voucher.id.in_(select(Entry.voucher_id).where(Entry.subject_id == subject_id)))
    if min_amount is not None or max_amount is not None:
        # 「金额」筛的是**某一条分录**的金额，不是整张凭证的合计 ——
        # 只要有一行落在区间里，这张凭证就该被捞出来。
        # 所以这里不做求和，直接拿单条 entry 的 amount 比。
        amt = select(Entry.voucher_id).where(Entry.book_id == book_id)
        if min_amount is not None:
            amt = amt.where(Entry.amount >= money(min_amount))
        if max_amount is not None:
            amt = amt.where(Entry.amount <= money(max_amount))
        stmt = stmt.where(Voucher.id.in_(amt))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = (
        stmt.order_by(Voucher.voucher_date.desc(), Voucher.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list(db.scalars(stmt).all())
    for v in rows:
        db.refresh(v)
    return rows, total
