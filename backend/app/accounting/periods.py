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
from sqlalchemy.exc import IntegrityError
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

#: 本年利润 / 未分配利润的候选编码，按优先级排。
#:
#: **为什么是「候选」而不是一个码。** 内置科目表换过一套编号：现在默认的是
#: 用友 4-2-2（3103 / 3104 利润分配 → 310415 未分配利润），而存量账套里仍是
#: 上一版《企业会计准则》编号（4103 / 4104 利润分配-未分配利润）。两边的
#: **科目名一样**、**编码不一样**，而这份表已经切换过一次了。
#:
#: 写死一个码的代价是另一套账套直接结不了：年度结转里 ``if profit and retained``
#: 找不到就整段跳过 —— **不报错，本年利润就是不冲平**，比报错难查得多。
#: 所以按顺序试，第一个存在的为准。
PROFIT_SUBJECT_CODES = ("3103", "4103")            # 本年利润
#: 310415 未分配利润是**末级**，能直接记账；3104 利润分配是它的上级，
#: 往非末级科目记账会被凭证接口拒掉，所以顺序不能反。
RETAINED_SUBJECT_CODES = ("310415", "3104", "4104")  # 未分配利润


# --------------------------------------------------------------------- 乐观锁
#
# ``period.version`` 是 P2-04 留下的字段，过去**只有 ``p.version += 1``、没有任何
# 比对**，全项目搜不到一处 ``if version ==`` —— 它是个装饰字段：写了、存了、
# 读得到，唯独不产生任何行为。
#
# 这里把它接上（审计 P1-19）。先说清**它保护的是什么、不保护什么**，
# 否则下一个人还会以为「有 version 就万事大吉」：
#
#   保护：期间**状态**的丢失更新。两个人同时开着 2026-11 的页面，
#         甲把 11 月结账了（version 3→4），乙那边屏幕还停在 version 3，
#         乙再点一次「结账/反结账」时带着 3 过来 → 409，而不是拿着
#         一份过期的账况把状态又改一遍，或者（反结账）把甲刚记的新凭证
#         批量退回未记账。
#   不保护：**期间之内的业务数据**。凭证的增删改、期初的改动，version 一概不动
#         （它们有各自的写保护：已结账期间由 fn_voucher_guard 触发器挡）。
#         要给凭证编辑做乐观锁，得在 voucher 上另设 version 字段，
#         由 ``api/vouchers.py`` 携带与比对 —— 不在本文件范围内。
#
# 为什么 ``expected_version`` 默认 None（不比对）：现有调用方（api/books.py）
# 还没带这个参数，缺省放行是为了不把已有接口打成 400。
# 路由层要真正闭环，需要在请求体里收下 version 并在调用时传进来 ——
# 契约见 ``assert_version`` 的说明。


class PeriodVersionConflict(ValueError):
    """期间版本冲突。

    继承 ValueError 是为了让不认识它的旧调用方也能正常报错而不是 500；
    但**路由层必须在 ``except ValueError`` 之前**单独捕获它并返回 **409**
    （而不是 400）：409 的语义就是「你手里的版本过期了，请刷新后重试」，
    400 会让前端以为是自己参数写错了。
    """


def _fresh_period(db: Session, period_id: int) -> tuple[Period, bool, int]:
    """取一行期间并锁住它，返回 (ORM 对象, 库里的 locked, 库里的 version)。

    **必须用列查询拿 locked / version，不能直接读传入对象上的属性。**
    SQLAlchemy 的 identity map 会把已经加载过的对象原样返回：调用方
    （路由层）早先 ``get_period`` 读过这一行，本会话里它就是那份**旧值**。
    原来 ``target.locked`` 读的就是这个旧值 —— 别人在这期间提交过一次结账，
    本会话照样看到 ``locked=False``。用列查询绕开 identity map 才是真的
    「读库」，``with_for_update()`` 同时把并发写者挡在外面。
    """
    row = db.execute(
        select(Period.id, Period.locked, Period.version)
        .where(Period.id == period_id).with_for_update()
    ).one()
    return db.get(Period, row[0]), bool(row[1]), int(row[2])


