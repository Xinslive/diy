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

import hashlib
import hmac
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


def _subject_by_code(db: Session, book_id: int, year: int, code: str) -> Optional[Subject]:
    """按编码取**某个年度**的科目。

    年度是必填参数而不是可选项：科目表现在每个年度各有一份，
    「1002 银行存款」在 2026 和 2027 是两行不同的记录。不带年度去取，
    拿到的可能是另一个年度的科目 —— 而它上面挂着的分录属于另一年。
    """
    return db.scalar(select(Subject).where(
        Subject.book_id == book_id, Subject.year == year, Subject.code == code))


def _subject_by_any_code(db: Session, book_id: int, year: int,
                         codes: tuple[str, ...]) -> Optional[Subject]:
    """按候选编码依次查找，取第一个存在的。"""
    for code in codes:
        found = _subject_by_code(db, book_id, year, code)
        if found is not None:
            return found
    return None


# --------------------------------------------------------------------- 本年利润科目
#
# 「本年利润」是结转损益的**目的科目**。它原先只能靠编码猜，而科目表是
# 各人自己的 —— 编码改成 3111 的有、叫「本年损益」的也有。识别不出来时
# 账就结不了转，**而界面上没有任何地方能改**（这一版之前确实没有）。
#
# 所以这里有**两级口径**，且顺序是「手工设定优先」：
#
#   1. 用户在「期末结账 → 结转损益」里手工指定的科目（``book_year.profit_subject_code``）
#   2. 候选编码自动识别（``PROFIT_SUBJECT_CODES``）
#
# 手工设定只覆盖「用哪一个科目」，**不改变识别失败时的行为**：两级都找不到，
# 照样报错 —— 那是「这套账还没有本年利润科目」的事实，不该被糊弄过去。
#
# 设定值失效时（那张科目表被整表换掉了，编码不在了）**回落到自动识别**，
# 而不是拿一个不存在的科目去记账。回落这件事要让人看见，所以接口把
# 「设的是什么」与「实际用的是什么」两件事分开返回。

#: 本年利润科目的来源标记。
PROFIT_SOURCE_SETTING = "setting"   # 用户手工指定
PROFIT_SOURCE_AUTO = "auto"         # 按候选编码自动识别
PROFIT_SOURCE_MISSING = "missing"   # 两级都没有


def profit_subject_setting(db: Session, book_id: int, year: int) -> Optional[str]:
    """用户手工设定的「本年利润」科目编码。**没设过就是 None**。"""
    from app.accounting.years import book_year_of

    row = book_year_of(db, book_id, year)
    if row is None:
        return None
    code = (row.profit_subject_code or "").strip()
    return code or None


def set_profit_subject_setting(db: Session, book_id: int, year: int,
                                code: Optional[str]) -> Optional[str]:
    """写下（或清掉）「本年利润」科目的手工设定值。

    年度账套行按需创建 —— 设定的对象是「这个年度的科目表里的某一个科目」，
    没有年度行就意味着这个年度还没有科目表，而那种情况下也压根没有科目可设。
    """
    from app.accounting.years import book_year_of, ensure_book_year

    value = (code or "").strip() or None
    row = book_year_of(db, book_id, year)
    if row is None:
        book = db.get(Book, book_id)
        if book is None:
            raise ValueError(f"账套 {book_id} 不存在")
        row = ensure_book_year(db, book, year)
    row.profit_subject_code = value
    db.flush()
    return value


def resolve_profit_subject(db: Session, book_id: int, year: int) -> tuple[Optional[Subject], str]:
    """本年利润科目。返回 ``(科目, 来源)``，找不到时科目是 ``None``。

    **所有用到本年利润的地方都要走这里** —— 结转损益、结转科目集合
    （排除本年利润）、年度结转（冲平本年利润）。三处各写一份的话，
    「手工设定」这条覆盖只会在其中一处生效，另两处照样按编码猜，
    而用户看到的现象是「我明明设了，结转还是结不了」。
    """
    setting = profit_subject_setting(db, book_id, year)
    if setting:
        found = _subject_by_code(db, book_id, year, setting)
        # 设定的科目不在这一年的科目表里（整表换过了）→ 回落自动识别，
        # 但**不覆盖设定值**：用户设的东西还在，界面上会提示「设的是 X、
        # 实际用的是 Y」，要不要改由用户决定。
        if found is not None:
            return found, PROFIT_SOURCE_SETTING
    found = _subject_by_any_code(db, book_id, year, PROFIT_SUBJECT_CODES)
    if found is not None:
        return found, PROFIT_SOURCE_AUTO
    return None, PROFIT_SOURCE_MISSING


