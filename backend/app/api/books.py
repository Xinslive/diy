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
from app.accounting.book_deletion import (DELETION_NAME, TRIGGER_RENAME, delete_book,
                                          is_deletion_name)
from app.accounting.initialization import (OPENINGS_SOURCE_IMPORT, blocked_reason,
                                           init_state, init_states, mark_openings_ready,
                                           mark_subjects_ready)
from app.accounting.util import money
from app.api.schemas import (BookEditIn, BookIn, BookOrderIn, CarryForwardIn, CloseIn,
                           OpeningListIn, ProfitSubjectIn, UncloseIn, YearEndIn)
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import (get_book_link, get_current_user, get_my_book, normalize_role,
                        require_admin, require_book_write)
from app.core.uploads import RowBudget, UploadTooLarge, check_archive_size
# 本地别名保持函数名 ``read_upload`` 不变（守卫测试按这个名字认共享容量闸），
# 指向的其实是同步版 —— 端点已改成同步 ``def``，不能再 await。
from app.core.uploads import read_upload_sync as read_upload
from app.ledger.engine import LedgerEngine
from app.models import Book, Period, PeriodOpening, Subject, SysUser, UserBook, Voucher

router = APIRouter(prefix="/api/books", tags=["账套与期间"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_name(name: str) -> str:
    from urllib.parse import quote

    return f"attachment; filename*=UTF-8''{quote(name)}"


def _years_of(db: Session, book_ids: list[int]) -> dict[int, list[dict]]:
    """一批账套下每个会计年度的结账进度。**一次查询算完所有账套**（审计 P2-3）。

    账套列表要显示「已结账 n/12 期」「使用中/已结转」，而这两个数都是
    **按年度**算的 —— 2026 年结完了不代表 2027 年也结完了。

    以前这里是「每本账调一次、每次 2 条 SQL」：12 本账 = 30 条 SQL，
    其中 24 条是同一对查询重复 12 遍。往返数随账套数线性增长，而
    ``POST /api/books`` 对任何登录用户开放（自助注册开着），一个人
    建几十上百本账是现实场景。

    现在改成「一次 group by 带 book_id → 在 Python 里按 book_id 归拢」，
    与同文件里 ``init_states`` 的写法一致（那个函数当初就是为了
    同样的理由抽出来的）。**不引入跨请求缓存** —— 改密、停用、换角色
    必须立刻生效（审计中等 4）。
    """
    if not book_ids:
        return {}
    rows = db.execute(
        select(
            Period.book_id,
            Period.year,
            func.count(Period.id),
            func.count(Period.id).filter(Period.locked.is_(True)),
        )
        .where(Period.book_id.in_(book_ids))
        .group_by(Period.book_id, Period.year)
        .order_by(Period.book_id, Period.year)
    ).all()
    vouchers: dict[tuple[int, int], int] = {}
    for book_id, year, n in db.execute(
        select(Period.book_id, Period.year, func.count(Voucher.id))
        .join(Voucher, Voucher.period_id == Period.id)
        .where(Period.book_id.in_(book_ids))
        .group_by(Period.book_id, Period.year)
    ).all():
        vouchers[(book_id, year)] = n
    out: dict[int, list[dict]] = {}
    for book_id, year, total, closed in rows:
        out.setdefault(book_id, []).append({
            "year": year,
            "total": total,
            "closed": closed,
            "vouchers": vouchers.get((book_id, year), 0),
            "carried": closed >= total,
        })
    return out


@router.get("")
def list_my_books(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Book, UserBook.role)
        .join(UserBook, UserBook.book_id == Book.id)
        .where(UserBook.user_id == user.id)
        # 先按**这个人自己排的顺序**，没排过的（NULL）沉到最后，
        # 最后再退回 company_name/name —— 那个子句同时管住两件事：
        # 从不拖动的用户看到的顺序和以前一模一样；同一个用户里多个
        # NULL（新账套）之间也有稳定的次序，不会每次刷新都在跳。
        .order_by(UserBook.sort_order.asc().nulls_last(),
                  Book.company_name, Book.name)
    ).all()
    # 一次算完所有账套的初始化状态（四条 group by），不给每个账套单开一轮 count
    inits = init_states(db, [b for b, _role in rows])
    # 年度进度同理：一次算完所有账套，不给每个账套单开一轮两条查询
    years_by_book = _years_of(db, [b.id for b, _role in rows])
    out = []
    for b, role in rows:
        years = years_by_book.get(b.id, [])
        out.append({
            "id": b.id, "name": b.name, "company_name": b.company_name,
            "start_year": b.start_year, "start_month": b.start_month,
            "subject_code_rule": b.subject_code_rule,
            "role": role, "status": b.status,
            # 年度是账套的属性，列表一行一个账套，年度由前端下拉选
            "years": years,
            "latest_year": years[-1]["year"] if years else b.start_year,
            # 初始化进度：卡片上那个「待初始化」标记就靠它，
            # 点「继续初始化」时前端不用再为每个账套多发一次请求
            "init": inits[b.id],
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

    # 「删除」是保留名 —— 它是「改名即删账」那条路径的触发词
    # （见 ``accounting/book_deletion.py``）。建账时就不让用它，
    # 这个名字才不会同时是「一本真账的名字」和「删账的信号」。
    if is_deletion_name(payload.name):
        raise HTTPException(400, f"账套名「{DELETION_NAME}」是保留名，请换一个名字")

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


@router.put("/order")
def reorder_books(payload: BookOrderIn, user: SysUser = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """把账套磁贴排成用户给的顺序（每人一份，见 UserBook.sort_order）。

    **为什么只读账号也能调。** 这条写的是「我这个人的磁贴怎么排」，
    不是账上的任何东西 —— 相当于拖了一下窗口的侧边栏。它不碰凭证、
    科目、期初，也不影响别的用户，所以在这里挂 ``require_book_write``
    反而是错的：只读同事打开自己的账套列表，一样有权把它排顺眼。
    这也是它被 tests/test_viewer_matrix.py 的豁免清单放出来的原因。

    **为什么不写审计。** 审计表按账套记，磁贴排序既不改变任何账面数据，
    也没有「谁排的」这种需要追责的事；真要记，它只会往每一本账的
    审计里塞一堆与账无关的行，把真正的改动淹没掉。

    **提交的是相对顺序，不是绝对序号。** 客户端拖完把当前看到的账套 id
    按新顺序整体发过来；这里再把「没出现在提交里」的账套按它们现有的
    顺序接在后面，重新编成 0..n-1 的连续序号。这样序号永远稠密，
    也不会因为别人在这期间新建了账套，就把这一串序号顶歪。
    """
    ids = payload.book_ids
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "账套列表里有重复项")
    if not ids:
        return {"ok": True, "book_ids": []}

    # 这个用户**当前**看到的顺序，和列表接口同一套口径（含 NULL 沉底）
    current = list(db.scalars(
        select(Book.id)
        .join(UserBook, UserBook.book_id == Book.id)
        .where(UserBook.user_id == user.id)
        .order_by(UserBook.sort_order.asc().nulls_last(), Book.company_name, Book.name)
    ).all())

    # 提交里出现、但不属于这个用户的 id 一律拒掉：不然就成了
    # 「拿别人的账套 id 探自己有没有权限」的探测口。
    mine = set(current)
    alien = [i for i in ids if i not in mine]
    if alien:
        raise HTTPException(400, f"有 {len(alien)} 个账套不在你的列表里")

    final = ids + [i for i in current if i not in set(ids)]
    links = {
        link.book_id: link
        for link in db.scalars(
            select(UserBook).where(UserBook.user_id == user.id)
        ).all()
    }
    for pos, book_id in enumerate(final):
        links[book_id].sort_order = pos
    db.commit()
    return {"ok": True, "book_ids": final}


@router.get("/{book_id}")
def get_book(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    return {
        "id": book.id, "company_name": book.company_name, "name": book.name,
        "subject_code_rule": book.subject_code_rule,
        # 前端据此把不该选的级次直接置灰 —— 只在编辑弹窗里要，别塞进账套列表
        "subject_max_level": _max_subject_level(db, book.id),
        "start_year": book.start_year, "start_month": book.start_month,
        "status": book.status, "years": _years_of(db, [book.id]).get(book.id, []),
        "init": init_state(db, book),
    }


@router.get("/{book_id}/init")
def get_book_init(book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年")):
    """某个年度的初始化进度。向导弹窗开起来时先拿它，决定第 2 步锁不锁。

    只读接口：谁都能看自己账套的初始化到哪一步了，**写**那一步才要权限。

    **按年度**：每个年度有自己的科目表，也就各有各的「第 1 步做没做」。
    """
    return init_state(db, book, year)


@router.post("/{book_id}/init/subjects/keep")
def keep_chart_subjects(
    book_id: int,
    book: Book = Depends(get_my_book),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
    _link: UserBook = Depends(require_book_write),
    year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年"),
):
    """确认「沿用系统标准科目表」，也就是把初始化第 1 步标记为完成。

    为什么要这条**只写一个时间戳**的接口：新建账套时系统已经装好标准科目表，
    对「从零开始记账」的用户来说第 1 步就是「确认一下就用它」。
    没有这条路，界面上就只能强制所有人去导入一份外部科目表 ——
    而一份标准科目表对一个刚开始记 2026 年账的公司本来就在账上，
    要求他为了走流程去找一份并不存在的文件。

    幂等：已经确认过再点一次不会重写时间戳，也不会多一条审计
    （时间戳只在第一次落地时写，「这一步是什么时候完成的」因此只有一个答案）。
    """
    from app.accounting.audit_helper import log
    from app.accounting.initialization import subject_import_state
    from app.accounting.years import ensure_book_year, resolve_year

    resolved = resolve_year(db, book, year)
    counts = subject_import_state(db, book.id, resolved)
    if counts["subject_count"] == 0:
        raise HTTPException(
            400,
            f"{resolved} 年度还没有任何科目，无法确认沿用标准科目表；"
            "请先导入一份外部科目表，或新建账套让它自动装载标准科目表",
        )
    book_year = ensure_book_year(db, book, resolved)
    changed = mark_subjects_ready(db, book_year, "CHART")
    if changed:
        log(db, action="INIT_SUBJECTS", entity="book", entity_id=book.id, user=user,
            book_id=book.id,
            detail=f"{resolved} 年初始化第 1 步：沿用系统标准科目表"
                   f"（{counts['subject_count']} 个科目）",
            before={"init_subjects_at": None, "init_subjects_source": None},
            after={"init_subjects_at": book_year.init_subjects_at.isoformat(),
                   "init_subjects_source": "CHART",
                   "year": resolved,
                   "subject_count": counts["subject_count"]})
        db.commit()
    return {"ok": True, "changed": changed, "init": init_state(db, book, resolved)}


@router.post("/{book_id}/init/openings/skip")
def skip_openings(
    book_id: int,
    book: Book = Depends(get_my_book),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
    _link: UserBook = Depends(require_book_write),
    year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年"),
):
    """声明「本期本来就没有期初余额」，把初始化第 2 步标记为完成。

    **为什么必须有这条路。** 新公司第一年、上一年度本来就是空账，期初该是 0。
    界面上只有「导入期初余额」一个出口时，这类账套的初始化永远停在第 2 步：
    账套卡片上那个「待初始化」摘不掉，向导每次弹出来都在催一件他根本不需要做的事。
    而「没做」与「确认过没有」在库里必须有区别 —— 前者是要提醒的隐患，
    后者是已经核对过的事实。

    已经有期初余额的账套不给跳过：那种情况下「无期初」与「把期初清空了」
    是两回事，不能用一个声明糊弄过去。
    """
    from app.accounting.audit_helper import log
    from app.accounting.initialization import (OPENINGS_SOURCE_NONE,
                                               subject_import_state)
    from app.accounting.years import ensure_book_year, resolve_year

    resolved = resolve_year(db, book, year)
    counts = subject_import_state(db, book.id, resolved)
    if counts["opening_count"] > 0:
        raise HTTPException(
            409,
            f"{resolved} 年度已经有 {counts['opening_count']} 条期初余额，"
            "不能再声明「无期初余额」（那与「把期初清空」不是一回事）",
        )
    book_year = ensure_book_year(db, book, resolved)
    changed = mark_openings_ready(db, book_year, OPENINGS_SOURCE_NONE)
    if changed:
        log(db, action="INIT_OPENINGS", entity="book", entity_id=book.id, user=user,
            book_id=book.id,
            detail=f"{resolved} 年初始化第 2 步：声明本期无期初余额（不导入任何期初数据）",
            before={"init_openings_at": None, "init_openings_source": None},
            after={"init_openings_at": book_year.init_openings_at.isoformat(),
                   "init_openings_source": OPENINGS_SOURCE_NONE,
                   "year": resolved})
        db.commit()
    return {"ok": True, "changed": changed, "init": init_state(db, book, resolved)}


def _delete_by_rename(book: Book, db: Session, link: UserBook, user: SysUser) -> dict:
    """账套只剩一个授权用户、那个人就是当前用户 —— 改名成「删除」等于删掉这本账。

    **门禁是「你是不是唯一授权人」，不是「你是不是 OWNER」。**
    账套只有一个授权人时，那个角色通常就是 OWNER；但也可能管理员把一本账
    只留给了某个会计（OWNER 离职、账转交）。两种情况下这个人都是这本账
    唯一能管的人 —— 拦住他，账套就成了没人能删的孤儿。
    （只读账号连这一步都到不了：``require_book_write`` 先把它挡了。）

    **为什么这条路要存在。** 管理员收回最后一份授权是**管理员**的动作，
    可账套常常是使用者自己的（他自己建的账）；等他去找管理员、说明白
    「这本账不要了」，中间还夹着一次口头沟通。而「把账套改名成『删除』」
    是他本来就会做的动作（改个名）—— 名字成了那个信号。

    界面上**不提示**这条路：没有按钮，没有文案，
    只有真去改这个名字的人会撞上它。
    """
    grants = db.scalars(select(UserBook).where(UserBook.book_id == book.id)).all()
    if len(grants) != 1 or grants[0].user_id != user.id:
        # 不是唯一授权人 → 不删。
        # 话只说「这个账套还有别人」，**不解释**这套机制 ——
        # 机制不外露是这套设计的要求。
        if normalize_role(link.role) != "OWNER":
            raise HTTPException(403, "只有账套所有者可以修改账套名和单位名")
        if not grants:
            raise HTTPException(409, "这个账套的授权状态已经变了，请刷新后重试")
        raise HTTPException(
            409, f"这个账套还有 {len(grants)} 个授权用户，不能改名。")
    result = delete_book(db, book, user, trigger=TRIGGER_RENAME)
    db.commit()
    return {"id": result["id"], "company_name": result["company_name"],
            "name": result["name"], "deleted": True}


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

    **「删除」是保留名**：账套只剩一个授权用户时，把名字改成「删除」
    等于删掉这本账。那一支**不走** OWNER 判定，走自己的门禁
    （见 :func:`_delete_by_rename`）。
    """
    changes = payload.changes()
    if "name" in changes and is_deletion_name(changes["name"]):
        return _delete_by_rename(book, db, link, user)
    if normalize_role(link.role) != "OWNER":
        raise HTTPException(403, "只有账套所有者可以修改账套名和单位名")
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
            # P1-19：乐观锁的版本号必须随列表一起下发。不下发的话前端手里
            # 永远没有可比对的数，结账/反结账就退化成「不带版本」= 不比对。
            "version": p.version,
            "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            "vouchers": counts.get(p.id, 0),
            "posted": posted.get(p.id, 0),
            "unposted": counts.get(p.id, 0) - posted.get(p.id, 0),
        }
        for p in periods
    ]


def _openings_period(db: Session, book: Book, year: int | None = None,
                     period_id: int | None = None) -> Period:
    """期初余额是**一个会计年度**的期初：返回该年度的第一个会计期间。

    期初快照挂在这个年度的**第一个期间**上（年度结转就是把本年期末写成
    次年 1 月的快照），所以「年度」和「年度的第一个期间」是同一份数据的
    两种说法。页面按年取数，月份对它没有意义。

    - ``year`` 明确给出就用它；
    - 只给 ``period_id``（老调用方）就取那一期**所在的年度** ——
      2026-05 与 2026-11 拿到的都是 2026 年度那份数；
    - 都没有就用账套的启用年度。

    以前这里永远返回「第一个期间」，页面也不带参数 —— 于是顶栏切到 2027 年，
    屏幕上摆的还是 2026-01 那份数，还配一句「账套已有业务数据」。
    用户看到的和顶栏选的对不上，怎么改都像是在跟一个不认识的页面较劲。
    """
    stmt = select(Period).where(Period.book_id == book.id)
    if year is not None:
        first = db.scalar(stmt.where(Period.year == year).order_by(Period.ord).limit(1))
        if first is None:
            raise HTTPException(404, f"账套没有 {year} 年的会计期间")
        return first
    if period_id is not None:
        period = db.scalar(stmt.where(Period.id == period_id))
        if period is None:
            raise HTTPException(404, "会计期间不存在或不属于该账套")
        first = db.scalar(stmt.where(Period.year == period.year).order_by(Period.ord).limit(1))
        if first is not None:
            return first
    first = db.scalar(stmt.order_by(Period.ord).limit(1))
    if first is None:
        raise HTTPException(400, "账套还没有会计期间")
    return first


def _openings_editable(db: Session, book: Book, first: Period) -> tuple[bool, str | None]:
    """这一**年度**的期初还能不能改，以及为什么不能。

    判据是「这个年度里有没有**已记账**的凭证」：

    - 账簿只统计已记账凭证（``LedgerEngine._status_filter``），所以年度内
      一张已记账凭证都没有时，这笔期初就还没被任何账簿、报表引用过 ——
      改它动不了任何已经算出来的数。
    - 未记账的凭证**不算**：它们不进账簿，改年度期初跟它们不冲突。
    - 改的是**年度**期初，所以年度里任何一期结过账都要挡住：那一期的
      数已经封存，动年度期初就是动已封账的账。
    """
    year = first.year
    # 口径与年度结转刷新期初那一步**同一个函数**（``accounting.periods``）——
    # 两处各写一遍查询时已经漂移过一次：结转那边数了全部凭证，
    # 于是「只有未记账凭证」的年度被拦下，用户明明还能改却收到
    # 「不能再刷新期初余额」。
    n = svc.posted_voucher_count(db, book.id, year)
    if n:
        return False, f"{year} 年度已有 {n} 张已记账凭证，期初余额不能再改"
    # 期间代码（2026-03）是模型上的**属性**而不是列，不能直接 select ——
    # 取月份自己拼，顺序按 ord 走。
    locked = list(db.scalars(
        select(Period.month).where(
            Period.book_id == book.id, Period.year == year, Period.locked.is_(True)
        ).order_by(Period.ord)
    ).all())
    if locked:
        codes = "、".join(f"{year}-{m:02d}" for m in locked)
        return False, f"{year} 年度已结账（{codes}），期初余额不能再改"
    return True, None


def _openings_amounts(db: Session, book: Book, period: Period,
                      subjects: list[Subject]) -> dict[int, Decimal]:
    """这一**年度**的期初：优先取快照，没有快照就按账簿实际在用的那个数算。

    ``period`` 是该年度的第一个期间。快照只在两个地方产生 —— 账套启用、
    年度结转。所以 2027 年度本来就没有快照时，账簿里的年度期初是由
    上一年期末实时推导的。页面如果一律显示 0，用户对不上总账，还以为账空了。
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
def get_openings(book_id: int, year: int | None = None, period_id: int | None = None,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """期初余额录入页用：一次拿全科目树 + **这一年度**的期初 + 是否还能改。

    口径是**年度**，不是期间：切到 2026 年的哪个月，拿到的都是 2026 年度
    那一份数。``period_id`` 仍收（老调用方），但只用来定年度。
    """
    first = _openings_period(db, book, year=year, period_id=period_id)
    editable, reason = _openings_editable(db, book, first)
    # 不过滤 active。年度结转会把**全部末级科目**的期末余额刷成次年期初，
    # 页面却把停用科目藏起来的话，那些期初在界面上是隐形的 ——
    # 用户看到「这个科目没有期初」，实际库里有。口径必须两边一致。
    # **科目表每个年度各有一份**（迁移 0032）：这一页摆的是「这一年」的
    # 科目表，期初挂在这一年的科目上。拿别的年度的科目来，期初就挂空了。
    subjects = db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.year == first.year)
        .order_by(Subject.code)
    ).all()
    saved = _openings_amounts(db, book, first, subjects)
    return {
        "period_id": first.id,
        # 快照所在的那一期（年度的第一个期间）。保留是为了老调用方还能读懂。
        "period": first.code,
        "year": first.year,
        "month": first.month,
        "editable": editable,
        "reason": reason,
        # 初始化进度随这一份一起下发：页面上「第 1 步没做完就别录期初」
        # 那道拦截条要的就是它，单独再发一次请求没有意义
        "init": init_state(db, book, first.year),
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
def export_openings(book_id: int, year: int | None = None, period_id: int | None = None,
                    book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """导出成可再导入的 Excel —— 既是备份，也是给下一个人填的模板。"""
    from app.printing import excel as xl

    # 导的必须是屏幕上那**一年**：页面跟着顶栏的年度走，导出还停在启用年的话，
    # 备份下来的数字和眼前看到的对不上，出了错根本查不出是哪一份。
    first = _openings_period(db, book, year=year, period_id=period_id)
    # 与录入页同一口径（含停用）。导少了行，用户核对时会以为账丢了；
    # 更糟的是导出的文件再导入回来会平白少一笔。
    subjects = db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.year == first.year,
                              Subject.is_leaf.is_(True)).order_by(Subject.code)
    ).all()
    saved = _openings_amounts(db, book, first, subjects)
    data = xl.opening_balance_excel(
        # 导出用的是**实际**方位：反方向余额（应收账款贷方等）导出后再导入必须
        # 原样回来，用科目自然方向导出会把方向洗成正的。
        #
        # 金额保持 Decimal（审计 P2-7）。这里原来写的是 ``float(abs(st))``：
        # 库里是 Numeric(20,2)、链路里是 Decimal，到导出这一步退回 float
        # 就等于在最末端把精度又丢了（大额只剩 15~17 位有效数字），
        # 而导出表是要交给审计方逐格核对的。printing/excel.py 那一侧
        # 已经是 Decimal 原样写进单元格，两边现在同一口径。
        [{"code": s_.code, "name": s_.name,
          "direction": ("借" if st > 0 else "贷") if st != 0 else ("借" if s_.direction == "DEBIT" else "贷"),
          "amount": abs(st)} for s_ in subjects for st in [saved.get(s_.id, Decimal("0"))]],
        book_name=book.name, year=first.year,
    )
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _xlsx_name(f"期初余额_{book.name}.xlsx")})


@router.post("/{book_id}/openings/parse")
def parse_openings(book_id: int, file: UploadFile = File(...),
                   basis: str = Query("opening", pattern="^(opening|closing)$"),
                   book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                   year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年")):
    """解析期初余额 Excel，**只解析不落库** —— 前端先给用户预览，确认后才写。

    虽然是 POST，但它**不改任何数据**：只解析出一个 preview 返回给前端。
    真正落库的是 ``set_openings``（那里挂了 require_book_write）。
    所以这里不挂写依赖 —— 只读账号也该能预览自己账上的期初表。

    **同步端点，刻意如此**（审计严重 1）：它和 ``vouchers/import`` 曾是全仓仅有的两个
    ``async def`` 端点，而 ``async def`` 端点直接跑在事件循环线程上 ——
    这里的解析（同步 openpyxl / xlrd）与 ``get_db`` 给的同步 Session
    会原地堵死整个 worker 的事件循环，连 ``/api/health`` 一起排队，
    而 CPU 占用并不高。改成 ``def`` 后由 FastAPI 丢进 anyio 线程池，
    与全仓其余端点一致。

    但「只读」只免的是**写权限**，不免**容量**（审计 P1-4）：这个入口曾经
    一道闸都没有。``raw = await file.read()`` 无上限整体读进内存，
    紧接着把整个工作表一次性物化成二维列表 —— 凭证导入那边有三道闸，
    这里一道都没有，而只读账号就能打。它还被 ``tests/test_viewer_matrix.py`` 按
    「不落库」列进写接口豁免清单，于是守卫测试也照不到它。
    现在与凭证导入共用 ``app/core/uploads.py``：先看 ``file.size`` 预检、
    再按上限 +1 读一次、读进来之后按 zip 目录声明的解压体积预检、
    物化工作表时逐行计数（第三道在解析模块里）。

    ``basis`` 决定取哪一套数：用友余额表同一张表里期初与期末分列，
    选错了不会报错、只会让用户对着一个对不上的差额发呆，所以做成参数交给界面选。

    容错：科目可按编码或名称匹配；方向列可省（按科目属性推）。借贷不平在这里
    就报出来，不等到入库才发现。

    **用友导出的 .xls 余额表也吃**（2026-10）：`.xls` 是 OLE 复合文档，
    openpyxl 只认 .xlsx，原来这条路拿 openpyxl 硬读，用友文件到这儿必然
    「无法读取 Excel 文件」。解析本身已经搬去
    ``app.accounting/opening_importer.py``，这里只留容量闸与状态码翻译。
    """
    from app.accounting.opening_importer import parse_opening_workbook

    # 初始化第 1 步（确定科目）没做完，就不让碰第 2 步（期初）。
    # 这一道拦的是**数据依赖**而不是流程偏好：期初挂在科目上，
    # 科目还没定下来时解析出来的每一条都挂不到账上，用户只会看到
    # 「读进来 N 条，全是『科目在本账套不存在』」。
    from app.accounting.years import ensure_book_year, resolve_year

    resolved = resolve_year(db, book, year)
    reason = blocked_reason(ensure_book_year(db, book, resolved))
    if reason:
        raise HTTPException(409, reason)

    # 容量三道闸的前两道在共享工具里（与凭证导入同一把尺，见 app/core/uploads.py）。
    # 413：文件太大是「换个更小的再来」，不是参数写错了。
    try:
        raw = read_upload(file, label="期初余额文件",
                          hint="请拆成多份分别导入，或先删除无关工作表")
    except UploadTooLarge as e:
        raise HTTPException(413, str(e))
    try:
        check_archive_size(raw, label="期初余额表")
    except UploadTooLarge as e:
        raise HTTPException(413, str(e))

    # 解析不过滤 active：导出那一侧已经含停用科目了，这侧再过滤就
    # 造出一个「导得出来、导不回去」的坑 —— 用户只能手工改表。
    by_code, by_name = {}, {}
    for s_ in db.scalars(select(Subject).where(
            Subject.book_id == book.id, Subject.year == resolved)).all():
        by_code[s_.code] = s_
        by_name[s_.name] = s_

    try:
        return parse_opening_workbook(
            raw,
            file.filename or "期初余额表",
            subjects_by_code=by_code,
            subjects_by_name=by_name,
            basis=basis,
        )
    except UploadTooLarge as e:
        raise HTTPException(413, str(e))
    except ValueError as e:
        # 上传太限是 ValueError 的子类，所以这条必须排在它后面 ——
        # 否则容量拒绝会被当成 400「参数写错了」，提示方向完全相反
        raise HTTPException(400, str(e))


@router.post("/{book_id}/periods")
def set_openings(book_id: int, payload: OpeningListIn, year: int | None = None,
                 period_id: int | None = None,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                 user: SysUser = Depends(get_current_user),
                 _link: UserBook = Depends(require_book_write)):
    """录入**某一年度**的期初余额。

    口径是**年度**：一笔期初就是一个会计年度的起点，落在该年度的第一个
    期间上。之后每个月的期初都由它往下滚，不再一月期初、二月期初各录一份。

    不传年度/期间就是账套的启用年度（老行为）。``period_id`` 仍收（老调用方），
    但只用来定年度。

    入参是裸数组，行数上限在 ``schemas.OpeningListIn`` 上（审计 P2-2）——
    没有上限时「1 份请求体 → N 条数据库行」的放大器一直开着。
    """
    from app.accounting.audit_helper import log

    # 初始化第 1 步没做完就写不了期初，与 openings/parse 是同一道闸。
    # 两处都要：只拦解析的话，用户仍可以手一把数 POST 进来 ——
    # 而那正是最需要拦住的路径（期初一旦存歪，之后每个月都跟着歪）。
    from app.accounting.years import ensure_book_year

    first = _openings_period(db, book, year=year, period_id=period_id)
    book_year = ensure_book_year(db, book, first.year)
    gate = blocked_reason(book_year)
    if gate:
        raise HTTPException(409, gate)
    editable, reason = _openings_editable(db, book, first)
    if not editable:
        raise HTTPException(400, reason)

    # 期初快照有数据库触发器保护（禁止 UPDATE），这里整体重建。
    # 旧值必须**先取出来**：整体重建之后原值就没了，而审计恰恰要靠它。
    before_rows = db.scalars(select(PeriodOpening).where(
        PeriodOpening.period_id == first.id)).all()
    before = {str(o.subject_id): str(o.amount) for o in before_rows}

    for row in before_rows:
        db.delete(row)
    db.flush()

    # 先在内存里过一遍校验并试算平衡，确认没问题再落库：
    # 期初一旦存歪，后面每个月的账都跟着歪，而且很难看出是哪一步错的。
    #
    # 科目**一次取齐**（审计 P2-3）：以前每个科目一次 ``db.get``，
    # 129 个科目 = 143 条 SQL。往返数线性于科目数，而期初录入页
    # 摆的正是整张科目表。
    subject_ids = {item.subject_id for item in payload}
    subjects: dict[int, Subject] = {}
    if subject_ids:
        # **只认本年度的科目。** 期初挂在科目上，科目属于哪一年，
        # 这笔期初就属于哪一年 —— 挂到别的年度的科目上，账就串了。
        subjects = {
            s.id: s for s in db.scalars(
                select(Subject).where(
                    Subject.book_id == book.id, Subject.year == first.year,
                    Subject.id.in_(subject_ids))
            ).all()
        }
    planned, dr, cr = [], Decimal("0"), Decimal("0")
    for item in payload:
        subject = subjects.get(item.subject_id)
        if subject is None:
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
        # 后果比写错更麻烦：期初差 0.01 会顺着 LedgerEngine 传导到之后
        # 每一个月、每一张账簿、每一份报表，而且极难回溯到是「期初录入时」
        # 就已经差了。
        try:
            amount = money(item.amount)
        except (ArithmeticError, TypeError, ValueError):
            # ArithmeticError 是 Decimal 异常族的公共祖先。这里原来写的是
            # ``InvalidOperation`` —— 它确实是 ArithmeticError 的子类，功能上等价，
            # 但写成祖先才覆盖得住 ``money()`` 将来可能抛的别的 Decimal 异常
            # （Overflow / DivisionByZero）。money() 自己也会把它们翻译成
            # ValueError，这里是第二道。
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
        db.add(PeriodOpening(book_id=book.id, period_id=first.id,
                             subject_id=subject_id, amount=amount))

    # P3-10：期初写入过去完全不留痕。期初是账面数字的起点，
    # 一旦存歪，之后每个月、每张账簿、每份报表都会跟着歪，而且极难回溯
    # 到是「录期初那一刻就差了」。审计里要能看出每个科目改前改后是多少。
    changed = [
        f"{subject_id}: {before.get(str(subject_id), '—')} → {amount}"
        for subject_id, amount in planned
        if before.get(str(subject_id), "0") != str(amount)
    ]
    log(db, action="SET_OPENINGS", entity="period_opening", entity_id=first.id,
        user=user, book_id=book.id,
        detail=f"录入 {first.year} 年度期初：{len(planned)} 个科目"
               + (f"，变动 {len(changed)} 项" if changed else "")
               + (f"（{'；'.join(changed[:10])}"
                  + ("…" if len(changed) > 10 else "") + "）" if changed else ""),
        before=before,
        after={str(subject_id): str(amount) for subject_id, amount in planned})
    # 第一次真正写进期初 = 初始化第 2 步完成。
    # 口径是「存进库了」，不是「预览里读进来一堆数」—— 用户看一眼没保存
    # 就走，账上并没有期初，初始化就该还停在第 2 步上。
    #
    # 与 SET_OPENINGS 同一次事务：状态与数据要么一起成立，要么一起回滚。
    # 单独提交会出现「期初写失败了但向导显示已完成」的反过来情形。
    first_openings = mark_openings_ready(db, book_year, OPENINGS_SOURCE_IMPORT)
    db.commit()
    # **把最新的 init 一并带回来。** keep/skip/导入科目这几个口子都这么做，
    # 只有保存期初没有 —— 于是前端只能靠「保存完再发一次 GET /init」才知道
    # 第 2 步做完了，而那一发的结果没有任何人写回 store：
    # 用户从期初页回到账套管理，看到的仍是「第 2 步还没做」。
    # 顺手带上，跨页面那条链就不用多绕一圈、也不会漏。
    return {"ok": True, "count": len(payload), "period": first.code,
            "year": first.year,
            "init_step_done": first_openings, "init": init_state(db, book, first.year)}


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
            db, book.id, period, user
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.get("/{book_id}/years/{year}/profit-subject")
def get_profit_subject(book_id: int, year: int, book: Book = Depends(get_my_book),
                       db: Session = Depends(get_db)):
    """本年利润科目 —— 手工设定优先，其次按候选编码自动识别。

    **返回里「设的是什么」与「实际用的是什么」是分开的两件事。**
    手工设定的编码可能已经不在这一年的科目表里（整表换过一次），
    这时实际用的是自动识别到的另一个科目。合成一句话返回的话，
    用户会以为自己的设定生效了 —— 而结转冲到的其实是另一个科目。
    """
    _require_year(db, book.id, year)
    return _profit_subject_state(db, book.id, year)


@router.put("/{book_id}/years/{year}/profit-subject")
def set_profit_subject(book_id: int, year: int, payload: ProfitSubjectIn,
                       book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                       user: SysUser = Depends(get_current_user),
                       # 设定会改变结转冲到的科目 —— 只读账套不许改
                       _link: UserBook = Depends(require_book_write)):
    """手工设定（或清掉）「本年利润」科目。

    **设定的是科目编码，不是 id**：科目表每个年度各有一份，同一编码在
    2026 与 2027 是两行不同的记录，存 id 就只能管一年。

    校验有三条，都是「设了也用不了」的情况，挡在写入之前：
      · 编码不在这一年的科目表里 —— 否则结转时找不到它，回落自动识别，
        用户以为设上了而实际没生效；
      · 不是末级科目 —— 凭证只记在末级上，结转凭证会被服务端拒掉；
      · 已停用 —— 停用的语义是「不让再挂新的分录」。
    """
    _require_year(db, book.id, year)
    code = (payload.code or "").strip()
    if code:
        subject = db.scalar(
            select(Subject).where(
                Subject.book_id == book.id, Subject.year == year, Subject.code == code
            )
        )
        if subject is None:
            raise HTTPException(400, f"{year} 年的科目表里没有编码 {code} 的科目")
        if not subject.is_leaf:
            raise HTTPException(400, f"{code} {subject.name} 不是末级科目，结转凭证只能记在末级科目上")
        if not subject.active:
            raise HTTPException(400, f"{code} {subject.name} 已停用，不能作为本年利润科目")
    else:
        subject = None

    before = svc.profit_subject_setting(db, book.id, year)
    svc.set_profit_subject_setting(db, book.id, year, code or None)
    from app.accounting.audit_helper import log

    log(db, action="UPDATE", entity="book_year", entity_id=f"{book.id}-{year}", user=user,
        book_id=book.id,
        detail=(f"设定 {year} 年本年利润科目为 {code}" if code
                else f"清掉 {year} 年本年利润科目的手工设定（回到自动识别）"),
        before={"profit_subject_code": before},
        after={"profit_subject_code": code or None})
    db.commit()
    return _profit_subject_state(db, book.id, year)


def _require_year(db: Session, book_id: int, year: int) -> None:
    """这个账套有没有这一年。没有就是 400 —— 不能给一个不存在的年度建设定。"""
    from app.accounting.years import book_year_of

    if book_year_of(db, book_id, year) is None:
        raise HTTPException(400, f"账套没有 {year} 年的记账年度")


def _profit_subject_state(db: Session, book_id: int, year: int) -> dict:
    """本年利润科目的当前状态（设定值 + 实际用的那个 + 来源）。"""
    setting = svc.profit_subject_setting(db, book_id, year)
    subject, source = svc.resolve_profit_subject(db, book_id, year)
    # 设定值指向的科目已经不在这一年的科目表里 → stale。
    # 这种情况**不停用设定**：用户可能马上要把那张表导回来，
    # 自动清掉等于替他做了决定。
    stale = bool(setting) and (
        db.scalar(select(Subject).where(
            Subject.book_id == book_id, Subject.year == year, Subject.code == setting
        )) is None
    )
    return {
        "year": year,
        "setting_code": setting,
        "source": source,
        "stale": stale,
        "candidates": list(svc.PROFIT_SUBJECT_CODES),
        "subject": (
            {"id": subject.id, "code": subject.code, "name": subject.name,
             "category": subject.category, "is_leaf": subject.is_leaf,
             "active": subject.active}
            if subject is not None else None
        ),
    }


@router.post("/{book_id}/periods/{period_id}/close")
def close_period(book_id: int, period_id: int,
                 payload: Optional[CloseIn] = None,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                 user: SysUser = Depends(get_current_user),
                 _link: UserBook = Depends(require_book_write)):
    period = svc.get_period(db, book.id, period_id)
    try:
        return svc.close_period(
            db, period, user,
            expected_version=payload.version if payload else None)
    except svc.PeriodVersionConflict as e:
        # **顺序很重要**：必须在 ``except ValueError`` 之前。
        # PeriodVersionConflict 继承 ValueError（为了让不认识它的旧调用方
        # 也能正常报错而不是 500），写在后面就会被 400 那条先接走 ——
        # 而 400 的语义是「你参数写错了」，前端会让人去检查表单，
        # 实际要做的却是「刷新页面看最新状态」。
        db.rollback()
        raise HTTPException(409, str(e))
    except (ValueError, ArithmeticError) as e:
        # 回滚不能省（审计 P2-2）。原来是直接 raise，靠 ``Session.close()``
        # 的隐式回滚兜底 —— 那是**依赖**：同一个会话里后面还有别的事务动作
        # （比如调用方的批量操作继续往下走），失败的事务就会一直挂着。
        # 同文件的 carry_forward / year_end 一直都有这一行，这里是漏的。
        db.rollback()
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
        return svc.batch_unclose(
            db, book.id, period, user, payload.revert_posting,
            expected_version=payload.version)
    except svc.PeriodVersionConflict as e:
        # 同上：409 必须排在 400 前面。反结账尤其要这样 —— 它会把区间内的
        # 凭证批量退回未记账，误报成「参数写错」会让人反复重试，
        # 而真相是「别人刚结过账，你手里的状态已经过期」。
        db.rollback()
        raise HTTPException(409, str(e))
    except (ValueError, ArithmeticError) as e:
        db.rollback()
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
def audit_logs(book_id: int, limit: int = Query(100, ge=1, le=1000),
               book: Book = Depends(get_my_book),
               db: Session = Depends(get_db), _admin: SysUser = Depends(require_admin)):
    """账套的审计流水。

    **``limit`` 必须夹下限（审计 P3-11）。** 以前这里只写了上限
    （``le=1000``），于是 ``?limit=-1`` 会一路传到 SQL 的 ``LIMIT -1``，
    而 PostgreSQL 对负 LIMIT 报 ``InvalidRowCountInLimitClause``
    （sqlstate 2201W）—— 那不在 ``main.py`` 的 4xx 白名单里，于是回一句
    **脱敏的 500**：明明是参数写错，却告诉调用方「服务器坏了」。
    同仓兄弟接口都夹取（``system.py:537`` 的 ``min(max(limit,1),500)``、
    ``system.py:1219``、``backup/service.py:318``），这里补齐成同一条口径：
    ``ge=1`` 把负数与 0 直接挡在参数校验这一层，回 422。
    """
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
