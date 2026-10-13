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
刻意不做跨请求缓存：正确性优先。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.models import Book, Entry, Period, PeriodOpening, Subject, Voucher
from app.models.book import book_first_ord

ZERO = Decimal("0")


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
    # ---- 单次请求内的记忆化（2026-10-13 审计 P2-03 / P2-15）----
    # 全部是实例级缓存，失效边界见模块 docstring：实例只读，写后换实例。
    _book_cache: Book | None = None
    _period_map_cache: dict[int, Period] | None = None
    _ancestor_cache: dict[int, list[int]] | None = None
    # 发生额：已算过的最大上界 + 那个上界下的完整结果 + 各上界下的过滤视图。
    # 上界变小不需要重查（结果本来就是按 (subject_id, period_ord) 分好组的，
    # 过滤一下就是更小上界的结果），所以总账「算到 to」+「算到 from-1」
    # 只产生**一次**聚合查询。
    _moves_top: int | None = None
    _moves_full: dict[tuple[int, int], tuple[Decimal, Decimal]] = field(default_factory=dict)
    _moves_views: dict[int, dict[tuple[int, int], tuple[Decimal, Decimal]]] = field(default_factory=dict)
    _openings_cache: dict[tuple[int, int], Decimal] | None = None
    # 每个科目已经算到哪一期：重复请求同一科目不再重推一遍
    _computed_through: dict[int, int] = field(default_factory=dict)

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
            self._period_map_cache = {
                p.ord: p for p in self.db.scalars(
                    select(Period).where(Period.book_id == self.book_id).order_by(Period.ord)
                ).all()
            }
        return self._period_map_cache

    def _book(self) -> Book:
        if self._book_cache is None:
            self._book_cache = self.db.get(Book, self.book_id)
        return self._book_cache

    # ---------------- 原语 ----------------

    def _ancestor_map(self) -> dict[int, list[int]]:
        """科目 -> [自己, 上级, 上上级...]，用于把发生额上卷到各级父科目。"""
        if self._ancestor_cache is not None:
            return self._ancestor_cache
        rows = list(
            self.db.execute(
                select(Subject.id, Subject.parent_id).where(Subject.book_id == self.book_id)
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

        self._ancestor_cache = {sid: chain(sid) for sid in parent}
        return self._ancestor_cache

    def _cum_movements(self, up_to_ord: int) -> dict[tuple[int, int], tuple[Decimal, Decimal]]:
        """(subject_id, period_ord) -> (当期借方, 当期贷方)。

        结果已经上卷到所有父科目：凭证只挂在明细级科目上，
        但账簿/报表需要每一级父科目的发生额，所以在这里一次算完。

        **单次请求内记忆化**：上界变大才重查（以前的 2 次/3 次全量聚合成 1 次），
        上界变小只过滤 —— 因为结果本来就按 (subject_id, period_ord) 分好组，
        「<= 更小的上界」就是丢掉更晚的期。失效边界见模块 docstring（实例只读）。
        """
        cached = self._moves_views.get(up_to_ord)
        if cached is not None:
            return cached
        if self._moves_top is None or up_to_ord > self._moves_top:
            self._moves_full = self._query_movements(up_to_ord)
            self._moves_top = up_to_ord
            self._moves_views.clear()
        if up_to_ord == self._moves_top:
            view = self._moves_full
        else:
            view = {k: v for k, v in self._moves_full.items() if k[1] <= up_to_ord}
        self._moves_views[up_to_ord] = view
        return view

    def _query_movements(self, up_to_ord: int) -> dict[tuple[int, int], tuple[Decimal, Decimal]]:
        """账簿最热的聚合查询（唯一的全账扫描，见 P2-06 的索引建议）。"""
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
        """期初快照 -> 每个科目的期初。单次请求内只算一次（结果与写无关）。"""
        if self._openings_cache is None:
            self._openings_cache = self._roll_up_openings()
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
        moves = self._cum_movements(to_ord)
        acc = {sid: [ZERO, ZERO] for sid in subject_ids}
        for (sid, o), (dr, cr) in moves.items():
            cell = acc.get(sid)
            if cell is not None and from_ord <= o <= to_ord:
                cell[0] += dr
                cell[1] += cr
        return {sid: (cell[0], cell[1]) for sid, cell in acc.items()}

    # ---------------- 批量计算 ----------------

    def compute(self, subject_ids: list[int] | None = None, up_to_ord: int | None = None) -> dict[int, Figures]:
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

        moves = self._cum_movements(top)
        snapshots = self._openings()

        if subject_ids is None:
            # 不过滤 active：这是**默认的全科目集**，报表总额、账簿汇总都走它。
            # 停用科目被排除的话，上级科目合计会偏小、报表总额会缺一块，
            # 而用户完全看不出少了什么。写路径的停用闸门在 vouchers.py。
            subject_ids = list(
                self.db.scalars(
                    select(Subject.id).where(Subject.book_id == self.book_id)
                ).all()
            )

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

                    f = Figures(
                        subject_id=sid, ord=o, opening=opening,
                        dr_cum=dr_cum, cr_cum=cr_cum, period_dr=dr, period_cr=cr,
                    )
                    self._figures[(sid, o)] = f

                    opening = opening + dr - cr

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

    def figures(self, subject_id: int, ord_: int) -> Figures:
        key = (subject_id, ord_)
        if key not in self._figures:
            self.compute([subject_id], ord_)
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
