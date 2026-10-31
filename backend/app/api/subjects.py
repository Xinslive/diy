"""科目表。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.accounting.initialization import init_state, mark_subjects_ready
from app.accounting.periods import current_accounting_year
from app.accounting.subject_importer import parse_subject_workbook
from app.api.schemas import SubjectEditIn, SubjectIn
from app.core.db import get_db
from app.core.deps import get_current_user, get_my_book, require_book_write
from app.core.uploads import UploadTooLarge, read_upload_sync as read_upload
from app.models import Book, Entry, Period, PeriodOpening, Subject, SysUser, UserBook, Voucher

router = APIRouter(prefix="/api/books/{book_id}/subjects", tags=["科目"])

#: 科目类别的中文名。报错里用它，不用英文枚举 —— 用户看的是「资产类」，
#: 不是「ASSET」。
_CATEGORY_LABELS = {
    "ASSET": "资产类",
    "LIABILITY": "负债类",
    "EQUITY": "所有者权益类",
    "COST": "成本类",
    "PROFIT": "损益类",
}


def _reject_carry_forward(code: str, name: str, category: str) -> None:
    """「结转科目」这个标记只许落在**损益类**科目上（审计 P1-1）。

    标错了的后果不是「多一个开关」：月末结转会把带这个标记的科目余额
    **整体搬进「本年利润」** —— 资产科目的钱凭空消失，而**借贷仍然相等**，
    试算平衡与系统自检全都发现不了，账实不符还会顺着期初滚到以后每一个月。

    所以这是 422（参数不合法）而不是 400：请求本身违反了科目语义，
    换个号、改个名都解决不了，必须把类别改对。
    """
    raise HTTPException(
        422,
        f"只有损益类科目可以参与月末结转：{code} {name} 是"
        f"{_CATEGORY_LABELS.get(category, category)}，不能标成「结转科目」。"
        f"标错了的话，月末结转会把它的余额整体搬进「本年利润」——"
        f"钱凭空消失，而借贷仍然平衡，系统自检查不出来。"
        f"如果它确实是损益类科目，请先把科目类别改成「损益类」再勾选。"
    )


def _subject_snapshot(s: Subject) -> dict:
    """科目快照。改名、换父、停用都靠它还原「改之前长什么样」。"""
    return {
        "id": s.id, "code": s.code, "name": s.name, "category": s.category,
        "direction": s.direction, "parent_id": s.parent_id, "level": s.level,
        "is_leaf": s.is_leaf, "carry_forward": s.carry_forward,
        "active": s.active, "sort_no": s.sort_no,
    }


def _tree(rows: list[Subject], current_year_activity: set[int] | None = None) -> list[dict]:
    current_year_activity = current_year_activity or set()
    by_parent: dict[int | None, list[Subject]] = {}
    for s in rows:
        by_parent.setdefault(s.parent_id, []).append(s)
    for lst in by_parent.values():
        lst.sort(key=lambda x: (x.sort_no, x.code))

    def build(parent):
        return [
            {
                "id": s.id, "code": s.code, "name": s.name, "level": s.level,
                "category": s.category, "direction": s.direction, "is_leaf": s.is_leaf,
                "carry_forward": s.carry_forward, "active": s.active,
                "qty_calc": s.qty_calc, "aux_calc": s.aux_calc, "fx_calc": s.fx_calc,
                "has_current_year_activity": s.id in current_year_activity,
                "children": build(s.id) if s.id in by_parent else [],
            }
            for s in by_parent.get(parent, [])
        ]

    return build(None)


@router.get("")
def list_subjects(book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  include_inactive: bool = Query(False),
                  year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年")):
    """include_inactive=true 时连停用科目一起返回：编辑老凭证时，
    老凭证上挂的那个科目可能已经停用，不返回的话格子里就空了。"""
    from app.accounting.years import resolve_year, subjects_of

    resolved = resolve_year(db, book, year)
    rows = subjects_of(db, book.id, resolved, include_inactive=include_inactive)
    # 「本年」= **账套当前在记的那个会计年度**（审计 P2-5）。
    # 以前这里用服务器当前日历年 + 连接会话时区（UTC）：北京时间的跨年
    # 8 小时窗口里算的是上一年，给老账套补录旧年度时算的也是错的年份。
    #
    # **现在这一栏是「这一份科目表所属的年度」**：科目表每个年度各有一份，
    # 2027 年那张表上的科目在 2027 年有发生额，在 2026 年没有 —— 判据
    # 必须跟着「正在看哪一年的科目表」走。
    current_year_activity = set(db.scalars(
        select(Entry.subject_id)
        .join(Period, and_(Period.book_id == Entry.book_id, Period.ord == Entry.period_ord))
        .where(Entry.book_id == book.id,
               Period.year == resolved,
               Entry.amount != 0)
        .distinct()
    ).all())
    return _tree(rows, current_year_activity)


@router.get("/flat")
def flat_subjects(book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  include_parents: bool = Query(
                      False, description="是否连上级科目一起返回。报表公式可以引用"
                                         "上级科目并自动汇总其下所有末级；凭证录入、"
                                         "发票制证这些地方仍然只要末级。"),
                  year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年")):
    from app.accounting.years import resolve_year, subjects_of

    resolved = resolve_year(db, book, year)
    rows = subjects_of(db, book.id, resolved, include_inactive=False)
    if not include_parents:
        rows = [s for s in rows if s.is_leaf]
    return [{"id": s.id, "code": s.code, "name": s.name, "direction": s.direction,
             "level": s.level, "category": s.category, "is_leaf": s.is_leaf,
             "carry_forward": s.carry_forward,
             "qty_calc": s.qty_calc, "aux_calc": s.aux_calc, "fx_calc": s.fx_calc} for s in rows]


@router.get("/frequent")
def frequent_subjects(book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                      limit: int = Query(20, ge=1, le=100),
                      year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年")):
    """最常用的末级科目：按这张账里已录入的分录数排。

    凭证录入时同一个科目一天要按几十次，摆在第一屏比让人去搜快得多。
    柠檬云这一栏叫「AI推荐」，我们没有模型，就用真实使用频次顶上 ——
    它比任何推荐算法都更贴近这个人、这套账自己的记账习惯。

    **按年度统计**：分录挂在各年自己的科目上，2026 年用得多的科目
    对 2027 年没有参考价值（那张表可能是新换的）。
    """
    from app.accounting.years import resolve_year

    resolved = resolve_year(db, book, year)
    rows = db.execute(
        select(Entry.subject_id, func.count().label("n"))
        .join(Subject, Subject.id == Entry.subject_id)
        .join(Period, and_(Period.book_id == Entry.book_id, Period.ord == Entry.period_ord))
        .where(Entry.book_id == book.id, Subject.active.is_(True),
               Period.year == resolved)
        .group_by(Entry.subject_id)
        .order_by(func.count().desc(), Entry.subject_id)
        .limit(limit)
    ).all()
    ids = [r.subject_id for r in rows]
    if not ids:
        return []
    subs = db.scalars(select(Subject).where(Subject.id.in_(ids))).all()
    by_id = {s.id: s for s in subs}
    out = []
    for r in rows:
        s = by_id.get(r.subject_id)
        if s is None or not s.active or not s.is_leaf:
            continue   # 只推荐能真正记账的末级科目
        out.append({"id": s.id, "code": s.code, "name": s.name, "category": s.category,
                    "direction": s.direction, "is_leaf": s.is_leaf, "used": r.n})
    return out


def _subject_import_state(db: Session, book_id: int, year: int) -> dict:
    """能不能覆盖导入**这个年度**的科目表。

    实现搬到了 ``app.accounting.initialization`` —— 初始化状态要用同一份计数，
    两边各查一遍的话，「账套列表说能导入、点进去说不能」这种不一致迟早出现。
    这里保留原名：本文件的调用点与既有测试都按这个名字认它。
    """
    from app.accounting.initialization import subject_import_state

    return subject_import_state(db, book_id, year)


def _parse_subject_upload(file: UploadFile, data: bytes | None = None) -> tuple[dict, str]:
    filename = file.filename or "未命名科目表"
    suffix = filename.lower()
    if not (suffix.endswith(".xls") or suffix.endswith(".xlsx")):
        raise HTTPException(400, "只支持 .xls 或 .xlsx 格式的科目表")
    try:
        if data is None:
            data = read_upload(
                file,
                label="科目表",
                hint="请拆成更小的文件或删除无关工作表后重试",
            )
        parsed = parse_subject_workbook(data, filename)
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return parsed, filename


@router.post("/import/preview")
def preview_subject_import(
    file: UploadFile = File(...),
    book: Book = Depends(get_my_book),
    db: Session = Depends(get_db),
    _link: UserBook = Depends(require_book_write),
    year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年"),
):
    """解析外部科目表并返回预览，不修改账套。"""
    from app.accounting.years import resolve_year

    try:
        data = read_upload(
            file,
            label="科目表",
            hint="请拆成更小的文件或删除无关工作表后重试",
        )
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    parsed, filename = _parse_subject_upload(file, data)
    resolved = resolve_year(db, book, year)
    state = _subject_import_state(db, book.id, resolved)
    # 期初不算「挡路」：它们会按**编码**搬到新科目表上（见 import_subjects）。
    # 凭证与分录才是硬挡 —— 它们按科目 id 指着具体的行，换表就指空了。
    blocked_reason = (
        f"{resolved} 年已有凭证或分录，不能覆盖该年度的科目表；"
        f"请先处理完这一年的业务数据，或换一个还没有数据的年度"
        if not state["can_overwrite"] else ""
    )
    remap_hint = (
        f"新科目表导入后，{resolved} 年的 {state['opening_count']} 条期初余额"
        f"会按科目编码搬到新科目上；新表里没有的编码若还有余额，导入会被拒绝"
        if state["opening_count"] > 0 else ""
    )
    return {
        "filename": filename,
        "source": "用友科目表" if parsed["file_format"] == "xls" else "Excel 科目表",
        **parsed,
        "current": state,
        "blocked_reason": blocked_reason,
        "remap_hint": remap_hint,
    }


@router.post("/import")
def import_subjects(
    file: UploadFile = File(...),
    book: Book = Depends(get_my_book),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
    _link: UserBook = Depends(require_book_write),
    year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年"),
):
    """确认覆盖**这个年度**的科目表：重新解析文件，并在一个事务内整表替换。

    **只动这一年的科目。** 别的年度的科目表一行都不碰 —— 这是「每个年度
    有自己的科目表」的全部意义：2027 年换一张科目表，2026 年正在用的
    那些科目、挂在它们上面的凭证与期初，全都原样不动。
    """
    from app.accounting.years import ensure_book_year, resolve_year

    try:
        data = read_upload(
            file,
            label="科目表",
            hint="请拆成更小的文件或删除无关工作表后重试",
        )
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    parsed, filename = _parse_subject_upload(file, data)
    if not parsed["ok"]:
        raise HTTPException(400, f"科目表有 {parsed['error_count']} 个问题，请修正后再导入")
    if not parsed["subjects"]:
        raise HTTPException(400, "科目表没有可导入的科目")

    # 覆盖是破坏性操作。锁住账套后再次检查，避免预览与确认之间有人写入凭证。
    locked_book = db.scalar(select(Book).where(Book.id == book.id).with_for_update())
    if locked_book is None:
        raise HTTPException(404, "账套不存在")
    resolved = resolve_year(db, locked_book, year)
    state = _subject_import_state(db, locked_book.id, resolved)
    if not state["can_overwrite"]:
        raise HTTPException(
            409,
            f"{resolved} 年已有业务数据，不能覆盖该年度的科目表"
            f"（凭证 {state['voucher_count']} 张、分录 {state['entry_count']} 条）。"
            f"请先处理完 {resolved} 年的业务数据，或换一个还没有数据的年度",
        )

    old_count = state["subject_count"]
    book_year = ensure_book_year(db, locked_book, resolved)
    old_rule = book_year.subject_code_rule
    try:
        # **本年度的期初先读出来。**
        #
        # 覆盖导入要把本年度的科目整批删掉，而 ``period_opening.subject_id``
        # 的外键是 RESTRICT —— 被期初引用的科目删不掉，导入当场失败。
        #
        # 读的是**编码**而不是科目 id：期初的归属是编码的事，与它挂在哪一行
        # 科目上无关（1002 的期初就是 1002 的期初，哪怕新表里 1002 换了 id）。
        from app.accounting.years import (attach_openings, detach_openings,
                                           opening_code_gaps, openings_of)

        snapshots = openings_of(db, locked_book.id, resolved)

        # **搬不过去的先说清楚，再动任何东西。**
        #
        # 编码在新科目表里没有、而余额非零 → 拒绝，并列出差异。
        # 静默丢弃的后果是「钱没了，而借贷仍然平衡，系统自检不会报错」——
        # 这套系统反复强调的头号风险。所以这个检查必须在删科目**之前**做，
        # 让拒绝发生在零破坏的状态下。
        new_codes = {item["code"] for item in parsed["subjects"]}
        gaps = opening_code_gaps(snapshots, new_codes)
        if gaps:
            detail = "、".join(
                f"{g['code']}（{g['amount']}）" for g in gaps[:10]
            )
            more = f" 等 {len(gaps)} 个编码" if len(gaps) > 10 else ""
            raise HTTPException(
                409,
                f"新科目表里没有这些编码，而 {resolved} 年的期初余额挂在它们上面，"
                f"搬不过去：{detail}{more}。"
                f"请在新科目表里保留这些编码，或先清掉这些期初余额再导入",
            )

        # 期初摘下来：科目要删，期初必须先走（RESTRICT 外键）。
        detach_openings(db, locked_book.id, resolved)

        max_level = db.scalar(
            select(func.max(Subject.level)).where(
                Subject.book_id == locked_book.id, Subject.year == resolved)
        ) or 0
        # 自引用外键要求先删下级，再删上级；逐级删除也让这条路径不依赖数据库的
        # 删除计划，PostgreSQL 和测试数据库的行为保持一致。
        #
        # **只删本年度的**：``Subject.year == resolved`` 那个条件是这次改造的
        # 关键 —— 少了它，「覆盖 2027 的科目表」会顺手把 2026 的科目全删了，
        # 而 2026 的凭证正指着它们（``entry.subject_id`` 外键）。
        for level in range(max_level, 0, -1):
            db.execute(delete(Subject).where(
                Subject.book_id == locked_book.id,
                Subject.year == resolved,
                Subject.level == level,
            ))

        by_code: dict[str, Subject] = {}
        max_import_level = max(item["level"] for item in parsed["subjects"])
        for level in range(1, max_import_level + 1):
            created: list[tuple[dict, Subject]] = []
            for item in parsed["subjects"]:
                if item["level"] != level:
                    continue
                parent = by_code.get(item["parent_code"]) if item["parent_code"] else None
                subject = Subject(
                    book_id=locked_book.id,
                    year=resolved,
                    code=item["code"],
                    name=item["name"],
                    category=item["category"],
                    direction=item["direction"],
                    parent_id=parent.id if parent else None,
                    level=level,
                    is_leaf=item["is_leaf"],
                    carry_forward=item["carry_forward"],
                    qty_calc=item["qty_calc"],
                    aux_calc=item["aux_calc"],
                    fx_calc=item["fx_calc"],
                    sort_no=item["sort_no"],
                    active=True,
                    ext_attrs=item["ext_attrs"],
                )
                db.add(subject)
                created.append((item, subject))
            db.flush()
            for item, subject in created:
                by_code[item["code"]] = subject

        # 编码规则是**年度**的属性：2027 年换了一张结构不同的科目表，
        # 它的接号规则就该跟着换，2026 年正在用的规则不该被带着一起变。
        book_year.subject_code_rule = parsed["code_rule"] or book_year.subject_code_rule
        db.flush()

        # **期初搬回新科目上。**
        #
        # 新科目已经建好（``by_code`` 是「编码 → 新科目行」），期初按编码
        # 挂回去。金额没变，变的只是挂在哪一行上。
        #
        # 挂不上的（编码对不上）已经由 ``opening_code_gaps`` 挡在前面了 ——
        # 到这里还挂不上只剩「金额为零且编码不存在」，那种没有数字要搬，
        # 丢掉它不改变任何金额。
        remapped = attach_openings(db, locked_book.id, snapshots, by_code)

        # 导入科目表就是初始化第 1 步的完成动作 —— 无论这个动作是从初始化
        # 向导里点的，还是从「科目表」页面上点的。与 SET_OPENINGS 的标记
        # 一样放在同一个事务里：状态与数据要么一起成立，要么一起回滚。
        mark_subjects_ready(db, book_year, "IMPORT")
        log(
            db,
            action="IMPORT_SUBJECTS",
            entity="book",
            entity_id=locked_book.id,
            user=user,
            book_id=locked_book.id,
            detail=f"覆盖导入 {resolved} 年科目表 {filename}："
                   f"{old_count} → {len(parsed['subjects'])} 个科目"
                   + (f"，期初按编码搬回 {remapped} 条" if remapped else ""),
            before={"subject_count": old_count, "subject_code_rule": old_rule, "year": resolved},
            after={
                "subject_count": len(parsed["subjects"]),
                "subject_code_rule": parsed["code_rule"],
                "filename": filename,
                "file_format": parsed["file_format"],
                "year": resolved,
                "openings_remapped": remapped,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(locked_book)
    return {
        "ok": True,
        "year": resolved,
        "deleted": old_count,
        "created": len(parsed["subjects"]),
        "code_rule": parsed["code_rule"],
        "filename": filename,
        # 初始化向导等的就是这份「第 1 步已完成」，随响应一起回去，
        # 前端不用再发一次 /init 才把下一步解锁
        "init": init_state(db, locked_book, resolved),
    }


@router.post("", status_code=201)
def create_subject(payload: SubjectIn, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), user: SysUser = Depends(get_current_user),
                   _link: UserBook = Depends(require_book_write),
                   year: int | None = Query(None, description="会计年度；不传取账套当前在记的那一年")):
    """在**某个年度**的科目表里新建科目。

    年度决定它落在哪一张表上，也就决定了它能出现在哪些年度的凭证里。

    ---- 年度从**两处**取，查询参数优先、请求体兜底 ----------------------
    过去只有查询参数这一条路，而前端把年度放在请求体里 —— FastAPI 不会把
    请求体的字段当查询参数读，于是年度**被静默丢掉**，后端退回「账套当前在记的
    那一年」。生产实测（2026-10-31，账套「泉州劲松珠宝有限公司」2023~2026 四张
    科目表）：用户在 **2023 年**那张表上给 1123 建下级，后端按 **2026 年**去认父科目，
    回一句「上级科目不存在」—— 父科目就摆在屏幕上。

    现在两处都收：``?year=`` 优先，没有时取请求体的 ``year``，再没有才走
    ``resolve_year``（账套当前在记的那一年）。**别再把它放回请求体就算完** ——
    那样还是会被查询参数盖掉。
    """
    from app.accounting.years import resolve_year

    resolved = resolve_year(db, book, year if year is not None else payload.year)
    exists = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == resolved, Subject.code == payload.code))
    if exists:
        raise HTTPException(400, f"科目编码已存在: {payload.code}")

    parent = _check_parent(db, book, payload.parent_id, self_id=None, year=resolved)
    # P1-1：父科目一旦有了下级就不再是末级，它**自己那份**余额（分录、期初）
    # 就脱离了明细口径。必须在动手之前就挡下来。
    if parent is not None:
        _assert_no_own_balance(db, parent, action=f"在它下面新建子科目 {payload.code}")

    # P1-1：只有**损益类**科目能参与月末结转。资产科目被标成结转科目，
    # 月末结转会把它的余额整体搬进「本年利润」——钱凭空消失，而借贷仍然
    # 平衡，系统自检查不出来。
    if payload.carry_forward and payload.category != "PROFIT":
        _reject_carry_forward(payload.code, payload.name, payload.category)

    s = Subject(
        book_id=book.id, year=resolved, code=payload.code, name=payload.name,
        category=payload.category,
        direction=payload.direction, parent_id=payload.parent_id,
        level=(parent.level + 1) if parent else 1,
        # P1-01：is_leaf 是服务端派生的，不接受客户端提交的值。
        # 刚建出来的科目必然没有下级，所以这里一定是 True —— 写死而不是
        # 读 payload，是为了让「客户端说了不算」这件事在代码里看得见。
        is_leaf=True, carry_forward=payload.carry_forward,
        qty_calc=payload.qty_calc, aux_calc=payload.aux_calc, fx_calc=payload.fx_calc,
        sort_no=payload.sort_no, active=True, ext_attrs={},
    )
    db.add(s)
    # 新建子科目后，父节点就不再是叶子了。is_leaf 是「有没有下级」的冗余字段，
    # 它错了会让期初录入拒掉本该能录的科目（或者反过来允许录到汇总级上），
    # 两头都是账面错误。更新路径有 _sync_leaf_flags，新建路径原先漏了。
    _sync_leaf_flags(db, None, payload.parent_id)
    db.flush()
    # P3-10：建科目过去不留痕。新增一个科目会让科目表、总账、报表的
    # 行列结构都变，事后没人说得清「这个科目是什么时候、谁加的」。
    log(db, action="CREATE_SUBJECT", entity="subject", entity_id=s.id, user=user,
        book_id=book.id,
        detail=f"新增 {resolved} 年科目 {s.code} {s.name}",
        after=_subject_snapshot(s))
    db.commit()
    db.refresh(s)
    return {"id": s.id, "code": s.code, "name": s.name}


def _descendant_ids(db: Session, subject_id: int) -> set[int]:
    """某科目下所有后代的 id（含各级子孙）。

    用来做两件事：环路检测（不能把父节点设成自己的后代）、
    以及改父后维护 is_leaf。不一次性全查是因为科目树通常只有几百条，
    但也没必要每次请求都把整棵树捞出来。
    """
    out: set[int] = set()
    frontier = [subject_id]
    while frontier:
        rows = list(db.scalars(
            select(Subject.id).where(Subject.parent_id.in_(frontier))
        ).all())
        rows = [r for r in rows if r not in out]
        if not rows:
            break
        out.update(rows)
        frontier = rows
    return out


def _check_parent(db: Session, book: Book, parent_id: int | None, self_id: int | None,
                  year: int) -> Subject | None:
    """校验要挂的父节点：必须属于本账套**且属于同一个年度**，且不能构成环。

    新建科目时有这条校验（P1-06：更新路径**没有**），于是拿着另一个账套的
    科目 id 来改父节点，HTTP 200 —— 该科目随即从本账套的科目树里消失，
    变成 UI 上不可见、库里还在、还被分录引用的孤儿节点。
    对使用者来说这是最难排查的一类问题：看不见但还在参与计算。

    **年度必须一致。** 科目表每个年度各有一份，父指针是**科目表内部**的
    关系 —— 把 2026 的科目挂到 2027 的科目下面，树的层级与上卷就全乱了
    （引擎按 parent_id 上卷，跨年上卷出来的余额没有意义）。
    """
    if not parent_id:
        return None
    if self_id is not None and parent_id == self_id:
        raise HTTPException(400, "上级科目不能是它自己")
    parent = db.get(Subject, parent_id)
    if parent is None:
        raise HTTPException(400, "上级科目不存在")
    # **三种「找不到」是三回事，报错必须说得清是哪一种** ——
    # 以前三种共用一句「上级科目不存在」，于是用户只会盯着屏幕上的父科目想
    # 「它明明在这儿」。实际最常见的是**年度对不上**：
    # 父科目在 2023 年的科目表上，而这次操作落在 2026 年那张表上 ——
    # 两张表里的 1123 是**两行不同的记录**（见 §4.5），后端按 2026 年去找，
    # 自然是「不存在」。
    if parent.book_id != book.id:
        raise HTTPException(400, "上级科目不存在（它不属于当前账套）")
    if parent.year != year:
        raise HTTPException(
            400,
            f"上级科目不存在（它属于 {parent.year} 年科目表，当前操作的是 {year} 年，"
            f"两张表不是同一份）。请把年度切到 {parent.year} 年再新建。")
    if self_id is not None and parent_id in _descendant_ids(db, self_id):
        raise HTTPException(400, "上级科目不能是它自己的下级（会形成循环）")
    return parent


def _own_entries_count(db: Session, subject_id: int) -> int:
    """该科目**自己**身上挂着的分录条数。

    分录这一项**不存在「被下级解释掉」的可能** —— 一条凭证分录钉死在某一个
    科目上，它就是那个科目自己的余额。父科目一旦挂上子科目就不再是末级，
    凭证校验（``accounting/vouchers._validate_entries``）会拒绝再往它身上
    记新的分录，损益结转（``accounting/periods.carry_forward_subjects``）也
    只走末级 —— 于是那笔挂在汇总级上的钱就停在那儿了。
    """
    return db.scalar(
        select(func.count()).select_from(Entry).where(Entry.subject_id == subject_id)
    ) or 0


def _unexplained_openings(db: Session, subject: Subject) -> list[tuple[str, Decimal, Decimal]]:
    """该科目自己那份、**下级没有替它解释掉**的期初余额。

    返回 ``[(期间, 自身快照, 下级快照之和), ...]``，只列真正有差额的期间。

    ---- 为什么不能「有几条非零快照就算它有余额」（2026-10-30 修）-----------
    父科目身上那条期初快照**不是它自己的一笔余额**，而是**整棵子树的汇总快照**：

    * 年度结转（``accounting/periods._refresh_opening``）对**每一个有余额的科目**
      都写一条期初快照，**含非末级**，金额取 ``closing`` —— 那是引擎上卷后的
      **子树合计**；
    * 引擎读期初时（``ledger/engine._roll_up_openings``）以**自身快照为基准**、
      下级只补增量，所以父子各写一条同额快照**不会被算两遍**。

    于是「父科目有一条非零期初快照」这件事在真实账里**几乎恒真** ——
    任何一个有余额的汇总科目都满足。拿它当「父科目自带余额」来拦，等于
    **凡是带余额的父科目一律不许加下级**。生产实测（2026-10-30）：紫欣工贸
    2026 年 54 个汇总科目、星悦荟 9 个全被这样挡住；用户的原话是
    「218102 的余额是他所有下级科目余额之和，也就是 218102 本身这一级没有余额」。

    ---- 真正该拦的是「下级解释不掉的那一部分」----------------------------
    判据是 ``自身快照 − Σ下级自身快照``：

    * **父科目还没有下级**（这次操作会把它**升格**为汇总级）→ 下级那一份是 0，
      差额就是它自己的全部余额 → 照旧拦。这正是审计 P1-1 要防的那一次升格：
      父级那份余额从此脱离明细口径，而明细账只按末级出账。
    * **父科目已经有下级** → 差额为 0 表示「下级已经替它记着这笔钱」
      （汇总快照本来就是这么写的），放行；差额**不为 0** 才是真的
      「它自己还挂着一笔没下沉的余额」，拦。

    这条判据顺带把「这次操作是不是一次**新的升格**」表达出来了 ——
    还没有下级的父科目天然差着全额，正是升格的那一次。

    生产库里 27 个账套年度、全部 407 个「带自身快照的汇总科目」，
    自身快照与下级快照之和**逐期完全相等**（一条不差）；按新判据重算，
    这 407 个**一个都不会再被拦**（另有 0 个汇总科目自己身上挂着分录）。
    """
    # **``Period.code`` 是 Python 属性不是列**（``f"{year}-{month:02d}"``），
    # 选它进 SQL 会直接报 ArgumentError —— 这里取 year/month 自己拼。
    own_rows = db.execute(
        select(PeriodOpening.period_id, PeriodOpening.amount, Period.year, Period.month)
        .join(Period, Period.id == PeriodOpening.period_id)
        .where(PeriodOpening.subject_id == subject.id)
    ).all()
    if not own_rows:
        return []

    child_ids = list(db.scalars(
        select(Subject.id).where(Subject.parent_id == subject.id)
    ).all())
    child_sum: dict[int, Decimal] = {}
    if child_ids:
        for period_id, total in db.execute(
            select(PeriodOpening.period_id, func.sum(PeriodOpening.amount))
            .where(PeriodOpening.subject_id.in_(child_ids))
            .group_by(PeriodOpening.period_id)
        ).all():
            child_sum[period_id] = Decimal(total)

    out: list[tuple[str, Decimal, Decimal]] = []
    for period_id, amount, year, month in own_rows:
        own = Decimal(amount)
        kids = child_sum.get(period_id, Decimal("0"))
        if own - kids != 0:
            out.append((f"{year}-{month:02d}", own, kids))
    return out


def _assert_no_own_balance(db: Session, subject: Subject, action: str) -> None:
    """没有自带余额的科目才允许被「升格」为汇总级（审计 P1-1）。

    **为什么 fail-closed。** 明细账按末级出账，凭证校验和期初写入又都拿
    ``is_leaf`` 当权威判据。父科目一旦挂上子科目就不再是末级，它自己那些
    **分录**就成了「挂在汇总级上的余额」—— 凭证校验从此拒绝再往它身上记分录，
    损益结转也只走末级，那笔钱就停在那儿了。

    这里选「拒绝」而不是「放行但警告」，理由和 P1-01 让 is_leaf 改由服务端派生
    是同一个：**错误的账面数字事后极难察觉，而拦住入口的代价只是多一次改挂操作。**
    放过它就等于把一次可预防的账务断层写进库里。

    **但「有余额」要问对问题（2026-10-30 修）。** 父科目身上那条期初快照是
    **子树的汇总快照**，不是它自己的一笔余额 —— 拿「有几条非零快照」来判
    「自带余额」，会把**每一个带余额的汇总科目**都锁死，用户连一棵已经建好的
    树都不敢再往下加一支。真正该问的是「**下级解释不掉的那一部分**」，
    口径见 ``_unexplained_openings``。

    报错要能指导下一步怎么做，而不是只说「不允许」：正确的处理是
    **先建一个末级科目、把分录改挂过去**，余额随之下沉，之后再来建子科目。

    存量数据里已经升格、身上又带着余额的科目不在这里拦（拦不住历史），
    由 ``accounting/periods.year_end_carry`` 的兜底把余额结转过去。
    """
    n_entries = _own_entries_count(db, subject.id)
    gaps = _unexplained_openings(db, subject)
    if not (n_entries or gaps):
        return

    what = []
    if n_entries:
        what.append(f"{n_entries} 条分录")
    if gaps:
        code, own, kids = gaps[0]
        detail = f"{code}：自身 {own}，下级合计 {kids}"
        more = f"（另有 {len(gaps) - 1} 期同样对不上）" if len(gaps) > 1 else ""
        what.append(f"{len(gaps)} 期期初余额与下级对不上（{detail}{more}）")

    raise HTTPException(
        400,
        f"{subject.code} {subject.name} 上已经有{'、'.join(what)}，不能{action}。"
        f"它一旦有了下级就不再是末级科目，自己那份余额就会脱离明细口径 —— "
        f"明细账只按末级出账，这笔钱在明细里看不到。"
        f"\n处理办法：先新建一个末级科目（先不要挂在它下面），把这些分录逐条改挂到"
        f"新科目上、期初余额也重新录到新科目上；余额下沉之后再{action}。"
        f"如果只是想停用这个科目，那用「停用」即可 —— 停用不影响历史统计与年度结转。"
    )


def _children_map(db: Session, book_id: int, year: int) -> dict[int, list[int]]:
    """某个年度科目表内的父子关系：父 id -> 子 id 列表（一次查完，不发 N 次查询）。

    科目树只有几百条，整棵树一次读进来比边走边查划算；而且递归重算时
    每层都要问「谁的孩子是它」，逐层查会把一次改父放大成几十次查询。

    **按年度查。** 科目表每个年度各有一份，父指针只在**同一张表**内部成立。
    """
    rows = db.execute(
        select(Subject.parent_id, Subject.id).where(
            Subject.book_id == book_id, Subject.year == year)
    ).all()
    out: dict[int, list[int]] = {}
    for parent_id, child_id in rows:
        if parent_id is not None:
            out.setdefault(parent_id, []).append(child_id)
    return out


def _recompute_subtree(db: Session, book_id: int, root_id: int, year: int) -> None:
    """从 root_id 起，递归重算整棵子树的 level 与 is_leaf（审计 P3-01 / P1-01）。

    两个字段都是**派生值**，凡是它们的依据变了就得重算：

    * ``level``：原来改父只把被移动的那个科目改成「新父级次 + 1」，它下面的
      下级、更深的后代一律留在旧级次上。于是把一棵两级的子树从第 1 层搬到
      第 3 层，根节点跟过去了，子孙还挂在原来的数上 —— 科目列表、明细账、
      前端缩进全都按持久化的 level 显示，树画出来是错的。
      测试里的改父用例搬的都是单个叶子科目，正好绕开了这个场景。

    * ``is_leaf``：同样派生自「有没有下级」，一起算才不会两个字段互相打架。

    之所以整棵重算而不是只改一层：级次是沿父链累加的，祖先动一位，
    整条链都错。没有循环（``_check_parent`` 拦了自引用和挂到后代），
    递归不会死循环；深度也受科目编码规则约束，是有限的几层。
    """
    children = _children_map(db, book_id, year)
    root = db.get(Subject, root_id)
    if root is None:
        return
    # 队列按层推进：同一层的节点 level 相同，算完再把子节点放进下一层，
    # 这样子孙拿到的父级次一定是已经算好的。
    queue: list[tuple[int, int]] = [(root_id, root.level)]
    while queue:
        subject_id, level = queue.pop(0)
        kids = children.get(subject_id, [])
        subject = db.get(Subject, subject_id)
        if subject is None:
            continue
        if subject.level != level:
            subject.level = level
        is_leaf = not kids
        if subject.is_leaf != is_leaf:
            subject.is_leaf = is_leaf
        queue.extend((kid, level + 1) for kid in kids)


def _sync_leaf_flags(db: Session, old_parent_id: int | None, new_parent_id: int | None) -> None:
    """改父 / 删子之后，把新旧两个父节点的 is_leaf 重新算一遍。

    is_leaf 是「有没有下级」的冗余字段。它错了，期初录入会拒掉本该能录的科目，
    或者反过来允许在汇总级科目上录期初 —— 两头都是账面错误。

    **None 不在这里处理**：它有两义 —— 「没有旧父节点」和「这个科目本来就是
    树根」。新建一个顶级科目时 ``_sync_leaf_flags(db, None, None)`` 也会走到这里，
    若把 None 当成「树根丢了孩子」把所有根都翻一遍，刚建出来的根节点立刻被标成
    汇总级。树根那种情况由 :func:`_recompute_root_leaf_flags` 在删除路径上单独处理。
    """
    for pid in {old_parent_id, new_parent_id}:
        if pid is None:
            continue
        # 会话是 autoflush=False（见 app/core/db.py），刚改的 parent_id 还没落库，
        # 下面的查询看不见它 —— 会把「有新下级」误判成 False，父节点就不更新。
        db.flush()
        has_child = db.scalar(
            select(Subject.id).where(Subject.parent_id == pid).limit(1)
        ) is not None
        parent = db.get(Subject, pid)
        if parent is not None and parent.is_leaf != (not has_child):
            parent.is_leaf = not has_child


def _recompute_root_leaf_flags(db: Session, book_id: int, year: int) -> None:
    """重算树根的 is_leaf。

    唯一的触发场景：**一个根的直接下级被删掉了**（或被改挂到别的根/别的层）。
    没有 id 可以更新，只能在「parent_id IS NULL 的那一批」里逐个看有没有孩子。

    原实现把 ``None`` 当成普通父节点跳过，于是根节点会永远停在 is_leaf=False：
    最后一个明细被删掉之后，这个一级科目再也录不进分录，而界面上没有任何地方
    提示它为什么不能记 —— 人只会以为系统坏了。
    """
    db.flush()
    for root in db.scalars(select(Subject).where(
            Subject.book_id == book_id, Subject.year == year,
            Subject.parent_id.is_(None))).all():
        has_child = db.scalar(
            select(Subject.id).where(Subject.parent_id == root.id).limit(1)
        ) is not None
        if root.is_leaf != (not has_child):
            root.is_leaf = not has_child


@router.patch("/{subject_id}")
def update_subject(subject_id: int, payload: SubjectEditIn, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), user: SysUser = Depends(get_current_user),
                   _link: UserBook = Depends(require_book_write)):
    s = db.scalar(select(Subject).where(
        Subject.id == subject_id, Subject.book_id == book.id).with_for_update())
    if s is None:
        raise HTTPException(404, "科目不存在")
    before = _subject_snapshot(s)
    was_active = s.active

    changes = {
        field: value for field, value in payload.model_dump(exclude_unset=True).items()
        if value is not None or field == "parent_id"
    }
    if "code" in changes:
        changes["code"] = changes["code"].strip()
        if not changes["code"]:
            raise HTTPException(400, "科目编码不能为空")
    if "name" in changes:
        changes["name"] = changes["name"].strip()
        if not changes["name"]:
            raise HTTPException(400, "科目名称不能为空")
    changed = {
        field: value for field, value in changes.items()
        if field != "is_leaf" and getattr(s, field) != value
    }
    if not changed:
        return {"id": s.id, "code": s.code, "name": s.name}

    # P1-1：「结转科目」这个标记只许落在**损益类**科目上。
    #
    # 写侧这道闸是必须的：结转损益会把带这个标记的科目余额整体搬进
    # 「本年利润」——资产科目的钱凭空消失，而**借贷仍然平衡**，
    # 试算平衡与系统自检全都发现不了。
    #
    # **只在「本次改动会造成违规」时报错**：库里早就存在的脏数据
    # （资产科目 + carry_forward=True）不该把人卡死 —— 改个名字、
    # 停用它都得还能做。读侧（``accounting/periods.carry_forward_subjects``）
    # 已经按 category 过滤，脏数据不会再被结走。
    new_category = changes.get("category", s.category)
    new_carry = changes.get("carry_forward", s.carry_forward)
    if "carry_forward" in changes and changes["carry_forward"] and new_category != "PROFIT":
        _reject_carry_forward(s.code, s.name, new_category)
    if "category" in changes and new_category != "PROFIT" and new_carry:
        _reject_carry_forward(s.code, s.name, new_category)

    # **口径必须覆盖整棵子树，不能只看它自己身上的分录。**
    #
    # 账簿引擎把分录上卷到各级父科目（``ledger/engine.py::_ancestor_map``），
    # 所以 1002 银行存款在总账 / 科目余额表 / 报表里看到的本年发生额，
    # 全部来自它下面的 100201、100202。对使用者来说「1002 本年度有记账」
    # 是板上钉钉的事实 —— 他看到的每一张报表都这么显示。
    #
    # 以前这里写的是 ``Entry.subject_id == subject_id``（只查直接分录），
    # 于是同一棵树上：末级的 100201 有分录 → 被挡住不能改编码；
    # 汇总级的 1002 一条直接分录都没有 → 放行。
    # 结果就是「儿子改不了爹能改」，而两者在账上是同一笔钱。
    # 改掉 1002 的编码会让资产负债表里按 1002 取数的公式指向一个空科目，
    # 货币资金凭空归零，且没有任何报错 —— 这正是这道守卫要防的那类事故。
    #
    # 取子树而不是取祖先：判断的是「这个科目**当前**管着的钱」，
    # 而 parent_id 的改动在下面才生效（改挂时不该用改挂后的子树来判）。
    scope_ids = {subject_id} | _descendant_ids(db, subject_id)
    # 「本年」= **这个科目所属的年度**。科目表每个年度各有一份，
    # 2027 年那张表上的科目在 2027 年有发生额，在 2026 年没有 ——
    # 判据必须跟着「这张科目属于哪一年」走，而不是账套当前在记的那一年
    # （两者可能不是同一年：用户在 2027 年翻看 2026 年的科目表时）。
    current_year_activity = db.scalar(
        select(Entry.id)
        .join(Period, and_(Period.book_id == Entry.book_id, Period.ord == Entry.period_ord))
        .where(Entry.book_id == book.id, Entry.subject_id.in_(scope_ids),
               Period.year == s.year, Entry.amount != 0)
        .limit(1)
    ) is not None
    # 只挡 **code**，和 db_init.sql 的 fn_subject_guard 同口径。
    #
    # 别图省事写成「除 name 外一律拒绝」：那样连 parent_id / active 一起卡死，
    # 而这两件事各自已经有更贴切的闸门，也各有非它不可的理由 ——
    #   · parent_id 有 _assert_no_own_balance（余额会被升格带走的那条）；
    #   · active 是「停用」，老凭证还指着它、账还得算得出来，
    #     _assert_no_own_balance 的报错里明确把停用指为解法。把它一起挡掉，
    #     等于当众收回那句话：有余额的科目就再也哪儿也去不了了。
    # 当前唯一被这条挡住的字段是 code —— 账上已有的报告、明细账、导出都按
    # 编码引用它，本年发生额还在就换不得，和触发器的意图完全一致。
    if current_year_activity and "code" in changed:
        raise HTTPException(400, f"科目 {s.code} 本年度已有发生额，只能修改科目名称")

    used = db.scalar(select(Entry.id).where(Entry.subject_id == subject_id).limit(1)) is not None
    if used and any(field in changed for field in ("direction", "category")):
        raise HTTPException(400, "科目已被凭证使用，不能修改类别 / 余额方向")

    if "code" in changed:
        duplicate = db.scalar(select(Subject.id).where(
            Subject.book_id == book.id, Subject.year == s.year,
            Subject.code == changes["code"],
            Subject.id != subject_id).limit(1))
        if duplicate is not None:
            raise HTTPException(400, f"科目编码已存在: {changes['code']}")

    # P1-06：新建时有父科目校验，更新路径原来直接赋值。补上，
    # 连带环路检测与 level 跟随。
    if "parent_id" in changed:
        parent = _check_parent(db, book, changes["parent_id"], self_id=subject_id,
                               year=s.year)
        # P1-1：改父级是「升格」的另一个入口，有两道：
        #   ① 新父节点因此有了下级，它自己那份余额会脱离明细口径；
        #   ② 被移动的这个科目若原本是末级，挂上去之后它也不再是末级，
        #      它自己的分录同样会脱离明细口径。
        # 只在「**新出现的**升格」上拦：本来就已有下级的科目（is_leaf=False）
        # 再挪一次位置并不会让它损失什么，不该借这条规则卡住正常的整理。
        if parent is not None:
            _assert_no_own_balance(db, parent, action=f"在它下面挂子科目 {s.code}")
        if changes["parent_id"] is not None and s.is_leaf:
            _assert_no_own_balance(
                db, s, action=f"改挂到上级科目 {parent.code}" if parent else "改挂到上级科目")
        old_parent_id = s.parent_id
        s.parent_id = changes["parent_id"]
        s.level = (parent.level + 1) if parent else 1
        # P3-01：整棵子树的级次一起重算，不只是被移动的这一个节点。
        _recompute_subtree(db, book.id, subject_id, s.year)
        _sync_leaf_flags(db, old_parent_id, changes["parent_id"])

    # P1-01：is_leaf 从这个循环里拿掉了。
    #
    # 原来它在字段循环里被无条件赋值，等于把「是不是末级」交给了客户端：
    # 而凭证校验（accounting/vouchers._validate_entries）和期初写入
    # （api/books.set_openings）都拿 is_leaf 当权威判据。于是普通账套写入者
    # 提交一次 PATCH，就能把一个有下级的汇总科目标成末级，接下来凭证校验放行、
    # 期初录入也放行 —— 分录真的挂到了汇总科目上，明细账里这笔钱凭空消失。
    # 前端不给选不算防线：接口能被直接调。
    #
    # 现在 is_leaf 只由服务端从子级关系算：挂上父节点时父节点变汇总（P1-06），
    # 移走子节点时父节点恢复末级，删掉最后一个子节点时同样恢复。
    for field in ("code", "name", "category", "direction", "carry_forward", "sort_no",
                  "qty_calc", "aux_calc", "fx_calc"):
        if field in changed:
            setattr(s, field, changes[field])
    # 停用是「不让再选它」，不是「删掉它」：老凭证还指着它，账还得算得出来。
    # 有分录的科目删不掉（下面 400），停用才是那条正路。
    if "active" in changed:
        s.active = changes["active"]
    db.flush()
    # P3-10：停用尤其要留痕。老凭证还指着这个科目（停用不是删除），
    # 出问题时「它什么时候被停的、谁停的」是第一条要问的事。
    verb = "停用" if (was_active and not s.active) else (
        "启用" if (not was_active and s.active) else "修改")
    log(db, action="UPDATE_SUBJECT", entity="subject", entity_id=s.id, user=user,
        book_id=book.id,
        detail=f"{verb}科目 {s.code} {s.name}"
               + (f"（active {was_active} → {s.active}）" if was_active != s.active else ""),
        before=before, after=_subject_snapshot(s))
    db.commit()
    db.refresh(s)
    return {"id": s.id, "code": s.code, "name": s.name}


@router.delete("/{subject_id}")
def delete_subject(subject_id: int, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), user: SysUser = Depends(get_current_user),
                   _link: UserBook = Depends(require_book_write)):
    s = db.get(Subject, subject_id)
    if s is None or s.book_id != book.id:
        raise HTTPException(404, "科目不存在")
    # 快照要在 delete 之前取 —— 删掉之后就再也拼不回来了。
    before = _subject_snapshot(s)
    if db.scalar(select(Entry).where(Entry.subject_id == subject_id).limit(1)):
        raise HTTPException(400, "科目已被凭证使用，不能删除；可改为停用")
    if db.scalar(select(Subject).where(Subject.parent_id == subject_id).limit(1)):
        raise HTTPException(400, "存在下级科目，不能删除")
    # P1-05：期初引用也要挡。PeriodOpening.subject_id 上挂着
    # ON DELETE CASCADE —— 原来只查了 Entry 和下级科目，没查期初，
    # 于是「只被期初引用、没有凭证引用」的科目一删，期初记录随之消失，
    # 余额凭空漂移（审计实测：2 条期初删成 1 条，余额 0 → -100）。
    #
    # 年度结转自动生成的期初**大量处于这个状态**（有余额、未记账），
    # 所以这不是边角情况。报错口径与「被凭证使用」保持一致：
    # 都是「可改为停用」—— 停用不影响历史统计。
    n_openings = db.scalar(
        select(func.count()).select_from(PeriodOpening).where(
            PeriodOpening.subject_id == subject_id)
    ) or 0
    if n_openings:
        raise HTTPException(
            400, f"该科目有 {n_openings} 期期初余额，不能删除；可改为停用"
                 "（停用不影响历史统计与年度结转）")
    # P3-10：删科目必须留 before 快照。这是少数「事后无法从库里还原」
    # 的写路径 —— 没有快照的话，被删掉的科目长什么样只能靠人回忆。
    log(db, action="DELETE_SUBJECT", entity="subject", entity_id=subject_id, user=user,
        book_id=book.id, detail=f"删除科目 {s.code} {s.name}", before=before)
    old_parent_id = s.parent_id
    db.delete(s)
    db.flush()
    # P1-01：删掉的可能正是父节点最后一个下级。不重算的话父节点会永远
    # 停在 is_leaf=False —— 从那以后它再也录不进分录，界面上却看不出原因。
    if old_parent_id is None:
        _recompute_root_leaf_flags(db, book.id, s.year)
    else:
        _sync_leaf_flags(db, old_parent_id, None)
    db.commit()
    return {"ok": True}
