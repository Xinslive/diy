"""账套、会计期间、结账/反结账、结转。"""
from __future__ import annotations

import io
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting import periods as svc
from app.accounting.util import money
from app.api.schemas import (BookEditIn, BookIn, CarryForwardIn, OpeningIn,
                           UncloseIn, YearEndIn)
from app.core.db import get_db
from app.core.deps import (get_book_link, get_current_user, get_my_book, require_admin,
                        require_book_write)
from app.ledger.engine import LedgerEngine
from app.models import Book, Period, PeriodOpening, Subject, SysUser, UserBook, Voucher

router = APIRouter(prefix="/api/books", tags=["账套与期间"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_name(name: str) -> str:
    from urllib.parse import quote

    return f"attachment; filename*=UTF-8''{quote(name)}"


def _years_of(db: Session, book_id: int) -> list[dict]:
    """账套下每个会计年度的结账进度。

    账套列表要显示「已结账 n/12 期」「使用中/已结转」，而这两个数都是
    **按年度**算的 —— 2026 年结完了不代表 2027 年也结完了。
    """
    rows = db.execute(
        select(
            Period.year,
            func.count(Period.id),
            func.count(Period.id).filter(Period.locked.is_(True)),
        )
        .where(Period.book_id == book_id)
        .group_by(Period.year)
        .order_by(Period.year)
    ).all()
    vouchers = dict(
        db.execute(
            select(Period.year, func.count(Voucher.id))
            .join(Voucher, Voucher.period_id == Period.id)
            .where(Period.book_id == book_id)
            .group_by(Period.year)
        ).all()
    )
    return [
        {
            "year": year,
            "total": total,
            "closed": closed,
            "vouchers": vouchers.get(year, 0),
            "carried": closed >= total,
        }
        for year, total, closed in rows
    ]


@router.get("")
def list_my_books(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Book, UserBook.role)
        .join(UserBook, UserBook.book_id == Book.id)
        .where(UserBook.user_id == user.id)
        .order_by(Book.company_name, Book.name)
    ).all()
    out = []
    for b, role in rows:
        years = _years_of(db, b.id)
        out.append({
            "id": b.id, "name": b.name, "company_name": b.company_name,
            "start_year": b.start_year, "start_month": b.start_month,
            "subject_code_rule": b.subject_code_rule,
            "role": role, "status": b.status,
            # 年度是账套的属性，列表一行一个账套，年度由前端下拉选
            "years": years,
            "latest_year": years[-1]["year"] if years else b.start_year,
        })
    return out


@router.post("", status_code=201)
def create_book(payload: BookIn, user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """任何登录用户都能建账套，建完自己就是 OWNER。

    这里不设管理员门槛：**自助注册已经开着**，再卡「必须管理员开账」的话，
    新同事得先找管理员开户才能开始干活。建账的人自动拿到 OWNER，
    所以「谁建的」这件事始终清晰 —— 管理员授权那层管的是**别人的**账套，
    不影响这道边界。
    """
    exists = db.scalar(
        select(Book).where(
            Book.company_name == payload.company_name,
            Book.name == payload.name,
        )
    )
    if exists:
        raise HTTPException(400, f"账套「{payload.company_name} · {payload.name}」已存在")

    book = Book(company_name=payload.company_name, name=payload.name,
                start_year=payload.start_year, start_month=payload.start_month,
                subject_code_rule=payload.subject_code_rule)
    db.add(book)
    db.flush()

    svc.create_periods(db, book)

    from app.seed.chart_of_accounts import install_chart

    subjects = install_chart(db, book.id)

    db.add(UserBook(user_id=user.id, book_id=book.id, role="OWNER"))
    db.flush()

    # 建账口子放开后，「这个账套是谁开的」必须查得到 —— 走审计日志留痕
    from app.accounting.audit_helper import log

    log(db, action="CREATE", entity="book", entity_id=book.id, user=user, book_id=book.id,
        detail=f"新建账套 {book.company_name} · {book.name}",
        after={"company_name": book.company_name, "name": book.name,
               "start_year": book.start_year, "start_month": book.start_month})
    db.commit()
    db.refresh(book)
    return {"id": book.id, "company_name": book.company_name, "name": book.name,
            "start_year": book.start_year, "subjects_installed": subjects}


def _max_subject_level(db: Session, book_id: int) -> int:
    """这个账套已经用到的最深科目级次。"""
    return db.scalar(
        select(func.max(Subject.level)).where(Subject.book_id == book_id)
    ) or 1


def _check_code_rule(db: Session, book: Book, rule: str) -> str:
    """挡掉「级次调到比已有科目还浅」的规则。

    规则只是**今后**新增科目怎么接号的模板，但级次是有下限的：
    账上已经有三级科目，规则却改成两级，新增时 `segmentOf` 会按最后一段
    给它接号，接出来的编码和现有科目的编码对不齐，科目表当场乱掉。
    """
    levels = len(rule.split("-"))
    used = _max_subject_level(db, book.id)
    if levels < used:
        raise HTTPException(
            400, f"科目级次不能调到 {levels} 级：这个账套已经有 {used} 级的科目"
        )
    return rule


@router.get("/{book_id}")
def get_book(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    return {
        "id": book.id, "company_name": book.company_name, "name": book.name,
        "subject_code_rule": book.subject_code_rule,
        # 前端据此把不该选的级次直接置灰 —— 只在编辑弹窗里要，别塞进账套列表
        "subject_max_level": _max_subject_level(db, book.id),
        "start_year": book.start_year, "start_month": book.start_month,
        "status": book.status, "years": _years_of(db, book.id),
    }


@router.put("/{book_id}")
def update_book(book_id: int, payload: BookEditIn, book: Book = Depends(get_my_book),
                db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                user: SysUser = Depends(get_current_user),
                _w: UserBook = Depends(require_book_write)):
    """改账套名 / 单位名。

    只允许 OWNER 改 —— 账套名会出现在每一页的页眉、每一张凭证的打印抬头、
    每一份导出的 Excel 上，名字错了整批单据都得重打。

    额外挂 ``require_book_write`` 是为了让「每个写接口都挂写依赖」成为**机械可查**
    的不变量（tests/test_viewer_matrix.py 遍历路由表检查），而不是靠人记得。
    它比下面的 OWNER 判断更宽松，叠加不会改变实际口径，只是把基础那道门先关上。
    """
    if link.role != "OWNER":
        raise HTTPException(403, "只有账套所有者可以修改账套名和单位名")
    changes = payload.changes()
    if not changes:
        return {"id": book.id, "company_name": book.company_name, "name": book.name}
    dup = db.scalar(
        select(Book).where(
            Book.id != book.id,
            Book.company_name == changes.get("company_name", book.company_name),
            Book.name == changes.get("name", book.name),
        )
    )
    if dup:
        raise HTTPException(400, "已存在同名的账套，请换一个名字")
    if "subject_code_rule" in changes:
        _check_code_rule(db, book, changes["subject_code_rule"])
    before = {"company_name": book.company_name, "name": book.name}
    for k, v in changes.items():
        setattr(book, k, v)
    db.flush()
    from app.accounting.audit_helper import log

    log(db, action="UPDATE", entity="book", entity_id=book.id, user=user, book_id=book.id,
        detail="修改账套属性：" + "、".join(f"{k}={v}" for k, v in changes.items()),
        before=before, after=changes)
    db.commit()
    return {"id": book.id, "company_name": book.company_name, "name": book.name}


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


def _openings_period(db: Session, book: Book, period_id: int | None) -> Period:
    """期初页面对齐顶栏：要看哪一期，就返回哪一期。

    以前这里永远返回「第一个期间」，页面也不带参数 —— 于是顶栏切到 2027 年，
    屏幕上摆的还是 2026-01 那份数，还配一句「账套已有业务数据」。
    用户看到的和顶栏选的对不上，怎么改都像是在跟一个不认识的页面较劲。
    """
    stmt = select(Period).where(Period.book_id == book.id)
    if period_id is not None:
        period = db.scalar(stmt.where(Period.id == period_id))
        if period is None:
            raise HTTPException(404, "会计期间不存在或不属于该账套")
        return period
    first = db.scalar(stmt.order_by(Period.ord).limit(1))
    if first is None:
        raise HTTPException(400, "账套还没有会计期间")
    return first


def _openings_editable(db: Session, period: Period) -> tuple[bool, str | None]:
    """这一期的期初还能不能改，以及为什么不能。

    判据是「**从这一期往后**有没有落到业务数据」，不是「整个账套有没有凭证」。
    改的是这一期的期初，前面已经封账的期间动不了；而只要这一期及其之后
    一个凭证都没有，这笔期初就还没被任何账簿、报表引用过 —— 换句话说，
    用户切到一个空年份时看到的那份期初，本来就是可以改的。
    """
    n = db.scalar(
        select(func.count()).select_from(Voucher).where(
            Voucher.period_id.in_(
                select(Period.id).where(
                    Period.book_id == period.book_id, Period.ord >= period.ord)
            )
        )
    ) or 0
    if n:
        return False, f"{period.code} 及其之后的期间已有业务数据（{n} 张凭证），期初余额不能再改"
    # 兜底：结账必然已经把凭证记账了，走不到这里；留着是为了万一将来出现
    # 「无凭证结账」的路径，别让它悄悄变成可写。
    if period.locked:
        return False, f"{period.code} 已结账，期初余额不能再改"
    return True, None


def _openings_amounts(db: Session, book: Book, period: Period,
                      subjects: list[Subject]) -> dict[int, Decimal]:
    """这一期的期初：优先取快照，没有快照就按账簿实际在用的那个数算。

    快照只在两个地方产生 —— 账套启用、年度结转。所以 2027 年 2 月往后的
    期间本来就没有快照，账簿里的期初是由上一期期末实时推导的。
    页面如果一律显示 0，用户对不上总账，还以为账空了。
    """
    snap = {
        o.subject_id: o.amount
        for o in db.scalars(
            select(PeriodOpening).where(PeriodOpening.period_id == period.id)).all()
    }
    if snap:
        return snap
    leaves = [s_.id for s_ in subjects if s_.is_leaf]
    if not leaves:
        return {}
    figures = LedgerEngine(db, book.id).compute(leaves, up_to_ord=period.ord)
    return {sid: f.opening for sid, f in figures.items()}


@router.get("/{book_id}/openings")
def get_openings(book_id: int, period_id: int | None = None,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """期初余额录入页用：一次拿全科目树 + 这一期的期初 + 是否还能改。"""
    period = _openings_period(db, book, period_id)
    editable, reason = _openings_editable(db, period)
    # 不过滤 active。年度结转会把**全部末级科目**的期末余额刷成次年期初，
    # 页面却把停用科目藏起来的话，那些期初在界面上是隐形的 ——
    # 用户看到「这个科目没有期初」，实际库里有。口径必须两边一致。
    subjects = db.scalars(
        select(Subject).where(Subject.book_id == book.id)
        .order_by(Subject.code)
    ).all()
    saved = _openings_amounts(db, book, period, subjects)
    return {
        "period_id": period.id,
        "period": period.code,
        "year": period.year,
        "month": period.month,
        "editable": editable,
        "reason": reason,
        "subjects": [
            {
                "id": s_.id, "code": s_.code, "name": s_.name,
                "category": s_.category, "direction": s_.direction,
                "is_leaf": s_.is_leaf, "level": s_.level, "carry_forward": s_.carry_forward,
                # 页面要把上级科目做成小计行（显示它这一支的合计），得知道谁是谁的爹
                "parent_id": s_.parent_id,
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
def export_openings(book_id: int, period_id: int | None = None,
                    book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """导出成可再导入的 Excel —— 既是备份，也是给下一个人填的模板。"""
    from app.printing import excel as xl

    # 导的必须是屏幕上那一期：页面跟着顶栏走，导出还停在第一期的话，
    # 备份下来的数字和眼前看到的对不上，出了错根本查不出是哪一份。
    first = _openings_period(db, book, period_id)
    # 与录入页同一口径（含停用）。导少了行，用户核对时会以为账丢了；
    # 更糟的是导出的文件再导入回来会平白少一笔。
    subjects = db.scalars(
        select(Subject).where(Subject.book_id == book.id,
                              Subject.is_leaf.is_(True)).order_by(Subject.code)
    ).all()
    saved = _openings_amounts(db, book, first, subjects)
    data = xl.opening_balance_excel(
        # 导出用的是**实际**方位：反方向余额（应收账款贷方等）导出后再导入必须
        # 原样回来，用科目自然方向导出会把方向洗成正的
        [{"code": s_.code, "name": s_.name,
          "direction": ("借" if st > 0 else "贷") if st != 0 else ("借" if s_.direction == "DEBIT" else "贷"),
          "amount": float(abs(st))} for s_ in subjects for st in [saved.get(s_.id, Decimal("0"))]],
        company=book.company_name, book_name=book.name, year=first.year, month=first.month,
    )
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _xlsx_name(f"期初余额_{book.name}.xlsx")})


@router.post("/{book_id}/openings/parse")
async def parse_openings(book_id: int, file: UploadFile = File(...),
                         book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """解析期初余额 Excel，**只解析不落库** —— 前端先给用户预览，确认后才写。

    虽然是 POST，但它**不改任何数据**：只解析出一个 preview 返回给前端。
    真正落库的是 ``set_openings``（那里挂了 require_book_write）。
    所以这里不挂写依赖 —— 只读账号也该能预览自己账上的期初表。
    

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

    # 解析不过滤 active：导出那一侧已经含停用科目了，这侧再过滤就
    # 造出一个「导得出来、导不回去」的坑 —— 用户只能手工改表。
    by_code, by_name = {}, {}
    for s_ in db.scalars(select(Subject).where(
            Subject.book_id == book.id)).all():
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
def set_openings(book_id: int, payload: list[OpeningIn], period_id: int | None = None,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                 user: SysUser = Depends(get_current_user),
                 _link: UserBook = Depends(require_book_write)):
    """录入**某一期**的期初余额。

    不传 period_id 就是启用期间（老行为）。传了就写那一期：跨年记账时，
    上一年结转过来的数要是对不上，改这一期比去翻上一年的凭证省事得多。
    """
    from app.accounting.audit_helper import log

    period = _openings_period(db, book, period_id)
    editable, reason = _openings_editable(db, period)
    if not editable:
        raise HTTPException(400, reason)

    # 期初快照有数据库触发器保护（禁止 UPDATE），这里整体重建。
    # 旧值必须**先取出来**：整体重建之后原值就没了，而审计恰恰要靠它。
    before_rows = db.scalars(select(PeriodOpening).where(
        PeriodOpening.period_id == period.id)).all()
    before = {str(o.subject_id): str(o.amount) for o in before_rows}

    for row in before_rows:
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
        # **先按数据库精度量化，再校验、再落库。**
        #
        # 原来试算平衡用的是全精度 Decimal，落库时列是 Numeric(20,2) 才被截断 ——
        # 于是「校验的数」和「存储的数」不是同一个数。审计实测：
        # 提交 0.005 + 0.005 - 0.010，全精度和是 0.000（试算通过），
        # 接口返回 200，而库里实际存下来的和是 0.01。
        #
        # 后果比���错更麻烦：期初差 0.01 会顺着 LedgerEngine 传导到之后
        # 每一个月、每一张账簿、每一份报表，而且极难回溯到是「期初录入时」
        # 就已经差了。
        try:
            amount = money(item.amount)
        except (InvalidOperation, TypeError, ValueError):
            raise HTTPException(400, f"科目 {subject.code} 金额非法：{item.amount!r}")
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
        db.add(PeriodOpening(book_id=book.id, period_id=period.id,
                             subject_id=subject_id, amount=amount))

    # P3-10：期初写入过去完全不留痕。期初是账面数字的起点，
    # 一旦存歪，之后每个月、每张账簿、每份报表都会跟着歪，而且极难回溯
    # 到是「录期初那一刻就差了」。审计里要能看出每个科目改前改后是多少。
    changed = [
        f"{subject_id}: {before.get(str(subject_id), '—')} → {amount}"
        for subject_id, amount in planned
        if before.get(str(subject_id), "0") != str(amount)
    ]
    log(db, action="SET_OPENINGS", entity="period_opening", entity_id=period.id,
        user=user, book_id=book.id,
        detail=f"录入 {period.code} 期初：{len(planned)} 个科目"
               + (f"，变动 {len(changed)} 项" if changed else "")
               + (f"（{'；'.join(changed[:10])}"
                  + ("…" if len(changed) > 10 else "") + "）" if changed else ""),
        before=before,
        after={str(subject_id): str(amount) for subject_id, amount in planned})
    db.commit()
    return {"ok": True, "count": len(payload), "period": period.code}


@router.get("/{book_id}/periods/{period_id}/carry-forward")
def carry_forward_status(book_id: int, period_id: int, book: Book = Depends(get_my_book),
                         db: Session = Depends(get_db)):
    """本期有没有结转过 —— 前端用它决定显示「执行」还是「重新结转」。"""
    period = svc.get_period(db, book.id, period_id)
    return svc.carry_forward_status(db, period)


@router.post("/{book_id}/periods/{period_id}/carry-forward")
def carry_forward(book_id: int, period_id: int,
                  payload: Optional[CarryForwardIn] = None,
                  book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  # replace=True 会删凭证，只读账套必须挡住（与凭证删除接口同一条规矩）
                  _link: UserBook = Depends(require_book_write),
                  user: SysUser = Depends(get_current_user)):
    period = svc.get_period(db, book.id, period_id)
    try:
        # 永远「先删旧结转凭证再重算」，payload 只为兼容旧前端保留
        return svc.carry_forward_profit(
            db, book.id, period, user, replace=bool(payload.replace) if payload else True
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.post("/{book_id}/periods/{period_id}/close")
def close_period(book_id: int, period_id: int, book: Book = Depends(get_my_book),
                 db: Session = Depends(get_db), user: SysUser = Depends(get_current_user),
                 _link: UserBook = Depends(require_book_write)):
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
                   db: Session = Depends(get_db), user: SysUser = Depends(get_current_user),
                   _link: UserBook = Depends(require_book_write)):
    period = svc.get_period(db, book.id, period_id)
    try:
        return svc.batch_unclose(db, book.id, period, user, payload.revert_posting)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{book_id}/year-end")
def year_end(book_id: int, payload: YearEndIn, book: Book = Depends(get_my_book),
             db: Session = Depends(get_db), user: SysUser = Depends(get_current_user),
             _link: UserBook = Depends(require_book_write)):
    try:
        return svc.year_end_carry(db, book, user, payload.from_year, payload.new_year)
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.get("/{book_id}/audit-logs")
def audit_logs(book_id: int, limit: int = Query(100, le=1000), book: Book = Depends(get_my_book),
               db: Session = Depends(get_db), _admin: SysUser = Depends(require_admin)):
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
