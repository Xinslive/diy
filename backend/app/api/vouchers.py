"""凭证：增删改查、批量、记账/反记账。"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.accounting import importer as svc_import
from app.accounting import organize as svc_organize
from app.accounting import vouchers as svc
from app.api.schemas import BatchIds, VoucherIn
from app.core.db import get_db
from app.core.deps import get_book_link, get_current_user, get_my_book
from app.models import (Book, Entry, ImportBatch, Period, Subject, SysUser,
                     UserBook, Voucher)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _filename(name: str) -> str:
    import urllib.parse

    return "attachment; filename*=UTF-8''" + urllib.parse.quote(name)


def _diag(exc: DBAPIError) -> str:
    diag = getattr(exc.orig, "diag", None)
    return (getattr(diag, "message_primary", None) or str(exc.orig)).strip()

router = APIRouter(prefix="/api/books/{book_id}/vouchers", tags=["凭证"])


def _serialize(db: Session, v: Voucher) -> dict:
    entries = sorted(v.entries, key=lambda e: e.line_no)
    subs = {s.id: s for s in db.scalars(
        select(Subject).where(Subject.book_id == v.book_id)).all()}
    return {
        "id": v.id, "voucher_no": v.voucher_no, "voucher_date": v.voucher_date.isoformat(),
        "status": v.status, "source": v.source, "remark": v.remark,
        "attachments": v.attachments, "created_by": v.created_by,
        "posted_by": v.posted_by,
        "posted_at": v.posted_at.isoformat() if v.posted_at else None,
        "period_id": v.period_id,
        "total_debit": str(sum(e.amount for e in entries if e.direction == "DEBIT")),
        "total_credit": str(sum(e.amount for e in entries if e.direction == "CREDIT")),
        "entries": [
            {
                "id": e.id, "line_no": e.line_no, "subject_id": e.subject_id,
                "subject_code": subs[e.subject_id].code if e.subject_id in subs else "",
                "subject_name": subs[e.subject_id].name if e.subject_id in subs else "",
                "direction": e.direction, "amount": str(e.amount), "summary": e.summary,
            }
            for e in entries
        ],
    }


def _guard(link: UserBook) -> None:
    if link.role == "VIEWER":
        raise HTTPException(403, "只读账套不能修改凭证")


@router.get("")
def query(book: Book = Depends(get_my_book), db: Session = Depends(get_db),
          from_period_id: Optional[int] = None, to_period_id: Optional[int] = None,
          date_from: Optional[date] = None, date_to: Optional[date] = None,
          status: Optional[str] = None, source: Optional[str] = None,
          keyword: Optional[str] = None, min_amount: Optional[Decimal] = None,
          max_amount: Optional[Decimal] = None, subject_id: Optional[int] = None,
          page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
    fp = db.get(Period, from_period_id) if from_period_id else None
    tp = db.get(Period, to_period_id) if to_period_id else None
    rows, total = svc.query_vouchers(
        db, book.id, from_period=fp, to_period=tp, date_from=date_from, date_to=date_to,
        status=status, source=source, keyword=keyword, min_amount=min_amount,
        max_amount=max_amount, subject_id=subject_id, page=page, page_size=page_size,
    )
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_serialize(db, v) for v in rows]}


@router.post("", status_code=201)
def create(payload: VoucherIn, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
           link: UserBook = Depends(get_book_link), user: SysUser = Depends(get_current_user)):
    _guard(link)
    try:
        v = svc.create_voucher(
            db, book_id=book.id, user=user, voucher_date=payload.voucher_date,
            entries=[e.model_dump() for e in payload.entries], remark=payload.remark,
            attachments=payload.attachments, source=payload.source,
            voucher_no=payload.voucher_no,
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return _serialize(db, v)


@router.put("/{voucher_id}")
def update(voucher_id: int, payload: VoucherIn, book: Book = Depends(get_my_book),
           db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
           user: SysUser = Depends(get_current_user)):
    _guard(link)
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    try:
        data = payload.model_dump()
        data["entries"] = [e.model_dump() for e in payload.entries]
        svc.update_voucher(db, v, user, data)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return _serialize(db, v)


@router.delete("/{voucher_id}")
def remove(voucher_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
           link: UserBook = Depends(get_book_link), user: SysUser = Depends(get_current_user)):
    _guard(link)
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    try:
        svc.delete_voucher(db, v, user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/post")
def post_many(payload: BatchIds, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
              link: UserBook = Depends(get_book_link), user: SysUser = Depends(get_current_user)):
    _guard(link)
    try:
        n = svc.post_vouchers(db, payload.ids, user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"posted": n}


@router.post("/unpost")
def unpost_many(payload: BatchIds, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                link: UserBook = Depends(get_book_link), user: SysUser = Depends(get_current_user)):
    _guard(link)
    try:
        n = svc.unpost_vouchers(db, payload.ids, user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"unposted": n}


@router.post("/batch-delete")
def batch_delete(payload: BatchIds, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                 link: UserBook = Depends(get_book_link), user: SysUser = Depends(get_current_user)):
    _guard(link)
    rows = list(db.scalars(select(Voucher).where(
        Voucher.id.in_(payload.ids), Voucher.book_id == book.id)).all())
    # 先整批体检再删：批量选中 20 张、其中 1 张已记账时，
    # 逐张删到那张才报错的话，用户只知道「删失败」却不知道是哪一张。
    posted = [v.voucher_no for v in rows if v.status == "POSTED"]
    if posted:
        raise HTTPException(
            400, f"已记账的凭证不能删除，请先反记账：{'、'.join(posted[:10])}"
                 + ("…" if len(posted) > 10 else ""))
    try:
        for v in rows:
            svc.delete_voucher(db, v, user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"deleted": len(rows)}


# ===================================================================== 整理凭证


class OrganizeIn(BaseModel):
    period_id: Optional[int] = None      # 为空表示整个账套逐期整理
    strategy: Literal["GAPS", "RENUM", "SORT_DATE", "SORT_DATE_DESC"] = "GAPS"
    include_system: bool = False         # 是否把结转损益等系统凭证一起重排


@router.post("/organize/preview")
def organize_preview(payload: OrganizeIn, book: Book = Depends(get_my_book),
                     db: Session = Depends(get_db), link: UserBook = Depends(get_book_link)):
    """先看差异再确认。GAPS 只体检，其余给出逐张对照表。"""
    _guard(link)
    try:
        plans = svc_organize.plan_organize(
            db, book_id=book.id, period_id=payload.period_id,
            strategy=payload.strategy, include_system=payload.include_system)
    except ValueError as e:
        raise HTTPException(400, str(e))
    out = [p.to_dict() for p in plans]
    return {
        "strategy": payload.strategy,
        "periods": out,
        "total_changed": sum(p["changed_count"] for p in out),
        "total_gaps": sum(len(p["gaps"]) for p in out),
    }


@router.post("/organize")
def organize(payload: OrganizeIn, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
             link: UserBook = Depends(get_book_link), user: SysUser = Depends(get_current_user)):
    _guard(link)
    try:
        result = svc_organize.apply_organize(
            db, book_id=book.id, user=user, period_id=payload.period_id,
            strategy=payload.strategy, include_system=payload.include_system)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    except DBAPIError as e:
        db.rollback()
        raise HTTPException(400, f"改号失败：{_diag(e)}")
    return result


# ===================================================================== Excel 导入


@router.get("/import-template.xlsx")
def import_template(book: Book = Depends(get_my_book)):
    """下载标准导入模板，表头固定，前端设计器/帮助页直接引用。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "凭证导入"
    headers = ["日期", "凭证号", "摘要", "科目编码", "科目名称", "借方金额", "贷方金额",
               "附单据数", "备注"]
    ws.append(headers)
    fill = PatternFill("solid", fgColor="DCE6F1")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = fill
        c.alignment = Alignment(horizontal="center")
    for col, w in zip("ABCDEFGHI", (12, 12, 24, 14, 20, 14, 14, 10, 24)):
        ws.column_dimensions[col].width = w
    ws.append(["2026-01-15", "记0001", "收到租金", "1001", "库存现金", 5000, "", 1, "示例行，导入前请删除"])
    ws.append(["", "", "收到租金", "6001", "主营业务收入", "", 5000, "", ""])
    ws.append(["2026-01-20", "", "支付水电费", "6602", "管理费用/水电费", 800, "", 2, "日期为空表示接上一张"])
    ws.append(["", "", "支付水电费", "1002", "银行存款", "", 800, "", ""])
    ws.append([])
    ws.append(["说明：1) 一张凭证占多行，第二行起日期与凭证号留空"])
    ws.append(["      2) 科目编码或科目名称填一个即可，会自动匹配本账套科目"])
    ws.append(["      3) 借方/贷方金额只填有金额的一侧"])
    ws.append(["      4) 凭证号留空由系统按期间自动编号"])
    buf = io.BytesIO()
    wb.save(buf)
    return Response(content=buf.getvalue(), media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename("凭证导入模板.xlsx")})


