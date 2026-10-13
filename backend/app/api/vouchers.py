"""凭证：增删改查、批量、记账/反记账。"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.accounting import importer as svc_import
from app.accounting import organize as svc_organize
from app.accounting import vouchers as svc
from app.accounting.util import find_period
from app.api.schemas import BatchIds, VoucherIn
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, get_my_book, require_book_write
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
    # 合计走 side_totals：红字（负数）算到对面去，两栏都只摆正数
    dr_total, cr_total = svc.side_totals(
        [{"direction": e.direction, "amount": e.amount} for e in entries])
    return {
        "id": v.id, "voucher_no": v.voucher_no, "voucher_date": v.voucher_date.isoformat(),
        "status": v.status, "source": v.source, "remark": v.remark,
        "attachments": v.attachments, "created_by": v.created_by,
        "posted_by": v.posted_by,
        "posted_at": v.posted_at.isoformat() if v.posted_at else None,
        "period_id": v.period_id,
        "total_debit": str(dr_total),
        "total_credit": str(cr_total),
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
           _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        v = svc.create_voucher(
            db, book_id=book.id, user=user, voucher_date=payload.voucher_date,
            entries=[e.model_dump() for e in payload.entries], remark=payload.remark,
            attachments=payload.attachments, source=payload.source,
            voucher_no=payload.voucher_no, insert_after_id=payload.insert_after,
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return _serialize(db, v)


@router.put("/{voucher_id}")
def update(voucher_id: int, payload: VoucherIn, book: Book = Depends(get_my_book),
           db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
           user: SysUser = Depends(get_current_user)):
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
           _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
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
              _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        n = svc.post_vouchers(db, book.id, payload.ids, user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"posted": n}


@router.post("/unpost")
def unpost_many(payload: BatchIds, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        n = svc.unpost_vouchers(db, book.id, payload.ids, user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"unposted": n}


@router.post("/batch-delete")
def batch_delete(payload: BatchIds, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                 _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    rows = list(db.scalars(select(Voucher).where(
        Voucher.id.in_(payload.ids), Voucher.book_id == book.id)).all())
    # ID 集合必须解析完整。少了哪张就整批拒绝，不做「删掉找得到的那些」。
    # 原来这里直接把查询结果拿去删：传 3 个 id、其中 1 个不存在（或属于别的
    # 账套），返回 {"deleted": 2} 且不置一词 —— 用户以为自己删了 3 张，
    # 实际 2 张，那 1 张还好好地躺在列表里。
    # 列表页状态过期、并发删除、跨账套误选都会走到这条路。
    wanted = sorted(set(payload.ids))
    found = {v.id for v in rows}
    missing = [i for i in wanted if i not in found]
    if missing:
        raise HTTPException(
            400, f"凭证不存在或不属于当前账套，已整批拒绝："
                 f"{', '.join(str(i) for i in missing[:10])}"
                 + ("…" if len(missing) > 10 else ""))
    # 先整批体检再删：批量选中 20 张、其中 1 张已记账时，
    # 逐张删到那张才报错的话，用户只知道「删失败」却不知道是哪一张。
    posted = [v.voucher_no for v in rows if v.status == "POSTED"]
    if posted:
        raise HTTPException(
            400, f"已记账的凭证不能删除，请先反记账：{'、'.join(posted[:10])}"
                 + ("…" if len(posted) > 10 else ""))
    try:
        # 一批要么全删要么一张不删。逐张各自 commit 的话，删到第 5 张失败时
        # 前 4 张已经落在库里，回滚也回滚不回来 —— 用户收到 400，
        # 账上却少了 4 张，且没有「哪几张成功了」的信息。
        for v in rows:
            svc.delete_voucher(db, v, user, commit=False)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"deleted": len(rows)}


# ===================================================================== 整理凭证


class OrganizeIn(BaseModel):
    period_id: Optional[int] = None      # 为空表示整个账套逐期整理
    strategy: Literal["RENUM", "SORT_DATE"] = "SORT_DATE"


@router.post("/organize/preview")
def organize_preview(payload: OrganizeIn, book: Book = Depends(get_my_book),
                     db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write)):
    """先看差异再确认。计划阶段只算，不碰数据。"""
    try:
        plans = svc_organize.plan_organize(
            db, book_id=book.id, period_id=payload.period_id,
            strategy=payload.strategy)
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
             _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    try:
        result = svc_organize.apply_organize(
            db, book_id=book.id, user=user, period_id=payload.period_id,
            strategy=payload.strategy)
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
    ws.append(["2026-01-15", "记001", "收到租金", "1001", "库存现金", 5000, "", 1, "示例行，导入前请删除"])
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
                  db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                  user: SysUser = Depends(get_current_user)):
    """上传并解析，暂存待确认。不写任何凭证。"""
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "只支持 .xlsx 格式（另存为 xlsx 后重试）")
    # 读取长度必须带上限（审计 P2-02）。原来是 `file.file.read()` —— 无参数
    # 意味着「有多少读多少」，Nginx 那条 50m 只是最后一道闸，而它是**请求体**
    # 限制：压缩过的上传、拆成多次的请求都能绕过，而且解析器拿到之后
    # 会把每一行都累积进内存。
    #
    # 两道检查：file.size 是 Starlette 在收到请求时记下的字节数，
    # 先用它拒掉明显超限的（连读都不读）；再按上限 +1 读一次，
    # 处理 size 不可信的情况。
    limit = settings.import_max_bytes
    size = getattr(file, "size", None)
    if size is not None and size > limit:
        raise HTTPException(
            413, f"文件 {size // 1024} KB 超过导入上限 {limit // 1024} KB，请拆成多份导入")
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            413, f"文件超过导入上限 {limit // 1024} KB，请拆成多份导入")
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
    # P2-02：进列表页顺带清掉「传完没确认」的过期批次。
    # 挂在读接口上是因为这是唯一保证它**一定会被触发**的地方 ——
    # 没人会为了清理去跑一个定时任务，而导入历史是每次进来都要看的页面。
    svc_import.purge_stale_batches(db, book.id)
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
                  _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
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
                   _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.book_id != book.id:
        raise HTTPException(404, "导入批次不存在")
    try:
        svc_import.discard(db, batch)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ===================================================================== 重排分录顺序




@router.get("/next")
def next_voucher(period_id: Optional[int] = None, book: Book = Depends(get_my_book),
                 db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                 user: SysUser = Depends(get_current_user)):
    """新开一张时页面上要预填的两个值：下一个凭证号、这个期间最后一张的日期。

    号码直接调 `svc.next_voucher_no` —— **跟落库时是同一个函数**。
    预览号和实际发号必须是同一套口径，所以宁可去后端问，也不在这儿照着列表
    猜一个「最大号 +1」（期间里号是可以回收复用的，猜错就是白填一次）。
    """
    period = db.get(Period, period_id) if period_id else find_period(db, book.id, date.today())
    if period is None or period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    last = db.scalar(
        select(func.max(Voucher.voucher_date))
        .where(Voucher.book_id == book.id, Voucher.period_id == period.id))
    return {
        "voucher_no": svc.next_voucher_no(db, period),
        "voucher_date": last.isoformat() if last else None,
        "period_id": period.id,
        "locked": period.locked,
    }


@router.get("/insert-preview")
def insert_preview(after_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                   _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user)):
    """「插入凭证」页面要预填的东西：插在谁后面、这张拿哪个号、谁要顺位。

    跟 `/vouchers/next` 一个道理 —— 号码口径只有后端一份，页面自己猜就一定会猜错。
    这里只算不改：顺延跟建凭证在同一个事务里，空着退出不会顶走谁的号。
    """
    v = db.get(Voucher, after_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "参照凭证不存在")
    period = db.get(Period, v.period_id)
    try:
        slot = svc.insert_slot(db, v)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "anchor_id": v.id,
        "anchor_no": v.voucher_no,
        "voucher_no": slot["no"],
        "voucher_date": v.voucher_date.isoformat(),
        "period_id": v.period_id,
        "locked": bool(period and period.locked),
        "anchor_posted": v.status == "POSTED",
        "moved": slot["moved"],
    }


@router.get("/period-check")
def period_check(period_id: Optional[int] = None, book: Book = Depends(get_my_book),
                 db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write)):
    """某个期间的「整理就绪」体检：断号 + 时序。全程只读。

    「断号检查」不再是整理凭证里的一个选项了 —— 用户不该为了看一眼有没有断号
    先想明白该选哪个「方式」。这里把体检单独开一个口：凭证列表页顶上说一声，
    整理凭证页拿它决定「按当前顺序重新编号」能不能用。
    """
    period = db.get(Period, period_id) if period_id else find_period(db, book.id, date.today())
    if period is None or period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    return svc_organize.check_period(db, period)


@router.get("/{voucher_id}/neighbour")
def neighbour(voucher_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db),
              _link: UserBook = Depends(require_book_write), user: SysUser = Depends(get_current_user),
              keyword: Optional[str] = None, min_amount: Optional[Decimal] = None,
              max_amount: Optional[Decimal] = None):
    """前一张 / 后一张 —— 翻的是**列表里那个结果集**，不是同期间的全部凭证。

    带着筛选条件来（凭证号/摘要、分录金额区间）时，就在筛选结果里前后挪，并允许跨月跨年：
    筛出「每月一张凭证号含 16 的」，进 8 月那张，前一张就是 7 月那张。

    两种情况下顺序都是**时间正序**，不是列表那个倒序：前一张是更早的，后一张是更晚的。
    列表倒着排是为了让最新的一眼在顶上，而「前一张 / 后一张」是翻时间的动作 ——
    混成同一个顺序，在 8 月那张点前一张会跳去 9 月，怎么点都回不来。
    不带条件时同样按 (日期, id) 升序，只把范围收在同期间内，
    连续录一叠凭证时「后一张」该是接下来要填的那张。

    结果集可能跨很多页，`page_size` 上限 200，所以要翻页收齐再定位。
    """
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")

    if keyword or min_amount is not None or max_amount is not None:
        ids: list[int] = []
        total = 0
        for page in range(1, 101):           # 封顶 20000 张，够用了，也防死循环
            rows, total = svc.query_vouchers(
                db, book.id, keyword=keyword, min_amount=min_amount, max_amount=max_amount,
                page=page, page_size=200)
            ids.extend(r.id for r in rows)
            if len(ids) >= total or not rows:
                break
        # query_vouchers 是倒序（新的在前），这里翻过来成正序。
        # 顺带把日期相同那批的先后也一起翻了：倒序是 id 降，翻过来正好是 id 升，
        # 跟不带条件时 `tuple_(date, id)` 升序的那条路对得上。
        ids.reverse()
        try:
            i = ids.index(voucher_id)
        except ValueError:                   # 这张不在结果集里（比如筛选条件刚改过）
            return {"prev_id": None, "next_id": None, "scope": "filtered", "index": -1, "total": len(ids)}
        return {
            "prev_id": ids[i - 1] if i > 0 else None,
            "next_id": ids[i + 1] if i < len(ids) - 1 else None,
            "scope": "filtered", "index": i, "total": len(ids),
        }

    scope = (Voucher.book_id == book.id, Voucher.period_id == v.period_id)
    prev_id = db.scalar(
        select(Voucher.id).where(*scope, tuple_(Voucher.voucher_date, Voucher.id) < (v.voucher_date, v.id))
        .order_by(Voucher.voucher_date.desc(), Voucher.id.desc()).limit(1)
    )
    next_id = db.scalar(
        select(Voucher.id).where(*scope, tuple_(Voucher.voucher_date, Voucher.id) > (v.voucher_date, v.id))
        .order_by(Voucher.voucher_date, Voucher.id).limit(1)
    )
    return {"prev_id": prev_id, "next_id": next_id, "scope": "period", "index": None, "total": None}


@router.get("/{voucher_id}")
def detail(voucher_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    v = db.get(Voucher, voucher_id)
    if v is None or v.book_id != book.id:
        raise HTTPException(404, "凭证不存在")
    return _serialize(db, v)
