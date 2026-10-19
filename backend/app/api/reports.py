"""报表：模板列表、求值、单元格公式编辑。"""
from __future__ import annotations

import uuid
import re
from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.api.schemas import CellIn
from app.core.db import get_db
from app.core.deps import get_current_user, get_my_book, require_book_write
from app.core.uploads import UploadTooLarge, read_upload_sync as read_upload
from app.models import (Book, Period, ReportCell, ReportCol, ReportResult, ReportRow,
                     ReportTemplate, Subject, SysUser, UserBook)
from app.reports.evaluate import COL_KINDS
from app.reports.formula import FormulaError, NON_FORMULA_KINDS, Parser, no_to_col, rewrite_refs
# ``app.reports.importer`` 在模块级 ``import olefile``，而实测
# olefile + openpyxl + lxml + xlrd 合计 18.7 MB / worker。报表模板导入是
# 偶发操作，不该让每个常驻 worker 为它付这份常驻内存，所以整行搬进用到它的
# 两个函数（``preview_template_import`` / ``import_templates``）。
# 写法沿用 app/api/books.py 里已有的先例。
from app.reports.import_schema import ImportTemplates

router = APIRouter(prefix="/api/reports", tags=["报表"])


def require_report_reader(
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
) -> SysUser:
    """读报表模板：**登录 + 至少对一个账套有授权**（审计 P2-13）。

    以前只要求登录，于是自助注册出来的空账号（一个账套都没有）能把
    ``/templates`` 和 ``/{id}/design`` 全读一遍 —— 资产负债表的科目排列
    和取数公式就等于把这家单位怎么算账公开了。

    fail-closed：没有任何账套授权的账号对模板**没有正当需求**，一律拒绝，
    不做「模板是全局的所以谁都能看」的放行。管理员放行（系统管理员本来就能
    从 ``/api/system/*`` 看到全部账套）。

    仍然挂 ``get_current_user``（作为依赖被它链进来），所以
    ``tests/test_report_design_auth.py::test_报表接口全部要求登录``
    那条静态守卫照旧成立。
    """
    if user.is_admin:
        return user
    if db.scalar(select(UserBook.id).where(UserBook.user_id == user.id).limit(1)) is None:
        raise HTTPException(403, "还没有任何账套授权，无法查看报表模板")
    return user


@router.get("/templates")
def templates(book_id: Optional[int] = None, db: Session = Depends(get_db),
              user: SysUser = Depends(require_report_reader),
              book: Book = Depends(get_my_book)):
    """报表模板列表：**官方内置基线 + 当前账套自己的**，按 code 去重。

    审计 P3-09 原判 C 级「待产品确认」，理由是模板跨账套共享。这里的结论
    是「跨账套共享」≠「对外部公开」：

    * 共享指的是同一次部署内多个账套复用同一套模板，背后还是同一批会计；
    * 模板的行、列、单元格公式就是这套单位怎么算账，属于内部经营信息；
    * 前端不存在合法的匿名消费方 —— 报表设计页在应用外壳内，必然已登录。

    所以仍然要求「登录 + 至少一个账套授权」（``require_report_reader``）。

    **去重是这一条的核心。** 官方基线（``book_id IS NULL``）和本账套的副本
    可能同 code 并存 —— 副本就是「这个账套改过这张内置表」的产物。按 code
    去重时**本账套优先**：用户改过的永远是改过那份，没改过的账套看基线。
    不去重的话，账套 A 改了内置利润表，列表里会同时冒出「利润表」和
    「利润表」两张，用户点开哪张全凭上下顺序。
    """
    _ = user
    rows = db.scalars(select(ReportTemplate).where(
        (ReportTemplate.book_id.is_(None)) | (ReportTemplate.book_id == book.id)
    ).order_by(ReportTemplate.id)).all()
    by_code: dict[str, ReportTemplate] = {}
    for t in rows:
        # 本账套的副本排在后面，覆盖同 code 的基线；基线先入，副本后入。
        by_code[t.code] = t
    return [{"id": t.id, "code": t.code, "name": t.name, "type": t.type,
             "is_builtin": t.is_builtin, "remark": t.remark,
             "book_id": t.book_id} for t in by_code.values()]


def _visible_template(db: Session, template_id: int, book: Book) -> ReportTemplate:
    """取出「当前账套看得见」的那份模板，否则 404。

    看得见 = 官方基线（``book_id IS NULL``）**或**归属当前账套。别的账套的
    模板一律当不存在 —— 不是 403，是 404：403 等于告诉对方「这个 id 确实
    存在，只是你没权限」，那本身就是信息泄露（能拿它去枚举别的账套
    建过几张报表、改过什么名字）。
    """
    tpl = db.get(ReportTemplate, template_id)
    if tpl is None or (tpl.book_id is not None and tpl.book_id != book.id):
        raise HTTPException(404, "报表模板不存在")
    return tpl