def assert_version(expected: int | None, current: int, period_code: str) -> None:
    """比对期间版本；不一致抛 :class:`PeriodVersionConflict`。

    ``expected`` 是 None 表示调用方没有提供版本（例如老前端），此时不比对。
    """
    if expected is None or int(expected) == current:
        return
    raise PeriodVersionConflict(
        f"{period_code} 已被他人操作（你手上的版本 {expected}，库里已是 {current}）。"
        "本次操作没有执行，请刷新页面确认当前状态后再操作。"
    )


def get_period(db: Session, book_id: int, period_id: int) -> Period:
    p = db.get(Period, period_id)
    if p is None or p.book_id != book_id:
        raise ValueError("会计期间不存在")
    return p


def _subject_by_code(db: Session, book_id: int, code: str) -> Optional[Subject]:
    return db.scalar(select(Subject).where(Subject.book_id == book_id, Subject.code == code))


def _subject_by_any_code(db: Session, book_id: int, codes: tuple[str, ...]) -> Optional[Subject]:
    """按候选编码依次查找，取第一个存在的。"""
    for code in codes:
        found = _subject_by_code(db, book_id, code)
        if found is not None:
            return found
    return None


# --------------------------------------------------------------------- 结账


def close_period(db: Session, period: Period, user: SysUser, auto_post: bool = True,
                 commit: bool = True, expected_version: int | None = None) -> dict:
    """关闭指定期间，并顺带关闭其之前所有未结账期间。

    ``commit=False`` 让调用方把多步操作合成一个事务（年度结转用它）。

    ``expected_version`` 是乐观锁（P1-19）：调用方把页面上看到的
    ``period.version`` 传进来，不一致就抛 :class:`PeriodVersionConflict`。
    """
    # P1-19：locked 与 version 都从**库里**读，不读传入对象上的旧值。
    target, locked_now, version_now = _fresh_period(db, period.id)
    assert_version(expected_version, version_now, target.code)
    if locked_now:
        raise ValueError(f"{target.code} 已经结账")

    # 锁链上每一期。列查询拿 locked / version：identity map 里可能是别人读进来
    # 的旧值，直接读属性会把「别人已经结过了」当成没结过。
    #
    # 行锁就是这一句拿的 —— ``FOR UPDATE`` 按**本次查询命中的行**加锁，
    # 锁一直持有到事务结束（不是到这条语句结束）。所以下面那条补 ORM 对象的
    # 查询不需要（也不能）再加 ``with_for_update()``：同一事务里对同一批行
    # 重复加锁只会自查自锁，那是另一种死锁。
    chain_rows = db.execute(
        select(Period.id, Period.locked, Period.version)
        .where(Period.book_id == target.book_id, Period.ord <= target.ord)
        .order_by(Period.ord)
        .with_for_update()
    ).all()
    fresh = {r[0]: (bool(r[1]), int(r[2])) for r in chain_rows}
    # 一条查询补齐整条链的 ORM 对象，替代原来逐个 ``db.get`` 的 N 次往返
    # （十年账套结一次 12 月就是 24 次）。
    #
    # ``populate_existing`` 不能省：``db.get`` 遇到 identity map 里已有的对象
    # 直接原样返回、不发 SQL，而那个对象可能是路由层早先读进来的**旧值**。
    # 列查询不填充 identity map 正是 ``_fresh_period`` 那段注释要绕开的坑，
    # 这里补对象时得主动把它填上，否则绕一圈又踩回去。
    #
    # 也正因为 populate_existing 是「用库里的值盖掉会话里的」，这一句必须待在
    # 任何期间写入之前 —— 放在上锁循环之后就变成拿自己刚写的值盖自己。
    chain = list(db.scalars(
        select(Period)
        .where(Period.id.in_(list(fresh)))
        .order_by(Period.ord)
        .execution_options(populate_existing=True)
    ).all())

    # 整条链都要先过一遍「整理就绪」：断号、时序不对的月份一旦封进历史，
    # 纸质凭证按号装订就跟系统对不上，再想改只能作废重记。
    svc_organize.assert_tidy(db, chain)

    auto_closed, auto_posted = [], 0
    pending = []
    for p in chain:
        if fresh[p.id][0]:
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
    # 结账校验只取「要结转的末级科目」，但**不过滤 active**：
    # 停用 ≠ 删除。一个挂着余额的停用损益科目若被过滤掉，
    # 就能被直接结过去 —— 期间锁了、损益却留在科目上跨年，而且没有任何报错。
    subjects = db.scalars(
        select(Subject).where(
            Subject.book_id == target.book_id,
            Subject.carry_forward.is_(True),
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
        p.version = fresh[p.id][1] + 1

    log(
        db, action="CLOSE", entity="period", entity_id=target.id, user=user,
        book_id=target.book_id,
        detail=(
            f"结账 {target.code}"
            + (f"，顺带结账 {'、'.join(auto_closed)}" if auto_closed else "")
            + (f"，自动记账 {auto_posted} 张" if auto_posted else "")
        ),
    )
    if commit:
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
    db: Session, book_id: int, period: Period, user: SysUser, revert_posting: bool = True,
    commit: bool = True, expected_version: int | None = None,
) -> dict:
    """批量反结账：从指定期间起，连续解锁到最后一个已锁期间。

    区间内的已记账凭证自动回到未记账 —— 账套已解锁而凭证仍标记「已记账」，
    账簿默认口径下余额会凭空消失，用户改完凭证重新记账时数字会跳变。

    ``commit=False`` 让调用方把多步操作合成一个事务（年度结转用它）。

    ``expected_version`` 是乐观锁（P1-19）。反结账尤其需要它：它会把区间内
    的凭证批量退回未记账，屏幕上的版本一变（别人刚反结过账、或刚记了新凭证），
    再点一次就会把对方的工作抹掉。
    """
    # P1-19：与 close_period 同一把钥匙 —— locked / version 一律从库里读。
    target, _locked, version_now = _fresh_period(db, period.id)
    assert_version(expected_version, version_now, target.code)

    # 行锁在下面这条 ``FOR UPDATE`` 上就已拿住整批已锁期间，持有到事务结束，
    # 所以补 ORM 对象时同样不需要再加锁（理由见 close_period 里的同一段注释）。
    locked_rows = db.execute(
        select(Period.id, Period.version)
        .where(Period.book_id == book_id, Period.locked.is_(True), Period.ord >= target.ord)
        .order_by(Period.ord)
        .with_for_update()
    ).all()
    chain_version = {r[0]: int(r[1]) for r in locked_rows}
    # 一条查询补齐整批期间对象，替代逐个 ``db.get``（N 次往返）。
    # populate_existing 同 close_period：identity map 里可能是旧值。
    locked = list(db.scalars(
        select(Period)
        .where(Period.id.in_(list(chain_version)))
        .order_by(Period.ord)
        .execution_options(populate_existing=True)
    ).all())
    if not locked:
        raise ValueError(f"{target.code} 及其之后的期间都未结账，无需反结账")

    preview = preview_unclose(db, book_id, target)
    period_ids = [p.id for p in locked]

    # 先解锁 —— 凭证的写保护触发器是按期间锁判断的，
    # 顺序反了会把自己锁死在门外。
    for p in locked:
        p.locked = False
        p.closed_at = None
        p.closed_by = None
        p.version = chain_version[p.id] + 1
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
    if commit:
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


def _is_system_carry_forward(v: Voucher, period: Period) -> bool:
    """这张 CARRY_FORWARD 凭证是不是**系统自己生成**的那一张。

    ``source == "CARRY_FORWARD"`` 这个标记**不是**「系统生成」的充分条件。
    它曾经是客户端可提交的（``VoucherIn.source`` 的 Literal 里就有），于是
    任何有写权限的人都能造一张挂上这个标记的凭证；而 ``_drop_carry_forward``
    原来只按这个标记删、且不看状态，于是「自己造一张 → 记上账 → 点重新结转」
    能把一张**已记账的**凭证悄悄删掉。正常删除路径（``vouchers.delete_voucher``）
    是拒绝删已记账凭证的，数据库触发器也专门给 CARRY_FORWARD 开了口子让
    「重新结转」能删 —— 也就是说这条例外是**整个删除保护网唯一的缺口**。

    现在从两头堵：

    * **源头**：客户端的 ``source`` 里已经没有 CARRY_FORWARD
      （``api/schemas.py``），写权限只由 ``create_voucher(internal=True)`` 发放。
    * **兜底**：本函数。判据是「签名」—— 系统生成方写死的 remark 尾部，
      加上每条分录都写死的摘要（``_CARRY_FORWARD_SIGNATURES``）。
      两个生成方对签名负责，见 ``carry_forward_profit`` 的 ``SUMMARY``
      与 ``year_end_carry`` 的 ``YEAR_END_SUMMARY``。

    说清楚它的分量：remark 与摘要**不是密码学意义上的防伪标记**，一个铁了心
    伪造的人照样能写对。它挡的是「随手打一个 source」和**修复之前就躺在库里**
    的伪造行 —— 那些行的摘要是人自己写的摘要，签不上。真正的边界是第一条
    （客户端压根提交不了这个 source）。之所以还要它：删除是不可逆的，
    而本系统的凭证号可复用、删掉只能靠审计快照追溯，宁可多认一道。

    认不出来就返回 False，调用方 **fail closed**：不删，报错让人来看。
    绝不「就删有把握的那几张、其余留着」 —— 那等于告诉用户
    「重新结转完成了」，而他的一张已记账凭证还在原地，下次重结转再被拦一次。
    """
    for prefix in (f"{period.code} ", f"{period.year}年 "):
        for suffix, summary in _CARRY_FORWARD_SIGNATURES.items():
            if v.remark == prefix + suffix and v.entries and all(
                e.summary == summary for e in v.entries
            ):
                return True
    return False


#: 结转凭证的系统签名：{remark 尾部: 每条分录都该写的摘要}。
#: remark 前缀由生成场景决定（期间 ``2026-09`` / 年度 ``2026年``），
#: 见 ``_is_system_carry_forward`` 里的两个 prefix。
_CARRY_FORWARD_SIGNATURES = {
    "结转损益（收入类）": "期间损益结转",
    "结转损益（成本费用类）": "期间损益结转",
    "结转本年利润": "结转本年利润",
}


def _drop_carry_forward(db: Session, period: Period, existing: list[Voucher],
                        user: SysUser) -> list[str]:
    """删掉本期已生成的结转凭证，为重新结转腾位置。

    两道范围限制，缺一不可：

    * ``source == "CARRY_FORWARD"``：同期的手工凭证哪怕金额一样也不该被牵连。
    * **签名对得上**（``_is_system_carry_forward``）：这一层才是防伪造的 ——
      标记是客户端曾经能自己写的，只认标记等于「谁都能指定哪张被删」。

    **已记账照样删**，这是本函数存在的理由（``keep_posted`` 那段逻辑要靠它
    重做记账状态；数据库触发器也专门为 CARRY_FORWARD 留了这个口子）。删除
    保护网在这里开口子的安全性，现在完全押在上面的签名检查上 —— 这也是
    它必须 fail closed 的原因。

    每张都留下审计快照 —— 凭证号可复用，删掉就再也查不回来了。
    """
    from app.accounting.audit_helper import voucher_snapshot

    unknown = [v for v in existing if not _is_system_carry_forward(v, period)]
    if unknown:
        raise ValueError(
            f"{period.code} 有一张来源不明的结转凭证（{'、'.join(v.voucher_no for v in unknown)}），"
            "已停止结转、什么都没删。这类凭证不是系统生成的（见「结转凭证的删除保护」），"
            "请先到凭证列表确认它的来历，手工处理后再结转。"
        )

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


def carry_forward_profit(db: Session, book_id: int, period: Period,
                         user: SysUser) -> dict:
    """月末结转损益：把损益类科目余额结转到「本年利润」。

    永远「先删旧、再重算」：本期只要已经有结转凭证，就删掉重新生成。
    财务最常见的动作就是「结完发现金额不对，改完再结一次」，
    让人自己去凭证列表里找出来删，既啰嗦又容易漏。

    P2-3：这里原来有个 ``replace: bool = True`` 形参。它**从未被函数体读过**
    —— 无论传什么都是「先删旧再重算」。而 API 层 ``books.py`` 一路把
    ``payload.replace`` 传了下来，于是 ``replace=false`` 的调用方拿到的是
    「旧凭证已被删除」的结果，与它要的相反，却什么错都没报。

    形参已删：留着它就是在对外承诺一个这个函数从不兑现的开关。请求体里
    那个 ``replace`` 字段也一并降级为「收下但不用」的兼容项（见 schemas）。
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

    profit = _subject_by_any_code(db, book_id, PROFIT_SUBJECT_CODES)
    if profit is None:
        raise ValueError(
            f"账套缺少「本年利润」科目（编码 {'、'.join(PROFIT_SUBJECT_CODES)} 都不是）"
        )

    # 结转损益的科目集合同样不过滤 active：停用的收入/费用科目如果被排除，
    # 它的本年累计就不会被结走，损益永远留在科目上，本年利润也算不对。
    subjects = list(
        db.scalars(
            select(Subject).where(
                Subject.book_id == book_id,
                Subject.carry_forward.is_(True),
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

    # 摘要**不写期间**：这些分录是一个月一笔的动作，凭证日期已经落在月末那天，
    # 期间在凭证头上摆着，摘要再抄一遍「结转 2026-09 损益」只是把同一句话说两遍
    # —— 而且那张凭证日后是要打印出来给人核对的，一行摘要挤掉半个字都不值当。
    # 写死成「期间损益结转」：全仓凡是这一类分录都长这样，肉眼一眼分得出
    # 「这张是系统结转出来的」，不用先去凭证头确认它是哪一期结的。
    #
    # 这行摘要连同下面的 remark 一起构成结转凭证的**系统签名**，
    # ``_is_system_carry_forward`` 靠它把「系统自己生成的」和
    # 「客户端伪造的 CARRY_FORWARD」分开（见那里的说明）。改这里就得同步改那里。
    SUMMARY = "期间损益结转"

    # 两张凭证一起提交，不各自 commit：replace 模式下旧凭证已经删掉了，
    # 中途失败若留下「删了旧的、只生成一半新的」，比直接报错糟糕得多。
    if income:
        entries = [{"subject_id": s.id, "direction": "DEBIT", "amount": amt,
                    "summary": SUMMARY} for s, amt in income]
        entries.append({"subject_id": profit.id, "direction": "CREDIT",
                        "amount": sum(a for _, a in income), "summary": SUMMARY})
        created.append(create_voucher(
            db, book_id=book_id, user=user, voucher_date=last_day, entries=entries,
            source="CARRY_FORWARD", remark=f"{period.code} 结转损益（收入类）",
            internal=True, commit=False))

    if expense:
        entries = [{"subject_id": profit.id, "direction": "DEBIT",
                    "amount": sum(a for _, a in expense), "summary": SUMMARY}]
        entries += [{"subject_id": s.id, "direction": "CREDIT", "amount": amt,
                     "summary": SUMMARY} for s, amt in expense]
        created.append(create_voucher(
            db, book_id=book_id, user=user, voucher_date=last_day, entries=entries,
            source="CARRY_FORWARD", remark=f"{period.code} 结转损益（成本费用类）",
            internal=True, commit=False))

    nos = [v.voucher_no for v in created]
    if created and keep_posted:
        from app.accounting.vouchers import post_vouchers

        post_vouchers(db, book_id, [v.id for v in created], user, commit=False)

    # P1-20：这里原来先 ``db.commit()`` 把凭证落库、再写审计日志、再 commit。
    # 两个事务之间任何失败（磁盘、连接中断、进程被杀）都留下「账已改、审计无记录」
    # 的状态 —— 而本系统的凭证号可回收，审计快照是**唯一**的追溯手段
    # （见 accounting/audit_helper 的自陈）。现在凭证与日志一起提交，
    # 与同文件 ``year_end_carry`` 的单事务做法对齐。
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

    ``subjects`` 必须覆盖**所有有余额的科目**，包括非末级的（审计 P1-1）。
    金额直接取 ``fmap[sid].closing`` —— 那是**子树聚合后**的余额
    （``ledger/engine`` 把分录上卷到各级父科目），不是「这个科目自己那一份」。
    一个科目自己的快照是该科目的**基准总额**，下级只补增量，
    引擎侧的去重口径见 ``LedgerEngine._roll_up_openings`` 的 subtree_total。
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
                   from_year: int | None = None, new_year: int | None = None,
                   commit: bool = True) -> dict:
    """年度结转：给**同一个账套**追加下一个会计年度。

    **全程单一事务**：解锁 12 月 → 删旧结转凭证 → 建结转凭证 → 记账 → 结账
    → 追加次年期间 → 刷新期初，任何一步失败都整体回滚。

    原来这条链路中间 commit 了四次，路由层的 ``db.rollback()`` 只能撤销
    最后一次之后的部分。审计实测：构造「次年已有凭证」让它失败，接口返回 400
    「2027 年已经有 1 张凭证」，但本年利润已经被冲平（-100 → 0）、
    未分配利润已经变成 -100、结转凭证还留在库里。
    失败响应让用户以为「什么都没发生」而重试或改数，
    而库里已经有半成品 —— 这比报错本身危险得多。

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
    profit = _subject_by_any_code(db, book.id, PROFIT_SUBJECT_CODES)
    retained = _subject_by_any_code(db, book.id, RETAINED_SUBJECT_CODES)
    if profit and retained:
        eng = LedgerEngine(db, book.id)
        bal = eng.figures(profit.id, last.ord).closing
        if bal != ZERO:
            if last.locked:
                batch_unclose(db, book.id, last, user, revert_posting=True, commit=False)
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

            # 冲平本年利润 = 往它余额的反方向记一笔同额分录。
            # 盈利在贷方（负）→ 借方冲平；亏损在借方（正）→ 贷方冲平。
            #
            # 摘要与 remark 一起是这张凭证的**系统签名**
            # （``_CARRY_FORWARD_SIGNATURES``）—— 「重新结转」时
            # ``_drop_carry_forward`` 靠它认出这张是系统生成的才肯删。
            # 改这两处字面量必须同步改签名表，否则第二次年度结转会
            # 撞上「来源不明的结转凭证」而拒绝执行。
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
                               source="CARRY_FORWARD", remark=f"{current_year}年 结转本年利润",
                               internal=True, commit=False)

            # 追加凭证后 12 月又变成未记账，重新记账并结账
            post_vouchers(db, book.id, [v.id], user, commit=False)
            close_period(db, last, user, commit=False)

    # 2. 追加下一年的会计期间（ord 跨年度连续，接着往下排）
    new_periods = ensure_periods(db, book, target_year)

    # 3. 下一年 1 月的期初 = 本年期末
    eng = LedgerEngine(db, book.id)
    # **审计 P2-08**：这里原来带 Subject.active 过滤，于是停用科目的期末余额
    # 不会被写进次年期初 ——「停用」这个本该安全的操作变成了账务断层，
    # 次年账凭空少一笔且无任何提示。**绝不能引入 active 过滤**：
    # 停用 ≠ 删除，老凭证还指着它，账还得算得出来。
    #
    # **审计 P1-1**：这里原来还带 ``is_leaf.is_(True)``。同一个缺陷，
    # 触发条件从 ``active`` 换成了 ``is_leaf`` —— 而 ``is_leaf`` 恰恰是
    # 服务端派生的字段，派生化把「能不能升格」的判断从人工交给了系统。
    #
    # 失效场景：100201 银行存款-工行 1–5 月已记账借记 1000；6 月在它下面
    # 建了子科目 10020101 并录期初 0 → 100201 因非末级被排除（期末 1000
    # 不写进次年）、10020101 期末为 0 被跳过 → 次年 100201 期初归 0，
    # 1000 元凭空消失、零提示，科目余额表还会试算不平。
    #
    # 现在按「**有余额的科目**」选取：一个都不漏，含非末级。金额取
    # ``closing``，而它本身就是引擎上卷后的**子树合计**（分录已上卷到
    # 各级父科目），父级自己那一份钱自然含在里面。
    # 「父子各写一条同额快照会不会被算两遍」不会 —— 引擎读期初时以自身
    # 快照为基准、下级只补增量，见 ``LedgerEngine._roll_up_openings``。
    subjects = db.scalars(select(Subject).where(Subject.book_id == book.id)).all()
    fmap = eng.compute([s.id for s in subjects], last.ord)

    first = new_periods[0]
    refreshed = _refresh_opening(db, book.id, first, fmap, subjects)
    count = refreshed
    log(db, action="YEAR_END", entity="book", entity_id=book.id, user=user,
        book_id=book.id,
        detail=f"年度结转：{current_year} → {target_year}，结转 {count} 条期初余额")
    if commit:
        db.commit()
    return {"book_id": book.id, "new_year": target_year, "carried_subjects": count,
            "first_period": first.code, "unlocked_for_carry": unlocked_for_carry}


