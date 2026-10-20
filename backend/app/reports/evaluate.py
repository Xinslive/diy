"""报表求值：把模板的单元格公式跑出金额。

**两个口径（审计 P1-15）**
------------------------
账簿引擎 ``LedgerEngine`` 有两个**刻意不同**的构造入口，本模块分别用：

* ``for_ledger`` —— 期初/借方累计/贷方累计/期末/本期借贷。
  与总账、科目余额表、打印导出**逐分相同**（含结转凭证）。
* ``for_report`` —— 损益取数（本期金额/本年累计/本期余额/本年余额）。
  **排除结转凭证**，否则月度结转之后损益类本期净额恒为 0。

两者对损益科目的差额**恰好是结转凭证金额**。这不是 bug 也不是「两套算法」，
但必须让界面看得见 —— 所以输出里带 ``carry_forward_excluded`` /
``excludes_carry_forward`` / ``carry_forward_period_amount`` / ``notices``，
前端据此在列头标注「本期金额已排除结转凭证」。README 开头「不存在某个报表和
总账对不上」这句话靠的就是这两个口径**可区分且可对账**，而不是靠它们相等。

**模板读权限的不变量（不要破坏）**
----------------------------------
公式里**无法指定账套 ID**：``科目()`` 只解析 ``Subject.book_id == book.id``，
所以一张全局模板天然只会按当前账套取数。给公式加「账套参数」等于开一个
跨账套取数后门 —— 那样任何能看模板的人都能读到别的账套的数。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ledger.engine import ZERO, LedgerEngine
from app.models import Book, Entry, Period, ReportCell, ReportCol, ReportRow, Subject, Voucher
from app.models.book import book_first_ord, year_start_ord
from app.reports.formula import D, Evaluator, FormulaError, formula_guard

#: 期间字面量里的**纯 ASCII 数字**。
#:
#: 刻意不用 ``str.isdigit()``：它对全角「２０２６」也返回 True，而
#: ``int("２０２６")`` 恰好能算 —— 于是全角年份会被当成合法期间静默折算；
#: 更糟的是 ``"²".isdigit()`` 也是 True 而 ``int("²")`` 抛 ValueError，
#: 那条异常不是 FormulaError，会一路穿出去变成 HTTP 500。
_DIGITS = re.compile(r"[0-9]+")

# 列类型 -> (取数口径, 期间占位符)
COL_KINDS = {
    "FORMULA": ("期末", "{{本期}}"),  # 迁移模板：每格显式公式决定取数口径
    "BALANCE_CLOSING": ("期末", "{{本期}}"),
    "BALANCE_OPENING": ("期初", "{{年初}}"),
    "PERIOD_AMOUNT": ("本期金额", "{{本期}}"),
    "YTD_AMOUNT": ("本年累计", "{{本期}}"),
    "PERIOD_DEBIT": ("本期借方", "{{本期}}"),
    "PERIOD_CREDIT": ("本期贷方", "{{本期}}"),
}

#: 走 ``for_report``（排除结转凭证）的取数口径，也就是**损益口径**。
#: 前端据此给列头加标注：这几列与总账的差额不是错，是口径差。
PF_KINDS = frozenset({"PERIOD_AMOUNT", "YTD_AMOUNT"})

#: 报表接口对外稳定的口径信号（前端标注的**唯一数据契约**）。
#:   carry_forward_excluded        —— 本次求值是否用了排除结转凭证的报表口径（恒为 true）
#:   cols[].excludes_carry_forward —— 该列**声明的**取数口径是否属于损益口径
#:                                  （单元格里的自定义公式不参与判断）
#:   carry_forward_period_amount   —— 本期结转凭证的借方合计（= 损益侧冲出的总额），
#:                                  本期没结转时为 null
#:   notices[]                     —— 可直接展示给用户的一句话，code 便于前端本地化
CARRY_FORWARD_NOTICE = {
    "code": "CARRY_FORWARD_EXCLUDED",
    "level": "info",
    "text": "「本期金额」「本年累计」等损益口径已排除结转凭证；"
            "损益科目上与总账 / 科目余额表的差额，即本期结转凭证金额。",
}


class ReportContext:
    """求值上下文：把账簿引擎和报表模板胶合起来。"""

    def __init__(self, db: Session, book: Book, period: Period, include_unposted: bool = False):
        self.db = db
        self.book = book
        self.period = period
        self.include_unposted = include_unposted
        # 「本年」起点：账套第一年从启用月开始，之后每年从 1 月开始。
        # ord 跨年度连续，所以这里不能直接拿 start_month 当第 N 年的年初。
        self.start_ord = year_start_ord(book.start_year, period.year, book.start_month)
        # 口径契约见模块 docstring。两个引擎都是**只读**的（引擎实例内记忆化，
        # 写后必须换实例），所以同一次求值里共用一个实例、把查询次数压到常数。
        self.eng = LedgerEngine.for_ledger(db, book.id, include_unposted)
        self.eng_pf = LedgerEngine.for_report(db, book.id, include_unposted)
        self.subjects = {
            s.code: s
            for s in db.scalars(select(Subject).where(Subject.book_id == book.id)).all()
        }
        # _orient 要按方向取正数，而公式里只拿得到 subject_id。
        # 一次性建好 id -> Subject 的索引：以前每格都 db.get() 一次，
        # 一张几十行的报表就是几十次多余的往返。
        self._subjects_by_id = {s.id: s for s in self.subjects.values()}
        # 父 -> 子。用来判断「这个科目是不是上级科目」—— 上级科目的取数口径
        # 由账簿引擎上卷（见 measure），报表层不再自己加一遍子树。
        self._subject_children: dict[int, list[int]] = {}
        for s in self.subjects.values():
            if s.parent_id is not None:
                self._subject_children.setdefault(s.parent_id, []).append(s.id)
        self.formulas: dict[tuple[int, int], str] = {}
        self._cells: dict[tuple[int, int], Decimal] = {}
        self._visiting: set[tuple[int, int]] = set()
        # 账套里真实存在的会计期间：(年, 月) -> Period。期间字面量必须指向
        # 其中一个（见 resolve_period）。
        self._known_periods_cache: Optional[dict[tuple[int, int], Period]] = None
        self._load_formulas()

    # -- 科目范围（末级 / 整棵子树）--

    #: 这些口径走 ``eng_pf``（for_report，排除结转凭证）。
    _PF_KINDS = frozenset({"period_amount", "period_amt", "ytd_amount", "ytd_amt"})

    def measure(self, sid: int, kind_key: str, period) -> Decimal:
        """按某个口径取数。

        **上级科目直接取账簿引擎上卷后的值**，报表层不再重算「整棵子树之和」。

        为什么不能自己加：账簿引擎早就把分录与期初快照上卷到父科目了
        （``_query_movements_uncached`` / ``_roll_up_openings``），而且它对
        期初有一条明确规则 —— **「自身快照是基准，下级只补增量」**：一个科目
        先做末级、后来又挂了子科目时，它自己身上那张快照**已经含着**下级的
        余额，是总数不是增量。

        报表层按「末级相加」算，得到的是另一个数：那个科目的数会随末级后代的
        个数而变（1 个末级后代时走引擎值、2 个及以上走末级之和），而账簿引擎
        始终给同一个数。同一条公式两个答案，就是「报表和总账对不上」。

        统一到引擎那一侧（而不是反过来改引擎）有两个理由：引擎是账簿的既有
        语义（总账、科目余额表、打印导出都按它算），而「末级相加」那条路
        建立在一个**已经不成立**的前提上 —— 「凭证只记到末级，上级科目身上
        永远是 0」。
        """
        fn = getattr(self, kind_key)
        if sid in self._subject_children:
            # 上级科目：先把整棵子树在引擎里算出来（一次聚合，与末级逐个取
            # 同一量级），再取它自己的上卷值。
            self.eng.compute([sid], period.ord)
            if kind_key in self._PF_KINDS:
                self.eng_pf.compute([sid], period.ord)
        return fn(sid, period)

    # -- 模板 --
    def _load_formulas(self) -> None:
        raise NotImplementedError  # 由 EvalContext 子类注入

    def subject_id(self, code: str) -> Optional[int]:
        s = self.subjects.get(code)
        return s.id if s else None

    # -- 期间 --
    def _ym(self, ord_: int) -> tuple[int, int]:
        """ord -> (year, month)。ord 跨年度连续，直接当月份用会算错年份。"""
        idx = ord_ - 1
        return self.book.start_year + idx // 12, idx % 12 + 1

    def _synthetic(self, ord_: int) -> Period:
        # 夹在账套第一个期间上：别算出 ord=0 这种不存在的期间。
        ord_ = max(ord_, book_first_ord(self.book))
        y, m = self._ym(ord_)
        return Period(book_id=self.book.id, year=y, month=m, ord=ord_)

    def _known_periods(self) -> dict[tuple[int, int], Period]:
        """账套里真实存在的会计期间：(年, 月) -> Period。

        走引擎的期间表（跨请求缓存，键里带 data_version）—— 期间是低频变更的，
        这里是纯读，不会拿到过期数据。
        """
        if self._known_periods_cache is None:
            self._known_periods_cache = {
                (p.year, p.month): p for p in self.eng.periods()
            }
        return self._known_periods_cache

    def _period_literal(self, year: int, month: int, expr) -> Period:
        """期间字面量 -> 账套里真实存在的那个会计期间。**越界即公式错误。**

        这里绝不「折算」：一个月份 13 折成次年 1 月、一个月份 00 折成首期、
        一个 1900 年折成首期，取到的都是**另一个期间**的数 —— 而且那个数
        长得完全正常（不是 0、不是空、不是红字），财务不会怀疑它。宁可这一格
        报一行红字，也不要整张表印着一个看着没问题的错数。
        """
        if not 1 <= month <= 12:
            raise FormulaError(f"无法识别的期间: {expr}（月份必须是 1~12）")
        known = self._known_periods()
        years = {y for (y, _m) in known}
        if years and not (min(years) <= year <= max(years)):
            raise FormulaError(
                f"无法识别的期间: {expr}"
                f"（本账套的会计期间在 {min(years)}~{max(years)} 年之间）")
        p = known.get((year, month))
        if p is None:
            raise FormulaError(
                f"无法识别的期间: {expr}"
                f"（本账套没有 {year} 年 {month} 月这个会计期间）")
        return p

    def resolve_period(self, expr) -> Period | None:
        """把公式里的期间字面量解析成一个会计期间。

        **解析不出来就是公式错误，绝不折算成别的期间。** 这一条是硬约束：
        报表页、报表打印、报表 Excel 导出、报表结果落库四个入口共用这里，
        一次「静默折算」就是四个通道同时印出错期间。
        """
        if expr in (None, "", "{{本期}}", "{{期末}}"):
            return self.period
        if expr == "{{上期}}":
            # 账套首期**没有**上期。这里以前是 `max(ord-1, 首期ord)` —— 把首期
            # 自己的 ord-1 夹回首期，于是首期报表里的 {{上期}} 拿到的是
            # **首期期末**：不报错、不为空、财务无从察觉（审计 P1-14）。
            # 「拿不到」必须看得见，所以明确报错：这一格会显示这句提示，
            # 整张表其余单元格照常算完。
            if self.period.ord <= book_first_ord(self.book):
                raise FormulaError(
                    f"账套第 {self.period.year}-{self.period.month:02d} 期是首个会计期间，"
                    f"没有「上期」可取（{{{{上期}}}} 在首期无意义）。"
                    f"请改用 {{{{年初}}}}，或把这格留空。")
            # 跨年时 ord-1 自然落到上一年的 12 月
            return self._synthetic(self.period.ord - 1)
        if expr == "{{年初}}":
            return self._synthetic(self.start_ord)
        text = str(expr).strip()
        # 只认 ASCII 数字：``str.isdigit()`` 对 "２０２６"（全角）也返回 True，
        # 而 ``int("²")`` 直接抛 ValueError —— 那条异常不是 FormulaError，
        # 会一路穿出去变成 HTTP 500（整张表都渲染不出来，不止那一格）。
        if "-" in text:
            y_text, m_text = text.split("-", 1)
            if not (_DIGITS.fullmatch(y_text) and _DIGITS.fullmatch(m_text)):
                raise FormulaError(f"无法识别的期间: {expr}")
            year, month = int(y_text), int(m_text)
        elif _DIGITS.fullmatch(text):
            # 裸年份 = 那一年的 12 期（口径见 formula.py 的「期间写法」）。
            year, month = int(text), 12
        else:
            raise FormulaError(f"无法识别的期间: {expr}")
        return self._period_literal(year, month, expr)

    # -- 账簿原语 --
    def opening(self, sid, period):
        return self.eng.figures(sid, period.ord).opening

    def debit_cum(self, sid, period):
        return self.eng.figures(sid, period.ord).dr_cum

    def credit_cum(self, sid, period):
        return self.eng.figures(sid, period.ord).cr_cum

    def closing(self, sid, period):
        return self.eng.figures(sid, period.ord).closing

    def period_dr(self, sid, period):
        return self.eng.figures(sid, period.ord).period_dr

    def period_cr(self, sid, period):
        return self.eng.figures(sid, period.ord).period_cr

    def _period_net(self, sid, period) -> Decimal:
        f = self.eng.figures(sid, period.ord)
        return f.period_dr - f.period_cr

    def _ytd_net(self, sid, period) -> Decimal:
        f = self.eng.figures(sid, period.ord)
        return f.dr_cum - f.cr_cum

    def _orient(self, sid, signed: Decimal) -> Decimal:
        """按科目自然方向取正数：收入类取正、成本费用类取正。"""
        subj = self._subjects_by_id.get(sid)
        if subj and subj.direction == "CREDIT":
            return -signed
        return signed

    # -- 公式里的「取数口径」入口 --
    #
    # 账簿口径（eng）：与总账/科目余额表逐分相同。
    def opening(self, sid, period):
        return self.eng.figures(sid, period.ord).opening

    def debit_cum(self, sid, period):
        return self.eng.figures(sid, period.ord).dr_cum

    def credit_cum(self, sid, period):
        return self.eng.figures(sid, period.ord).cr_cum

    def closing(self, sid, period):
        return self.eng.figures(sid, period.ord).closing

    def period_dr(self, sid, period):
        return self.eng.figures(sid, period.ord).period_dr

    def period_cr(self, sid, period):
        return self.eng.figures(sid, period.ord).period_cr

    def closing_amt(self, sid, period) -> Decimal:
        return self._orient(sid, self.eng.figures(sid, period.ord).closing)

    def opening_amt(self, sid, period) -> Decimal:
        return self._orient(sid, self.eng.figures(sid, period.ord).opening)

    # 损益口径（eng_pf = for_report，排除结转凭证）：与账簿口径差一个结转全额。
    def period_amount(self, sid, period) -> Decimal:
        f = self.eng_pf.figures(sid, period.ord)
        return self._orient(sid, f.period_dr - f.period_cr)

    def ytd_amount(self, sid, period) -> Decimal:
        f = self.eng_pf.figures(sid, period.ord)
        return self._orient(sid, f.dr_cum - f.cr_cum)

    def period_amt(self, sid, period) -> Decimal:
        return self.period_amount(sid, period)

    def ytd_amt(self, sid, period) -> Decimal:
        return self.ytd_amount(sid, period)

    # -- 口径标注的数据契约 --
    def carry_forward_period_amount(self) -> Decimal:
        """本期结转凭证的**借方合计**（= 从损益类冲出去的那笔总额）。没有结转就是 0。

        这是「报表和总账差多少」的量级参考：``for_report`` 与 ``for_ledger``
        对某个损益科目的差额，恰好是该科目当期的结转金额。它只用于**展示** ——
        让界面把差额说清楚，而不是让用户自己拿两页数字相减。

        **为什么报借方合计而不是净额**：结转凭证本身是借贷相等的
        （借 损益 / 贷 本年利润），所以「借 − 贷」全科目合计**恒等于 0**，
        报出来等于没报。真正有信息量的是被冲出去的那一侧。

        过滤条件与 ``LedgerEngine._query_movements`` 保持一致（同样的
        账套 / 期间 ord / 记账状态过滤），只把「排除 CARRY_FORWARD」换成
        「只要 CARRY_FORWARD」。它不是第三套口径。
        """
        stmt = (
            select(func.coalesce(
                func.sum(Entry.amount).filter(Entry.direction == "DEBIT"), 0))
            .join(Voucher, Voucher.id == Entry.voucher_id)
            .where(
                Entry.book_id == self.book.id,
                Entry.period_ord == self.period.ord,
                Voucher.source == "CARRY_FORWARD",
            )
        )
        if not self.include_unposted:
            stmt = stmt.where(Voucher.status == "POSTED")
        (dr,) = self.db.execute(stmt).one()
        return Decimal(dr)

    # -- 单元格（带记忆化与环检测） --
    def cell(self, row: int, col: int) -> Decimal:
        key = (row, col)
        if key in self._cells:
            return self._cells[key]
        if key in self._visiting:
            raise FormulaError(f"公式循环引用: 第{row}行 第{col}列")
        self._visiting.add(key)
        try:
            src = self.formulas.get(key, "")
            # 这里也要兜一层：单元格之间互相引用时栈比单条公式更深
            # （A1 -> Evaluator.run -> _eval -> cell -> ... 每一跳都加帧），
            # 只有每一层自己翻译成 FormulaError，才不会从某一跳漏成 500。
            with formula_guard():
                value = ZERO if not src.strip() else Evaluator(self).run(src)
        finally:
            self._visiting.discard(key)
        self._cells[key] = value
        return value

    def memoized_cell(self, row: int, col: int) -> Optional[Decimal]:
        """本轮求值里**已经算过**的这一格；没算过返回 ``None``。

        报表里被别的格子引用过的格子很多（合计行、期初列、校验行），而顶层
        循环走的是 ``evaluator.run(src)`` —— 于是这些格子在循环轮到它时
        **又完整重算一遍**，哪怕 ``_cells`` 里已经躺着它的值。

        这里只做「读」，不代算：没算过时返回 ``None``，由调用方照旧自己跑。
        这一点是刻意的 —— 代算会把这一格塞进 ``_cells``，从而改变**报错归属**：
        报错该出现在哪一格、说的是哪一对行列，是 ``_visiting`` 的栈决定的，
        顺手改了会让「这一格报错了」变成「它引用的那一格报错了」。

        值相同是构造上保证的：``_cells`` 里存的就是同一个 ctx、同一条公式串
        跑出来的 ``Decimal``，而这次求值期间账是只读的。
        """
        return self._cells.get((row, col))


class EvalContext(ReportContext):
    def __init__(self, db, book, period, formulas: dict[tuple[int, int], str], include_unposted=False):
        self._formulas = formulas
        super().__init__(db, book, period, include_unposted)

    def _load_formulas(self) -> None:
        self.formulas = self._formulas


# --------------------------------------------------------------------- 主入口


def evaluate(
    db: Session,
    template_id: int,
    book: Book,
    period: Period,
    include_unposted: bool = False,
) -> dict:
    rows = list(
        db.scalars(
            select(ReportRow).where(ReportRow.template_id == template_id).order_by(ReportRow.row_no)
        ).all()
    )
    cols = list(
        db.scalars(
            select(ReportCol).where(ReportCol.template_id == template_id).order_by(ReportCol.col_no)
        ).all()
    )
    cells = list(
        db.scalars(select(ReportCell).where(ReportCell.template_id == template_id)).all()
    )
    formulas = {(c.row_no, c.col_no): c.formula for c in cells}

    ctx = EvalContext(db, book, period, formulas, include_unposted)
    evaluator = Evaluator(ctx)

    grid, errors = [], []
    for r in rows:
        row_values = []
        for c in cols:
            src = formulas.get((r.row_no, c.col_no), "")
            # col_no 必须带在 cell 里：前端按下标渲染表格，而下标是 0 基、
            # 列号是 1 基，前端拿 ci 当 col_no 存公式会整体错位一列。
            base = {"col_no": c.col_no, "label": c.label, "kind": c.kind}
            if not src.strip():
                row_values.append({**base, "value": None, "formula": "", "error": ""})
                continue
            try:
                # 优先复用本轮求值里已经算过的这一格（见 ReportContext.memoized_cell）：
                # 值必然相同，省掉的是整棵 AST 的重走。没算过就照旧自己跑，
                # 报错归属保持原样。
                value = ctx.memoized_cell(r.row_no, c.col_no)
                if value is None:
                    value = evaluator.run(src)
                row_values.append({**base, "value": str(value), "formula": src, "error": ""})
            except FormulaError as e:
                errors.append(f"{r.label}(第{r.row_no}行 第{c.label}列): {e}")
                row_values.append({**base, "value": None, "formula": src, "error": str(e)})
        grid.append(
            {
                "row_no": r.row_no,
                "label": r.label,
                "indent": r.indent,
                "bold": r.bold,
                "hidden": r.hidden,
                "style": r.style,
                "cells": row_values,
            }
        )

    # 口径标注：只有本期真的结转过才需要提示（没结转时两个口径完全相等）。
    cf_amount = ctx.carry_forward_period_amount()
    notices = []
    if cf_amount != ZERO:
        notices.append({
            **CARRY_FORWARD_NOTICE,
            "cols": [c.col_no for c in cols if c.kind in PF_KINDS],
            "amount": str(cf_amount),
        })

    return {
        "rows": grid,
        # 每列带上「这一列的口径是否排除了结转凭证」，前端据此在列头标注
        # 「本期金额已排除结转凭证」（审计 P1-15 的数据契约，见模块 docstring）。
        "cols": [{"col_no": c.col_no, "label": c.label, "kind": c.kind,
                  "hidden": c.hidden, "width": c.width,
                  "excludes_carry_forward": c.kind in PF_KINDS}
                 for c in cols],
        "errors": errors,
        # **曾经叫 balanced**（审计 P3-3）。那个名字在报表语境下是误导：
        # 资产负债表模板里有「平衡校验（应为 0）」那一行，用户看到 balanced
        # 只会理解成「资产 = 负债 + 权益」，而它实际只是「没有公式报错」。
        # 全仓检索确认过这个字段**没有任何消费方**（前端只读 errors；
        # tests 里的 res["balanced"] 是期初接口自己的字段，真在算借贷平衡），
        # 所以直接改名而不是留一个别名 —— 留着只会让下一个读代码的人继续误会。
        "formula_error_free": not errors,
        # -- 口径标注（P1-15） --
        "carry_forward_excluded": True,           # 本次求值用的就是报表口径
        # 本期没结转就报 null：两个口径此时完全相等，没什么可标注的
        "carry_forward_period_amount": (str(cf_amount) if cf_amount != ZERO else None),
        "notices": notices,
        "period": f"{period.year}-{period.month:02d}",
        "include_unposted": include_unposted,
    }