def _require_template_write(db: Session, user: SysUser, book_id: int | None) -> None:
    """改报表模板 = 改**本账套**的口径：要求「明确的账套 + 该账套的 OWNER」。

    审计 P1-5：``book_id`` 以前是**可选**查询参数，缺省时条件退化成
    「该用户在任意账套非 VIEWER」。于是「在账套 B 是会计、在账套 A 是只读」
    的人不带 ``book_id`` 调 ``POST /api/reports/templates`` 就能过门禁。
    模板按账套隔离（迁移 0020）之后这条更要说清：**模板不再是全局口径**，
    改的就是当前账套自己那份，所以门禁就该看当前账套的角色。

    两道 fail-closed 的闸：

    1. **必须显式给 book_id**。没有目标账套就没有「以谁的身份改」这件事，
       缺省放行等于把门重新打开。管理员也不例外：目标账套还要进审计
       日志的 ``book_id`` 列。
    2. **要求 OWNER**，不认「不是 VIEWER 就放行」这种黑名单判断。角色认不
       出来一律当拒绝（与 ``frontend/src/stores/session.js::canWrite`` 一致）。
    """
    if book_id is None:
        raise HTTPException(
            400, "报表模板归属账套，必须用 book_id 指明你以哪个账套的身份修改")
    link = db.scalar(select(UserBook).where(
        UserBook.user_id == user.id, UserBook.book_id == book_id))
    if link is None:
        raise HTTPException(403, "没有该账套的访问权限")
    if user.is_admin:
        return
    if link.role != "OWNER":
        raise HTTPException(403, "报表公式是本账套的口径，只有账套所有者或管理员可以修改")


def _require_owned_template(db: Session, template_id: int, book_id: int | None,
                            user: SysUser) -> ReportTemplate:
    """写路径的统一入口：门禁 + 归属。

    与读路径最大的不同在这里 —— **官方内置基线（``book_id IS NULL``）
    不接受任何直接写入**。它是所有账套共看的那一份，任何写都会立刻扩散到
    别的账套去。真的要改它，走 ``fork``：先复制一份归属当前账套，改副本。
    基线因此永远是干净的出厂状态。

    **为什么基线回 400 而不是 404（顺序是刻意的，不是随手排的）。**

    本函数原本先判 ``tpl.book_id != book_id`` 回 404，而 ``book_id`` 已被
    ``_require_template_write`` 保证非 None，于是「基线」（``book_id IS NULL``）
    必然先被 404 拦掉 —— 下面那句解释「怎么改基线」的 400 永远到不了用户
    面前，只留下一句无从理解的「报表模板不存在」。

    把基线提到 404 之前不会重新引入存在性泄露：404 那条要防的是**别的账套
    的私有模板**，403 等于告诉对方「这个 id 确实存在」，能拿它去枚举别人
    建过几张报表、改过什么名字。而基线是**公开的** —— 每个账套的报表列表里
    都列着它们，id 集合根本不是秘密。对一个谁都能看见的东西回一句
    「不能这么改，去设计模式」不泄露任何新信息。
    """
    _require_template_write(db, user, book_id)
    tpl = db.get(ReportTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "报表模板不存在")
    # 基线在 404 之前，理由见 docstring。
    if tpl.book_id is None:
        raise HTTPException(
            400, "系统内置报表是所有账套共用的原版，不能直接改；"
                 "进入设计模式后会先复制一份到当前账套，改那份")
    if tpl.book_id != book_id:
        # 别人的模板与不存在的模板一样回 404，理由见 _visible_template。
        raise HTTPException(404, "报表模板不存在")
    return tpl


@router.post("/templates/{template_id}/fork", status_code=201)
def fork_template(template_id: int, book_id: Optional[int] = None,
                  user: SysUser = Depends(get_current_user), db: Session = Depends(get_db),
                  book: Book = Depends(get_my_book)):
    """把一张模板复制一份归属当前账套 —— **写时复制的那一步**。

    进入设计模式时前端先调这里，于是「改内置表」的实际动作落在副本上：

    * 目标**已经不是**内置基线（``book_id`` 就是当前账套）→ 什么都不做，
      直接返回它自己。反复进设计模式不会复制出一堆副本。
    * 目标是内置基线，而本账套**已有**同 code 的副本 → 返回那个副本。
      第二次改的还是同一份，不会越改越多。
    * 目标是内置基线且本账套没有副本 → 真复制一份（行、列、单元格公式
      整份带走，``_clone_skeleton``），归属当前账套。

    副本的 ``code`` 沿用基线的，``is_builtin`` 为 false（它是账套自己维护的
    表，可以删 —— 删了就回到看基线）。列表按 code 去重时副本优先，于是
    用户改过的那份就是他看到的那份，而别的账套继续看干净的基线。

    为什么不做成「保存时才发现要复制」：那样每个写端点都得在响应里回传
    新 id，前端十几处都要跟着切。现在把复制收在**进入设计模式这一个
    时刻**，写路径就完全不用知道写时复制这件事了。
    """
    _require_template_write(db, user, book_id)
    src = _visible_template(db, template_id, book)
    if src.book_id == book.id:
        return {"id": src.id, "code": src.code, "name": src.name, "forked": False}
    existing = db.scalar(select(ReportTemplate).where(
        ReportTemplate.book_id == book.id, ReportTemplate.code == src.code))
    if existing is not None:
        return {"id": existing.id, "code": existing.code, "name": existing.name,
                "forked": False}
    tpl = ReportTemplate(code=src.code, book_id=book.id, name=src.name,
                         type=src.type, remark=src.remark, is_builtin=False)
    db.add(tpl)
    db.flush()
    _clone_skeleton(db, src.id, tpl.id)
    log(db, action="FORK_REPORT_TEMPLATE", entity="report_template", entity_id=tpl.id,
        user=user, book_id=book.id,
        detail=f"复制内置报表 {src.name}（{src.code}）到当前账套",
        before={"template_id": src.id, "code": src.code, "book_id": src.book_id},
        after={"template_id": tpl.id, "code": tpl.code, "book_id": tpl.book_id})
    db.commit()
    return {"id": tpl.id, "code": tpl.code, "name": tpl.name, "forked": True}


