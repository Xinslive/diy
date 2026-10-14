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

from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ledger.engine import ZERO, LedgerEngine
from app.models import Book, Entry, Period, ReportCell, ReportCol, ReportRow, Subject, Voucher
from app.models.book import book_first_ord, period_ord, year_start_ord
from app.reports.formula import D, Evaluator, FormulaError, formula_guard

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
        self.formulas: dict[tuple[int, int], str] = {}
        self._cells: dict[tuple[int, int], Decimal] = {}
        self._visiting: set[tuple[int, int]] = set()
        self._load_formulas()

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

    def resolve_period(self, expr) -> Period | None:
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
        if "-" in text:
            y, m = text.split("-", 1)
            y, m = int(y), int(m)
            return self._synthetic(period_ord(y, m, self.book.start_year))
        if text.isdigit():
            y = int(text)
            return self._synthetic(period_ord(y, 12, self.book.start_year))
        raise FormulaError(f"无法识别的期间: {expr}")

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