def ensure_periods(db: Session, book: Book, year: int) -> list[Period]:
    """确保某个年度的 12 个会计期间存在，返回该年度的期间（按 ord 升序）。

    账套第一年从 start_month 开始（启用月之前不出现在账上），
    之后的年度都从 1 月起。已存在就原样返回，不改动任何期间状态。

    **P3-11：check-then-act 必须有锁。** 原来这里是「查一遍、没有就插」，
    两个并发的年度结转（同一个账套两个人同时点，或前端重试）
    会双双查到空、都去插入，第二个撞上 ``period`` 上的唯一约束，
    抛出一条裸的 ``IntegrityError`` 到 500 —— 账没建上，用户只看到一个
    「Internal Server Error」，连哪一步失败都不知道。

    两道处理，缺一不可：

    1. **先锁账套行**（``book.id`` 上 ``FOR UPDATE``）。同一个账套的
       建期间被串行化，后一个进来时前一个已经提交，重查就能看到结果。
       锁顺序是「期间 → 账套」（``year_end_carry`` 先结账再 ``ensure_periods``），
       全项目只有本文件用行锁，不会与别的路径构成死锁环。
    2. **唯一约束兜底 + 友好报错**。锁不住的场景（锁之前就已经有人插进来，
       或者别的入口绕过锁）由 savepoint 接住：回滚的只是这批 insert，
       **不会把调用方（年度结转）已经做的工作一起回滚**。
    """
    db.execute(select(Book.id).where(Book.id == book.id).with_for_update())

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
    try:
        # savepoint 而不是裸 flush：IntegrityError 会把整个事务标记为失败，
        # 而 ensure_periods 是被 year_end_carry 当中途步骤调用的，
        # 一次并发撞车不该把前面解锁 12 月、结转损益的成果一起废掉。
        with db.begin_nested():
            for p in out:
                db.add(p)
            db.flush()
    except IntegrityError:
        # 并发的另一方已经铺好了：以库里的现状为准，不报错。
        db.expire_all()
        rows = list(
            db.scalars(
                select(Period).where(Period.book_id == book.id, Period.year == year)
                .order_by(Period.ord)
            ).all()
        )
        if rows:
            return rows
        raise ValueError(
            f"{year} 年的会计期间创建失败：库里仍然没有这一年的期间，"
            f"但插入时被数据库拒绝（账套 {book.id}）。"
            "请检查该账套的期间数据是否有重复。"
        )
    return out


def create_periods(db: Session, book: Book) -> list[Period]:
    """建账套时铺第一年的会计期间。"""
    return ensure_periods(db, book, book.start_year)