@router.get("/{template_id}/design")
def design(template_id: int, db: Session = Depends(get_db), book_id: Optional[int] = None,
           user: SysUser = Depends(require_report_reader), book: Book = Depends(get_my_book)):
    """模板的行 / 列 / 单元格公式结构。

    要求登录、至少一个账套授权（理由同 ``templates``），以及**这张模板对
    当前账套可见**。这里返回的是取数公式本身 —— 比报表名和科目名更能说明
    一家单位怎么算账，别的账套的模板不该流到这儿。
    """
    _ = user
    tpl = _visible_template(db, template_id, book)
    rows = db.scalars(select(ReportRow).where(ReportRow.template_id == template_id)
                      .order_by(ReportRow.row_no)).all()
    cols = db.scalars(select(ReportCol).where(ReportCol.template_id == template_id)
                      .order_by(ReportCol.col_no)).all()
    # 单元格也必须排序。rows / cols 排了、cells 没排，于是同一个模板两次
    # 打开可能给出不同顺序的公式列表 —— 堆顺序一变就变。设计页按地址渲染
    # 不在乎顺序，但这份返回是给调用方的事实描述，顺序不定就没法比对、
    # 没法缓存，写时复制那种「复制前后的结构必须逐条相等」的校验也会
    # 变成随机红。
    cells = db.scalars(select(ReportCell).where(ReportCell.template_id == template_id)
                      .order_by(ReportCell.row_no, ReportCell.col_no)).all()
    return {
        "id": tpl.id, "code": tpl.code, "name": tpl.name, "type": tpl.type,
        "rows": [{"row_no": r.row_no, "label": r.label, "indent": r.indent,
                  "bold": r.bold, "hidden": r.hidden} for r in rows],
        "cols": [{"col_no": c.col_no, "label": c.label, "kind": c.kind,
                  "hidden": c.hidden, "width": c.width} for c in cols],
        "cells": [{"row_no": c.row_no, "col_no": c.col_no, "formula": c.formula} for c in cells],
    }


@router.get("/{template_id}/render")
def render(template_id: int, book_id: int, period_id: int, db: Session = Depends(get_db),
           book: Book = Depends(get_my_book), include_unposted: bool = Query(False)):
    from app.reports.evaluate import evaluate

    # 求值前先确认这张模板对当前账套可见：别的账套的公式不该在这里被算出来。
    _visible_template(db, template_id, book)
    period = db.get(Period, period_id)
    if period is None or period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    data = evaluate(db, template_id, book, period, include_unposted)

    saved = db.scalars(select(ReportResult).where(
        ReportResult.template_id == template_id, ReportResult.book_id == book.id,
        ReportResult.period_id == period.id)).all()
    data["cached_at"] = max((r.created_at for r in saved), default=None)
    return data


def _check_formula(src: str, row_no: int, col_no: int) -> None:
    """存之前先过一遍**语法**（只解析，不求值，所以不碰数据库）。

    为什么不能只靠渲染期兜底：渲染是**逐格**的，一个语法错的格子只是那一格
    显示一行红字，可它已经把整张表变成「有错」状态；而用户改完公式保存时
    什么提示都没有，第二天才在报表页看到。存的时候拦一下，错误出现在
    **输入的那一刻**，400 + 能照着改的文案。

    只查语法是有意的：``科目("还没建的编码", ...)`` 语法没问题，只是现在
    取不到数 —— 那是求值期的事，拦住它会让「先建公式再补科目」这种正常
    做法没法保存。
    """
    if not (src or "").strip():
        return
    try:
        Parser(src).parse()
    except FormulaError as e:
        raise HTTPException(
            400, f"第 {row_no} 行 / {col_no} 列的公式语法错误：{e}") from None


@router.put("/{template_id}/cells")
def save_cells(template_id: int, payload: list[CellIn], db: Session = Depends(get_db),
               book_id: Optional[int] = None, user=Depends(get_current_user)):
    """改单元格公式。门禁与归属见 ``_require_owned_template``：
    必须是**当前账套自己那份**模板，官方内置基线不接受直接写入
    （前端在进入设计模式时已经先 ``fork`` 过一份，改的是副本）。

    公式本身先过一遍语法（``_check_formula``）：语法错的公式存不进去，
    而不是存进去、等报表渲染时才在某一格蹦出一行红字。
    """
    _require_owned_template(db, template_id, book_id, user)
    for item in payload:
        _check_formula(item.formula, item.row_no, item.col_no)

    # P3-10：改公式必须留 before。公式写错一个字符，报表就会悄无声息地
    # 少算一列，而页面照样渲染得出来 —— 没有原公式根本无从判断是哪改坏的。
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for item in payload:
        cell = db.scalar(select(ReportCell).where(
            ReportCell.template_id == template_id, ReportCell.row_no == item.row_no,
            ReportCell.col_no == item.col_no))
        key = f"r{item.row_no}c{item.col_no}"
        if cell:
            before[key] = cell.formula
            cell.formula = item.formula
        else:
            db.add(ReportCell(template_id=template_id, row_no=item.row_no,
                              col_no=item.col_no, formula=item.formula))
        after[key] = item.formula
    db.flush()

    if before or after:
        log(db, action="UPDATE_REPORT_CELLS", entity="report_template",
            entity_id=template_id, user=user, book_id=book_id,
            detail=f"修改报表 {template_id} 的 {len(after)} 个单元格公式",
            before=before, after=after)
    db.commit()
    return {"ok": True, "updated": len(payload)}


