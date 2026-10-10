"""账套、会计期间、结账/反结账、结转。"""
from __future__ import annotations

import io
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting import periods as svc
from app.api.schemas import BookIn, OpeningIn, UncloseIn, YearEndIn
from app.core.db import get_db
from app.core.deps import get_book_link, get_current_user, get_my_book
from app.models import Book, Period, PeriodOpening, Subject, SysCompany, SysUser, UserBook, Voucher

router = APIRouter(prefix="/api/books", tags=["账套与期间"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_name(name: str) -> str:
    from urllib.parse import quote

    return f"attachment; filename*=UTF-8''{quote(name)}"


@router.get("")
def list_my_books(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    closed_counts = dict(
        db.execute(
            select(Period.book_id, func.count())
            .where(Period.locked.is_(True)).group_by(Period.book_id)
        ).all()
    )
    rows = db.execute(
        select(Book, UserBook.role, SysCompany.name)
        .join(UserBook, UserBook.book_id == Book.id)
        .join(SysCompany, SysCompany.id == Book.company_id)
        .where(UserBook.user_id == user.id)
        .order_by(Book.year, Book.name)
    ).all()
    return [
        {
            "id": b.id, "name": b.name, "year": b.year, "start_month": b.start_month,
            "company": company, "role": role, "status": b.status,
            "closed_periods": db.scalar(
                select(func.count()).select_from(Period).where(
                    Period.book_id == b.id, Period.locked.is_(True))
            ),
        }
        for b, role, company in rows
    ]


@router.post("", status_code=201)
def create_book(payload: BookIn, user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    company = None
    if payload.company_id:
        company = db.get(SysCompany, payload.company_id)
    if company is None:
        company = db.scalar(select(SysCompany).where(SysCompany.name == payload.company_name))
    if company is None:
        company = SysCompany(name=payload.company_name)
        db.add(company)
        db.flush()

    exists = db.scalar(
        select(Book).where(Book.company_id == company.id, Book.name == payload.name, Book.year == payload.year)
    )
    if exists:
        raise HTTPException(400, f"账套「{payload.name} {payload.year}」已存在")

    book = Book(company_id=company.id, name=payload.name, year=payload.year,
                start_month=payload.start_month)
    db.add(book)
    db.flush()

    svc.create_periods(db, book)

    from app.seed.chart_of_accounts import install_chart

    subjects = install_chart(db, book.id)

    db.add(UserBook(user_id=user.id, book_id=book.id, role="OWNER"))
    db.commit()
    db.refresh(book)
    return {"id": book.id, "name": book.name, "year": book.year, "subjects_installed": subjects}


@router.get("/{book_id}")
def get_book(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    company = db.get(SysCompany, book.company_id)
    return {
        "id": book.id, "name": book.name, "year": book.year,
        "start_month": book.start_month, "status": book.status,
        "company": {"id": company.id, "name": company.name},
    }


@router.get("/{book_id}/periods")
def list_periods(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    from sqlalchemy import func
    from app.models import Voucher

    periods = db.scalars(select(Period).where(Period.book_id == book.id).order_by(Period.ord)).all()
    counts = dict(
        db.execute(
            select(Voucher.period_id, func.count()).where(Voucher.book_id == book.id).group_by(Voucher.period_id)
        ).all()
    )
    posted = dict(
        db.execute(
            select(Voucher.period_id, func.count())
            .where(Voucher.book_id == book.id, Voucher.status == "POSTED")
            .group_by(Voucher.period_id)
        ).all()
    )
    return [
        {
            "id": p.id, "code": p.code, "year": p.year, "month": p.month, "ord": p.ord,
            "locked": p.locked, "closed_by": p.closed_by,
            "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            "vouchers": counts.get(p.id, 0),
            "posted": posted.get(p.id, 0),
            "unposted": counts.get(p.id, 0) - posted.get(p.id, 0),
        }
        for p in periods
    ]


@router.get("/{book_id}/openings")
def get_openings(book_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """期初余额录入页用：一次拿全科目树 + 已录入值 + 是否还能改。"""
    first = db.scalar(
        select(Period).where(Period.book_id == book.id).order_by(Period.ord).limit(1)
    )
    saved = {
        o.subject_id: o.amount
        for o in db.scalars(select(PeriodOpening).where(PeriodOpening.period_id == first.id)).all()
    }
    subjects = db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.active.is_(True))
        .order_by(Subject.code)
    ).all()
    has_voucher = db.scalar(select(Voucher.id).where(Voucher.book_id == book.id).limit(1)) is not None
    return {
        "period_id": first.id,
        "period": first.code,
        "year": first.year,
        "month": first.month,
        "editable": not has_voucher,
        "reason": None if not has_voucher else "账套已有业务数据，期初余额不能再改",
        "subjects": [
            {
                "id": s_.id, "code": s_.code, "name": s_.name,
                "category": s_.category, "direction": s_.direction,
                "is_leaf": s_.is_leaf, "level": s_.level, "carry_forward": s_.carry_forward,
                # 内部统一「借正贷负」。side 是这笔期初**实际**所在的方位，
                # 可能与科目自然方向相反（应收账款挂预收、应交税费留抵都属正常），
                # 所以不能按 direction 反推，否则往返一次就把余额方向洗掉了。
                "signed": str(st),
                "amount": str(abs(st)),
                "side": "借" if st > 0 else "贷",
                "natural_side": "借" if s_.direction == "DEBIT" else "贷",
            }
            for s_ in subjects
            # walrus 在值表达式里取值，signed/side/amount 三者始终同源
            for st in [saved.get(s_.id, Decimal("0"))]
        ],
    }


@router.get("/{book_id}/openings.xlsx")
def export_openings(book_id: int, book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """导出成可再导入的 Excel —— 既是备份，也是给下一个人填的模板。"""
    from app.printing import excel as xl

    first = db.scalar(
        select(Period).where(Period.book_id == book.id).order_by(Period.ord).limit(1)
    )
    saved = {
        o.subject_id: o.amount
        for o in db.scalars(select(PeriodOpening).where(PeriodOpening.period_id == first.id)).all()
    }
    subjects = db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.active.is_(True),
                              Subject.is_leaf.is_(True)).order_by(Subject.code)
    ).all()
    company = db.get(SysCompany, book.company_id)
    data = xl.opening_balance_excel(
        # 导出用的是**实际**方位：反方向余额（应收账款贷方等）导出后再导入必须
        # 原样回来，用科目自然方向导出会把方向洗成正的
        [{"code": s_.code, "name": s_.name,
          "direction": ("借" if st > 0 else "贷") if st != 0 else ("借" if s_.direction == "DEBIT" else "贷"),
          "amount": float(abs(st))} for s_ in subjects for st in [saved.get(s_.id, Decimal("0"))]],
        company=company.name, book_name=book.name, year=first.year, month=first.month,
    )
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _xlsx_name(f"期初余额_{book.name}.xlsx")})


@router.post("/{book_id}/openings/parse")
async def parse_openings(book_id: int, file: UploadFile = File(...),
                         book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """解析期初余额 Excel，**只解析不落库** —— 前端先给用户预览，确认后才写。

    为什么不按「第 1 行是表头」写死：导出的模板自己就带两行大标题，而且用户
    从别处抄来的表几乎不可能是干净的四列。这里改成**认表头** —— 扫前若干行找到
    那一行，再按表头文字定位各列。这样既能吃回自己导出的文件，也能吃用户自己
    整理过的表；认不出表头时才退回按位置取 A/B/C/D。

    容错：科目可按编码或名称匹配；方向列可省（按科目属性推）。借贷不平在这里
    就报出来，不等到入库才发现。
    """
    from openpyxl import load_workbook

    raw = await file.read()
    try:
        wb = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"无法读取 Excel 文件：{exc}") from exc
    ws = wb.active
    if ws is None:
        raise HTTPException(400, "Excel 里没有工作表")

    def _find_header(grid):
        """返回 (表头行号, {列序号: 角色})；认不出就返回 (None, {})。"""
        roles = {"code": None, "name": None, "direction": None, "amount": None}
        for idx, row in enumerate(grid[:15], start=1):
            texts = [str(c).strip() for c in row if c not in (None, "")]
            if not any("科目" in t for t in texts):
                continue
            if not any(("方向" in t) or ("余额" in t) or ("金额" in t) for t in texts):
                continue
            for col, cell in enumerate(row):
                t = str(cell).strip() if cell not in (None, "") else ""
                if not t:
                    continue
                if ("编码" in t) or ("代码" in t):
                    roles["code"] = col
                elif "名称" in t:
                    roles["name"] = col
                elif ("方向" in t) or ("借贷" in t):
                    roles["direction"] = col
                elif ("余额" in t) or ("金额" in t) or ("数" in t):
                    roles["amount"] = col
            if roles["amount"] is not None:
                return idx, roles
        return None, roles

    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    head_idx, cols = _find_header(grid)
    if head_idx is None:
        head_idx, cols = 0, {"code": 0, "name": 1, "direction": 2, "amount": 3}

    by_code, by_name = {}, {}
    for s_ in db.scalars(select(Subject).where(
            Subject.book_id == book.id, Subject.active.is_(True))).all():
        by_code[s_.code] = s_
        by_name[s_.name] = s_

    rows, errors = [], []
    dr = cr = Decimal("0")
    # head_idx 是表头的 Excel 行号（1 基），数据从它的下一行开始；
    # 起始行号同样 1 基，所以是 head_idx + 1 —— 报给用户的行号要能在 Excel 里对上
    # head_idx 是表头的 Excel 行号（1 基）。grid 是 0 基列表，表头占了下标
    # head_idx - 1，所以第一条数据的下标正好是 head_idx —— 切片和行号都用它，
    # 差一个格就会静默吞掉第一行数据。
    for i, raw_row in enumerate(grid[head_idx:], start=head_idx + 1):
        if raw_row is None or all(c in (None, "") for c in raw_row):
            continue

        def cell(role):
            c = cols.get(role)
            return raw_row[c] if c is not None and c < len(raw_row) else None

        code, name, direction, amount = cell("code"), cell("name"), cell("direction"), cell("amount")
        if code in (None, "") and name in (None, ""):
            continue
        subject = by_code.get(str(code).strip() or "") or by_name.get(str(name).strip() or "")
        entry = {
            "line": i,
            "code": str(code or "").strip(),
            "name": str(name or (subject.name if subject else "")).strip(),
            "amount": str(amount if amount not in (None, "") else ""),
            "subject_id": subject.id if subject else None,
            "direction": subject.direction if subject else None,
            "natural": str(direction or "").strip(),
            "error": "",
        }
        if subject is None:
            entry["error"] = "科目在本账套不存在"
        elif not subject.is_leaf:
            entry["error"] = "不是明细科目，期初应录在明细级"
        else:
            try:
                amt = Decimal(str(amount or 0))
            except Exception:  # noqa: BLE001
                entry["error"] = f"金额无法识别：{amount!r}"
                amt = None
            if amt is not None:
                if amt == 0:
                    entry["error"] = ""
                    entry["amount"] = "0"
                else:
                    # 方向以「表里写的」为准，缺省才按科目属性推：
                    # 同一科目做反向调整是常事，按属性强推会把红字余额记反。
                    nat = entry["natural"]
                    if nat in ("借", "DEBIT", "D"):
                        signed = abs(amt)
                    elif nat in ("贷", "CREDIT", "C"):
                        signed = -abs(amt)
                    else:
                        signed = abs(amt) if subject.direction == "DEBIT" else -abs(amt)
                    entry["signed"] = str(signed)
                    entry["display_side"] = "借" if signed > 0 else "贷"
                    if signed > 0:
                        dr += abs(signed)
                    else:
                        cr += abs(signed)
        rows.append(entry)
        if entry["error"]:
            errors.append(f"第{i}行 {entry['code'] or entry['name']}：{entry['error']}")

    if not rows:
        raise HTTPException(400, "没有解析出任何数据行")

    return {
        "rows": rows,
        "errors": errors,
        "total_debit": str(dr),
        "total_credit": str(cr),
        "balanced": dr == cr,
    }


@router.post("/{book_id}/periods")
def set_openings(book_id: int, payload: list[OpeningIn], book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """录入账套期初余额（只在启用月份之前允许一次）。"""
    first = db.scalar(
        select(Period).where(Period.book_id == book.id).order_by(Period.ord).limit(1)
    )
    if db.scalar(select(Voucher).where(Voucher.book_id == book.id).limit(1)):
        raise HTTPException(400, "账套已有业务数据，不能再修改期初余额")

    # 期初快照有数据库触发器保护（禁止 UPDATE），这里整体重建
    for row in db.scalars(select(PeriodOpening).where(PeriodOpening.period_id == first.id)).all():
        db.delete(row)
    db.flush()

    # 先在内存里过一遍校验并试算平衡，确认没问题再落库：
    # 期初一旦存歪，后面每个月的账都跟着歪，而且很难看出是哪一步错的。
    planned, dr, cr = [], Decimal("0"), Decimal("0")
    for item in payload:
        subject = db.get(Subject, item.subject_id)
        if subject is None or subject.book_id != book.id:
            raise HTTPException(400, f"科目不存在: {item.subject_id}")
        # 「借正贷负」是全系统内部口径，期初也照此存。
        # 刻意**不**按 subject.direction 纠正符号：科目属性是「通常方向」，
        # 不是强约束——应收账款出现贷方余额、应交税费出现负数都是真实业务。
        amount = Decimal(str(item.amount))
        if not amount.is_finite():
            raise HTTPException(400, f"科目 {subject.code} 金额非法")
        if amount != 0 and not subject.is_leaf:
            raise HTTPException(400, f"{subject.code} {subject.name} 不是明细科目，期初应录在明细级")

        planned.append((subject.id, amount))
        if amount > 0:
            dr += amount
        elif amount < 0:
            cr += -amount

    if dr != cr:
        raise HTTPException(
            400, f"期初试算不平：借方合计 {dr}，贷方合计 {cr}，差额 {dr - cr}")

    for subject_id, amount in planned:
        db.add(PeriodOpening(book_id=book.id, period_id=first.id,
                             subject_id=subject_id, amount=amount))
    db.commit()
    return {"ok": True, "count": len(payload)}


@router.post("/{book_id}/periods/{period_id}/carry-forward")
def carry_forward(book_id: int, period_id: int, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    period = svc.get_period(db, book.id, period_id)
    try:
        return svc.carry_forward_profit(db, book.id, period, user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{book_id}/periods/{period_id}/close")
def close_period(book_id: int, period_id: int, book: Book = Depends(get_my_book),
                 db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    period = svc.get_period(db, book.id, period_id)
    try:
        return svc.close_period(db, period, user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{book_id}/periods/{period_id}/unclose-preview")
def unclose_preview(book_id: int, period_id: int, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db)):
    period = svc.get_period(db, book.id, period_id)
    return svc.preview_unclose(db, book.id, period)


@router.post("/{book_id}/periods/{period_id}/unclose")
def unclose_period(book_id: int, period_id: int, payload: UncloseIn, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    period = svc.get_period(db, book.id, period_id)
    try:
        return svc.batch_unclose(db, book.id, period, user, payload.revert_posting)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{book_id}/year-end")
def year_end(book_id: int, payload: YearEndIn, book: Book = Depends(get_my_book),
             db: Session = Depends(get_db), user: SysUser = Depends(get_current_user)):
    try:
        return svc.year_end_carry(db, book, user, payload.new_book_name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{book_id}/audit-logs")
def audit_logs(book_id: int, limit: int = Query(100, le=1000), book: Book = Depends(get_my_book),
               db: Session = Depends(get_db)):
    from app.models import AuditLog

    rows = db.scalars(
        select(AuditLog).where(AuditLog.book_id == book.id)
        .order_by(AuditLog.id.desc()).limit(limit)
    ).all()
    return [
        {
            "id": r.id, "action": r.action, "entity": r.entity, "entity_id": r.entity_id,
            "username": r.username, "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "before": r.before, "after": r.after,
        }
        for r in rows
    ]
