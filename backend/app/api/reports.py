"""报表：模板列表、求值、单元格公式编辑。"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import CellIn
from app.core.db import get_db
from app.core.deps import get_current_user, get_my_book
from app.models import (Book, Period, ReportCell, ReportCol, ReportResult, ReportRow,
                     ReportTemplate, SysUser, UserBook)

router = APIRouter(prefix="/api/reports", tags=["报表"])


@router.get("/templates")
def templates(db: Session = Depends(get_db)):
    rows = db.scalars(select(ReportTemplate).order_by(ReportTemplate.id)).all()
    return [{"id": t.id, "code": t.code, "name": t.name, "type": t.type,
             "is_builtin": t.is_builtin, "remark": t.remark} for t in rows]


def _can_write(db: Session, user: SysUser, book_id: int | None = None) -> bool:
    """报表模板跨账套共享，因此只要用户对任一账套有写权限即可修改。"""
    stmt = select(UserBook).where(UserBook.user_id == user.id)
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

    for item in payload:
        cell = db.scalar(select(ReportCell).where(
            ReportCell.template_id == template_id, ReportCell.row_no == item.row_no,
            ReportCell.col_no == item.col_no))
        if cell:
            cell.formula = item.formula
        else:
            db.add(ReportCell(template_id=template_id, row_no=item.row_no,
                              col_no=item.col_no, formula=item.formula))
    db.commit()
    return {"ok": True, "updated": len(payload)}


@router.post("/{template_id}/persist")
def persist(template_id: int, book_id: int, period_id: int, db: Session = Depends(get_db),
            book: Book = Depends(get_my_book)):
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
    db.commit()
    return {"ok": True, "row_no": at}


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
    db.commit()
    return {"ok": True, "deleted_cells": len(doomed)}