@router.post("/{template_id}/persist")
def persist(template_id: int, book_id: int, period_id: int, db: Session = Depends(get_db),
            book: Book = Depends(get_my_book),
            _link: UserBook = Depends(require_book_write)):
    """把一次求值结果落库，便于导出和排查。"""
    from app.reports.evaluate import evaluate

    # 先确认这张报表对当前账套可见，**再**碰期间与求值：``template_id`` 是
    # 前端传来的任意整数，只判「存在」的话，任何账套都能拿着别人的 id 在自己
    # 账套里落一份别人口径的报表结果（落库的行/列/公式全是那家的）。
    # 顺序与同文件的 ``render`` 一致；不存在与不是你的落在同一句 404 上，
    # 这条接口才不是跨账套的存在性探测器。
    _visible_template(db, template_id, book)
    period = db.get(Period, period_id)
    if period is None or period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    data = evaluate(db, template_id, book, period, False)

    db.query(ReportResult).filter(
        ReportResult.template_id == template_id, ReportResult.book_id == book.id,
        ReportResult.period_id == period.id
    ).delete(synchronize_session=False)

    from decimal import Decimal

    for row in data["rows"]:
        for idx, cell in enumerate(row["cells"]):
            col_no = data["cols"][idx]["col_no"]
            db.add(ReportResult(
                template_id=template_id, book_id=book.id, period_id=period.id,
                row_no=row["row_no"], col_no=col_no, formula=cell["formula"],
                value=Decimal(cell["value"]) if cell["value"] else None,
                error=cell["error"],
            ))
    db.commit()
    return {"saved": True, "errors": data["errors"]}


# ===================================================================== 外部模板迁移


