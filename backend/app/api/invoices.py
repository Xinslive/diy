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
from app.core.uploads import UploadTooLarge, human_bytes, read_upload_sync as read_upload
from app.invoice import service
from app.invoice import storage
from app.models import InvoiceBatch, InvoiceRecord, Book, SysUser, UserBook


router = APIRouter(prefix="/api/books/{book_id}/invoices", tags=["发票"])


def _batch(book: Book, db: Session, batch_id: int) -> InvoiceBatch:
    batch = db.get(InvoiceBatch, batch_id)
    if batch is None or batch.book_id != book.id:
        raise HTTPException(404, "发票批次不存在")
    return batch


@router.get("/limits")
def get_invoice_limits(book: Book = Depends(get_my_book)):
    """单次上传的容量上限。

    前端要把这两个数显示出来（超限前就让人知道），所以由后端给 —— 前端
    抄一份常量的话，改了配置就会两边不一致，而不一致的那边只会安静地
    让用户撞上后端的报错。
    """
    return {
        "max_files": settings.invoice_max_files,
        "max_total_bytes": settings.invoice_max_total_bytes,
        "max_bytes": settings.invoice_max_bytes,
    }


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


def _check_upload_limits(files: list[UploadFile]) -> list[UploadFile]:
    """单次请求的容量闸：文件数与**合计**体积。

    合计而不是单个：网关那条 client_max_body_size 限的是整个请求体，
    「每个文件 20MB」不等于「一次能传 20MB」。非 PDF 也算进合计 ——
    它们照样占请求体。

    故意放在读第一个字节**之前**：超限的请求连文件内容都不进内存。
    """
    if not files:
        raise HTTPException(400, "请选择要上传的 PDF 文件")
    if len(files) > settings.invoice_max_files:
        raise UploadTooLarge(f"一次最多上传 {settings.invoice_max_files} 个文件，本次选了 {len(files)} 个")
    total = sum(getattr(upload, "size", 0) or 0 for upload in files)
    if total > settings.invoice_max_total_bytes:
        raise UploadTooLarge(
            f"本次上传合计 {human_bytes(total)}，超过单次上限 {human_bytes(settings.invoice_max_total_bytes)}。"
            "请分批上传")
    return files


@router.post("/batches", status_code=201)
def upload_invoice_batch(files: list[UploadFile] = File(...), book: Book = Depends(get_my_book),
                         db: Session = Depends(get_db),
                         _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        # 容量闸必须在读第一个字节之前；「非 PDF 忽略」交给 service ——
        # 那是唯一一处判定什么该被跳过，接口层不再写第二份规则。
        files = _check_upload_limits(files)
        payloads: list[tuple[str, bytes]] = []
        for upload in files:
            name = upload.filename or "发票.pdf"
            payloads.append((name, read_upload(
                upload, limit=settings.invoice_max_bytes, label=f"发票 {name}",
                hint="请拆成多份分别上传")))
        batch = service.create_batch(db, book.id, user, payloads)
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
    # 一次分组查询算完所有批次的「已忽略」张数，别按批次逐个查。
    ignored = service.ignored_counts(db, [row.id for row in rows])
    return [service.batch_summary(row, ignored.get(row.id, 0)) for row in rows]


@router.get("/batches/{batch_id}")
def get_invoice_batch(batch_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    batch = _batch(book, db, batch_id)
    records = db.scalars(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id).order_by(InvoiceRecord.page_index)).all()
    ignored = sum(1 for record in records if record.status == "IGNORED")
    return {**service.batch_summary(batch, ignored), "invoices": [service.serialize_record(record) for record in records]}


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
        # RFC 5987 写法（P2-05）。原来这里是
        # ``f'inline; filename="{batch.filename}"'``，只把 `"` 去掉。
        #
        # Starlette 发送响应头时按 **latin-1** 编码取值，而发票文件名默认就叫
        # 「发票.pdf」—— 于是 `UnicodeEncodeError`，**下载任何中文名发票都是 500**。
        # 字节还在 data/invoices/batch_N.pdf，原件没丢，但用户拿不回来。
        #
        # 同一个项目里 app/api/vouchers.py::_filename() 早就是正确写法
        # （``filename*=UTF-8''`` + percent-encoding），这里只是没复用。
        #
        # 不写 includeSubDomains 之类的额外属性；`inline` 保留原有的
        # 「浏览器里直接看」行为，下载另由前端决定。
        headers={"Content-Disposition": f"inline; {_rfc5987(batch.filename)}"},
    )


def _rfc5987(name: str) -> str:
    """把文件名编码进 Content-Disposition 的 ``filename*=UTF-8''`` 形式。

    与 vouchers.py::_filename 是同一个函数、同一套理由：latin-1 编不动的
    字符必须 percent-encoding 后走扩展参数，不能塞进裸的 filename=。
    """
    import urllib.parse

    # 控制字符（尤其 CR/LF）一律剔除 —— h11 在写出响应头时会因为头值里的
    # 换行报错，而文件名来自上传方，是不可信输入。
    safe = "".join(ch for ch in (name or "") if ch.isprintable())
    return "filename*=UTF-8''" + urllib.parse.quote(safe)
