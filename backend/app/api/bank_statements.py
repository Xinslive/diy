"""流水对账接口：对账单台账、流水明细、流水科目规则、格式画像。

**这些接口只认「现金科目」**（1001/1002/1012 的末级明细）。理由写在
:mod:`app.bankstatement.service` 里：绑错科目会让余额核对拿存货去跟
银行流水比，而那永远对不上。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.schemas import (BankStatementReparseIn, BankStatementRowPatchIn,
                             BankStatementRowDirectionIn, BankStatementProfileIn,
                             BankStatementRuleIn, BankStatementRulePatch)
from app.core.db import get_db
from app.core.deps import get_current_user, get_my_book, require_book_write
# 共享容量闸：读字节数有上限，超了翻成 413（见 app/core/uploads.py）。
from app.core.uploads import (UploadTooLarge, human_bytes, read_upload_sync as read_upload)
from app.bankstatement import service
from app.models import Book, SysUser, UserBook

router = APIRouter(prefix="/api/books/{book_id}/bank-statements", tags=["流水对账"])

log = logging.getLogger("cwq")


def _db_error(request: Request, exc: Exception) -> HTTPException:
    """数据库异常 → 脱敏 500。理由与 ``app/api/invoices.py`` 同款。"""
    rid = getattr(request.state, "request_id", "-")
    log.exception("流水对账接口数据库异常 rid=%s", rid)
    return HTTPException(
        500, f"数据库内部错误（请求号 {rid}），请联系管理员并提供该请求号")


@router.get("")
def list_statements(request: Request, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db)):
    """对账单台账。

    每份都带上「账面余额 / 差额 / 状态汇总」，这样页面不用再发一轮请求
    去拼这些数字 —— 而差额正是这个页面存在的理由。
    """
    try:
        return service.list_statements(db, book.id)
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.post("", status_code=201)
def upload_statement(
    request: Request,
    file: UploadFile = File(...),
    account_subject_id: int = Form(...),
    book: Book = Depends(get_my_book),
    db: Session = Depends(get_db),
    _link: UserBook = Depends(require_book_write),
    user: SysUser = Depends(get_current_user),
):
    """上传一份对账单。

    ``account_subject_id`` 是这份对账单**属于哪个现金科目** ——
    1002 下面有好几个子科目时，选哪个就是这份流水记到哪个账户上。
    """
    try:
        data = read_upload(file, label="对账单文件")
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    try:
        result = service.upload_statement(
            db, book.id, user, file.filename or "对账单", data, account_subject_id)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.get("/{statement_id}")
def get_statement(statement_id: int, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db)):
    try:
        statement = service._statement(db, book.id, statement_id)
        return service.serialize_statement(db, statement)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{statement_id}/rows")
def get_rows(statement_id: int, book: Book = Depends(get_my_book),
             db: Session = Depends(get_db)):
    try:
        return service.list_rows(db, book.id, statement_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{statement_id}/reparse")
def reparse(statement_id: int, payload: BankStatementReparseIn, request: Request,
            book: Book = Depends(get_my_book), db: Session = Depends(get_db),
            _link: UserBook = Depends(require_book_write)):
    """重新解析。

    带 ``column_mapping`` 就是**按人工指定的列重跑**（自动映射失败时那条路）；
    不带就是重读原件按最新的规则自动重跑。
    """
    try:
        result = service.reparse_statement(
            db, book.id, statement_id,
            column_mapping=payload.column_mapping or None,
            header_row=payload.header_row,
        )
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.delete("/{statement_id}")
def delete_statement(statement_id: int, request: Request,
                     book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                     _link: UserBook = Depends(require_book_write)):
    try:
        result = service.delete_statement(db, book.id, statement_id)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.get("/{statement_id}/file")
def download_original(statement_id: int, book: Book = Depends(get_my_book),
                      db: Session = Depends(get_db)):
    """下载原件。

    **原件必须能拿回来**：解析器会改、别名表会补，用户说「这笔认错了」
    时，我们要能让他看看原件 —— 而不是让他回网银再导一遍。
    """
    from fastapi.responses import FileResponse

    from app.bankstatement import storage

    try:
        statement = service._statement(db, book.id, statement_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not storage.exists(statement.file_path):
        raise HTTPException(404, "原件已不在磁盘上")
    path = storage.path_for(statement.file_path)
    return FileResponse(path, filename=statement.filename or path.name)


@router.patch("/rows/{row_id}")
def patch_row(row_id: int, payload: BankStatementRowPatchIn, request: Request,
              book: Book = Depends(get_my_book), db: Session = Depends(get_db),
              _link: UserBook = Depends(require_book_write)):
    """改一行：指定对方科目，或忽略 / 恢复。"""
    try:
        # **exclude_unset=True**：只提交「这次真的改了」的字段。
        #
        # 少了它，``model_dump()`` 会把没传的字段也带上（值是 None）——
        # 于是「只改状态」那次调用顺手把对方科目也清空了。改一个字段
        # 抹掉另一个，是最难查的那类 bug。
        result = service.update_row(
            db, book.id, row_id, payload.model_dump(exclude_unset=True))
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.post("/rows/{row_id}/direction")
def post_row_direction(row_id: int, payload: BankStatementRowDirectionIn, request: Request,
                       book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                       _link: UserBook = Depends(require_book_write)):
    """人工指定一行的收支方向。"""
    try:
        result = service.set_row_direction(db, book.id, row_id, payload.direction)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@router.post("/{statement_id}/plan-vouchers")
def plan_vouchers(statement_id: int, request: Request, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db),
                  _link: UserBook = Depends(require_book_write)):
    """算可制证清单与去重结论。**不写凭证。**"""
    try:
        return service.plan_vouchers(db, book.id, statement_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{statement_id}/commit-vouchers")
def commit_vouchers(statement_id: int, request: Request, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db),
                    _link: UserBook = Depends(require_book_write),
                    user: SysUser = Depends(get_current_user)):
    """按清单生成凭证。**一张流水一张凭证。**"""
    try:
        result = service.commit_vouchers(db, book.id, statement_id, user)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


# ----------------------------------------------------------------------
# 流水科目规则
# ----------------------------------------------------------------------

rules_router = APIRouter(prefix="/api/books/{book_id}/bank-statement-rules",
                          tags=["流水对账"])


@rules_router.get("")
def list_rules(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    return [service.serialize_rule(db, rule) for rule in service.list_rules(db, book.id)]


@rules_router.post("", status_code=201)
def post_rule(payload: BankStatementRuleIn, request: Request,
               book: Book = Depends(get_my_book), db: Session = Depends(get_db),
               _link: UserBook = Depends(require_book_write),
               user: SysUser = Depends(get_current_user)):
    try:
        result = service.create_rule(db, book.id, user, payload.model_dump())
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@rules_router.patch("/{rule_id}")
def patch_rule(rule_id: int, payload: BankStatementRulePatch, request: Request,
               book: Book = Depends(get_my_book), db: Session = Depends(get_db),
               _link: UserBook = Depends(require_book_write)):
    try:
        result = service.update_rule(db, book.id, rule_id, payload.model_dump(exclude_unset=True))
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@rules_router.delete("/{rule_id}")
def delete_rule(rule_id: int, request: Request, book: Book = Depends(get_my_book),
                db: Session = Depends(get_db),
                _link: UserBook = Depends(require_book_write)):
    try:
        result = service.delete_rule(db, book.id, rule_id)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


# ----------------------------------------------------------------------
# 格式画像
# ----------------------------------------------------------------------

profiles_router = APIRouter(prefix="/api/books/{book_id}/bank-statement-profiles",
                             tags=["流水对账"])


@profiles_router.get("")
def list_profiles(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    return service.list_profiles(db, book.id)


@profiles_router.post("", status_code=201)
def post_profile(payload: BankStatementProfileIn, request: Request,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                 _link: UserBook = Depends(require_book_write)):
    """记住一种格式的列映射。"""
    try:
        result = service.save_profile(db, book.id, payload.model_dump())
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc


@profiles_router.delete("/{profile_id}")
def delete_profile(profile_id: int, request: Request, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db),
                   _link: UserBook = Depends(require_book_write)):
    try:
        result = service.delete_profile(db, book.id, profile_id)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise _db_error(request, exc) from exc
