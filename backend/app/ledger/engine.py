"""账簿聚合引擎。

全系统只有这一套数字计算逻辑，总账 / 明细账 / 科目余额表 / 利润表 /
资产负债表全部由它派生。

三个原语：
    期初期初期初期初(科目, 期间)
    借方累计(科目, 期间)     该期间**本年 1 月** .. 期间的已记账借方合计
    贷方累计(科目, 期间)

派生：
    期末 = 期初 + 借方累计 - 贷方累计
    当期借方 = 借方累计(P) - 借方累计(P-1)

期初的来源有两条：
    - 该期间有期初快照（结账时冻结 / 账套初始化录入）→ 直接取快照
    - 否则 → 由上一期期末实时推导
这样未结账期间不需要物化任何数据，反结账只要删掉快照即可自动恢复可写。

「账套首期之前」也是一个**必须显式成立**的语义（2026-10-13 审计 P0-03）：
账套启用月是第一个会计期间，启用月**之前没有期间**，但启用月的期初是一个
真实存在的数（账套启用时录入的那张期初快照）。区间查询要的是
「起始期的期初 = 起始期上一期的期末」，起始期就是首期时这个「上一期」
不存在 —— 以前总账在这种情况下退回用**结束期的期初**，区间发生额被计入两次。
现在 ``compute()`` 对 ``up_to_ord < 账套首期`` 返回「账套首期之前」那一张零发生额的
Figures，closing 即账套首期的期初，区间查询不再需要分叉。

------------------------- 口径契约（账簿 vs 报表）-------------------------
同一个引擎有两个**刻意不同**的口径，构造入口固定用下面两个工厂方法，
别再到处手写关键字参数（手写迟早会出现「三个地方三种口径」）：

    LedgerEngine.for_ledger(db, book_id, include_unposted)
        账簿口径：总账 / 明细账 / 科目余额表 / 试算平衡 / 打印导出。
        **含结转凭证**（source=CARRY_FORWARD），因为账上确实有这几张凭证。

    LedgerEngine.for_report(db, book_id, include_unposted)
        报表口径：利润表等损益类取数，**排除结转凭证**，
        否则月度结转之后损益类科目本期净额恒为 0（ARCHITECTURE §结转损益）。

两者对损益类科目的差额**恰好是结转凭证的金额**，这不是 bug 而是口径不同：
做过结转的当期，利润表「本期金额」与总账「本期贷方/借方」必然差一个结转全额。
任何一端要展示这个差额，都必须由报表/前端负责标注（引擎只保证两个口径可区分）。

------------------------- 记忆化边界 -------------------------
本引擎实例是**单次请求内**的只读计算器（审计 P2-03 / P2-15）：期初汇总、
发生额聚合、上卷树都在实例内记忆化，因此总账一页只跑一次全量聚合，
报表求值也不再「每个科目一次全账聚合」。
失效边界很硬也很简单：**实例只读**。任何写入（建/删凭证、记账、结账、
年度结转、改科目树）之后必须换一个实例，否则拿到的是写入前的数。
写路径现有用法都符合这一点（``accounting/periods.py`` 在写之前就把要用的
数字全部取出来，写完另起一个引擎），不要为了「省一个对象」把实例跨写复用。

------------------------- 跨请求缓存（2026-10 改）-------------------------
**以前这里是「刻意不做跨请求缓存：正确性优先」。现在做了，但用版本号换回
了正确性** —— 那句结论没有被推翻，只是被实现得比「不做」更好。

压测（``压测基线报告_2026-10-15.md``）量出来多人同时看同一账套时，
每次请求都把同一份聚合重算一遍，77.7ms 就在这里。判据是数据库里的
``book.data_version``：任何影响聚合的写都在同一事务里 +1（迁移 0015，
PostgreSQL 语句级触发器做的），引擎构造时**第一条 SQL** 就是读它。

保证的是：写提交之后，**任何在此之后开始的请求**一定读到新数；
多 worker 之间天然一致（版本号在库里，缓存只是各自一份副本）。

**为什么不用 TTL**：TTL 会让「用户刚记完账、刷新总账看到旧数」成为可能，
而 README 的核心承诺就是不存在「某个报表和总账对不上」。完整论证见
``app/ledger/cache.py`` 的模块 docstring。

**「先读版本号」是硬顺序**，不要为了省一次主键查询把它换到后面 ——
反序会把旧数据缓存进新版本的键，那是最难查的一类 bug。

**记忆化留了什么、没留什么（2026-10-14 审计：峰值内存 O(科目数 × 期间数)）。**
``_figures`` 每个科目只留**推算到的最后一期**，中间各期算完即弃 —— 期间游标
（期初 / 本年累计）只活在 ``compute()`` 那个循环的局部变量里。对外行为一个字
没变：``compute()`` 本来就只返回 ``top`` 那一期（``before_book`` 那一期除外），
``figures(sid, ord)`` 传中间某期时会重推一次到那一期。**重推不发 SQL** ——
期初快照、发生额聚合、科目树都已经在实例内了，走的是纯算术。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.ledger.cache import cache_for
from app.models import Book, Entry, Period, PeriodOpening, Subject, Voucher
from app.models.book import book_first_ord

ZERO = Decimal("0")


def shared(kind: str, key: Any):
    """取某个跨请求缓存。

    「某种东西缓存多大、留多久」在 config.py 里，不是每个调用点各写一遍 ——
    写在调用点的话，改一次要翻半个目录。
    """
    from app.core.config import settings

    return cache_for(
        kind,
        maxsize=settings.ledger_cache_maxsize,
        ttl_seconds=settings.ledger_cache_ttl_seconds,
    )


@dataclass
class Figures:
    """单个科目在单个期间上的全部派生量。signed 表示借正贷负。"""

    subject_id: int
    ord: int
    opening: Decimal = ZERO      # signed
    dr_cum: Decimal = ZERO       # **本年**累计借方发生额（每年 1 月归零）
    cr_cum: Decimal = ZERO       # **本年**累计贷方发生额（每年 1 月归零）
    period_dr: Decimal = ZERO    # 当期借方发生额
    period_cr: Decimal = ZERO    # 当期贷方发生额

    @property
    def closing(self) -> Decimal:
        """期末余额 = 该期期初 + 该期发生额。

        注意 dr_cum / cr_cum 是「本年累计」，而 opening 已经是该期的期初，
        两者相加会把年内发生额重复计入。期末只能用本期发生额。
        """
        return self.opening + self.period_dr - self.period_cr

    @property
    def signed(self) -> Decimal:
        return self.closing


@dataclass(frozen=True)
class MergedFigures:
    """同一个**编码**在多个年度的科目表上合并出来的派生量。

    科目表每个年度各有一份（迁移 0032），所以「一个科目」在这套模型里
    不是一行、而是一个**编码在各年度的若干行**。跨年区间要把它们合成
    一行来报，合成后的口径写在这里（见 :meth:`LedgerEngine.merge_by_code`）。
    """

    #: 代表科目：**区间最后一年**的那一行（报告的「当前年」视角）
    subject: Subject
    #: 期初（signed，借正贷负）
    opening: Decimal
    #: 区间（或当期）借方发生额
    period_dr: Decimal
    #: 区间（或当期）贷方发生额
    period_cr: Decimal
    #: 本年累计借方（**区间最后一年**那一行的口径）
    ytd_dr: Decimal
    #: 本年累计贷方（**区间最后一年**那一行的口径）
    ytd_cr: Decimal
    #: 期末（signed）
    closing: Decimal


@dataclass
class LedgerEngine:
    db: Session
    book_id: int
    include_unposted: bool = False
    # 结转损益是内部结转：排除后统计的是「真实的经营损益」，
    # 利润表必须用它，否则月度结转之后损益类科目本期净额恒为 0。
    exclude_carry_forward: bool = False
    # 排除某一张凭证。凭证录入页要看「除这张以外」的余额：那张凭证自己正在被编辑，
    # 边填边自动保存，它要是算进去，余额会跟着手上这张一起动 —— 看的是自己写进去的数。
    exclude_voucher_id: int | None = None
    _figures: dict[tuple[int, int], Figures] = field(default_factory=dict)
    # 上面这个字典**每个科目只留推到的最后一期**（外加「账套首期之前」那一张，
    # 见 compute() 的 before_book 分支），不是每期一张 —— 内存是 O(科目数)
    # 而不是 O(科目数 × 期间数)。失效边界与读法见模块 docstring。
    # ---- 单次请求内的记忆化（2026-10-13 审计 P2-03 / P2-15）----
    # 全部是实例级缓存，失效边界见模块 docstring：实例只读，写后换实例。
    _book_cache: Book | None = None
    _period_map_cache: dict[int, Period] | None = None
    _ancestor_cache: dict[int, list[int]] | None = None
    # 发生额：按「科目范围」分桶缓存，每桶记已算过的最大上界 + 那个上界下的
    # 完整结果 + 各上界下的过滤视图。
    #
    # **为什么多了一个「科目范围」维度。** 压测实测（压测基线报告 2026-10-15）：
    # 一次明细账请求的 85.9ms 里有 77.7ms 花在这条聚合上，而聚合扫的是
    # **整个账套**（317 末级 × 24 期间 = 7,608 组，EXPLAIN 显示 24ms /
    # 60,121 buffers）。明细账只要 1 个科目 × 24 期间 = 24 组 ——
    # **97% 的聚合结果是白算的**。范围必须进缓存键，否则「查得全的」
    # 和「只查了一个科目的」会互相串。
    #
    # 上界变小仍然不需要重查（结果本来就按 (subject_id, period_ord) 分好组，
    # 过滤一下就是更小上界的结果），所以总账「算到 to」+「算到 from-1」
    # 只产生**一次**聚合查询。
    #
    # 键：scope=None 表示全账套；否则是排好序的科目 id 元组。
    _moves_top: dict[Any, int] = field(default_factory=dict)
    _moves_full: dict[Any, dict[tuple[int, int], tuple[Decimal, Decimal]]] = field(default_factory=dict)
    _moves_views: dict[tuple[Any, int], dict[tuple[int, int], tuple[Decimal, Decimal]]] = field(default_factory=dict)
    _openings_cache: dict[tuple[int, int], Decimal] | None = None
    # 每个科目已经算到哪一期：重复请求同一科目不再重推一遍
    _computed_through: dict[int, int] = field(default_factory=dict)

    # ---- 跨请求缓存（2026-10 性能收尾 P0）----
    # 下面是实例级字典的**对面**：那些只活一次请求，这两个跨请求活到被 LRU 挤掉。
    # 键里必须带 data_version —— 正确性全靠它，理由与「为什么不用 TTL」见
    # app/ledger/cache.py 的模块 docstring。
    _shared_periods: dict[Any, Any] | None = None
    _shared_ancestors: dict[Any, dict[int, list[int]]] | None = None
    _shared_openings: dict[Any, dict[tuple[int, int], Decimal]] | None = None
    _shared_moves: dict[Any, dict[tuple[int, int], tuple[Decimal, Decimal]]] | None = None
    # 本实例读到的账套数据版本。**在 __post_init__ 里读，且必须是第一条 SQL** ——
    # 顺序是硬约束，反序会把旧数据缓存进新版本的键（见 cache.py 的说明）。
    _data_version: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # 先读版本号，之后才允许有任何别的查询。
        # 这一行是整个跨请求缓存的地基：它决定了后面所有缓存键的形状。
        # 代价是一次主键查询；收益是「写完立刻刷新总账看到的一定是新数」。
        self._data_version = self.db.scalar(
            select(Book.data_version).where(Book.id == self.book_id)
        ) or 0

    # 口径相关的键片段。两个口径必须分开 —— 它们的差额恰好是结转凭证金额，
    # 混在一起会让利润表在月度结转之后显示「本期净额恒为 0」。
    @property
    def _caliber(self) -> str:
        return "REPORT" if self.exclude_carry_forward else "LEDGER"

    @property
    def _key_salt(self) -> tuple:
        """凡是会改变结果的过滤条件，都必须进这个盐里。

        漏一个的后果不是「慢」，是**算错**：比如漏了 include_unposted，
        未记账凭证的余额就不会出现在账簿上，而缓存照样命中。
        """
        return (self.book_id, self._data_version, self._caliber,
                self.include_unposted, self.exclude_voucher_id)

    # ---------------- 构造入口（口径契约） ----------------

    @classmethod
    def for_ledger(cls, db: Session, book_id: int, include_unposted: bool = False,
                   *, exclude_voucher_id: int | None = None) -> "LedgerEngine":
        """账簿口径：总账 / 明细账 / 科目余额表 / 打印。**含结转凭证**。"""
        return cls(db, book_id, include_unposted, exclude_voucher_id=exclude_voucher_id)

    @classmethod
    def for_report(cls, db: Session, book_id: int,
                   include_unposted: bool = False) -> "LedgerEngine":
        """报表口径：损益取数**排除结转凭证**（ARCHITECTURE §结转损益）。

        与 ``for_ledger`` 的差额恰好是结转凭证金额 —— 报表页要标注这个差额，
        不能靠「两边数字不一样」让用户自己猜。
        """
        return cls(db, book_id, include_unposted, exclude_carry_forward=True)

    # ---------------- 基础查询 ----------------

    def _status_filter(self) -> Select:
        stmt = select()
        if not self.include_unposted:
            stmt = stmt.where(Voucher.status == "POSTED")
        return stmt

    def periods(self) -> list[Period]:
        pmap = self._period_map()
        return [pmap[o] for o in sorted(pmap)]

    def _period_map(self) -> dict[int, Period]:
        if self._period_map_cache is None:
            key = ("periods",) + self._key_salt

            def produce() -> dict[int, Period]:
                return {
                    p.ord: p for p in self.db.scalars(
                        select(Period).where(Period.book_id == self.book_id)
                        .order_by(Period.ord)
                    ).all()
                }

            self._period_map_cache = shared("periods", key).get_or_set(key, produce)
        return self._period_map_cache

    def _year_of(self, ord_: int) -> int | None:
        """某个连续期间序号属于哪个会计年度。

        科目表现在每个年度各有一份，所以「全科目集」必须先定下**是哪一年**。
        ord 是跨年度连续的，年度只能查表拿 —— 算术反推要拿 ``start_month``
        修正（启用月不是 1 月时），写错一处余额就整体漂移。
        """
        period = self._period_map().get(ord_)
        return period.year if period is not None else None

    def _book(self) -> Book:
        if self._book_cache is None:
            self._book_cache = self.db.get(Book, self.book_id)
        return self._book_cache

    # ---------------- 原语 ----------------

    def _ancestor_map(self) -> dict[int, list[int]]:
        """科目 -> [自己, 上级, 上上级...]，用于把发生额上卷到各级父科目。"""
        if self._ancestor_cache is not None:
            return self._ancestor_cache

        key = ("ancestors",) + self._key_salt

        def produce() -> dict[int, list[int]]:
            rows = list(
                self.db.execute(
                    select(Subject.id, Subject.parent_id)
                    .where(Subject.book_id == self.book_id)
                ).all()
            )
            parent = {sid: pid for sid, pid in rows}
            cache: dict[int, list[int]] = {}

            def chain(sid: int) -> list[int]:
                if sid in cache:
                    return cache[sid]
                out, cur, guard = [sid], parent.get(sid), 0
                while cur and cur not in out and guard < 32:
                    out.append(cur)
                    cur = parent.get(cur)
                    guard += 1
                cache[sid] = out
                return out

            return {sid: chain(sid) for sid in parent}

        self._ancestor_cache = shared("ancestors", key).get_or_set(key, produce)
        return self._ancestor_cache

    def _cum_movements(self, up_to_ord: int,
                       subject_ids: list[int] | None = None, *,
                       widen_on_miss: bool = False
                       ) -> dict[tuple[int, int], tuple[Decimal, Decimal]]:
        """(subject_id, period_ord) -> (当期借方, 当期贷方)。

        结果已经上卷到所有父科目：凭证只挂在明细级科目上，
        但账簿/报表需要每一级父科目的发生额，所以在这里一次算完。

        ``subject_ids`` 是**范围收窄**：只聚合这些科目（及其父科目）。
        传 None 表示全账套。

        **为什么要收窄。** 明细账一次只要一个（或几个）科目，而原实现每次都把
        整个账套聚合一遍。实测 317 科目 / 6 万分录的账套上，单次请求的聚合是
        77.7ms（占该请求 85.9ms 的 90%），而明细账真正用到的组数不到总量的 4%。

        **收窄不能引入「每个科目查一次」。** ``figures(sid, ord)`` 会逐科目调
        ``compute([sid], ord)``，若范围直接进 SQL，明细账就会从 1 次查询变成
        N 次。所以先走 :meth:`_pick_donor`：凡是「已缓存的范围 ⊇ 本次范围」
        且「已查到的上界 ≥ 本次上界」，直接从那份结果切片，**不发 SQL**。

        切片只多不少（宁可多给几个科目，也绝不漏掉要的），而调用方一律用
        ``moves.get((sid, o))`` 按自己要的科目取，多出来的键无人读 ——
        这是刻意的：宁可在内存里多留几条，也不能让某次请求查不到本该有的数。

        失效边界见模块 docstring（实例只读）。

        ``widen_on_miss`` 区分两种调用形状，它们要的东西相反：

        * ``compute(subject_ids=[...])`` —— 调用方**知道自己要哪些科目**
          （明细账就是）。收窄到它，一次只算这几棵子树。
        * ``figures(sid)`` → ``compute([sid])`` —— 调用方**逐个**来，
          后面可能还有十几个科目。收窄会让它变成「每个科目查一次」，
          那正是审计 P2-15 的 N+1 被修掉的原因。所以这种形状
          在缓存盖不住时**退化为全账套**，之后每个 ``figures()`` 都命中
          —— 整份报表仍然只跑一次聚合。
        """
        if subject_ids:
            scope: Any = tuple(self._expand_scope(subject_ids))
            key = (scope, up_to_ord)
            cached = self._moves_views.get(key)
            if cached is not None:
                return cached
            donor = self._pick_donor(scope, up_to_ord)
            if donor is None and widen_on_miss:
                # 没有任何缓存盖住这个科目 —— 说明外面还有别的科目要来，
                # 按全账套查一次，后面就都命中了。
                return self._cum_movements(up_to_ord, None)
            return self._materialize(scope, up_to_ord, donor)
        return self._materialize(None, up_to_ord, self._pick_donor(None, up_to_ord))

    def _materialize(self, scope: Any, up_to_ord: int,
                     donor: tuple[dict[tuple[int, int], tuple[Decimal, Decimal]], int] | None
                     ) -> dict[tuple[int, int], tuple[Decimal, Decimal]]:
        """把（缓存切片的）结果落到 ``_moves_views`` 并返回。"""
        key = (scope, up_to_ord)
        if donor is not None:
            full, top = donor
            view = full if up_to_ord == top else {
                k: v for k, v in full.items() if k[1] <= up_to_ord}
        else:
            view = self._query_movements(
                up_to_ord, None if scope is None else list(scope))
            self._moves_full[scope] = view
            self._moves_top[scope] = up_to_ord
        self._moves_views[key] = view
        return view

    def _expand_scope(self, subject_ids: list[int]) -> list[int]:
        """把请求的科目集合展开成「它自己和它的所有后代」。

        **为什么必须展开。** 凭证分录只挂在**末级**科目上，父科目身上一条
        直接分录都没有 —— 这是整套账的硬约束（写入侧 ``fn_subject_leaf_guard``）。
        所以把父科目 id 直接塞进 ``IN`` 会一条都匹配不到：实测
        ``_moves_full={(2,): {}}``，父科目的发生额直接变成 0，
        而这种错误**不报错**，只是算出一个偏小的余额。

        展开成后代之后，SQL 过滤的是叶子，上卷仍然交给
        :meth:`_query_movements` 的 Python 那一步 —— 那里本来就要算祖先链。
        """
        want = set(subject_ids)
        out = set(want)
        for sid, chain in (self._ancestor_map() or {}).items():
            if sid in want or any(a in want for a in chain):
                out.add(sid)
        return sorted(out)

    def _pick_donor(self, scope: Any, up_to_ord: int
                    ) -> tuple[dict[tuple[int, int], tuple[Decimal, Decimal]], int] | None:
        """找一份能覆盖本次请求的已缓存结果，找得到就不必再查库。

        「覆盖」= **科目范围 ⊇ 本次范围** 且 **已查到的上界 ≥ 本次上界**。
        两个条件缺一不可：范围不够会漏科目，上界不够会少算期间 ——
        而少算期间的数不会报错，只会算出一个偏小的余额。

        宁可多留也不放过的取舍见 :meth:`_cum_movements` 的 docstring。
        """
        want = None if scope is None else set(scope)
        best: tuple[dict[tuple[int, int], tuple[Decimal, Decimal]], int] | None = None
        for sc, full in self._moves_full.items():
            top = self._moves_top.get(sc)
            if top is None or top < up_to_ord:
                continue
            if sc is None:                       # 全账套覆盖任何范围
                return full, top
            if want is not None and want <= set(sc):
                # 上界更小的那个优先：切片结果更贴近本次范围，浪费更少
                if best is None or top < best[1]:
                    best = (full, top)
        return best

    def _query_movements(self, up_to_ord: int, subject_ids: list[int] | None = None
                         ) -> dict[tuple[int, int], tuple[Decimal, Decimal]]:
        """账簿最热的聚合查询（唯一的全账扫描，见 P2-06 的索引建议）。

        ``subject_ids`` 不为空时把科目条件下推到 SQL：这是本次优化里
        真正省掉 97% 聚合工作的一步 —— **过滤在聚合之前，不是之后**。
        上卷到父科目仍然在 Python 里做（父科目本身没有分录可过滤，
        它是从叶子累加上来的）。

        传进来的 ``subject_ids`` 必须是**已经展开到末级**的集合
        （:meth:`_cum_movements` 负责展开）—— 直接传父科目 id 会匹配不到
        任何分录，且不报错。

        **结果跨请求复用。** 这是压测里那条 77.7ms 的聚合，缓存键带
        ``data_version`` 与全部口径过滤条件（见 :attr:`_key_salt`）。
        返回的是**共享的字典**，调用方只读不改 —— 真去改它就是在改别人的缓存。
        """
        scope = tuple(subject_ids) if subject_ids else None
        key = ("moves",) + self._key_salt + (scope, up_to_ord)
        return shared("moves", key).get_or_set(
            key, lambda: self._query_movements_uncached(up_to_ord, subject_ids))

    def _query_movements_uncached(self, up_to_ord: int, subject_ids: list[int] | None
                                  ) -> dict[tuple[int, int], tuple[Decimal, Decimal]]:
        """真正发 SQL 的那一半。与 :meth:`_query_movements` 只差「有没有走缓存」。"""
        stmt = (
            select(
                Entry.subject_id,
                Entry.period_ord,
                func.coalesce(
                    func.sum(Entry.amount).filter(Entry.direction == "DEBIT"), ZERO
                ).label("dr"),
                func.coalesce(
                    func.sum(Entry.amount).filter(Entry.direction == "CREDIT"), ZERO
                ).label("cr"),
            )
            .join(Voucher, Voucher.id == Entry.voucher_id)
            .where(Entry.book_id == self.book_id, Entry.period_ord <= up_to_ord)
            .group_by(Entry.subject_id, Entry.period_ord)
        )
        if subject_ids:
            # 空列表**不**过滤（那等于「什么都不要」，不是「全都要」）。
            # 调用方传 None 表示全账套，传非空列表表示收窄，两者语义不同，
            # 所以这里判的是 truthy 而不是 `is not None`。
            stmt = stmt.where(Entry.subject_id.in_(subject_ids))
        if not self.include_unposted:
            stmt = stmt.where(Voucher.status == "POSTED")
        if self.exclude_carry_forward:
            stmt = stmt.where(Voucher.source != "CARRY_FORWARD")
        if self.exclude_voucher_id:
            stmt = stmt.where(Voucher.id != self.exclude_voucher_id)

        rolled: dict[tuple[int, int], list[Decimal]] = {}
        ancestors = self._ancestor_map()
        for sid, o, dr, cr in self.db.execute(stmt).all():
            for node in ancestors.get(sid, [sid]):
                key = (node, o)
                cell = rolled.setdefault(key, [ZERO, ZERO])
                cell[0] += Decimal(dr)
                cell[1] += Decimal(cr)
        return {k: (v[0], v[1]) for k, v in rolled.items()}

    def _openings(self) -> dict[tuple[int, int], Decimal]:
        """期初快照上卷后的结果。跨请求复用（键里带 data_version）。"""
        if self._openings_cache is None:
            key = ("openings",) + self._key_salt
            self._openings_cache = shared("openings", key).get_or_set(
                key, self._roll_up_openings)
        return self._openings_cache

    def _roll_up_openings(self) -> dict[tuple[int, int], Decimal]:
        """把「末级科目才写的期初快照」上卷成每一级父科目的期初。

        写入侧的硬约束（``accounting/periods._refresh_opening``、
        ``api/books.set_openings``）：**period_opening 只对末级科目写**，
        父级的期初天然由子树汇总而来。

        但科目树是活的：科目表里「先做末级、后来又加下级」是常规操作
        （原来 100202 是末级、录入过期初，之后在它下面挂了 10020201）。
        这时它身上会**同时**留下一条自己的快照和下级的快照 ——
        以前这里是「自身 + 整棵子树」相加，同一笔余额被计入两次，
        父级期初与明细行对不上，而且列与列之间自洽（期初 + 本期 = 期末），
        只有与上期期末交叉核对才看得出来。

        现在的规则是「**自身快照是基准，下级只补增量**」：

            自身有快照：期初 = 自身快照 + Σ下级(子树期初 - 下级自身快照)
            自身无快照：期初 = Σ下级子树期初

        自身快照写于「它还是末级」的时候，那一刻它已经包含了全部下级的余额，
        所以它是总数、不是增量；把下级再整棵加上去就是重复计入。
        减掉下级自身快照、只加上级多出来的部分，两种情况都对：
        纯净的末级快照集（平时）行为完全不变，父级照样是子树之和。
        注意这里**不看 Subject.is_leaf**：那是服务端派生的状态，不是账实。

        另外只为「真的有信息」的 (科目, 期) 返回条目 —— 没有自身快照、
        下级也没有快照时返回 0，会让 ``compute()`` 在这一期把期初清零，
        而这个科目的余额其实应该从上一期滚过来（用户在月中给某期补录期初时，
        只录了一部分科目，其余科目的余额会凭空归零）。
        """
        stmt = (
            select(PeriodOpening.subject_id, Period.ord, PeriodOpening.amount)
            .join(Period, Period.id == PeriodOpening.period_id)
            .where(PeriodOpening.book_id == self.book_id)
        )
        own = {(sid, o): Decimal(amt) for sid, o, amt in self.db.execute(stmt).all()}
        ancestors = self._ancestor_map()
        # 反向：找出每个科目的所有下级
        children: dict[int, list[int]] = {}
        for sid, chain in ancestors.items():
            if len(chain) > 1:
                children.setdefault(chain[1], []).append(sid)

        ords = {o for (_sid, o) in own}
        resolved: dict[tuple[int, int], Decimal] = {}

        def subtree_total(sid: int, o: int, depth: int = 0) -> Decimal:
            key = (sid, o)
            if key in resolved:
                return resolved[key]
            own_row = own.get(key)
            subs = children.get(sid, []) if depth < 16 else []
            if not subs:
                total = own_row if own_row is not None else ZERO
            elif own_row is not None:
                total = own_row + sum(
                    (subtree_total(c, o, depth + 1) - own.get((c, o), ZERO) for c in subs), ZERO)
            else:
                total = sum((subtree_total(c, o, depth + 1) for c in subs), ZERO)
            resolved[key] = total
            return total

        out: dict[tuple[int, int], Decimal] = {}
        # 「这一格有信息吗」= 自己有快照，或者子树里任意一级有快照。
        # 从每个快照沿祖先链**向上**标记：一遍搞定，与遍历顺序无关。
        # 不能只看直接下级 —— 孙级才录了期初时，中间那层和顶层同样有信息。
        informed: set[tuple[int, int]] = set()
        for sid, chain in ancestors.items():
            for o in ords:
                if (sid, o) in own:
                    informed.update((node, o) for node in chain)
        for sid in ancestors:
            for o in ords:
                key = (sid, o)
                if key in informed:
                    out[key] = subtree_total(sid, o)
        for key, amount in own.items():
            out.setdefault(key, amount)
        return out

    def range_movements(self, subject_ids: list[int], from_ord: int,
                        to_ord: int) -> dict[int, tuple[Decimal, Decimal]]:
        """区间发生额合计：subject_id -> (借方, 贷方)。

        不能用「累计相减」求区间发生额 —— dr_cum/cr_cum 是**本年**累计，
        区间跨过年界（比如 2026-12 到 2027-02）时相减会得到乱七八糟的数。
        这里直接把区间内各期的发生额加起来，跨多少年都对。
        """
        # 只聚合调用方点名的那几个科目（下面 acc 也只读这几个），
        # 全账套口径的聚合在报表总额那种场景才需要。
        moves = self._cum_movements(to_ord, subject_ids)
        acc = {sid: [ZERO, ZERO] for sid in subject_ids}
        for (sid, o), (dr, cr) in moves.items():
            cell = acc.get(sid)
            if cell is not None and from_ord <= o <= to_ord:
                cell[0] += dr
                cell[1] += cr
        return {sid: (cell[0], cell[1]) for sid, cell in acc.items()}

    # ---------------- 批量计算 ----------------

    def compute(self, subject_ids: list[int] | None = None, up_to_ord: int | None = None,
                *, widen_on_miss: bool = False) -> dict[int, Figures]:
        """批量计算一批科目在 up_to_ord 期间的派生量。

        报表模板会同时引用几十个科目，批量接口保证每个科目只扫一次索引；
        单次请求内的重复调用直接命中记忆化（见模块 docstring）。

        **up_to_ord 小于账套首期时**（2026-10-13 审计 P0-03）返回的是
        「账套首期之前」那一张图：期初 = 账套首期的期初快照，零发生额、零累计。
        这样 ``compute(ids, ord - 1).closing`` 恒等于「ord 的期初」，
        包括 ord 恰好是账套首期的情况 —— 区间查询不再需要判断
        「起始期是不是首期」再走另一条分支。
        """
        pmap = self._period_map()
        start = book_first_ord(self._book())
        top = up_to_ord if up_to_ord is not None else max(pmap) if pmap else start
        # 账套启用月之前没有会计期间，但「首期之前」这个时点上的余额是有的
        # （就是首期的期初）。先把推导钳到首期算出期初，最后折成零发生额那一张。
        before_book = up_to_ord is not None and up_to_ord < start
        if top < start:
            top = start

        # **科目范围要在聚合之前定下来**（下面的 subject_ids is None 分支
        # 会把它补成全科目集）。顺序反了就只能按全账套聚合 —— 那正是这次
        # 优化要消掉的那笔开销。
        narrowed = subject_ids
        if subject_ids is None:
            # 不过滤 active：这是**默认的全科目集**，报表总额、账簿汇总都走它。
            # 停用科目被排除的话，上级科目合计会偏小、报表总额会缺一块，
            # 而用户完全看不出少了什么。写路径的停用闸门在 vouchers.py。
            #
            # **全科目集 = 目标期间所属年度的那一份科目表。**
            # 科目表每个年度各有一份（迁移 0032），不圈定年度的话这里会把
            # 2026 和 2027 的科目一起捞进来 —— 同一个编码出现两次，
            # 报表按编码取数时哪一份生效就成了抛硬币。
            year = self._year_of(top)
            stmt = select(Subject.id).where(Subject.book_id == self.book_id)
            if year is not None:
                stmt = stmt.where(Subject.year == year)
            subject_ids = list(self.db.scalars(stmt).all())
            # narrowed 保持 None：全科目集不等于「把 317 个 id 塞进 IN」。
            # 两者结果相同，但 IN 列表越长规划器越难选计划，而「不带科目
            # 条件」正是 ``ix_entry_subject_ord`` 首列 book_id 最擅长的形态。
            narrowed = None

        moves = self._cum_movements(top, narrowed, widen_on_miss=widen_on_miss)
        snapshots = self._openings()

        # 逐期推进：
        #   期初(P)     = 期初(P-1) + 本期借方(P-1) - 本期贷方(P-1)
        #   本年借方累计(P) = 本年借方累计(P-1) + 本期借方(P)
        # 注意这里用的是「本期发生额」而不是「累计发生额」——
        # 用累计值会让期初逐月重复累加，余额虚增。
        #
        # ord 是跨年度连续的，所以「本年累计」必须在每个年度的 1 月归零。
        # 不归零的话，2027 年 1 月的「本年累计」会把 2026 全年的数也算进去，
        # 利润表 / 资产负债表的年初数就会变成「两年累计」。
        result: dict[int, Figures] = {}
        for sid in subject_ids:
            # 这个科目已经推算过、且推到了 top：直接取缓存（实例只读，不会过期）
            if self._computed_through.get(sid, -1) >= top and (sid, top) in self._figures:
                f = self._figures[(sid, top)]
            else:
                opening = snapshots.get((sid, start), ZERO)
                dr_cum = cr_cum = ZERO
                f = Figures(subject_id=sid, ord=top)
                for o in range(start, top + 1):
                    if (sid, o) in snapshots:
                        opening = snapshots[(sid, o)]
                    if o > start and (o - 1) % 12 == 0:
                        # 跨进新的一年：期初延续（快照已在上一步写入），
                        # 本年累计从零开始
                        dr_cum = cr_cum = ZERO

                    dr, cr = moves.get((sid, o), (ZERO, ZERO))
                    dr_cum += dr
                    cr_cum += cr

                    if o == top:
                        # **只留最后这一期。** 期初由 opening 游标带着往下走，
                        # 中间各期算完即弃 —— 旧写法每一期都写进 _figures，
                        # 于是峰值内存是 O(科目数 × 期间数)（500 科目 × 120 期
                        # = 6 万个 Figures），而对外只有 top 这一期被用得上。
                        # 要中间某期时 figures() 会重推那一条路径，纯算术、
                        # 不发 SQL（见 _cum_movements 的记忆化）。
                        f = Figures(
                            subject_id=sid, ord=o, opening=opening,
                            dr_cum=dr_cum, cr_cum=cr_cum, period_dr=dr, period_cr=cr,
                        )

                    opening = opening + dr - cr

                self._figures[(sid, top)] = f
                self._computed_through[sid] = top

            if before_book:
                # 首期之前：没有发生额、没有累计，只把首期的期初摆在那里。
                # 区间查询取「起始期上一期的期末」时，首期起始就靠这一张。
                f = Figures(subject_id=sid, ord=up_to_ord, opening=f.opening,
                            dr_cum=ZERO, cr_cum=ZERO, period_dr=ZERO, period_cr=ZERO)
                self._figures[(sid, up_to_ord)] = f
            result[sid] = f
        return result

    def batch_figures(self, subject_ids: list[int] | None = None,
                      up_to_ord: int | None = None) -> dict[int, Figures]:
        """报表取数的批量入口：subject_id -> Figures（等价于 ``compute``）。

        给 ``reports/evaluate.py`` 这类「一张模板要几十个科目」的调用方用：
        先 ``batch_figures(ids, ord)`` 预热一次，公式里再逐个
        ``eng.figures(sid, ord)`` 就全部命中记忆化 —— 整份报表只跑**一次**
        全账聚合，不是每引用一个科目跑一次（审计 P2-15 的 N+1）。

        不预热也能拿到同样的查询次数（``figures()`` 自己会算），
        预热的好处是结果可控、不依赖求值顺序。
        """
        return self.compute(subject_ids, up_to_ord)

    # ---------------- 跨年归并 ----------------

    def merge_by_code(self, subjects: list[Subject], to_ord: int,
                      from_ord: int | None = None) -> list[MergedFigures]:
        """把**多个年度**的同一批编码合并成一行（跨年区间用）。

        科目表每个年度各有一份（迁移 0032），于是同一个编码在 2026 与 2027
        是**两行不同的记录、两个 id**。跨年区间（2026-12 → 2027-02）要回答的
        是「这个编码在这段区间里怎么动的」，而两边的分录挂在**不同 id** 上
        —— 拿 2027 的科目 id 去查 2026 的分录，一条都匹配不到，
        区间发生额会凭空少掉 2026 那半截。

        所以跨年区间按**编码**归并：

        * **期初**取区间**第一年**那一行在 ``from_ord - 1`` 的期末
          （与旧模型「一张科目表跨年连着算」的口径一致）；
        * **发生额**是各年度该编码发生额的**和**；
        * **期末** = 期初 + 借 − 贷；
        * **本年累计**取区间**最后一年**那一行 ——「本年」= 报告结束所在的年，
          与旧模型一致（旧模型里 dr_cum 也是每年 1 月归零的）；
        * 行上的科目属性（名称、级次、方向、是否末级）取**最后一年**那一行，
          即报告的「当前年」视角。

        **只有一个年度时，这个方法与逐科目算完全等价**（归并组里只有一行），
        所以单年查询也走它 —— 口径只有一份实现，不会出现「单年一种算法、
        跨年另一种算法」的两套数。

        **结构不一致时拒绝合并。** 同一个编码在一年是上级、一年是末级时，
        把两边的数相加既不是这个编码的总额，也不是它的发生额，
        而且试算平衡会**重复计数**（上级那年的数含它的下级，
        下级又单独计一次）。这种情况宁可报错，让用户按年度分别查 ——
        报一个说不清的数，比报一个错数更糟。
        """
        if not subjects:
            return []

        groups: dict[str, list[Subject]] = {}
        for s in subjects:
            groups.setdefault(s.code, []).append(s)
        for members in groups.values():
            members.sort(key=lambda s: s.year)

        # ---- 结构一致性：同一编码的末级性必须一致 ----
        for code, members in groups.items():
            if len(members) < 2:
                continue
            leaves = {m.is_leaf for m in members}
            if len(leaves) > 1:
                detail = "、".join(
                    f"{m.year} 年是{'末级' if m.is_leaf else '上级'}科目" for m in members)
                raise ValueError(
                    f"科目 {code} 在各年度的级次不一致（{detail}）—— "
                    "跨年区间要求各年度的科目表结构一致，请按年度分别查询")

        ids = [s.id for s in subjects]
        fmap = self.compute(ids, to_ord)
        if from_ord is None:
            open_map = None
            moves = {sid: (fmap[sid].period_dr, fmap[sid].period_cr) for sid in ids}
        else:
            # **区间期初 = 起始期那一期的期初**（用区间自己这几年的科目行算）。
            #
            # 这里原来取的是「起始期的**上一期**期末」，即
            # ``compute(ids, from_ord - 1).closing``。两者的口径在正常情况下
            # 等价，但**跨年时不等价**，而且差得很离谱：
            #
            # 科目表每个年度各有一份，2025 年的 ``1002`` 与 2024 年的
            # ``1002`` 是两行、两个 id，**分录与期初快照都挂在各自那一年的
            # id 上**。拿 2025 的那行去算 2024-12 的期末 —— 那个时点上它
            # 一行分录都没有、也没有期初快照 —— 算出来是 **0**。
            #
            # 于是「2025-01 ~ 2025-12」这个区间的期初恒为 0、期末也跟着错，
            # 而**行内自洽**（期初 + 区间发生额 = 期末），只有与单期查询
            # 交叉核对才看得出来。「给我 2025 年这一年的余额」是最自然的
            # 提问之一，它拿到的是错数。
            #
            # 现在改成直接取**起始期自己的期初**（``compute(ids, from_ord)``），
            # 口径与单期查询**逐字一致** —— 同一期末日期、两种查法，期末合计
            # 必须相等（``balance_sheet_of_accounts`` 的区间闸门守着这条）。
            open_map = self.compute(ids, from_ord)
            moves = self.range_movements(ids, from_ord, to_ord)

        out: list[MergedFigures] = []
        for _code, members in groups.items():
            first, last = members[0], members[-1]
            if open_map is not None:
                # ``first`` 是组里**最早**那一年的行 —— 也就是区间起始期
                # 所属年度的那一行（区间涉及哪几年，取的就是哪几年的表）。
                opening = open_map[first.id].opening
            else:
                opening = fmap[first.id].opening
            dr = sum((moves.get(m.id, (ZERO, ZERO))[0] for m in members), ZERO)
            cr = sum((moves.get(m.id, (ZERO, ZERO))[1] for m in members), ZERO)
            out.append(MergedFigures(
                subject=last,
                opening=opening,
                period_dr=dr,
                period_cr=cr,
                ytd_dr=fmap[last.id].dr_cum,
                ytd_cr=fmap[last.id].cr_cum,
                closing=opening + dr - cr,
            ))
        return out

    def figures(self, subject_id: int, ord_: int) -> Figures:
        key = (subject_id, ord_)
        if key not in self._figures:
            # widen_on_miss=True：这是「逐个来」的形状，缓存盖不住时退化为
            # 全账套聚合 —— 见 _cum_movements 的 docstring why。
            self.compute([subject_id], ord_, widen_on_miss=True)
        return self._figures[key]

    def one(self, subject_id: int, ord_: int | None = None) -> Figures:
        return self.figures(subject_id, ord_ or max(self._period_map()))

    # ---------------- 报表公式原语 ----------------

    def opening(self, subject_id: int, ord_: int) -> Decimal:
        return self.figures(subject_id, ord_).opening

    def debit_cum(self, subject_id: int, ord_: int) -> Decimal:
        return self.figures(subject_id, ord_).dr_cum

    def credit_cum(self, subject_id: int, ord_: int) -> Decimal:
        return self.figures(subject_id, ord_).cr_cum

    def closing(self, subject_id: int, ord_: int) -> Decimal:
        return self.figures(subject_id, ord_).closing


# ---------------------------------------------------------------------
# 展示层辅助
# ---------------------------------------------------------------------


def display_side(subject: Subject, signed: Decimal) -> tuple[str, Decimal]:
    """把借正贷负的 signed 金额转成「方向 + 绝对金额」。

    **方向只看符号，不看科目属性。**
    signed 的定义就是「借方合计 - 贷方合计」，符号本身已经说明净额落在哪一边：
        signed > 0 -> 借方        signed < 0 -> 贷方

    科目属性（Subject.direction）只用于资产负债表「资产列 / 负债列」这类取数，
    不能拿来判断余额在哪一方。早期版本按自然方向判断，导致 2001 短期借款
    期末 -1000 被显示成「借 1000」，与试算平衡自相矛盾 ——
    balance_sheet_of_accounts 是按符号正负累计借贷方的，同一行会一边算进贷方、
    一边标成借方。

    subject 保留在签名里：调用点遍布账簿各查询，保持位置参数调用不变。
    """
    if signed == 0:
        return "平", ZERO
    return ("借" if signed > 0 else "贷"), abs(signed)
