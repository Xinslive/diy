"""会计期间服务：结账、批量反结账、结转损益、年度结转。

设计说明
--------
期初余额永远是**推导值**：期初(P) = 期末(P-1) = 期初(P-1) + 借方累计(P-1) - 贷方累计(P-1)，
一路回推到账套启用月份那笔录入的期初余额。因此：

* 平时不需要写任何期初快照，改动上游数据余额自动跟随，不存在快照失效的问题。
* period_opening 表只存两处「真·期初」：账套启用时的录入、年度结转时的结转。

「已结账」由 period.locked 保证：锁上的期间任何写入都被数据库触发器拒绝。
反过来，解锁之后重算出来的数字自然与改动一致 —— 这正是反结账要的效果。

结账级联
--------
不允许出现「3 月已结账、1 月还开着」这种状态：它会让已结账期间的累计数随
上游改动而变化。所以关闭第 N 期时，前面未结账的期间一并结账（这正是反结账
的镜像操作）。用户不需要理解规则，只需要在结果里看到「顺带结账了哪几期」。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting import organize as svc_organize
from app.accounting.audit_helper import log
from app.accounting.util import ZERO, money, period_label
from app.ledger.engine import LedgerEngine
from app.models import (
    Book,
    Entry,
    Period,
    PeriodOpening,
    Subject,
    SysUser,
    UserBook,
    Voucher,
)
from app.models.book import period_ord

PROFIT_SUBJECT_CODE = "4103"     # 本年利润
RETAINED_SUBJECT_CODE = "4104"   # 利润分配 — 未分配利润


def get_period(db: Session, book_id: int, period_id: int) -> Period:
    p = db.get(Period, period_id)
    if p is None or p.book_id != book_id:
        raise ValueError("会计期间不存在")
    return p


def _subject_by_code(db: Session, book_id: int, code: str) -> Optional[Subject]:
    return db.scalar(select(Subject).where(Subject.book_id == book_id, Subject.code == code))


# --------------------------------------------------------------------- 结账


def close_period(db: Session, period: Period, user: SysUser, auto_post: bool = True) -> dict:
    """关闭指定期间，并顺带关闭其之前所有未结账期间。"""
    target = db.execute(
        select(Period).where(Period.id == period.id).with_for_update()
    ).scalar_one()
    if target.locked:
        raise ValueError(f"{target.code} 已经结账")

    chain = list(
        db.execute(
            select(Period)
            .where(Period.book_id == target.book_id, Period.ord <= target.ord)
            .order_by(Period.ord)
            .with_for_update()
        ).scalars()
    )

    # 整条链都要先过一遍「整理就绪」：断号、时序不对的月份一旦封进历史，
    # 纸质凭证按号装订就跟系统对不上，再想改只能作废重记。
    svc_organize.assert_tidy(db, chain)

    auto_closed, auto_posted = [], 0
    pending = []
    for p in chain:
        if p.locked:
            continue
        posted = _auto_post(db, p, user) if auto_post else _check_unposted(db, p)
        auto_posted += posted
        pending.append(p)

    # 链上**每一期**都要损益结平，不是只看最后那一期。
    #
    # 以前只查 target，漏出来的是这样的事故：用户删掉 11 月的结转凭证，
    # 直接去结 12 月 —— 12 月连带把 11 月一起锁了，而 11 月的损益还挂在收入
    # 科目上没人结走。结转损益是按**本年累计**算的，12 月那张凭证会把 11 月
    # 的收入一并扫进本年利润，于是 11 月的钱出现在 12 月的凭证里，
    # 两边都「结账成功」，账面却已经错了，而且再也查不出来是谁动的手。
    eng = LedgerEngine(db, target.book_id)
    subjects = db.scalars(
        select(Subject).where(
            Subject.book_id == target.book_id,
            Subject.carry_forward.is_(True),
            Subject.active.is_(True),
            Subject.is_leaf.is_(True),
        )
    ).all()
    # 一次算到最后一期，中间每一期的中间结果都会留在缓存里
    eng.compute([s.id for s in subjects], max(p.ord for p in pending))
    unsettled = []
    for p in pending:
        residual = [
            f"{s.code} {s.name} 余额 {f.closing}"
            for s in subjects
            if abs((f := eng.figures(s.id, p.ord)).closing) > ZERO
        ]
        if residual:
            unsettled.append(f"{p.code}：{'，'.join(residual)}")
    if unsettled:
        raise ValueError(
            "以下期间还没做「结转损益」，损益科目有余额，不能结账：\n" + "\n".join(unsettled)
        )

    # 体检全过了才真的上锁 —— 别一边改 locked 一边验，验不过就是脏数据
    for p in pending:
        if p.id != target.id:
            auto_closed.append(p.code)
        p.locked = True
        p.closed_at = func.now()
        p.closed_by = user.username
        p.version += 1

    log(
        db, action="CLOSE", entity="period", entity_id=target.id, user=user,
        book_id=target.book_id,
        detail=(
            f"结账 {target.code}"
            + (f"，顺带结账 {'、'.join(auto_closed)}" if auto_closed else "")
            + (f"，自动记账 {auto_posted} 张" if auto_posted else "")
        ),
    )
    db.commit()
    return {
        "period": target.code,
        "auto_closed_periods": auto_closed,
        "auto_posted": auto_posted,
    }


def _auto_post(db: Session, period: Period, user: SysUser) -> int:
    rows = db.scalars(
        select(Voucher).where(Voucher.period_id == period.id, Voucher.status == "UNPOSTED")
    ).all()
    for v in rows:
        v.status = "POSTED"
        v.posted_by = f"{user.username}(结账自动)"
        v.posted_at = func.now()
    db.flush()
    return len(rows)


def _check_unposted(db: Session, period: Period) -> int:
    nos = db.scalars(
        select(Voucher.voucher_no).where(
            Voucher.period_id == period.id, Voucher.status == "UNPOSTED"
        ).order_by(Voucher.voucher_no)
    ).all()
    if nos:
        raise ValueError(f"{period.code} 仍有 {len(nos)} 张未记账凭证：{', '.join(nos[:10])}")
    return 0


# --------------------------------------------------------------------- 批量反结账


def preview_unclose(db: Session, book_id: int, period: Period) -> dict:
    """反结账影响范围预览。"""
    locked = list(
        db.scalars(
            select(Period).where(
                Period.book_id == book_id, Period.locked.is_(True), Period.ord >= period.ord
            ).order_by(Period.ord)
        ).all()
    )
    ids = [p.id for p in locked]
    posted_count = db.scalar(
        select(func.count()).select_from(Voucher).where(
            Voucher.period_id.in_(ids), Voucher.status == "POSTED"
        )
    ) or 0
    voucher_total = db.scalar(
        select(func.count()).select_from(Voucher).where(Voucher.period_id.in_(ids))
    ) or 0
    return {
        "from_period": period.code,
        "periods": [p.code for p in locked],
        "will_unpost_vouchers": posted_count,
        "total_vouchers": voucher_total,
    }


def batch_unclose(
    db: Session, book_id: int, period: Period, user: SysUser, revert_posting: bool = True
) -> dict:
    """批量反结账：从指定期间起，连续解锁到最后一个已锁期间。

    区间内的已记账凭证自动回到未记账 —— 账套已解锁而凭证仍标记「已记账」，
    账簿默认口径下余额会凭空消失，用户改完凭证重新记账时数字会跳变。
    """
    locked = list(
        db.execute(
            select(Period)
            .where(Period.book_id == book_id, Period.locked.is_(True), Period.ord >= period.ord)
            .order_by(Period.ord)
            .with_for_update()
        ).scalars()
    )
    if not locked:
        raise ValueError(f"{period.code} 及其之后的期间都未结账，无需反结账")

    preview = preview_unclose(db, book_id, period)
    period_ids = [p.id for p in locked]

    # 先解锁 —— 凭证的写保护触发器是按期间锁判断的，
    # 顺序反了会把自己锁死在门外。
    for p in locked:
        p.locked = False
        p.closed_at = None
        p.closed_by = None
        p.version += 1
    db.flush()

    reverted = 0
    if revert_posting:
        for v in db.scalars(
            select(Voucher).where(
                Voucher.period_id.in_(period_ids), Voucher.status == "POSTED"
            )
        ).all():
            v.status = "UNPOSTED"
            v.posted_at = None
            v.posted_by = None
            reverted += 1
        db.flush()

    log(
        db, action="UNCLOSE", entity="period", entity_id=period.id, user=user,
        book_id=book_id,
        detail=f"反结账 {preview['from_period']} ~ {locked[-1].code}，{len(locked)} 个期间，"
               f"{reverted} 张凭证转未记账",
        after=preview,
    )
    db.commit()
    return {**preview, "unposted_vouchers": reverted}


# --------------------------------------------------------------------- 结转损益


def carry_forward_status(db: Session, period: Period) -> dict:
    """本期是否已结转损益，以及已生成哪几张凭证。

    前端据此把按钮在「执行结转损益」和「重新结转损益」之间切换，
    并在确认框里点名将被删除的凭证 —— 删之前必须让人知道删的是哪几张。
    """
    rows = _carry_forward_vouchers(db, period)
    return {
        "period": period.code,
        "has": bool(rows),
        "vouchers": [v.voucher_no for v in rows],
        # 全都已记账才算已记账：混着来的话重结转会按未记账处理，需要提示用户
        "posted": bool(rows) and all(v.status == "POSTED" for v in rows),
    }


def _carry_forward_vouchers(db: Session, period: Period) -> list[Voucher]:
    return list(
        db.scalars(
            select(Voucher).where(
                Voucher.period_id == period.id, Voucher.source == "CARRY_FORWARD"
            ).order_by(Voucher.id)
        ).all()
    )


def _drop_carry_forward(db: Session, period: Period, existing: list[Voucher],
                        user: SysUser) -> list[str]:
    """删掉本期已生成的结转凭证，为重新结转腾位置。

    只删 ``source == "CARRY_FORWARD"`` 的凭证：同期的手工凭证哪怕金额一样也
    不该被牵连。每张都留下审计快照 —— 凭证号可复用，删掉就再也查不回来了。
    """
    from app.accounting.audit_helper import voucher_snapshot

    removed = []
    for v in existing:
        log(
            db, action="DELETE", entity="voucher", entity_id=v.id, user=user,
            book_id=v.book_id,
            detail=f"重新结转损益：删除旧结转凭证 {v.voucher_no}",
            before=voucher_snapshot(v),
        )
        removed.append(v.voucher_no)
        db.delete(v)
    db.flush()
    return removed


def carry_forward_profit(db: Session, book_id: int, period: Period, user: SysUser,
                         replace: bool = True) -> dict:
    """月末结转损益：把损益类科目余额结转到「本年利润」。

    永远「先删旧、再重算」：本期只要已经有结转凭证，就删掉重新生成。
    财务最常见的动作就是「结完发现金额不对，改完再结一次」，
    让人自己去凭证列表里找出来删，既啰嗦又容易漏。
    """
    period = db.execute(select(Period).where(Period.id == period.id).with_for_update()).scalar_one()
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能结转")

    # 上一个月没结账，就不许先给这个月结转。
    #
    # 结转损益走的是「本年累计」口径：本年利润要的是从年初到现在的总损益。
    # 所以 12 月那张凭证天生就会把 11 月没结走的收入一起扫进来。正常情况下
    # 11 月已经结过转、收入科目余额是 0，扫进来的是 0，没有影响；
    # 但要是 11 月的结转凭证被删了、又没补上，11 月的收入就会顶着 12 月的
    # 凭证走掉 —— 11 月结账时报「已结账」，12 月凭证里却写着 11 月的钱。
    # 拦住入口比事后查账便宜得多：上个月没封口，这个月就别想结转。
    prev = db.scalar(
        select(Period).where(
            Period.book_id == period.book_id, Period.ord == period.ord - 1
        )
    )
    if prev is not None and not prev.locked:
        raise ValueError(
            f"{prev.code} 还没结账，不能先给 {period.code} 结转损益。"
            f"请先结账 {prev.code}。"
        )

    existing = _carry_forward_vouchers(db, period)
    removed: list[str] = []
    keep_posted = False
    if existing:
        # 记账状态要跟着重来：账簿只统计已记账凭证，若旧的是已记账而新生成的是
        # 未记账，用户点一次重结转，账上的数字就会凭空变一笔。
        keep_posted = all(v.status == "POSTED" for v in existing)
        removed = _drop_carry_forward(db, period, existing, user)
        # 旧凭证已经 flush 掉，下面重算余额时看到的才是「未结转」的真实损益
        db.flush()

    profit = _subject_by_code(db, book_id, PROFIT_SUBJECT_CODE)
    if profit is None:
        raise ValueError(f"账套缺少「本年利润」科目（编码 {PROFIT_SUBJECT_CODE}）")

    subjects = list(
        db.scalars(
            select(Subject).where(
                Subject.book_id == book_id,
                Subject.carry_forward.is_(True),
                Subject.active.is_(True),
                Subject.is_leaf.is_(True),
                Subject.id != profit.id,
            ).order_by(Subject.code)
        ).all()
    )

    # 口径：**含未记账**。账簿默认只统计已记账凭证，可反结账会把区间内的凭证
    # 全部退回未记账 —— 那一刻按已记账口径算出来的损益全是 0，
    # 结转损益就会回一句「无需结转」，而用户明明看到满屏的收入凭证。
    # 期末结转要的是「本期实际发生额」，跟记账状态无关；
    # 新生成的结转凭证默认也是未记账，结账时统一自动记账，口径自然对齐。
    eng = LedgerEngine(db, book_id, include_unposted=True)
    fmap = eng.compute([s.id for s in subjects], period.ord)

    income, expense = [], []
    for s in subjects:
        bal = fmap[s.id].closing
        if bal == ZERO:
            continue
        (expense if bal > 0 else income).append((s, abs(bal)))

    if not income and not expense:
        raise ValueError(
            f"{period.code} 损益类科目本期发生额全部为 0，没有可结转的内容"
            + ("（该期间已结账，若要重做请先反结账）" if not removed else "")
        )

    from app.accounting.vouchers import create_voucher

    last_day = date(period.year, period.month, _days_in_month(period.year, period.month))
    created = []

    # 两张凭证一起提交，不各自 commit：replace 模式下旧凭证已经删掉了，
    # 中途失败若留下「删了旧的、只生成一半新的」，比直接报错糟糕得多。
    if income:
        entries = [{"subject_id": s.id, "direction": "DEBIT", "amount": amt,
                    "summary": f"结转{period.code}损益"} for s, amt in income]
        entries.append({"subject_id": profit.id, "direction": "CREDIT",
                        "amount": sum(a for _, a in income), "summary": f"结转{period.code}损益"})
        created.append(create_voucher(
            db, book_id=book_id, user=user, voucher_date=last_day, entries=entries,
            source="CARRY_FORWARD", remark=f"{period.code} 结转损益（收入类）", commit=False))

    if expense:
        entries = [{"subject_id": profit.id, "direction": "DEBIT",
                    "amount": sum(a for _, a in expense), "summary": f"结转{period.code}损益"}]
        entries += [{"subject_id": s.id, "direction": "CREDIT", "amount": amt,
                     "summary": f"结转{period.code}损益"} for s, amt in expense]
        created.append(create_voucher(
            db, book_id=book_id, user=user, voucher_date=last_day, entries=entries,
            source="CARRY_FORWARD", remark=f"{period.code} 结转损益（成本费用类）", commit=False))

    nos = [v.voucher_no for v in created]
    if created and keep_posted:
        from app.accounting.vouchers import post_vouchers

        post_vouchers(db, book_id, [v.id for v in created], user, commit=False)
    db.commit()

    log(
        db, action="CARRY_FORWARD", entity="period", entity_id=period.id, user=user,
        book_id=book_id,
        detail=(
            f"结转损益 {period.code}，生成凭证 {'、'.join(nos)}"
            + (f"，删除旧结转凭证 {'、'.join(removed)}" if removed else "")
        ),
    )
    db.commit()

    return {
        "period": period.code,
        "vouchers": nos,
        "removed": removed,
        "replaced": bool(removed),
        "income": [(s.code, s.name, str(amt)) for s, amt in income],
        "expense": [(s.code, s.name, str(amt)) for s, amt in expense],
    }


def _days_in_month(year: int, month: int) -> int:
    return 31 if month == 12 else (date(year, month + 1, 1) - date(year, month, 1)).days


# --------------------------------------------------------------------- 年度结转


def _last_active_year(db: Session, book_id: int, periods: list[Period]) -> int:
    """最后一个**账上真有动静**的年度，也就是「本年」。

    账套会提前把下一年的 12 个空期间铺好 —— 真实用法就是这样：跨年前把新年
    开好，第二天上班直接进新年记账，不必先去设置里点一次「新增年度」。
    所以 period 表里的最后一年通常是「还没开始的空年度」，不能拿它当本年，
    否则在 2026 年末尾点「结转到 2027 年」，后端却去查 2027 有没有全部结账，
    报出来的年度和用户点的年度对不上。

    判据是「这一年的账上有没有东西」：有过凭证，或写过期初快照。
    两者都没有的年度只是预留的空壳，不参与年度结转。
    """
    years = {p.year for p in periods}

    voucher_years = set(db.scalars(
        select(Period.year)
        .join(Voucher, Voucher.period_id == Period.id)
        .where(Voucher.book_id == book_id)
        .distinct()
    ).all())
    opening_years = set(db.scalars(
        select(Period.year)
        .join(PeriodOpening, PeriodOpening.period_id == Period.id)
        .where(PeriodOpening.book_id == book_id)
        .distinct()
    ).all())

    active = (voucher_years | opening_years) & years
    # 一本全新的账还什么都没记：退回启用年度，让「还没结账」的提示指向对的那一年
    return max(active) if active else min(years)


def _refresh_opening(db: Session, book_id: int, first: Period,
                     fmap: dict, subjects: list[Subject]) -> int:
    """把本年期末余额写成下一年 1 月的期初，返回写入条数。

    「刷新」而不是「叠加」：期初是一次快照，重写就重写。
    这里刻意允许**覆盖**已存在的期初 —— 财务上最常见的动作就是
    「结完 2026 之后又回去改了 2026 的数」，改完必须能再结一次把
    2027 年 1 月的期初带过来，否则新账和旧账的年初数永远对不上。

    但只要下一年已经记了账，就不能动它的期初：
    那时的期初已经被凭证引用，硬改会让已记账的凭证和新期初对不上。
    """
    used = db.scalar(
        select(func.count(Voucher.id))
        .join(Period, Period.id == Voucher.period_id)
        .where(Period.book_id == book_id, Period.year == first.year)
    )
    if used:
        raise ValueError(
            f"{first.year} 年已经有 {used} 张凭证，不能再刷新期初余额。"
            f"要改 {first.year - 1} 年的数，请先反结账并处理掉 {first.year} 年的凭证。")

    # 重写而不是叠加：先清掉这一期旧的快照
    db.query(PeriodOpening).filter(PeriodOpening.period_id == first.id).delete(
        synchronize_session=False)

    count = 0
    for s in subjects:
        amount = fmap[s.id].closing
        if amount == ZERO:
            continue
        db.add(PeriodOpening(book_id=book_id, period_id=first.id,
                             subject_id=s.id, amount=amount))
        count += 1
    return count


def year_end_carry(db: Session, book: Book, user: SysUser,
                   from_year: int | None = None, new_year: int | None = None) -> dict:
    """年度结转：给**同一个账套**追加下一个会计年度。

    以前这里是「新建一个下一年度的账套」—— 复制科目表、复制授权、再把
    本年期末余额搬过去当新账套的期初。后果是账套列表一年一行，
    同一个单位被拆成一堆「租金账套037569 / 租金账套037569 2027」，
    用户每过一年就要重新认一遍哪个才是自己在用的。

    正确做法：年度只是账套的一个属性。结转要做的是
      1) 结转本年利润（落在本年 12 月）
      2) 追加下一年的 12 个会计期间，ord 接着往下排
      3) 把本年期末余额写成下一年 1 月的期初快照
    科目表、授权、期初录入都不用动 —— 它们本来就是账套的属性。
    """
    from app.accounting.vouchers import create_voucher, post_vouchers

    periods = list(
        db.scalars(select(Period).where(Period.book_id == book.id).order_by(Period.ord)).all()
    )
    if not periods:
        raise ValueError("账套尚未初始化会计期间")

    # 「本年」优先取前端传来的那个年度 —— 用户在页面上看的是哪一年，
    # 就结转哪一年，别让后端替用户猜。猜错的话报错年度和按钮上的年度对不上，
    # 用户只会觉得自己点错了。
    if from_year is not None:
        known_years = sorted({p.year for p in periods})
        if from_year not in known_years:
            raise ValueError(
                f"本账套没有 {from_year} 年的会计期间，"
                f"现有年度：{', '.join(str(y) for y in known_years)}")
        current_year = from_year
    else:
        current_year = _last_active_year(db, book.id, periods)

    if new_year is not None and new_year != current_year + 1:
        raise ValueError(
            f"只能结转到紧接的下一个年度（{current_year + 1}），收到的是 {new_year}")
    target_year = current_year + 1

    year_periods = [p for p in periods if p.year == current_year]
    open_periods = [p.code for p in year_periods if not p.locked]
    if open_periods:
        raise ValueError(f"{current_year} 年度结转前必须全部结账，仍未结账：{', '.join(open_periods)}")

    last = year_periods[-1]

    # 1. 结转本年利润
    #    这张凭证必须落在本年最后一个月，若该期已结账则先解锁再补做 ——
    #    年度结转是一键动作，不该要求用户手动反结账一次。
    unlocked_for_carry = False
    profit = _subject_by_code(db, book.id, PROFIT_SUBJECT_CODE)
    retained = _subject_by_code(db, book.id, RETAINED_SUBJECT_CODE)
    if profit and retained:
        eng = LedgerEngine(db, book.id)
        bal = eng.figures(profit.id, last.ord).closing
        if bal != ZERO:
            if last.locked:
                batch_unclose(db, book.id, last, user, revert_posting=True)
                last = db.get(Period, last.id)
                unlocked_for_carry = True
            # 借贷方向必须**按 bal 的正负分两套**。系统内部口径是「借正贷负」
            # （closing = opening + period_dr - period_cr），所以：
            #
            #   盈利 bal<0（本年利润在贷方）→ 借 本年利润 / 贷 未分配利润（留存收益增加）
            #   亏损 bal>0（本年利润在借方）→ 贷 本年利润 / 借 未分配利润（留存收益减少）
            #
            # 之前这里不分盈亏，一律生成「借本年利润 / 贷未分配利润」，
            # 代码注释还声称两种情况都成立 —— 这个判断是错的：
            # 亏损时那样做等于给本年利润**再记一笔借方**，
            # 审计实测 100 的亏损被结成 200（不但没冲平，还翻倍），
            # 未分配利润方向也跟着反。因为分录借贷平衡，系统自检不会报错，
            # 错误会静默沉淀并污染次年每一份报表。
            #
            # 12 月里已有的结转凭证先删掉再重做：改了 2026 的数再点一次年度结转
            # 是常规操作，不删就会叠出第二张结转凭证，未分配利润凭空翻倍。
            stale = [v for v in _carry_forward_vouchers(db, last)
                     if v.remark == f"{current_year}年 结转本年利润"]
            if stale:
                _drop_carry_forward(db, last, stale, user)
                db.commit()

            # 冲平本年利润 = 往它余额的反方向记一笔同额分录。
            # 盈利在贷方（负）→ 借方冲平；亏损在借方（正）→ 贷方冲平。
            amt = abs(bal)
            profit_dir = "DEBIT" if bal < 0 else "CREDIT"
            entries = [
                {"subject_id": profit.id, "direction": profit_dir, "amount": amt,
                 "summary": "结转本年利润"},
                {"subject_id": retained.id,
                 "direction": "CREDIT" if bal < 0 else "DEBIT", "amount": amt,
                 "summary": "结转本年利润"},
            ]
            v = create_voucher(db, book_id=book.id, user=user,
                               voucher_date=date(current_year, 12, 31), entries=entries,
                               source="CARRY_FORWARD", remark=f"{current_year}年 结转本年利润")
            db.commit()

            # 追加凭证后 12 月又变成未记账，重新记账并结账
            post_vouchers(db, book.id, [v.id], user)
            close_period(db, last, user)
    db.commit()

    # 2. 追加下一年的会计期间（ord 跨年度连续，接着往下排）
    new_periods = ensure_periods(db, book, target_year)

    # 3. 下一年 1 月的期初 = 本年期末
    eng = LedgerEngine(db, book.id)
    subjects = db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.is_leaf.is_(True),
                              Subject.active.is_(True))).all()
    fmap = eng.compute([s.id for s in subjects], last.ord)

    first = new_periods[0]
    refreshed = _refresh_opening(db, book.id, first, fmap, subjects)
    count = refreshed
    log(db, action="YEAR_END", entity="book", entity_id=book.id, user=user,
        book_id=book.id,
        detail=f"年度结转：{current_year} → {target_year}，结转 {count} 条期初余额")
    db.commit()
    return {"book_id": book.id, "new_year": target_year, "carried_subjects": count,
            "first_period": first.code, "unlocked_for_carry": unlocked_for_carry}


def ensure_periods(db: Session, book: Book, year: int) -> list[Period]:
    """确保某个年度的 12 个会计期间存在，返回该年度的期间（按 ord 升序）。

    账套第一年从 start_month 开始（启用月之前不出现在账上），
    之后的年度都从 1 月起。已存在就原样返回，不改动任何期间状态。
    """
    rows = list(
        db.scalars(
            select(Period).where(Period.book_id == book.id, Period.year == year)
            .order_by(Period.ord)
        ).all()
    )
    if rows:
        return rows
    months = range(book.start_month, 13) if year == book.start_year else range(1, 13)
    out = [
        Period(book_id=book.id, year=year, month=m,
               ord=period_ord(year, m, book.start_year), locked=False)
        for m in months
    ]
    for p in out:
        db.add(p)
    db.flush()
    return out


def create_periods(db: Session, book: Book) -> list[Period]:
    """建账套时铺第一年的会计期间。"""
    return ensure_periods(db, book, book.start_year)