@router.post("/import")
def upload_import(file: UploadFile = File(...), book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                  user: SysUser = Depends(get_current_user)):
    """上传并解析，暂存待确认。不写任何凭证。"""
    _guard(link)
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "只支持 .xlsx 格式（另存为 xlsx 后重试）")
    data = file.file.read()
    try:
        batch = svc_import.stage(db, book.id, user, file.filename, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "batch_id": batch.id, "filename": batch.filename, "status": batch.status,
        "total": batch.total, "ok_count": batch.ok_count, "error_count": batch.error_count,
        "vouchers": batch.payload.get("vouchers", []),
        "errors": batch.payload.get("errors", []),
        "unknown_subjects": batch.payload.get("unknown_subjects", {}),
        "unknown_headers": batch.payload.get("unknown_headers", []),
    }


@router.get("/import")
def list_imports(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ImportBatch).where(ImportBatch.book_id == book.id)
        .order_by(ImportBatch.id.desc()).limit(100)
    ).all()
    return [{"id": b.id, "filename": b.filename, "status": b.status, "total": b.total,
             "ok_count": b.ok_count, "error_count": b.error_count,
             "committed_count": b.committed_count, "message": b.message,
             "created_by": b.created_by, "created_at": b.created_at.isoformat()}
            for b in rows]


