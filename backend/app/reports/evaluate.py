"""报表求值：把模板的单元格公式跑出金额。"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ledger.engine import ZERO, LedgerEngine
from app.models import Book, Period, ReportCell, ReportCol, ReportRow, Subject
from app.reports.formula import D, Evaluator, FormulaError

# 列类型 -> (取数口径, 期间占位符)
COL_KINDS = {
    "BALANCE_CLOSING": ("期末", "{{本期}}"),
    "BALANCE_OPENING": ("期初", "{{年初}}"),
    "PERIOD_AMOUNT": ("本期金额", "{{本期}}"),
    "YTD_AMOUNT": ("本年累计", "{{本期}}"),
    "PERIOD_DEBIT": ("本期借方", "{{本期}}"),
    "PERIOD_CREDIT": ("本期贷方", "{{本期}}"),
}


class ReportContext:
    """求值上下文：把账簿引擎和报表模板胶合起来。"""

    def __init__(self, db: Session, book: Book, period: Period, include_unposted: bool = False):
        self.db = db
        self.book = book
        self.period = period
        self.start_ord = book.start_month
        self.eng = LedgerEngine(db, book.id, include_unposted)
        # 结转损益凭证不计入损益额，否则月度结转之后利润表全是 0
        self.eng_pf = LedgerEngine(db, book.id, include_unposted, exclude_carry_forward=True)
        self.subjects = {
            s.code: s
            for s in db.scalars(select(Subject).where(Subject.book_id == book.id)).all()
        }
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
    def resolve_period(self, expr) -> Period | None:
        if expr in (None, "", "{{本期}}", "{{期末}}"):
            return self.period
        if expr == "{{上期}}":
            prev = self.period.ord - 1
            return Period(book_id=self.book.id, year=self.period.year, month=max(prev, 1), ord=prev)
        if expr == "{{年初}}":
            return Period(
                book_id=self.book.id, year=self.period.year, month=self.start_ord, ord=self.start_ord
            )
        text = str(expr).strip()
        if "-" in text:
            y, m = text.split("-", 1)
            return Period(book_id=self.book.id, year=int(y), month=int(m), ord=int(m))
        if text.isdigit():
            y = int(text)
            return Period(book_id=self.book.id, year=y, month=12, ord=12)
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
        subj = self.db.get(Subject, sid)
        if subj and subj.direction == "CREDIT":
            return -signed
        return signed

    def period_amount(self, sid, period) -> Decimal:
        f = self.eng_pf.figures(sid, period.ord)
        return self._orient(sid, f.period_dr - f.period_cr)

    def ytd_amount(self, sid, period) -> Decimal:
        f = self.eng_pf.figures(sid, period.ord)
        return self._orient(sid, f.dr_cum - f.cr_cum)

    def closing_amt(self, sid, period) -> Decimal:
        return self._orient(sid, self.eng.figures(sid, period.ord).closing)

    def opening_amt(self, sid, period) -> Decimal:
        return self._orient(sid, self.eng.figures(sid, period.ord).opening)

    def period_amt(self, sid, period) -> Decimal:
        f = self.eng_pf.figures(sid, period.ord)
        return self._orient(sid, f.period_dr - f.period_cr)

    def ytd_amt(self, sid, period) -> Decimal:
        f = self.eng_pf.figures(sid, period.ord)
        return self._orient(sid, f.dr_cum - f.cr_cum)

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
            value = ZERO if not src.strip() else Evaluator(self).run(src)
        finally:
            self._visiting.discard(key)
        self._cells[key] = value
        return value


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
            if not src.strip():
                row_values.append({"value": None, "formula": "", "error": ""})
                continue
            try:
                value = evaluator.run(src)
                row_values.append({"value": str(value), "formula": src, "error": ""})
            except FormulaError as e:
                errors.append(f"{r.label}(第{r.row_no}行 第{c.label}列): {e}")
                row_values.append({"value": None, "formula": src, "error": str(e)})
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

    return {
        "rows": grid,
        "cols": [{"col_no": c.col_no, "label": c.label, "kind": c.kind,
                  "hidden": c.hidden, "width": c.width} for c in cols],
        "errors": errors,
        "balanced": not errors,
        "period": f"{period.year}-{period.month:02d}",
        "include_unposted": include_unposted,
    }
