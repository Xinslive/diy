"""发票导入、规则配置与自动生成凭证接口。"""
from __future__ import annotations

import io
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
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

log = logging.getLogger("cwq")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def _db_error(request: Request, exc: Exception) -> HTTPException:
    """把数据库异常翻成**脱敏**的 500。

    ``str(exc)`` 里有整条 SQL、绑定参数（**凭证金额、摘要、发票号码**）、
    表名、列名与约束名 —— 这些一律**只进服务端日志**（这里有完整堆栈），
    响应里只留一句脱敏提示 + 请求号。

    为什么在这里就收口，而不是抛出去交给 ``app/main.py`` 的全局处理器：
    全局处理器对 23505 / 23503 / 23502 / 23514 会把 ``diag.message_primary``
    回给客户端，而那句话里仍然有约束名与「Key (subject_id)=(5) is not
    present in table "subject"」这样的库结构信息（审查报告 P1-1 / P1-2）。

    业务错误不走这里：能归类成业务错误的（重复上传、参数非法、批次已确认）
    已经在服务层翻成了应用自己写的中文文案，由下面的 ``except ValueError``
    变成 4xx。
    """
    rid = _request_id(request)
    log.exception("发票接口数据库异常 rid=%s", rid)
    return HTTPException(
        500, f"数据库内部错误（请求号 {rid}），请联系管理员并提供该请求号")


def _batch(book: Book, db: Session, batch_id: int, *, lock: bool = False) -> InvoiceBatch:
    """取一个批次，顺带守住「这批是不是这个账套的」。

    ``lock=True`` 时用 ``SELECT … FOR UPDATE`` **把这一行锁住**，锁一直持有到
    调用方所在事务提交或回滚。

    为什么需要这把锁：同一个批次的两个「确认生成」并发到达时，两个事务都读到
    PENDING，于是各自建了一套凭证 —— 同一张发票的费用/收入被**重复入账**，
    而批次摘要还少报一张、发票侧只追溯得到第 2 张，三处都看不出异常
    （审查报告 P0-1）。锁住批次行之后，第二个请求会**阻塞**到第一个提交完，
    锁释放时重新读到 COMMITTED，于是确定性地失败。

    只有**会改批次状态**的端点才加锁：只读的端点（列表 / 详情 / 下载原件）
    拿一把行锁毫无意义，却会让并发的确认生成在它那儿排队。

    注：``SELECT … FOR UPDATE`` 是 PostgreSQL 的写法，SQLite 会**静默忽略**
    它（SQLAlchemy 的 SQLite 方言不生成该子句）—— 在 SQLite 上这把锁并不
    存在。本项目用 PostgreSQL，测试库也是 PostgreSQL。
    """
    stmt = select(InvoiceBatch).where(
        InvoiceBatch.id == batch_id, InvoiceBatch.book_id == book.id)
    if lock:
        stmt = stmt.with_for_update()
    batch = db.scalar(stmt)
    if batch is None:
        raise HTTPException(404, "发票批次不存在")
    return batch


@router.get("/limits")
def get_invoice_limits(book: Book = Depends(get_my_book)):
    """单次上传的容量上限。

    前端要把这两个数显示出来（超限前就让人知道），所以由后端给 ——
    前端抄一份常量的话，改了配置就会两边不一致，而不一致的那边只会安静地
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
def put_invoice_config(payload: InvoiceConfigIn, request: Request, book: Book = Depends(get_my_book),
                       db: Session = Depends(get_db),
                       _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        result = service.save_config(db, book.id, payload.model_dump())
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc
    return result


@router.get("/rules")
def get_invoice_rules(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    return service.list_rules(db, book.id)


@router.post("/rules", status_code=201)
def post_invoice_rule(payload: InvoiceRuleIn, request: Request, book: Book = Depends(get_my_book),
                      db: Session = Depends(get_db),
                      _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        return service.create_rule(db, book.id, user, payload.model_dump())
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.patch("/rules/{rule_id}")
def patch_invoice_rule(rule_id: int, payload: InvoiceRulePatch, request: Request, book: Book = Depends(get_my_book),
                       db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                       user: SysUser = Depends(get_current_user)):
    try:
        return service.update_rule(db, book.id, rule_id, user, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.delete("/rules/{rule_id}")
def remove_invoice_rule(rule_id: int, request: Request, book: Book = Depends(get_my_book),
                        db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                        user: SysUser = Depends(get_current_user)):
    try:
        service.delete_rule(db, book.id, rule_id, user)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc
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
def upload_invoice_batch(request: Request, files: list[UploadFile] = File(...), book: Book = Depends(get_my_book),
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
    except (ValueError, RuntimeError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc
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
def preview_invoice_batch(batch_id: int, request: Request, payload: InvoicePreviewIn | None = None,
                          book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                          _link: UserBook = Depends(require_book_write)):
    batch = _batch(book, db, batch_id)
    try:
        return service.preview_batch(db, batch, (payload.overrides if payload else {}))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.post("/batches/{batch_id}/commit")
def commit_invoice_batch(batch_id: int, request: Request, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                         _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    # **锁住批次行**：锁一直持有到 service.commit_batch 内部的 db.commit()，
    # 失败路径的 db.rollback() 会随事务回滚把锁释放掉。
    # 锁顺序是「批次锁 → 记录锁」，写死在 service.commit_batch 的 docstring 里。
    batch = _batch(book, db, batch_id, lock=True)
    try:
        return service.commit_batch(db, batch, user)
    except service.BatchConflict as exc:
        # 同一个批次的第二次确认：冲突（409），不是「你的请求不合法」（400）。
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, ArithmeticError) as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.post("/batches/{batch_id}/discard")
def discard_invoice_batch(batch_id: int, request: Request, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                          _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    batch = _batch(book, db, batch_id)
    try:
        service.discard_batch(db, batch, user)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc
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
