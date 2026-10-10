"""账簿聚合引擎。

全系统只有这一套数字计算逻辑，总账 / 明细账 / 科目余额表 / 利润表 /
资产负债表全部由它派生。

三个原语：
    期初期初期初期初(科目, 期间)
    借方累计(科目, 期间)     账套起始月 .. 期间 的已记账借方合计
    贷方累计(科目, 期间)

派生：
    期末 = 期初 + 借方累计 - 贷方累计
    当期借方 = 借方累计(P) - 借方累计(P-1)

期初的来源有两条：
    - 该期间有期初快照（结账时冻结 / 账套初始化录入）→ 直接取快照
    - 否则 → 由上一期期末实时推导
这样未结账期间不需要物化任何数据，反结账只要删掉快照即可自动恢复可写。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.models import Book, Entry, Period, PeriodOpening, Subject, Voucher

ZERO = Decimal("0")


@dataclass
class Figures:
    """单个科目在单个期间上的全部派生量。signed 表示借正贷负。"""

    subject_id: int
    ord: int
    opening: Decimal = ZERO      # signed
    dr_cum: Decimal = ZERO       # 累计借方发生额
    cr_cum: Decimal = ZERO       # 累计贷方发生额
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
    _figures: dict[tuple[int, int], Figures] = field(default_factory=dict)

    # ---------------- 基础查询 ----------------

    def _status_filter(self) -> Select:
        stmt = select()
        if not self.include_unposted:
            stmt = stmt.where(Voucher.status == "POSTED")
        return stmt

    def periods(self) -> list[Period]:
        return list(
            self.db.scalars(
                select(Period).where(Period.book_id == self.book_id).order_by(Period.ord)
            ).all()
        )

    def _period_map(self) -> dict[int, Period]:
        return {p.ord: p for p in self.periods()}

    def _book(self) -> Book:
        return self.db.get(Book, self.book_id)

    # ---------------- 原语 ----------------

    def _ancestor_map(self) -> dict[int, list[int]]:
        """科目 -> [自己, 上级, 上上级...]，用于把发生额上卷到各级父科目。"""
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

        return {sid: chain(sid) for sid in parent}

    def _cum_movements(self, up_to_ord: int) -> dict[tuple[int, int], tuple[Decimal, Decimal]]:
        """(subject_id, period_ord) -> (当期借方, 当期贷方)。

        结果已经上卷到所有父科目：凭证只挂在明细级科目上，
        但账簿/报表需要每一级父科目的发生额，所以在这里一次算完。
        """
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
        """期初快照 -> 每个科目的期初 = 自身快照 + 所有下级科目快照之和。"""
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

        def subtree_total(sid: int, o: int, depth: int = 0) -> Decimal:
            total = own.get((sid, o), ZERO)
            if depth < 16:
                for child in children.get(sid, []):
                    total += subtree_total(child, o, depth + 1)
            return total

        out: dict[tuple[int, int], Decimal] = {}
        for sid in ancestors:
            for o in set(k[1] for k in own):
                out[(sid, o)] = subtree_total(sid, o)
        out.update({k: v for k, v in own.items() if k not in out})
        return out

    # ---------------- 批量计算 ----------------

    def compute(self, subject_ids: list[int] | None = None, up_to_ord: int | None = None) -> dict[int, Figures]:
        """批量计算一批科目在 up_to_ord 期间的派生量。

        报表模板会同时引用几十个科目，批量接口保证每个科目只扫一次索引。
        """
        pmap = self._period_map()
        book = self._book()
        start = book.start_month
        top = up_to_ord if up_to_ord is not None else max(pmap) if pmap else start
        if top < start:
            top = start

        moves = self._cum_movements(top)
        snapshots = self._openings()

        if subject_ids is None:
            subject_ids = list(
                self.db.scalars(
                    select(Subject.id).where(Subject.book_id == self.book_id, Subject.active.is_(True))
                ).all()
            )

        # 逐期推进：
        #   期初(P)   = 期初(P-1) + 本期借方(P-1) - 本期贷方(P-1)
        #   借方累计(P) = 借方累计(P-1) + 本期借方(P)
        # 注意这里用的是「本期发生额」而不是「累计发生额」——
        # 用累计值会让期初逐月重复累加，余额虚增。
        result: dict[int, Figures] = {}
        for sid in subject_ids:
            opening = snapshots.get((sid, start), ZERO)
            dr_cum = cr_cum = ZERO
            f = Figures(subject_id=sid, ord=top)
            for o in range(start, top + 1):
                if (sid, o) in snapshots:
                    opening = snapshots[(sid, o)]

                dr, cr = moves.get((sid, o), (ZERO, ZERO))
                dr_cum += dr
                cr_cum += cr

                f = Figures(
                    subject_id=sid, ord=o, opening=opening,
                    dr_cum=dr_cum, cr_cum=cr_cum, period_dr=dr, period_cr=cr,
                )
                self._figures[(sid, o)] = f

                opening = opening + dr - cr

            result[sid] = f
        return result

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