# --------------------------------------------------------------------- 结转科目集合
#
# 「哪些科目参与结转损益」**只有这一份定义**（审计 P1-1 / P1-2）。
#
# 以前有两份，而且口径不同：
#   · ``carry_forward_profit``：carry_forward=True + is_leaf=True + 排除本年利润
#   · ``close_period`` 的结平校验：carry_forward=True + is_leaf=True（**不排除本年利润**）
# 两份不一致的直接后果：**本年利润一旦被标成结转科目，该期间永远结不了账** ——
# 结转集合里根本没有它，它永远结不平，而结平校验又一直盯着它。
#
# 收口之后两条路径用同一个 helper，判据也统一成三条：
#   ① carry_forward is True   —— 用户可以关掉某个损益科目的结转（这一条不能少！
#                                 少了它，「故意不结转」就会失效，损益永远挂着）
#   ② is_leaf is True         —— 汇总级科目不参与
#   ③ category == "PROFIT"    —— **只有损益类科目才能结转**（这一条是 P1-1 的要害）
# 并且**永远排除本年利润** —— 它是结转的**目的科目**，不是被结转的对象。
#
# ③ 这条为什么必须在服务端：科目表导入按外部表的「类型」列自动置
# ``carry_forward``（``accounting/subject_importer.py``），那张表的「类型」列
# 写错一个词，资产科目就被标成结转科目，随后任何一次月末结转都会把它的余额
# 整体搬进本年利润 —— 钱凭空消失，而**借贷仍然平衡，系统自检抓不到**。
#
# ---- 这里**只有一道**有效防线（审查修复）------------------------------
# 以前的注释说「写侧有校验（``api/subjects.py``）拦一道，读侧再按 category
# 过滤一次，两道都留着」。那是**循环论证**：``category`` 本身是从同一列
# 「类型」推导出来的（``subject_importer.py`` 里 ``_CATEGORY_MAP`` →
# ``carry_forward = category == "PROFIT"``），于是「按 category 过滤」与
# 「按 carry_forward 过滤」判的是**同一件事**，不是两道独立的防线。
#
# 事实是：
#   · **写侧**（``api/subjects.py``）有一道**真正独立**的校验 ——
#     「carry_forward 只许落在损益类科目上」。它独立于「类型」列：用户手工
#     勾「结转科目」而类别是资产类，当场被拒。
#   · **导入侧**的那一道以前与写侧同源（都从「类型」列来），所以导入进来的
#     脏标记**读侧兜不住**。导入侧现在补了一道**独立于「类型」列**的校验：
#     科目编码的首位（大类码）与「类型」必须一致（见
#     ``subject_importer`` 里的说明与那里的实现）—— 两份证据对不上就拒。
#   · **读侧**的 ``category == "PROFIT"`` 过滤本身**不是独立防线**，
#     它只是把写侧的判据再表述一遍。它的价值在于：万一库里已经躺着脏标记
#     （导入进来的、或历史遗留的），结转时只认真正的损益类科目，
#     不会把资产科目的余额搬进本年利润。
def carry_forward_subjects(db: Session, book_id: int, year: int, *,
                            exclude_profit: bool = True) -> list[Subject]:
    """参与结转损益的科目集合。**结转与结平校验共用这一份定义。**

    ``exclude_profit=True``（唯一正确的用法）把本年利润排除在外。
    传 False 只会得到一个自相矛盾的集合（本年利润会把它自己结给自己），
    留着这个参数只是为了把「排除本年利润」这件事写成显式契约。

    ``year`` 是必填：科目表每个年度各有一份，结转集合必须取**被结转那个
    期间所属年度**的科目。取错年度的话，结出来的是另一张科目表上的余额。
    """
    stmt = select(Subject).where(
        Subject.book_id == book_id,
        Subject.year == year,
        Subject.carry_forward.is_(True),
        Subject.is_leaf.is_(True),
        Subject.category == "PROFIT",
    )
    if exclude_profit:
        # **排除的是「实际会被当成目的科目的那一个」**（``resolve_profit_subject``），
        # 外加自动识别的那一个 —— 两个都排掉：
        #
        #   · 手工设定优先：用户把本年利润指定成 3111，被结转的集合里就不能再有它，
        #     否则它会**结转给自己**（自己冲自己，借贷仍然平衡，系统自检抓不到）。
        #   · 再排掉自动识别的那一个：不排的话，用户设了 3111 之后，
        #     旧口径认的 4103 反而会被当成普通损益科目结走 —— 那是行为回退。
        excluded = _excluded_profit_ids(db, book_id, year)
        if excluded:
            stmt = stmt.where(Subject.id.notin_(excluded))
    return list(db.scalars(stmt.order_by(Subject.code)).all())


def _excluded_profit_ids(db: Session, book_id: int, year: int) -> set[int]:
    """结转时必须排除的「本年利润」科目 id 集合（见 ``carry_forward_subjects``）。"""
    ids: set[int] = set()
    resolved, _ = resolve_profit_subject(db, book_id, year)
    if resolved is not None:
        ids.add(resolved.id)
    auto = _subject_by_any_code(db, book_id, year, PROFIT_SUBJECT_CODES)
    if auto is not None:
        ids.add(auto.id)
    return ids


def current_accounting_year(db: Session, book: Book) -> int:
    """这个账套**当前在记的那一个会计年度**（审计 P2-5）。

    口径：**最近一期有凭证的期间所在的年度**；一张凭证都没有时退回账套的
    启用年度（``book.start_year``）。

    为什么不是「服务器当前日历年」：系统的会计年度是**账套的属性**
    （``book.start_year`` + ``period.year``，见 06a97b4「期初余额改成
    会计年度口径」），而 ``CURRENT_DATE`` 是**服务器当前日期**加上
    **连接会话时区**（本项目的连接池把时区钉死成 UTC）。于是：
      · 每年 1 月 1 日的 8 小时窗口里，北京时间已经跨年、UTC 还没跨年；
      · 给一本 2026 年建的账补录 2025 年的分录时，「本年」算的是 2026，
        而用户正在操作的是 2025 —— 守卫判的是错的那个年。

    这里取「有凭证的最近年度」，是因为它就是**用户正在记的那一年**：
    账上一有动静，它立刻跟着走；老账套补录旧年度时，它也不会被服务器的
    日历年带偏。
    """
    year = db.scalar(
        select(func.max(Period.year))
        .join(Voucher, Voucher.period_id == Period.id)
        .where(Period.book_id == book.id)
    )
    return int(year) if year is not None else int(book.start_year)


