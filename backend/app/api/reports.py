"""报表：模板列表、求值、单元格公式编辑。"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.api.schemas import CellIn
from app.core.db import get_db
from app.core.deps import get_current_user, get_my_book, require_book_write
from app.models import (Book, Period, ReportCell, ReportCol, ReportResult, ReportRow,
                     ReportTemplate, SysUser, UserBook)
from app.reports.evaluate import COL_KINDS
from app.reports.formula import no_to_col, rewrite_refs

router = APIRouter(prefix="/api/reports", tags=["报表"])


@router.get("/templates")
def templates(db: Session = Depends(get_db)):
    rows = db.scalars(select(ReportTemplate).order_by(ReportTemplate.id)).all()
    return [{"id": t.id, "code": t.code, "name": t.name, "type": t.type,
             "is_builtin": t.is_builtin, "remark": t.remark} for t in rows]


def _can_write(db: Session, user: SysUser, book_id: int | None = None) -> bool:
    """报表模板跨账套共享，因此只要用户对任一账套有**写**权限即可修改。

    这里原来只查「有没有这条授权」，没查角色 —— 于是 VIEWER（只读）也能改公式、
    增删行列。授权存在 ≠ 有权写，少了 role 这一层判断，整套只读模型在这条路径上
    就形同虚设。补上 ``role != "VIEWER"``。
    """
    stmt = select(UserBook).where(UserBook.user_id == user.id, UserBook.role != "VIEWER")
    if book_id:
        stmt = stmt.where(UserBook.book_id == book_id)
    return db.scalar(stmt.limit(1)) is not None


@router.get("/{template_id}/design")
def design(template_id: int, db: Session = Depends(get_db)):
    tpl = db.get(ReportTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "报表模板不存在")
    rows = db.scalars(select(ReportRow).where(ReportRow.template_id == template_id)
                      .order_by(ReportRow.row_no)).all()
    cols = db.scalars(select(ReportCol).where(ReportCol.template_id == template_id)
                      .order_by(ReportCol.col_no)).all()
    cells = db.scalars(select(ReportCell).where(ReportCell.template_id == template_id)).all()
    return {
        "id": tpl.id, "code": tpl.code, "name": tpl.name, "type": tpl.type,
        "rows": [{"row_no": r.row_no, "label": r.label, "indent": r.indent,
                  "bold": r.bold, "hidden": r.hidden} for r in rows],
        "cols": [{"col_no": c.col_no, "label": c.label, "kind": c.kind,
                  "hidden": c.hidden, "width": c.width} for c in cols],
        "cells": [{"row_no": c.row_no, "col_no": c.col_no, "formula": c.formula} for c in cells],
    }


@router.get("/{template_id}/render")
def render(template_id: int, book_id: int, period_id: int, db: Session = Depends(get_db),
           book: Book = Depends(get_my_book), include_unposted: bool = Query(False)):
    from app.reports.evaluate import evaluate

    period = db.get(Period, period_id)
    if period is None or period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    data = evaluate(db, template_id, book, period, include_unposted)

    saved = db.scalars(select(ReportResult).where(
        ReportResult.template_id == template_id, ReportResult.book_id == book.id,
        ReportResult.period_id == period.id)).all()
    data["cached_at"] = max((r.created_at for r in saved), default=None)
    return data


@router.put("/{template_id}/cells")
def save_cells(template_id: int, payload: list[CellIn], db: Session = Depends(get_db),
               book_id: Optional[int] = None, user=Depends(get_current_user)):
    """报表模板是跨账套的，改公式只需具备任一账套的写权限。"""
    if db.get(ReportTemplate, template_id) is None:
        raise HTTPException(404, "报表模板不存在")
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")

    # P3-10：改公式必须留 before。公式写错一个字符，报表就会悄无声息地
    # 少算一列，而页面照样渲染得出来 —— 没有原公式根本无从判断是哪改坏的。
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for item in payload:
        cell = db.scalar(select(ReportCell).where(
            ReportCell.template_id == template_id, ReportCell.row_no == item.row_no,
            ReportCell.col_no == item.col_no))
        key = f"r{item.row_no}c{item.col_no}"
        if cell:
            before[key] = cell.formula
            cell.formula = item.formula
        else:
            db.add(ReportCell(template_id=template_id, row_no=item.row_no,
                              col_no=item.col_no, formula=item.formula))
        after[key] = item.formula
    db.flush()

    if before or after:
        log(db, action="UPDATE_REPORT_CELLS", entity="report_template",
            entity_id=template_id, user=user, book_id=book_id,
            detail=f"修改报表 {template_id} 的 {len(after)} 个单元格公式",
            before=before, after=after)
    db.commit()
    return {"ok": True, "updated": len(payload)}


@router.post("/{template_id}/persist")
def persist(template_id: int, book_id: int, period_id: int, db: Session = Depends(get_db),
            book: Book = Depends(get_my_book),
            _link: UserBook = Depends(require_book_write)):
    """把一次求值结果落库，便于导出和排查。"""
    from app.reports.evaluate import evaluate

    period = db.get(Period, period_id)
    if period is None or period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    data = evaluate(db, template_id, book, period, False)

    db.query(ReportResult).filter(
        ReportResult.template_id == template_id, ReportResult.book_id == book.id,
        ReportResult.period_id == period.id
    ).delete(synchronize_session=False)

    from decimal import Decimal

    for row in data["rows"]:
        for idx, cell in enumerate(row["cells"]):
            col_no = data["cols"][idx]["col_no"]
            db.add(ReportResult(
                template_id=template_id, book_id=book.id, period_id=period.id,
                row_no=row["row_no"], col_no=col_no, formula=cell["formula"],
                value=Decimal(cell["value"]) if cell["value"] else None,
                error=cell["error"],
            ))
    db.commit()
    return {"saved": True, "errors": data["errors"]}


# ===================================================================== 模板管理

from pydantic import BaseModel, Field  # noqa: E402


class ReportTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    remark: str = ""
    # 从哪个内置模板复制行列骨架；为空则建一张空白表
    base_code: Optional[str] = "BALANCE_SHEET"


class ReportTemplatePatch(BaseModel):
    name: Optional[str] = None
    remark: Optional[str] = None


class ReportRowPatch(BaseModel):
    label: Optional[str] = None
    indent: Optional[int] = None
    bold: Optional[bool] = None
    hidden: Optional[bool] = None
    style: Optional[dict] = None


class ReportColPatch(BaseModel):
    label: Optional[str] = None
    kind: Optional[str] = None
    hidden: Optional[bool] = None
    width: Optional[int] = None


#: 行号位移的临时偏移。必须够大，确保搬走的行号不会撞上任何现存行号。
_ROW_SHIFT = 100_000


@contextmanager
def _shift_rows(db: Session, template_id: int, at: int, direction: int):
    """在第 ``at`` 行处插入/删除一行，把后缀整体平移一格。

    ``uq_report_row`` / ``uq_report_cell`` 是**即时**唯一约束，直接把 5 改成 6
    会在中间态与还停在 6 的行撞库（Postgres 不保证 UPDATE 的执行顺序）。
    这里先整体挪到 ``_ROW_SHIFT`` 之外的空档，flush，再落到最终行号。
    """
    rows = list(db.scalars(
        select(ReportRow).where(ReportRow.template_id == template_id)).all())
    cells = list(db.scalars(
        select(ReportCell).where(ReportCell.template_id == template_id)).all())

    moved_rows = [r for r in rows if r.row_no >= at]
    moved_cells = [c for c in cells if c.row_no >= at]

    # 阶段一：整段挪进空档
    for r in moved_rows:
        r.row_no += _ROW_SHIFT
    for c in moved_cells:
        c.row_no += _ROW_SHIFT
    db.flush()

    try:
        yield
        db.flush()
        # 阶段二：先退回空档，再按 direction 落到最终行号。
        # 注意是「-SHIFT + direction」，不能直接 += direction，否则会停在空档里。
        for r in moved_rows:
            r.row_no = r.row_no - _ROW_SHIFT + direction
        for c in moved_cells:
            c.row_no = c.row_no - _ROW_SHIFT + direction
        db.flush()
    except Exception:
        db.rollback()
        raise


@contextmanager
def _shift_cols(db: Session, template_id: int, at: int, direction: int):
    """在第 ``at`` 列处插入/删除一列，把后缀整体平移一格。

    和 :func:`_shift_rows` 同一个理由：``uq_report_col`` / ``uq_report_cell`` 是即时
    唯一约束，直接改列号会在中间态撞库。
    """
    cols = list(db.scalars(
        select(ReportCol).where(ReportCol.template_id == template_id)).all())
    cells = list(db.scalars(
        select(ReportCell).where(ReportCell.template_id == template_id)).all())

    moved_cols = [c for c in cols if c.col_no >= at]
    moved_cells = [c for c in cells if c.col_no >= at]

    for c in moved_cols:
        c.col_no += _ROW_SHIFT
    for c in moved_cells:
        c.col_no += _ROW_SHIFT
    db.flush()

    try:
        yield
        db.flush()
        for c in moved_cols:
            c.col_no = c.col_no - _ROW_SHIFT + direction
        for c in moved_cells:
            c.col_no = c.col_no - _ROW_SHIFT + direction
        db.flush()
    except Exception:
        db.rollback()
        raise


def _rewrite_formulas(db: Session, template_id: int, *, row_shift=None, col_shift=None) -> int:
    """行列位移后，把整张表的公式引用同步改一遍。

    要改的是**全部**单元格，不只是被挪动的那些：第 5 行的公式完全可能引用
    第 20 行。只改被挪的格子，另一半引用会静默失准。

    返回改写条数（公式为空或没被引用影响的不算）。
    """
    touched = 0
    for c in db.scalars(select(ReportCell)
                        .where(ReportCell.template_id == template_id)).all():
        new = rewrite_refs(c.formula, row_shift=row_shift, col_shift=col_shift)
        if new != c.formula:
            c.formula = new
            touched += 1
    return touched


def _clone_skeleton(db: Session, src_id: int, dst_id: int) -> None:
    for r in db.scalars(select(ReportRow).where(ReportRow.template_id == src_id)).all():
        db.add(ReportRow(template_id=dst_id, row_no=r.row_no, label=r.label,
                         indent=r.indent, bold=r.bold, hidden=r.hidden, style=r.style))
    for c in db.scalars(select(ReportCol).where(ReportCol.template_id == src_id)).all():
        db.add(ReportCol(template_id=dst_id, col_no=c.col_no, label=c.label,
                         kind=c.kind, hidden=c.hidden, width=c.width))
    for c in db.scalars(select(ReportCell).where(ReportCell.template_id == src_id)).all():
        db.add(ReportCell(template_id=dst_id, row_no=c.row_no, col_no=c.col_no,
                          formula=c.formula))


@router.post("/templates", status_code=201)
def create_template(payload: ReportTemplateIn, book_id: Optional[int] = None,
                    user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """新建报表模板：复制一份内置表结构，财务在此基础上改公式。"""
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    tpl = ReportTemplate(code=f"CUSTOM_{uuid.uuid4().hex[:10].upper()}",
                         name=payload.name, type="CUSTOM",
                         remark=payload.remark, is_builtin=False)
    db.add(tpl)
    db.flush()
    base = db.scalar(select(ReportTemplate).where(ReportTemplate.code == payload.base_code)) \
        if payload.base_code else None
    if base is not None:
        _clone_skeleton(db, base.id, tpl.id)
    db.commit()
    return {"id": tpl.id, "code": tpl.code, "name": tpl.name, "type": tpl.type}


@router.post("/templates/{template_id}/copy")
def copy_template(template_id: int, name: Optional[str] = None, book_id: Optional[int] = None,
                  user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """复制模板（含全部公式）。改公式前先留底，财务自己就能做。"""
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    src = db.get(ReportTemplate, template_id)
    if src is None:
        raise HTTPException(404, "报表模板不存在")
    tpl = ReportTemplate(code=f"CUSTOM_{uuid.uuid4().hex[:10].upper()}",
                         name=name or f"{src.name} 副本", type="CUSTOM",
                         remark=src.remark, is_builtin=False)
    db.add(tpl)
    db.flush()
    _clone_skeleton(db, src.id, tpl.id)
    db.commit()
    return {"id": tpl.id, "code": tpl.code, "name": tpl.name}


@router.patch("/templates/{template_id}")
def update_template(template_id: int, payload: ReportTemplatePatch,
                    book_id: Optional[int] = None, user: SysUser = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    tpl = db.get(ReportTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "报表模板不存在")
    if payload.name is not None:
        tpl.name = payload.name
    if payload.remark is not None:
        tpl.remark = payload.remark
    db.commit()
    return {"id": tpl.id, "name": tpl.name, "remark": tpl.remark}


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, book_id: Optional[int] = None,
                    user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    tpl = db.get(ReportTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "报表模板不存在")
    if tpl.is_builtin:
        raise HTTPException(400, "内置报表模板不允许删除，可以先复制一份再改")
    db.delete(tpl)
    db.commit()
    return {"ok": True}


@router.patch("/templates/{template_id}/rows/{row_no}")
def update_row(template_id: int, row_no: int, payload: ReportRowPatch, book_id: Optional[int] = None,
               user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """改行的标签/缩进/加粗/隐藏。

    只靠插删行拼不出想要的表 —— 「合计行加粗」「几行不打印」都得能单独调。
    """
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    row = db.scalar(select(ReportRow).where(ReportRow.template_id == template_id,
                                             ReportRow.row_no == row_no))
    if row is None:
        raise HTTPException(404, "行不存在")
    if payload.label is not None:
        row.label = payload.label[:128]
    if payload.indent is not None:
        row.indent = max(0, min(6, payload.indent))
    if payload.bold is not None:
        row.bold = payload.bold
    if payload.hidden is not None:
        row.hidden = payload.hidden
    if payload.style is not None:
        row.style = payload.style
    db.commit()
    return {"ok": True, "row_no": row_no}


@router.post("/templates/{template_id}/rows")
def add_row(template_id: int, payload: dict, book_id: Optional[int] = None,
            user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """在指定位置插入一行，下方行的公式单元格整体下移，公式内容不变。"""
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    if db.get(ReportTemplate, template_id) is None:
        raise HTTPException(404, "报表模板不存在")
    rows = list(db.scalars(select(ReportRow).where(ReportRow.template_id == template_id)
                           .order_by(ReportRow.row_no)).all())
    at = int(payload.get("row_no") or (rows[-1].row_no + 1 if rows else 1))
    if at <= 0:
        raise HTTPException(400, "row_no 必须为正整数")

    with _shift_rows(db, template_id, at, direction=+1):
        db.add(ReportRow(template_id=template_id, row_no=at,
                         label=str(payload.get("label", "")),
                         indent=int(payload.get("indent", 0) or 0),
                         bold=bool(payload.get("bold", False)),
                         hidden=bool(payload.get("hidden", False)),
                         style=payload.get("style") or {}))
    shifted = _rewrite_formulas(db, template_id, row_shift=(at, +1))
    db.commit()
    return {"ok": True, "row_no": at, "formulas_shifted": shifted}


@router.delete("/templates/{template_id}/rows/{row_no}")
def delete_row(template_id: int, row_no: int, book_id: Optional[int] = None,
               user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    row = db.scalar(select(ReportRow).where(ReportRow.template_id == template_id,
                                             ReportRow.row_no == row_no))
    if row is None:
        raise HTTPException(404, "行不存在")
    # 目标行和它的公式单元格必须在位移之前抓住 —— 位移之后它们已经不在 row_no 上了
    doomed = list(db.scalars(select(ReportCell).where(
        ReportCell.template_id == template_id, ReportCell.row_no == row_no)).all())
    with _shift_rows(db, template_id, row_no, direction=-1):
        db.delete(row)
        for c in doomed:
            db.delete(c)
    # 删除用 at+1：被删掉的行号本身不再参与映射
    shifted = _rewrite_formulas(db, template_id, row_shift=(row_no + 1, -1))
    db.commit()
    return {"ok": True, "deleted_cells": len(doomed), "formulas_shifted": shifted}


# --------------------------------------------------------------------- 列
#
# 「自由可调」不只是改公式：利润表想加一列「上年同期」、资产负债表想多拆一个
# 层级，都得能自己加列。所以列和行一样支持增删改，且同样要同步改写公式引用
# （插一列会让 B 列变成 C 列，公式里的 B10 得跟着走）。


@router.post("/templates/{template_id}/cols", status_code=201)
def add_col(template_id: int, payload: dict, book_id: Optional[int] = None,
            user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """在指定位置插入一列。默认沿用左边那一列的取数口径。"""
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    if db.get(ReportTemplate, template_id) is None:
        raise HTTPException(404, "报表模板不存在")
    cols = list(db.scalars(select(ReportCol).where(ReportCol.template_id == template_id)
                           .order_by(ReportCol.col_no)).all())
    at = int(payload.get("col_no") or (cols[-1].col_no + 1 if cols else 1))
    if at <= 1:
        raise HTTPException(400, "列号从 2 开始，第 1 列是固定的「项目」列")

    # 口径默认「沿用左边最近的一个数值列」；左边一个都没有（插在标签列右边，
    # 最常见的用法）就退回整张表的第一个数值列。绝不能继承 STATIC —— 第 1 列
    # 是固定的「项目」标签列，继承它会造出一个既不显示、又没有任何取数口径的
    # 幽灵列，看着像插成功了，导出却没有这一列。
    value_cols = [c for c in cols if c.kind != "STATIC"]
    left = [c for c in value_cols if c.col_no < at]
    neighbour = left[-1] if left else (value_cols[0] if value_cols else None)
    kind = str(payload.get("kind") or (neighbour.kind if neighbour else "BALANCE_CLOSING"))
    if kind == "STATIC" or kind not in COL_KINDS:
        raise HTTPException(400, f"未知的取数口径 {kind}，可选：{', '.join(sorted(COL_KINDS))}")

    with _shift_cols(db, template_id, at, direction=+1):
        db.add(ReportCol(template_id=template_id, col_no=at,
                         label=str(payload.get("label") or "新列"),
                         kind=kind,
                         hidden=bool(payload.get("hidden", False)),
                         width=int(payload.get("width") or 140)))
    shifted = _rewrite_formulas(db, template_id, col_shift=(at, +1))
    db.commit()
    return {"ok": True, "col_no": at, "letter": no_to_col(at), "formulas_shifted": shifted}


@router.patch("/templates/{template_id}/cols/{col_no}")
def update_col(template_id: int, col_no: int, payload: ReportColPatch, book_id: Optional[int] = None,
               user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    col = db.scalar(select(ReportCol).where(ReportCol.template_id == template_id,
                                             ReportCol.col_no == col_no))
    if col is None:
        raise HTTPException(404, "列不存在")
    if payload.kind is not None and payload.kind != col.kind:
        if payload.kind != "STATIC" and payload.kind not in COL_KINDS:
            raise HTTPException(400, f"未知的取数口径 {payload.kind}")
        col.kind = payload.kind
    if payload.label is not None:
        col.label = payload.label[:64]
    if payload.hidden is not None:
        col.hidden = payload.hidden
    if payload.width is not None:
        col.width = max(60, min(600, payload.width))
    db.commit()
    return {"ok": True, "col_no": col_no}


@router.delete("/templates/{template_id}/cols/{col_no}")
def delete_col(template_id: int, col_no: int, book_id: Optional[int] = None,
               user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if not _can_write(db, user, book_id):
        raise HTTPException(403, "没有可写账套的权限")
    col = db.scalar(select(ReportCol).where(ReportCol.template_id == template_id,
                                             ReportCol.col_no == col_no))
    if col is None:
        raise HTTPException(404, "列不存在")
    if col.kind == "STATIC":
        raise HTTPException(400, "「项目」列是固定列，不能删")
    remaining = db.scalar(
        select(func.count()).select_from(ReportCol).where(
            ReportCol.template_id == template_id, ReportCol.col_no != col_no,
            ReportCol.kind != "STATIC"))
    if not remaining:
        raise HTTPException(400, "至少要保留一个数值列")

    doomed = list(db.scalars(select(ReportCell).where(
        ReportCell.template_id == template_id, ReportCell.col_no == col_no)).all())
    with _shift_cols(db, template_id, col_no, direction=-1):
        db.delete(col)
        for c in doomed:
            db.delete(c)
    shifted = _rewrite_formulas(db, template_id, col_shift=(col_no + 1, -1))
    db.commit()
    return {"ok": True, "deleted_cells": len(doomed), "formulas_shifted": shifted}
