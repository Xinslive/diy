"""发票导入、规则配置与自动生成凭证接口。"""
from __future__ import annotations

import io
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.schemas import InvoiceConfigIn, InvoicePreviewIn, InvoiceRuleIn, InvoiceRulePatch
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, get_my_book, require_book_write
# 本地别名保持函数名 ``read_upload`` 不变（守卫测试按这个名字认共享容量闸），
# 指向的其实是同步版 —— 端点是同步 ``def``，不能再 await。
from app.core.uploads import UploadTooLarge, read_upload_sync as read_upload
from app.invoice import service
from app.invoice import storage
from app.models import InvoiceBatch, InvoiceRecord, Book, SysUser, UserBook


router = APIRouter(prefix="/api/books/{book_id}/invoices", tags=["发票"])


def _batch(book: Book, db: Session, batch_id: int) -> InvoiceBatch:
    batch = db.get(InvoiceBatch, batch_id)
    if batch is None or batch.book_id != book.id:
        raise HTTPException(404, "发票批次不存在")
    return batch


@router.get("/config")
def get_invoice_config(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    return service.get_config(db, book.id)


@router.put("/config")
def put_invoice_config(payload: InvoiceConfigIn, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                       _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        result = service.save_config(db, book.id, payload.model_dump())
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return result


@router.get("/rules")
def get_invoice_rules(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    return service.list_rules(db, book.id)


@router.post("/rules", status_code=201)
def post_invoice_rule(payload: InvoiceRuleIn, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                      _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        return service.create_rule(db, book.id, user, payload.model_dump())
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.patch("/rules/{rule_id}")
def patch_invoice_rule(rule_id: int, payload: InvoiceRulePatch, book: Book = Depends(get_my_book),
                       db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                       user: SysUser = Depends(get_current_user)):
    try:
        return service.update_rule(db, book.id, rule_id, user, payload.model_dump(exclude_unset=True))
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.delete("/rules/{rule_id}")
def remove_invoice_rule(rule_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                        _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        service.delete_rule(db, book.id, rule_id, user)
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/batches", status_code=201)
def upload_invoice_batch(file: UploadFile = File(...), book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                         _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    filename = file.filename or "发票.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 PDF 发票文件")
    try:
        data = read_upload(file, limit=settings.invoice_max_bytes, label="发票 PDF")
        batch = service.create_batch(db, book.id, user, filename, data)
        preview = service.preview_batch(db, batch)
    except UploadTooLarge as exc:
        db.rollback()
        raise HTTPException(413, str(exc)) from exc
    except (ValueError, RuntimeError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return preview


@router.get("/batches")
def list_invoice_batches(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    rows = db.scalars(select(InvoiceBatch).where(InvoiceBatch.book_id == book.id).order_by(InvoiceBatch.id.desc()).limit(100)).all()
    return [service.batch_summary(row) for row in rows]


@router.get("/batches/{batch_id}")
def get_invoice_batch(batch_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    batch = _batch(book, db, batch_id)
    records = db.scalars(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id).order_by(InvoiceRecord.page_index)).all()
    return {**service.batch_summary(batch), "invoices": [service.serialize_record(record) for record in records]}


@router.post("/batches/{batch_id}/preview")
def preview_invoice_batch(batch_id: int, payload: InvoicePreviewIn | None = None,
                          book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                          _link: UserBook = Depends(require_book_write)):
    batch = _batch(book, db, batch_id)
    try:
        return service.preview_batch(db, batch, (payload.overrides if payload else {}))
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.post("/batches/{batch_id}/commit")
def commit_invoice_batch(batch_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                         _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    batch = _batch(book, db, batch_id)
    try:
        return service.commit_batch(db, batch, user)
    except (ValueError, ArithmeticError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.post("/batches/{batch_id}/discard")
def discard_invoice_batch(batch_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                          _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    batch = _batch(book, db, batch_id)
    try:
        service.discard_batch(db, batch, user)
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.get("/batches/{batch_id}/file")
def download_invoice_file(batch_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    batch = _batch(book, db, batch_id)
    try:
        path = storage.path_for(batch.file_path)
        content = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "发票原件不存在") from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{batch.filename.replace(chr(34), "")}"'},
    )