@router.post("/import/{batch_id}/commit")
def commit_import(batch_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  link: UserBook = Depends(get_book_link), user: SysUser = Depends(get_current_user)):
    _guard(link)
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.book_id != book.id:
        raise HTTPException(404, "导入批次不存在")
    try:
        result = svc_import.commit(db, batch, user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    batch.message = result["message"]
    db.commit()
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    return result


@router.post("/import/{batch_id}/discard")
def discard_import(batch_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                   link: UserBook = Depends(get_book_link), user: SysUser = Depends(get_current_user)):
    _guard(link)
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.book_id != book.id:
        raise HTTPException(404, "导入批次不存在")
    try:
        svc_import.discard(db, batch)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ===================================================================== 重排分录顺序


@router.post("/{voucher_id}/sort-entries")
def sort_entries(voucher_id: int, payload: dict, book: Book = Depends(get_my_book),
                 db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                 user: SysUser = Depends(get_current_user)):
    """按科目编码重排分录行，导出/打印更整齐。"""
    _guard(link)
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    if str(payload.get("by", "code")) != "code":
        raise HTTPException(400, "目前只支持按科目编码排序")

    subs = {s.id: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book.id)).all()}
    entries = sorted(v.entries, key=lambda e: subs[e.subject_id].code if e.subject_id in subs else "")
    for i, e in enumerate(entries, start=1):
        e.line_no = i
    db.flush()
    from app.accounting.audit_helper import log

    log(db, action="SORT", entity="voucher", entity_id=v.id, user=user, book_id=book.id,
        detail=f"整理凭证 {v.voucher_no} 的分录顺序")
    db.commit()
    return _serialize(db, v)


@router.get("/{voucher_id}")
def detail(voucher_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    return _serialize(db, v)