# --------------------------------------------------------------------- 结账


def close_period(db: Session, period: Period, user: SysUser, auto_post: bool = True,
                 commit: bool = True, expected_version: int | None = None) -> dict:
    """关闭指定期间，并顺带关闭其之前所有未结账期间。

    ``commit=False`` 让调用方把多步操作合成一个事务（年度结转用它）。

    ``expected_version`` 是乐观锁（P1-19）：调用方把页面上看到的
    ``period.version`` 传进来，不一致就抛 :class:`PeriodVersionConflict`。

    **一张凭证都没有的期间结不了账**，没有旁路 —— 理由与后果见下面那道闸门。
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

    # 链上**将被新锁上**的期间。已结账的跳过 —— 它们早就封存了，
    # 里面有没有凭证是既成事实，拦不住也不该拦。
    to_lock = [p for p in chain if not fresh[p.id][0]]

    # 空月份不许结账，没有旁路。**而且是对链上每一期**（审计 P2-1）。
    #
    # 以前一张凭证都没有的期间也能结，而且三道闸门**恰好都对它放行**：
    # 整理体检对空集通过，自动记账记 0 张，损益检查因为余额全为零也通过。
    # 于是「这个月的账忘了录」被静默封进历史 —— 期间一锁，补凭证就得反结账，
    # 而反结账会连带把后面已经结好的月份全部退回，一笔糊涂账就是这么来的。
    #
    # 以前这道闸**只看被结的那一期**，于是「1~11 月一张凭证都没有、只有
    # 12 月有」时，点「结账 12 月」会把 11 个空月份**静默封进历史** ——
    # 后果与这道闸要防的一模一样，工作量还更大（要连带反结账 12 个月、
    # 批量退回所有已记账凭证）。闸门挡住了「直接结那个空月」，却没挡住
    # 「结后面的月时把空月顺带封掉」。
    #
    # 曾经想过留一个「我确认本月无业务」的旁路，否掉了：那个确认拦不住它 ——
    # 人会点确定，而点确定的人多半正是没意识到自己漏录了的人。
    # 真的要表达「本月无业务」，办法是录入凭证（哪怕只是一张说明凭证），
    # 那张凭证本身就是「这个月我看过、是空的」的记录。
    #
    # 一条 group by 拿到链上各期的凭证数，不是一期一条。
    counts: dict[int, int] = {}
    if to_lock:
        counts = dict(
            db.execute(
                select(Voucher.period_id, func.count())
                .where(Voucher.period_id.in_([p.id for p in to_lock]))
                .group_by(Voucher.period_id)
            ).all()
        )
    # ---- 跨年链：显式拒绝（审查修复）----------------------------------
    #
    # 级联结账的链是「ord <= 被结那一期的全部未结账期间」，而 **ord 跨年度
    # 连续** —— 链是可以跨年的。以前跨年链是被**默默放行**的，而它有两处
    # 致命后果：
    #
    #   ① 损益结平校验按**被校验期间自己的年度**才有意义（科目表每个年度
    #      各有一份，见下面那段）。以前只取 target 那一年，于是链上属于
    #      其他年度的期间用的是**另一年的科目行** —— 那些行在那些期间既
    #      没有分录也没有期初快照，closing 恒为 0，于是**被判成「已结平」**：
    #      前几年挂在损益科目上的余额就这么被放过去了。
    #   ② 跨年结账会把上一年的期间一起锁上，而上一年的账可能压根还没结转
    #      损益 —— 那是不可逆的封存。
    #
    # 所以这条链一旦跨年就**当场拒绝**，并说清下一步该做什么。
    #
    # 放在「空月份不许结账」那道闸**之前**：那是结构性的判断（你要做的
    # 这件事本身就不该做），而空月份是数据问题 —— 先说结构性的那条，
    # 用户才不会以为「补几张凭证就能跨年结账」。
    chain_years = sorted({p.year for p in to_lock})
    if len(chain_years) > 1:
        first_year, last_year = chain_years[0], chain_years[-1]
        raise ValueError(
            f"结账链跨了年度（{'、'.join(str(y) for y in chain_years)}），已停止结账："
            f"级联结账会把 {first_year} 年的期间一起锁上，而每个年度的科目表是两套 —— "
            f"跨年的损益结平校验会拿错科目表算，等于没校验。"
            f"请先结完上一年度（{first_year} 年）的全部期间，再结 {last_year} 年的期间。"
        )

    empty = [p for p in to_lock if not counts.get(p.id)]
    if empty:
        raise ValueError(
            f"{'、'.join(p.code for p in empty)} 没有任何凭证，不能结账。"
            "结账会把这些月份一起锁上，而期间一锁，补凭证就得反结账"
            "（连带把后面已经结好的月份全部退回）。"
            "请确认这些月份是不是漏录了；若确实没有业务，"
            "请录入凭证（哪怕只是一张说明凭证）后再结账。"
        )

    auto_closed, auto_posted = [], 0
    pending = []
    for p in to_lock:
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
    eng = LedgerEngine.for_ledger(db, target.book_id)
    # 结账校验取的是**「要结转的末级科目」**—— 与结转损益用的是**同一个
    # helper**（``carry_forward_subjects``，审计 P1-1 / P1-2）。以前这里是
    # 另写一份（不排除本年利润、也不看 category），两份口径不一致的直接
    # 后果就是「本年利润一旦被标成结转科目，该期间永远结不了账」。
    #
    # 不过滤 active：停用 ≠ 删除。一个挂着余额的停用损益科目若被过滤掉，
    # 就能被直接结过去 —— 期间锁了、损益却留在科目上跨年，而且没有任何报错。
    #
    # ---- 科目表按**被校验期间自己的年度**取（审查修复）----------------
    # 科目表每个年度各有一份，而结平校验要问的是「**这一期**的损益科目在
    # **这一期**还剩多少余额」。以前这里只取 ``target.year`` 那一套 ——
    # 链上跨年的那些期间（虽然现在已经被上面那道闸挡住了）会拿到**另一年
    # 的科目行**：那些行在那些期间既没有分录也没有期初快照，closing 恒为 0，
    # 于是**被判成「已结平」**，损益余额就这么被放过去了。
    #
    # 同一个年度只查一次（``carry_forward_subjects`` 每次调用都发一条查询），
    # 不是每期查一次。
    by_year: dict[int, list[Subject]] = {}
    for year in chain_years:
        by_year[year] = carry_forward_subjects(db, target.book_id, year)
    subject_ids = sorted({s.id for subs in by_year.values() for s in subs})
    # 一次算到最后一期，中间每一期的中间结果都会留在缓存里
    eng.compute(subject_ids, max(p.ord for p in pending))
    unsettled = []
    for p in pending:
        residual = [
            f"{s.code} {s.name} 余额 {f.closing}"
            for s in by_year[p.year]
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
        from app.plugins.hooks import period_closed
        period_closed(db, user_id=user.id, book_id=p.book_id, period_id=p.id)

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


def _signature_key() -> bytes:
    """结转凭证签名用的密钥 —— **由服务端配置派生，不出现在库里**。

    取自 ``JWT_SECRET``（生产环境强制校验、fail-closed，见
    ``app/core/config.py``），再按用途派生一把子密钥，免得与签发令牌共用
    同一段密钥材料。

    说清楚它的分量：开发 / 测试环境里 ``JWT_SECRET`` 是公开的默认值，
    那时这道签名挡的是「随手打一个 source」，不是「铁了心伪造」；
    生产环境里它是真秘密，伪造不出签名。
    """
    from app.core.config import settings

    return hmac.new(
        settings.jwt_secret.encode("utf-8"),
        b"cwq/carry-forward-signature/v1",
        hashlib.sha256,
    ).digest()


def _prev_carry_forward_id(db: Session, v: Voucher) -> int:
    """同一期间里排在它前面的**最后一张**系统结转凭证 id（没有就是 0）。"""
    prev = db.scalar(
        select(func.max(Voucher.id)).where(
            Voucher.period_id == v.period_id,
            Voucher.source == "CARRY_FORWARD",
            Voucher.id < v.id,
        )
    )
    return int(prev or 0)


def _carry_forward_token(db: Session, v: Voucher) -> str:
    """结转凭证签名里的**不可猜测成分**。

    ``HMAC(服务端密钥, 账套 + 凭证 id + 前一张系统结转凭证 id)``。

    为什么是这三样：凭证 id 绑住「这一行」；前一张系统凭证 id 把同一期
    的几张结转凭证**串成一条链**（动一张、其余的签名就都对不上）；
    密钥在服务端，没有它谁也算不出这个值。
    """
    raw = f"{v.book_id}:{v.id}:{_prev_carry_forward_id(db, v)}".encode("utf-8")
    return hmac.new(_signature_key(), raw, hashlib.sha256).hexdigest()[:16]


def _remark_token(remark: str, base: str) -> str | None:
    """从 remark 里取出签名尾巴；形状不对返回 None。

    形状是 ``<base> #<16 位十六进制>``。
    """
    if not remark.startswith(base + " #"):
        return None
    token = remark[len(base) + 2:]
    if len(token) != 16:
        return None
    try:
        int(token, 16)
    except ValueError:
        return None
    return token


def _remark_matches(remark: str, base: str) -> bool:
    """remark 是 base 本身，或者是 base + 签名尾巴。"""
    return remark == base or _remark_token(remark, base) is not None


def _sign_carry_forward(db: Session, v: Voucher, remark: str) -> str:
    """给刚生成的结转凭证 remark 补上签名尾巴，返回最终 remark。

    调用点在 ``create_voucher`` **之后**（凭证 id 那时才拿到）。

    审计快照里记的是**签名之前**的 remark（快照在 ``create_voucher`` 内部
    就拍了）：那不影响追溯 —— 快照里的 remark 照样能认出这是哪张结转凭证，
    签名尾巴只用于「能不能被『重新结转』删掉」这一项判定。
    """
    v.remark = f"{remark} #{_carry_forward_token(db, v)}"
    db.flush()
    return v.remark


def _signed_era_start(db: Session, book_id: int) -> int | None:
    """这个账套里**第一张带签名**的结转凭证 id（还没有就是 None）。

    它是「存量老凭证」与「伪造」的分界线：系统从启用签名的那一刻起，
    生成的每一张结转凭证都带签名尾巴 —— 所以**在第一张带签名的凭证之后**
    出现的、remark 里没有签名尾巴的 CARRY_FORWARD 凭证，一定不是系统
    生成的（升级前的存量凭证 id 都在它之前，仍然认）。

    没有它的话，「兼容老签名」这一条就等于给伪造开了一条永久的后门：
    伪造者只要照抄老 remark 的样子就能让凭证被「重新结转」删掉。
    """
    return db.scalar(
        select(func.min(Voucher.id)).where(
            Voucher.book_id == book_id,
            Voucher.source == "CARRY_FORWARD",
            # PostgreSQL 的正则匹配运算符 ``~``：remark 末尾那一段
            # ``#<16 位十六进制>`` 就是签名尾巴。
            Voucher.remark.op("~")(r"#[0-9a-f]{16}$"),
        )
    )


def _is_system_carry_forward(v: Voucher, period: Period, db: Session,
                             era: int | None = None) -> bool:
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
      加上每条分录都写死的摘要（``_CARRY_FORWARD_SIGNATURES``），
      **再加上 remark 末尾那段不可猜测的签名尾巴**。

    ---- 签名尾巴（审查修复）------------------------------------------
    原来的签名只有 remark + 摘要，**两样都是人能写对的**：伪造一张
    remark 写着「2026-09 结转损益（收入类）」、摘要写着「期间损益结转」的
    凭证，「重新结转」就会把它当成系统生成的那张删掉 —— 而它可能已经记账。
    这道口子正是「删除保护网唯一的缺口」。

    现在 remark 末尾多一段 ``#<16 位十六进制>``，它是
    ``HMAC(服务端密钥, 账套 + 凭证 id + 前一张系统结转凭证 id)``：
    **没有服务端密钥算不出来**，伪造者哪怕把 remark 与摘要一字不差地抄对，
    也签不上。

    **向后兼容**：签名尾巴是**后加**的，库里已有的系统结转凭证（remark
    里没有 ``#`` 那一段）**仍然认得出** —— 判据是「remark 尾部对得上
    且每条分录摘要都对得上」。没有这一条，一次上线就会让所有存量账套
    的「重新结转」全部报「来源不明的结转凭证」。

    认不出来就返回 False，调用方 **fail closed**：不删，报错让人来看。
    绝不「就删有把握的那几张、其余留着」 —— 那等于告诉用户
    「重新结转完成了」，而他的一张已记账凭证还在原地，下次重结转再被拦一次。
    """
    if not v.entries:
        return False
    for prefix in (f"{period.code} ", f"{period.year}年 "):
        for suffix, summary in _CARRY_FORWARD_SIGNATURES.items():
            if not all(e.summary == summary for e in v.entries):
                continue
            base = prefix + suffix
            if v.remark == base:
                # **存量老凭证**：没有签名尾巴，但 remark 与摘要都对得上。
                # 仍然认 —— 否则一次上线就会让所有存量账套的「重新结转」
                # 全部报「来源不明的结转凭证」。
                #
                # 但只认**升级之前生成的**：这个账套里第一张带签名的结转
                # 凭证 id 就是分界线（见 ``_signed_era_start``）——
                # 在那之后出现的无签名凭证一定是伪造的。
                if era is None:
                    era = _signed_era_start(db, v.book_id)
                if era is None or v.id < era:
                    return True
                continue
            token = _remark_token(v.remark, base)
            if token is not None and hmac.compare_digest(
                token, _carry_forward_token(db, v)
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

    era = _signed_era_start(db, period.book_id)
    unknown = [v for v in existing if not _is_system_carry_forward(v, period, db, era=era)]
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

    profit, _source = resolve_profit_subject(db, book_id, period.year)
    if profit is None:
        # 报错要说清**下一步做什么**。原来那句「账套缺少「本年利润」科目」只说了
        # 现象，用户既不知道该去哪儿改，也不知道这件事能改 —— 而这一版之前
        # 确实无处可改，只能干看着。
        raise ValueError(
            f"{period.year} 年的科目表里没有「本年利润」科目"
            f"（自动识别的编码 {'、'.join(PROFIT_SUBJECT_CODES)} 都不是）。"
            f"请到「期末结账 → 结转损益 → 设置本年利润科目」里指定一个。"
        )

    # 结转损益的科目集合 = **共享 helper**（``carry_forward_subjects``）。
    #
    # 以前这里是另写一份：carry_forward=True + is_leaf=True + 排除本年利润。
    # 现在连「只看损益类科目」也一起收进来了 —— 读侧不再信任 ``carry_forward``
    # 这个标记，**只认真正的损益类科目**（审计 P1-1）。于是库里已经存在的
    # 脏标记（资产科目被勾成结转科目）不会再把它的余额搬进本年利润。
    #
    # 不过滤 active：停用的收入/费用科目如果被排除，
    # 它的本年累计就不会被结走，损益永远留在科目上，本年利润也算不对。
    subjects = carry_forward_subjects(db, book_id, period.year)

    # 口径：**含未记账**。账簿默认只统计已记账凭证，可反结账会把区间内的凭证
    # 全部退回未记账 —— 那一刻按已记账口径算出来的损益全是 0，
    # 结转损益就会回一句「无需结转」，而用户明明看到满屏的收入凭证。
    # 期末结转要的是「本期实际发生额」，跟记账状态无关；
    # 新生成的结转凭证默认也是未记账，结账时统一自动记账，口径自然对齐。
    eng = LedgerEngine.for_ledger(db, book_id, include_unposted=True)
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
        v = create_voucher(
            db, book_id=book_id, user=user, voucher_date=last_day, entries=entries,
            source="CARRY_FORWARD", remark=f"{period.code} 结转损益（收入类）",
            internal=True, commit=False)
        # remark 末尾补签名尾巴（不可猜测的那一段，见 ``_carry_forward_token``）。
        # 凭证 id 这时才拿到，所以签名只能落在 create_voucher 之后。
        _sign_carry_forward(db, v, f"{period.code} 结转损益（收入类）")
        created.append(v)

    if expense:
        entries = [{"subject_id": profit.id, "direction": "DEBIT",
                    "amount": sum(a for _, a in expense), "summary": SUMMARY}]
        entries += [{"subject_id": s.id, "direction": "CREDIT", "amount": amt,
                     "summary": SUMMARY} for s, amt in expense]
        v = create_voucher(
            db, book_id=book_id, user=user, voucher_date=last_day, entries=entries,
            source="CARRY_FORWARD", remark=f"{period.code} 结转损益（成本费用类）",
            internal=True, commit=False)
        _sign_carry_forward(db, v, f"{period.code} 结转损益（成本费用类）")
        created.append(v)

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


def posted_voucher_count(db: Session, book_id: int, year: int) -> int:
    """某年度**已记账**（POSTED）凭证的数量。

    「这一年的期初还能不能被改」只有一条判据，就是这个数：

    * 账簿只统计已记账凭证（``LedgerEngine._status_filter``），所以年度内
      一张已记账凭证都没有时，这笔期初**没被任何账簿、报表引用过** ——
      改它动不了任何已经算出来的数。
    * **未记账的凭证不算**：它们不进账簿，改年度期初跟它们不冲突。
    * 已结账的年度自动被挡住：结账会把该期凭证记账（``_auto_post``），
      所以有锁的年度必然有已记账凭证，不需要再单判一次锁。

    ---- 为什么必须是这一个函数 ------------------------------------------
    结转刷新次年期初（``_refresh_opening``）与期初余额录入页
    （``api/books.py::_openings_editable``）问的是**同一个问题**，
    两处各写一遍查询，口径已经漂移过一次：结转那边数了**全部**凭证，
    于是「次年只有未记账凭证」也被拦下 —— 生产事故：泉州星悦荟商业
    管理有限公司 2021 结转 2022，2022 年有 328 张导入的未记账凭证，
    结转被「2022 年已经有 328 张凭证，不能再刷新期初余额」挡下，
    而那 328 张一张都没记账，期初根本没被引用过。
    """
    return db.scalar(
        select(func.count(Voucher.id))
        .join(Period, Period.id == Voucher.period_id)
        .where(Period.book_id == book_id, Period.year == year,
               Voucher.status == "POSTED")
    ) or 0


def _refresh_opening(db: Session, book_id: int, first: Period,
                     fmap: dict, src_subjects: list[Subject],
                     dst_subjects: list[Subject]) -> int:
    """把本年期末余额按**编码**搬进下一年 1 月的期初，返回写入条数。

    「刷新」而不是「叠加」：期初是一次快照，重写就重写。
    这里刻意允许**覆盖**已存在的期初 —— 财务上最常见的动作就是
    「结完 2026 之后又回去改了 2026 的数」，改完必须能再结一次把
    2027 年 1 月的期初带过来，否则新账和旧账的年初数永远对不上。

    但只要下一年已经**记了账**，就不能动它的期初：
    那时的期初已经被账簿引用，硬改会让已算出来的数和新期初对不上。

    判据是**已记账**（POSTED）凭证，见 ``posted_voucher_count``：
    未记账的凭证不进账簿，不构成冲突 —— 「改完上一年再结一次刷新下一年期初」
    是常规动作，下一年堆着一堆未记账的导入凭证时同样要放行。

    ---- 按编码搬，而不是按 id --------------------------------------------
    **为什么必须按编码。** 年度之间是**两套不同的科目行**：2026 的 1002 和
    2027 的 1002 是两行记录、两个 id（科目表下沉到年度就是这个意思）。
    id 之间没有任何对应关系，而**编码是唯一稳定的业务键** —— 它是
    「同一个会计科目」在两张科目表之间的对应关系。

    按 id 搬是搬不过去的：上一年的 id 在新年度根本不存在。

    ---- 搬不动就拒绝 ---------------------------------------------------
    余额非零、而在新年度科目表里找不到同编码科目 —— **拒绝结转**，
    把差异列给用户。静默丢弃的后果是「钱凭空消失，而借贷仍然平衡，
    系统自检查不出来」：那正是这个系统反复强调的头号风险。

    余额为零的科目对不上**不拦**：那只是科目表改了（停用了一个空科目），
    没有钱会丢。

    ---- 被关掉结转的损益科目也拒绝（审查修复）------------------------
    ``category == "PROFIT"`` 且 ``carry_forward=False`` 的科目，余额非零时
    **不能**作为期初结转到下一年：结转损益不结走它、结账的结平校验也不看它，
    于是它会一路挂到下一年的期初上 —— 那一年利润表看不到它、资产负债表不平，
    而全程无报错。见上面 ``blocked`` 那段。

    ``src_subjects`` 必须覆盖**所有有余额的科目**，包括非末级的（审计 P1-1）。
    金额直接取 ``fmap[sid].closing`` —— 那是**子树聚合后**的余额
    （``ledger/engine`` 把分录上卷到各级父科目），不是「这个科目自己那一份」。
    一个科目自己的快照是该科目的**基准总额**，下级只补增量，
    引擎侧的去重口径见 ``LedgerEngine._roll_up_openings`` 的 subtree_total。
    """
    used = posted_voucher_count(db, book_id, first.year)
    if used:
        raise ValueError(
            f"{first.year} 年已经有 {used} 张已记账凭证，不能再刷新期初余额。"
            f"要改 {first.year - 1} 年的数，请先反结账并处理掉 {first.year} 年的凭证。")

    # 先配对再动数据：对不上的要在**任何写入之前**就报出来。
    dst_by_code = {s.code: s for s in dst_subjects}
    pairs: list[tuple[Subject, Decimal]] = []
    missing: list[str] = []
    # ---- 被关掉结转的损益科目：拒绝年度结转（审查修复）----------------
    #
    # ``carry_forward=False`` 的损益科目被**同时**排除在结转损益与结账的
    # 结平校验之外（两侧共用 ``carry_forward_subjects``，所以自洽）。
    # 但这里承接期初时**不看 category、不看 carry_forward**，只按
    # 「余额非零且新年度有同编码科目」搬 —— 于是那笔损益被写进**下一年度
    # 1 月的期初快照**，挂在损益科目上：下一年利润表看不到它、资产负债表
    # 不平，而**全程无报错**。
    #
    # 所以年度结转在这里 **fail closed**：这类余额不能作为期初结转下去。
    blocked: list[str] = []
    for s in src_subjects:
        amount = fmap[s.id].closing
        if amount == ZERO:
            continue        # 没有钱要对不上：科目表改了空科目，不拦
        if s.category == "PROFIT" and not s.carry_forward:
            blocked.append(f"  {s.code} {s.name} 余额 {amount}")
            continue
        dst = dst_by_code.get(s.code)
        if dst is None:
            missing.append(f"  {s.code} {s.name} 余额 {amount}")
            continue
        pairs.append((dst, amount))

    if blocked:
        raise ValueError(
            f"{first.year} 年结转不了：下列损益科目的「结转」被关掉了，"
            f"它们的余额不能作为期初结转到 {first.year} 年 ——\n"
            + "\n".join(blocked)
            + f"\n这些科目是损益类（category=PROFIT）却关掉了结转："
            f"结转损益不会把它们结走，结账的损益结平校验也不看它们，"
            f"于是它们的余额会一路挂到 {first.year} 年的期初上 —— "
            f"那一年利润表看不到这笔钱、资产负债表也不平，而没有任何报错。\n"
            f"请先把它改回结转科目（启用「结转」开关）再做一次「结转损益」，"
            f"或者把这笔余额用红字凭证清零，再重新做年度结转。"
        )

    if missing:
        raise ValueError(
            f"{first.year} 年的科目表和 {first.year - 1} 年对不上，"
            f"有余额的科目在新年度找不到同编码科目，余额搬不过去：\n"
            + "\n".join(missing)
            + "\n请先在 "
            f"{first.year} 年的科目表里补上这些编码（或把它们的余额清零），再重新结转。"
            "直接结转会让这笔钱凭空消失 —— 而借贷仍然平衡，系统自检查不出来。"
        )

    # 重写而不是叠加：先清掉这一期旧的快照
    db.query(PeriodOpening).filter(PeriodOpening.period_id == first.id).delete(
        synchronize_session=False)

    count = 0
    for dst, amount in pairs:
        db.add(PeriodOpening(book_id=book_id, period_id=first.id,
                             subject_id=dst.id, amount=amount))
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
      3) **给新年度一份自己的科目表**（还没有就复制本年度的）
      4) 把本年期末余额**按编码**搬成下一年 1 月的期初快照

    ---- 第 3、4 步为什么是「年度」而不是「账套」------------------------
    科目表现在挂在**年度账套**上（``book_year``）：2026 和 2027 各有一份。
    于是跨年结转不再是把同一行科目换个期间记一下，而是**把余额搬过两张
    科目表** —— 编码是唯一稳定的对应关系（id 跨年根本不是一回事）。

    搬不动（余额非零、但新年度没有同编码科目）就**拒绝结转**并列出差异，
    见 ``_refresh_opening``。
    """
    from app.accounting.vouchers import create_voucher, post_vouchers
    from app.accounting.years import copy_chart, subjects_of

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
    # 本年利润认手工设定（``resolve_profit_subject``），与结转损益同一份口径 ——
    # 两处各认各的就会出现「结转用 A、年度结转冲平用 B」，本年利润永远冲不平。
    profit, _ = resolve_profit_subject(db, book.id, current_year)
    retained = _subject_by_any_code(db, book.id, current_year, RETAINED_SUBJECT_CODES)
    if profit and retained:
        eng = LedgerEngine.for_ledger(db, book.id)
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
                     if _remark_matches(v.remark, f"{current_year}年 结转本年利润")]
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
            # remark 末尾的签名尾巴由 ``_sign_carry_forward`` 在建完凭证后补上。
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
            # 补签名尾巴（不可猜测的那一段，见 ``_carry_forward_token``）
            _sign_carry_forward(db, v, f"{current_year}年 结转本年利润")

            # 追加凭证后 12 月又变成未记账，重新记账并结账
            post_vouchers(db, book.id, [v.id], user, commit=False)
            close_period(db, last, user, commit=False)

    # 2. 追加下一年的会计期间（ord 跨年度连续，接着往下排）
    new_periods = ensure_periods(db, book, target_year)

    # 3. 给新年度一份**自己的**科目表
    #
    #   · 新年度还没有科目表 → **复制本年度的**（新账套的初始科目表通常
    #     和旧账套一样，之后各自演化；用户想换成别的随时可以覆盖导入，
    #     只要新年度还没凭证也没期初，覆盖就是放行的）。
    #   · 新年度已经有科目表 → **不动**。那是用户自己导入的，
    #     复制一遍等于把用户刚做的工作抹掉。
    src_subjects = subjects_of(db, book.id, current_year)
    dst_subjects = subjects_of(db, book.id, target_year)
    copied = 0
    if not dst_subjects:
        copied = copy_chart(db, book.id, current_year, target_year)
        dst_subjects = subjects_of(db, book.id, target_year)

    # 3b. 把「本年利润科目」的**手工设定**带到新年度
    #
    # 新年度的科目表是照抄本年度的（``copy_chart``），所以本年度手工指定的
    # 本年利润编码在新年度多半也在。不带过去的话，新年度会退回自动识别 ——
    # 而这本账的科目表压根不认 3103/4103，于是**新年度一开账就结不了转**，
    # 用户得再设一遍同样的东西。带着走，跨年就不必重复劳动。
    #
    # **只在那个编码在新年度的科目表里确实存在时才带**：
    # 新年度若是用户自己导入的另一张表，这个编码可能压根没有 ——
    # 带过去就是一个指向不存在科目的设定（解析时会回落自动识别，
    # 但界面上会多一句「设的那个不在了」的提示）。
    src_profit = profit_subject_setting(db, book.id, current_year)
    if src_profit and db.scalar(
        select(Subject).where(
            Subject.book_id == book.id, Subject.year == target_year,
            Subject.code == src_profit)
    ) is not None:
        set_profit_subject_setting(db, book.id, target_year, src_profit)

    # 4. 下一年 1 月的期初 = 本年期末（**按编码**搬过两张科目表）
    eng = LedgerEngine.for_ledger(db, book.id)
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
    #
    # **只算本年度的科目**：上一年度的科目是另一批行（另一个年度的科目表），
    # 把它们也算进来会得到一堆搬不过去的余额。
    fmap = eng.compute([s.id for s in src_subjects], last.ord)

    first = new_periods[0]
    count = _refresh_opening(db, book.id, first, fmap, src_subjects, dst_subjects)

    # **新年度的初始化状态**：第 1 步（定科目）已由 copy_chart 标好
    # （目标年度本来就有自己的科目表的话，它早就是好的）；第 2 步（期初）
    # 现在也做完了 —— 期初就是上面承接过来的那一份。
    #
    # 不标的话，新年度会一直挂在「初始化没做完」上：向导每进一次就催一次，
    # 而期初接口还会以「该年度还没完成初始化第 1 步」把这一年的期初挡下来
    # —— **明明账上已经有数了**。
    from app.accounting.initialization import (OPENINGS_SOURCE_CARRY,
                                               mark_openings_ready)
    from app.accounting.years import ensure_book_year

    mark_openings_ready(db, ensure_book_year(db, book, target_year),
                        OPENINGS_SOURCE_CARRY)

    log(db, action="YEAR_END", entity="book", entity_id=book.id, user=user,
        book_id=book.id,
        detail=f"年度结转：{current_year} → {target_year}，"
               f"复制科目表 {copied} 条，结转 {count} 条期初余额")
    if commit:
        db.commit()
    return {"book_id": book.id, "new_year": target_year, "carried_subjects": count,
            "copied_subjects": copied,
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

    # **年度账套行必须先于期间存在**（迁移 0032）：period 上有一条指向
    # book_year(book_id, year) 的组合外键，没有对应年度行就插不进期间。
    # 顺序反了会得到一条「违反外键约束」的 IntegrityError，而它真正的
    # 原因是「年度还没登记」。
    from app.accounting.years import ensure_book_year

    try:
        # **这一步也要在 savepoint 里面**：账套本身不存在时，登记年度账套
        # 会先被外键拒绝。在 savepoint 外面的话，那条裸 IntegrityError 会
        # 直接甩到接口层变成 500 —— 而这里本该给的是一句人话
        # （账套不存在是参数错，不是服务器炸了）。
        with db.begin_nested():
            ensure_book_year(db, book, year)
    except IntegrityError:
        raise ValueError(
            f"{year} 年的会计期间创建失败：年度账套登记被数据库拒绝"
            f"（账套 {book.id}）。请确认该账套存在。"
        )

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