@router.post("/templates/import/preview")
def preview_template_import(file: UploadFile = File(...), book_id: Optional[int] = None,
                            user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """只解析、检查当前账套科目，不创建模板。"""
    _require_template_write(db, user, book_id)
    # 必须在 try **之前**导入：``except TemplateImportError`` 子句要用的名字
    # 得先在作用域内就位，否则真正走到这条异常路径时会先撞 NameError。
    from app.reports.importer import (MAX_TEMPLATE_BYTES, TemplateImportError,
                                      parse_ufo_report)
    try:
        raw = read_upload(file, limit=MAX_TEMPLATE_BYTES, label="报表模板", hint="单个模板最大 5 MB")
        parsed = parse_ufo_report(raw, file.filename or "")
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except TemplateImportError as exc:
        raise HTTPException(400, str(exc)) from exc
    book_subjects = list(db.scalars(select(Subject).where(Subject.book_id == book_id).order_by(Subject.code)))
    by_code = {subject.code: subject for subject in book_subjects}
    existing = db.scalar(select(ReportTemplate).where(
        ReportTemplate.book_id == book_id,
        ReportTemplate.code == f"IMPORTED_{parsed['source']['fingerprint']}"))
    document = {key: parsed[key] for key in ("name", "type", "source", "rows", "cols", "cells", "issues")}
    return {
        "document": document, "stats": parsed["stats"], "warnings": parsed["warnings"],
        "subjects": [{"code": code, "name": by_code[code].name if code in by_code else "",
                      "matched": code in by_code} for code in parsed["subject_codes"]],
        "available_subjects": [{"code": subject.code, "name": subject.name} for subject in book_subjects],
        "existing": {"id": existing.id, "name": existing.name} if existing else None,
    }


@router.post("/templates/import", status_code=201)
def import_templates(payload: ImportTemplates, book_id: Optional[int] = None,
                     user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """先校验整批，再在一个事务内新建模板；不覆盖内置或已有报表。"""
    _require_template_write(db, user, book_id)
    from app.reports.importer import subject_codes, validate_formula
    book_codes = set(db.scalars(select(Subject.code).where(Subject.book_id == book_id)))
    validated = []
    seen_codes: set[str] = set()
    for item in payload.templates:
        code = f"IMPORTED_{item.source.fingerprint}"
        if code in seen_codes:
            raise HTTPException(409, "同一批次包含重复模板，请只选择 .rep / .rbk 中的一份")
        seen_codes.add(code)
        # 判重按**账套**：code 是文件内容指纹，隔离之前整个系统只能导一次，
        # 现在每个账套各导各的互不冲突（这正是部分唯一索引
        # UNIQUE(book_id, code) 解开的那个老限制）。
        if db.scalar(select(ReportTemplate.id).where(
                ReportTemplate.book_id == book_id, ReportTemplate.code == code)):
            raise HTTPException(409, f"「{item.name}」已经导入，请打开已有模板")
        row_numbers = {row.row_no for row in item.rows}
        col_numbers = {col.col_no for col in item.cols}
        if row_numbers != set(range(1, len(item.rows) + 1)) or col_numbers != set(range(1, len(item.cols) + 1)):
            raise HTTPException(400, "模板的行列编号必须连续且不能重复")
        if item.cols[0].kind != "STATIC":
            raise HTTPException(400, "模板第一列必须是项目列")
        mapping = item.subject_mapping
        for original, target in mapping.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", original) or target not in book_codes:
                raise HTTPException(400, f"科目映射 {original} → {target} 无效")
        cells, seen_cells = [], set()
        fixed_cols = {col.col_no for col in item.cols if col.kind in NON_FORMULA_KINDS}
        for cell in item.cells:
            coord = (cell.row_no, cell.col_no)
            if coord in seen_cells or cell.row_no not in row_numbers or cell.col_no not in col_numbers or cell.col_no in fixed_cols:
                raise HTTPException(400, "模板单元格重复、越界或位于固定列")
            seen_cells.add(coord)
            formula = re.sub(r'科目\("([^"\\]+)",',
                             lambda match: f'科目("{mapping.get(match.group(1), match.group(1))}",', cell.formula)
            try:
                validate_formula(formula, row_numbers, col_numbers)
            except FormulaError as exc:
                raise HTTPException(400, f"「{item.name}」第 {cell.row_no} 行 / {cell.col_no} 列：{exc}") from exc
            cells.append({"row_no": cell.row_no, "col_no": cell.col_no, "formula": formula})
        missing = sorted(set(subject_codes(cells)) - book_codes)
        if (item.issues or missing) and not payload.accept_warnings:
            raise HTTPException(400, "模板仍有待配置公式或未匹配科目，请先核对并确认提示")
        validated.append((item, code, cells, missing))

    created = []
    for item, code, cells, missing in validated:
        notes = [f"导入自 {item.source.format}：{item.source.filename}"]
        if item.issues:
            notes.append(f"{len(item.issues)} 个金额格待配置公式")
        if missing:
            notes.append(f"{len(missing)} 个科目待匹配")
        template = ReportTemplate(code=code, book_id=book_id, name=item.name, type=item.type,
                                  remark="；".join(notes), is_builtin=False)
        db.add(template)
        db.flush()
        db.add_all([ReportRow(template_id=template.id, **row.model_dump()) for row in item.rows])
        db.add_all([ReportCol(template_id=template.id, **col.model_dump()) for col in item.cols])
        db.add_all([ReportCell(template_id=template.id, **cell) for cell in cells])
        db.flush()
        log(db, action="IMPORT_REPORT_TEMPLATE", entity="report_template", entity_id=template.id,
            user=user, book_id=book_id, detail=template.remark,
            after={"name": item.name, "rows": len(item.rows), "cols": len(item.cols),
                   "formulas": len(cells), "pending_formulas": len(item.issues),
                   "missing_subjects": missing, "subject_mapping": item.subject_mapping})
        created.append({"id": template.id, "name": template.name, "type": template.type,
                        "pending_formulas": len(item.issues), "missing_subjects": len(missing)})
    db.commit()
    return {"created": created, "count": len(created)}


# ===================================================================== 模板管理

from pydantic import BaseModel, Field  # noqa: E402


class ReportTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    remark: str = ""
    # 从哪个内置模板复制行列骨架；为空则建一张空白表
    base_code: Optional[str] = "BALANCE_SHEET"


class ReportTemplatePatch(BaseModel):
    name: Optional[str] = None
    remark: Optional[str] = None


class ReportRowPatch(BaseModel):
    label: Optional[str] = None
    indent: Optional[int] = None
    bold: Optional[bool] = None
    hidden: Optional[bool] = None
    style: Optional[dict] = None


class ReportColPatch(BaseModel):
    label: Optional[str] = None
    kind: Optional[str] = None
    hidden: Optional[bool] = None
    width: Optional[int] = None


#: 行号位移的临时偏移。必须够大，确保搬走的行号不会撞上任何现存行号。
_ROW_SHIFT = 100_000


@contextmanager
def _shift_rows(db: Session, template_id: int, at: int, direction: int):
    """在第 ``at`` 行处插入/删除一行，把后缀整体平移一格。

    ``uq_report_row`` / ``uq_report_cell`` 是**即时**唯一约束，直接把 5 改成 6
    会在中间态与还停在 6 的行撞库（Postgres 不保证 UPDATE 的执行顺序）。
    这里先整体挪到 ``_ROW_SHIFT`` 之外的空档，flush，再落到最终行号。
    """
    rows = list(db.scalars(
        select(ReportRow).where(ReportRow.template_id == template_id)).all())
    cells = list(db.scalars(
        select(ReportCell).where(ReportCell.template_id == template_id)).all())

    moved_rows = [r for r in rows if r.row_no >= at]
    moved_cells = [c for c in cells if c.row_no >= at]

    # 阶段一：整段挪进空档
    for r in moved_rows:
        r.row_no += _ROW_SHIFT
    for c in moved_cells:
        c.row_no += _ROW_SHIFT
    db.flush()

    try:
        yield
        db.flush()
        # 阶段二：先退回空档，再按 direction 落到最终行号。
        # 注意是「-SHIFT + direction」，不能直接 += direction，否则会停在空档里。
        for r in moved_rows:
            r.row_no = r.row_no - _ROW_SHIFT + direction
        for c in moved_cells:
            c.row_no = c.row_no - _ROW_SHIFT + direction
        db.flush()
    except Exception:
        db.rollback()
        raise


@contextmanager
def _shift_cols(db: Session, template_id: int, at: int, direction: int):
    """在第 ``at`` 列处插入/删除一列，把后缀整体平移一格。

    和 :func:`_shift_rows` 同一个理由：``uq_report_col`` / ``uq_report_cell`` 是即时
    唯一约束，直接改列号会在中间态撞库。
    """
    cols = list(db.scalars(
        select(ReportCol).where(ReportCol.template_id == template_id)).all())
    cells = list(db.scalars(
        select(ReportCell).where(ReportCell.template_id == template_id)).all())

    moved_cols = [c for c in cols if c.col_no >= at]
    moved_cells = [c for c in cells if c.col_no >= at]

    for c in moved_cols:
        c.col_no += _ROW_SHIFT
    for c in moved_cells:
        c.col_no += _ROW_SHIFT
    db.flush()

    try:
        yield
        db.flush()
        for c in moved_cols:
            c.col_no = c.col_no - _ROW_SHIFT + direction
        for c in moved_cells:
            c.col_no = c.col_no - _ROW_SHIFT + direction
        db.flush()
    except Exception:
        db.rollback()
        raise


def _rewrite_formulas(db: Session, template_id: int, *, row_shift=None, col_shift=None,
                      drop_row: int | None = None, drop_col: int | None = None) -> int:
    """行列位移后，把整张表的公式引用同步改一遍。

    要改的是**全部**单元格，不只是被挪动的那些：第 5 行的公式完全可能引用
    第 20 行。只改被挪的格子，另一半引用会静默失准。

    ``drop_row`` / ``drop_col`` 是被删掉的那一行/列 —— 指向它的引用按 0
    处理而不是跟着上移（审计 P1-13：以前删第 35 行，引用 ``B35`` 的公式会
    静默指向原第 34 行，报表照常渲染出一个错误的数字）。

    返回改写条数（公式为空或没被引用影响的不算）。
    """
    touched = 0
    for c in db.scalars(select(ReportCell)
                        .where(ReportCell.template_id == template_id)).all():
        new = rewrite_refs(c.formula, row_shift=row_shift, col_shift=col_shift,
                           drop_row=drop_row, drop_col=drop_col)
        if new != c.formula:
            c.formula = new
            touched += 1
    return touched


def _clone_skeleton(db: Session, src_id: int, dst_id: int) -> None:
    for r in db.scalars(select(ReportRow).where(ReportRow.template_id == src_id)).all():
        db.add(ReportRow(template_id=dst_id, row_no=r.row_no, label=r.label,
                         indent=r.indent, bold=r.bold, hidden=r.hidden, style=r.style))
    for c in db.scalars(select(ReportCol).where(ReportCol.template_id == src_id)).all():
        db.add(ReportCol(template_id=dst_id, col_no=c.col_no, label=c.label,
                         kind=c.kind, hidden=c.hidden, width=c.width))
    for c in db.scalars(select(ReportCell).where(ReportCell.template_id == src_id)).all():
        db.add(ReportCell(template_id=dst_id, row_no=c.row_no, col_no=c.col_no,
                          formula=c.formula))


@router.post("/templates", status_code=201)
def create_template(payload: ReportTemplateIn, book_id: Optional[int] = None,
                    user: SysUser = Depends(get_current_user), db: Session = Depends(get_db),
                    book: Book = Depends(get_my_book)):
    """新建报表模板：复制一份表结构，财务在此基础上改公式。

    新模板**一出生就归属当前账套**（``book_id``），不是「先全局、再按需
    隔离」。自建模板没有「先让所有人看到一份原版」的必要，留个归属更省心。
    """
    _require_template_write(db, user, book_id)
    tpl = ReportTemplate(code=f"CUSTOM_{uuid.uuid4().hex[:10].upper()}",
                         book_id=book.id, name=payload.name, type="CUSTOM",
                         remark=payload.remark, is_builtin=False)
    db.add(tpl)
    db.flush()
    # base_code 按 code 找，且**只看当前账套看得见的**：本账套副本优先于
    # 同 code 的官方基线（与列表去重同一个口径，否则「以利润表为基础新建」
    # 可能复制到另一份去）。
    #
    # 排序必须**显式**写死，``.order_by(book_id.desc())`` 是反的：PostgreSQL 的
    # DESC 默认 NULLS FIRST，而官方基线恰恰是 ``book_id IS NULL``，于是
    # db.scalar() 取到的是出厂基线，把上面那句「副本优先」原样反过来 ——
    # 账套 fork 改过的那张利润表完全不影响新建出来的表，用户看着「以我改过
    # 的利润表为基础」得到一份原版，还找不到哪里出了错（本项目 PG 上实测过）。
    #
    # ``book_id IS NULL`` 对 NULL 行算出 true、对本账套行算出 false，ASC 排序下
    # false 在前（PG 的 ASC 默认 NULLS LAST）—— 「先本账套、后基线」由排序保证，
    # 不依赖 id 大小这类偶然。写法与 ``api/printing.py::_template`` 的默认模板
    # 取用同一个先例。后面的 book_id DESC 只用于「同 code 有多份本账套副本」时
    # 取最近建的那张，与列表去重的「副本覆盖基线」口径一致。
    base = db.scalar(select(ReportTemplate).where(
        ReportTemplate.code == payload.base_code).where(
        (ReportTemplate.book_id.is_(None)) | (ReportTemplate.book_id == book.id)
    ).order_by(ReportTemplate.book_id.is_(None), ReportTemplate.book_id.desc())) \
        if payload.base_code else None
    if base is not None:
        _clone_skeleton(db, base.id, tpl.id)
    db.commit()
    return {"id": tpl.id, "code": tpl.code, "name": tpl.name, "type": tpl.type}


@router.post("/templates/{template_id}/copy")
def copy_template(template_id: int, name: Optional[str] = None, book_id: Optional[int] = None,
                  user: SysUser = Depends(get_current_user), db: Session = Depends(get_db),
                  book: Book = Depends(get_my_book)):
    """复制模板（含全部公式）到当前账套。改公式前先留底，财务自己就能做。

    源模板必须是当前账套看得见的（官方基线或本账套自己的）；副本一律归属
    当前账套。code 用新的 UUID，所以同一个源可以在不同账套各复制一份而
    不撞唯一索引。
    """
    _require_template_write(db, user, book_id)
    src = _visible_template(db, template_id, book)
    tpl = ReportTemplate(code=f"CUSTOM_{uuid.uuid4().hex[:10].upper()}",
                         book_id=book.id, name=name or f"{src.name} 副本", type="CUSTOM",
                         remark=src.remark, is_builtin=False)
    db.add(tpl)
    db.flush()
    _clone_skeleton(db, src.id, tpl.id)
    db.commit()
    return {"id": tpl.id, "code": tpl.code, "name": tpl.name}


@router.patch("/templates/{template_id}")
def update_template(template_id: int, payload: ReportTemplatePatch,
                    book_id: Optional[int] = None, user: SysUser = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    tpl = _require_owned_template(db, template_id, book_id, user)
    if payload.name is not None:
        tpl.name = payload.name
    if payload.remark is not None:
        tpl.remark = payload.remark
    db.commit()
    return {"id": tpl.id, "name": tpl.name, "remark": tpl.remark}


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, book_id: Optional[int] = None,
                    user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """删报表。删掉的只能是本账套自己那份；官方内置基线永远留着。

    删掉的是内置表的**副本**时，那个账套就回到看基线的状态 —— 这正是
    「我不想要我的改动」的那条退路，所以副本的 ``is_builtin`` 是 false。
    """
    tpl = _require_owned_template(db, template_id, book_id, user)
    # 兜底，不再是主路径：基线（book_id IS NULL）已在 _require_owned_template
    # 里以 400 挡下，走不到这里 —— 留给「book_id 非空却是内置模板」这种
    # 说不清来路的存量/脏数据。留着是因为它便宜且 fail-closed，而删掉它
    # 会让真出现这种数据时直接删掉一张所有账套共看的表。
    if tpl.is_builtin:
        raise HTTPException(400, "内置报表模板不允许删除，可以先复制一份再改")
    db.delete(tpl)
    db.commit()
    return {"ok": True}


@router.patch("/templates/{template_id}/rows/{row_no}")
def update_row(template_id: int, row_no: int, payload: ReportRowPatch, book_id: Optional[int] = None,
               user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """改行的标签/缩进/加粗/隐藏。

    只靠插删行拼不出想要的表 —— 「合计行加粗」「几行不打印」都得能单独调。
    """
    _require_owned_template(db, template_id, book_id, user)
    row = db.scalar(select(ReportRow).where(ReportRow.template_id == template_id,
                                             ReportRow.row_no == row_no))
    if row is None:
        raise HTTPException(404, "行不存在")
    if payload.label is not None:
        row.label = payload.label[:128]
    if payload.indent is not None:
        row.indent = max(0, min(6, payload.indent))
    if payload.bold is not None:
        row.bold = payload.bold
    if payload.hidden is not None:
        row.hidden = payload.hidden
    if payload.style is not None:
        row.style = payload.style
    db.commit()
    return {"ok": True, "row_no": row_no}


@router.post("/templates/{template_id}/rows")
def add_row(template_id: int, payload: dict, book_id: Optional[int] = None,
            user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """在指定位置插入一行，下方行的公式单元格整体下移，公式内容不变。"""
    _require_owned_template(db, template_id, book_id, user)
    rows = list(db.scalars(select(ReportRow).where(ReportRow.template_id == template_id)
                           .order_by(ReportRow.row_no)).all())
    at = int(payload.get("row_no") or (rows[-1].row_no + 1 if rows else 1))
    if at <= 0:
        raise HTTPException(400, "row_no 必须为正整数")

    with _shift_rows(db, template_id, at, direction=+1):
        db.add(ReportRow(template_id=template_id, row_no=at,
                         label=str(payload.get("label", "")),
                         indent=int(payload.get("indent", 0) or 0),
                         bold=bool(payload.get("bold", False)),
                         hidden=bool(payload.get("hidden", False)),
                         style=payload.get("style") or {}))
    shifted = _rewrite_formulas(db, template_id, row_shift=(at, +1))
    db.commit()
    return {"ok": True, "row_no": at, "formulas_shifted": shifted}


@router.delete("/templates/{template_id}/rows/{row_no}")
def delete_row(template_id: int, row_no: int, book_id: Optional[int] = None,
               user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_owned_template(db, template_id, book_id, user)
    row = db.scalar(select(ReportRow).where(ReportRow.template_id == template_id,
                                             ReportRow.row_no == row_no))
    if row is None:
        raise HTTPException(404, "行不存在")
    # 目标行和它的公式单元格必须在位移之前抓住 —— 位移之后它们已经不在 row_no 上了
    doomed = list(db.scalars(select(ReportCell).where(
        ReportCell.template_id == template_id, ReportCell.row_no == row_no)).all())
    with _shift_rows(db, template_id, row_no, direction=-1):
        db.delete(row)
        for c in doomed:
            db.delete(c)
    # 删除用 at+1 做位移（被删掉的行号本身不参与映射），同时把 drop_row 交给
    # 改写器：引用被删行的公式按 0 处理，绝不静默别名到相邻行（审计 P1-13）。
    shifted = _rewrite_formulas(db, template_id, row_shift=(row_no + 1, -1),
                                drop_row=row_no)
    db.commit()
    return {"ok": True, "deleted_cells": len(doomed), "formulas_shifted": shifted}


# --------------------------------------------------------------------- 列
#
# 「自由可调」不只是改公式：利润表想加一列「上年同期」、资产负债表想多拆一个
# 层级，都得能自己加列。所以列和行一样支持增删改，且同样要同步改写公式引用
# （插一列会让 B 列变成 C 列，公式里的 B10 得跟着走）。


@router.post("/templates/{template_id}/cols", status_code=201)
def add_col(template_id: int, payload: dict, book_id: Optional[int] = None,
            user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """在指定位置插入一列。默认沿用左边那一列的取数口径。"""
    _require_owned_template(db, template_id, book_id, user)
    cols = list(db.scalars(select(ReportCol).where(ReportCol.template_id == template_id)
                           .order_by(ReportCol.col_no)).all())
    at = int(payload.get("col_no") or (cols[-1].col_no + 1 if cols else 1))
    if at <= 1:
        raise HTTPException(400, "列号从 2 开始，第 1 列是固定的「项目」列")

    # 口径默认「沿用左边最近的一个数值列」；左边一个都没有（插在标签列右边，
    # 最常见的用法）就退回整张表的第一个数值列。绝不能继承不带口径的列 ——
    # STATIC 是固定的「项目」标签列，LINE_NO 是「行次」列（资产负债表的
    # 8 列版式里各有一个）。继承它们会造出一个既不显示、也永远算不出数的
    # 幽灵列：看着像插成功了，导出却没有这一列，或者更糟 —— 行次列里的
    # 「1」「31」被当成金额显示出来。
    value_cols = [c for c in cols if c.kind not in NON_FORMULA_KINDS]
    left = [c for c in value_cols if c.col_no < at]
    neighbour = left[-1] if left else (value_cols[0] if value_cols else None)
    kind = str(payload.get("kind") or (neighbour.kind if neighbour else "BALANCE_CLOSING"))
    if kind in NON_FORMULA_KINDS or kind not in COL_KINDS:
        raise HTTPException(400, f"未知的取数口径 {kind}，可选：{', '.join(sorted(COL_KINDS))}")

    with _shift_cols(db, template_id, at, direction=+1):
        db.add(ReportCol(template_id=template_id, col_no=at,
                         label=str(payload.get("label") or "新列"),
                         kind=kind,
                         hidden=bool(payload.get("hidden", False)),
                         width=int(payload.get("width") or 140)))
    shifted = _rewrite_formulas(db, template_id, col_shift=(at, +1))
    db.commit()
    return {"ok": True, "col_no": at, "letter": no_to_col(at), "formulas_shifted": shifted}


@router.patch("/templates/{template_id}/cols/{col_no}")
def update_col(template_id: int, col_no: int, payload: ReportColPatch, book_id: Optional[int] = None,
               user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_owned_template(db, template_id, book_id, user)
    col = db.scalar(select(ReportCol).where(ReportCol.template_id == template_id,
                                             ReportCol.col_no == col_no))
    if col is None:
        raise HTTPException(404, "列不存在")
    if payload.kind is not None and payload.kind != col.kind:
        if payload.kind not in COL_KINDS:
            raise HTTPException(400, f"未知的取数口径 {payload.kind}")
        col.kind = payload.kind
    if payload.label is not None:
        col.label = payload.label[:64]
    if payload.hidden is not None:
        col.hidden = payload.hidden
    if payload.width is not None:
        col.width = max(60, min(600, payload.width))
    db.commit()
    return {"ok": True, "col_no": col_no}


@router.delete("/templates/{template_id}/cols/{col_no}")
def delete_col(template_id: int, col_no: int, book_id: Optional[int] = None,
               user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_owned_template(db, template_id, book_id, user)
    col = db.scalar(select(ReportCol).where(ReportCol.template_id == template_id,
                                             ReportCol.col_no == col_no))
    if col is None:
        raise HTTPException(404, "列不存在")
    if col.kind in NON_FORMULA_KINDS:
        raise HTTPException(
            400, "「项目」列与「行次」列是固定列，不能删"
            if col.kind == "LINE_NO" else "「项目」列是固定列，不能删")
    # 「至少留一个数值列」必须数**真正取数的**列：把 STATIC/LINE_NO 算进去，
    # 会让一张只剩标签列和行次列的表也通过校验 —— 那种表一格数都算不出来，
    # 却能在界面上看着像一张正常的表。
    remaining = db.scalar(
        select(func.count()).select_from(ReportCol).where(
            ReportCol.template_id == template_id, ReportCol.col_no != col_no,
            ReportCol.kind.not_in(tuple(NON_FORMULA_KINDS))))
    if not remaining:
        raise HTTPException(400, "至少要保留一个数值列")

    doomed = list(db.scalars(select(ReportCell).where(
        ReportCell.template_id == template_id, ReportCell.col_no == col_no)).all())
    with _shift_cols(db, template_id, col_no, direction=-1):
        db.delete(col)
        for c in doomed:
            db.delete(c)
    # 列与行是同一个 bug（审计 P1-13）：删列以前也只做了位移，
    # 引用被删列的公式会静默指向左边挪过来的那一列。
    shifted = _rewrite_formulas(db, template_id, col_shift=(col_no + 1, -1),
                                drop_col=col_no)
    db.commit()
    return {"ok": True, "deleted_cells": len(doomed), "formulas_shifted": shifted}
