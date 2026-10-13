"""打印与导出。

两条输出通道：

* **Excel** —— 用于二次加工、数据核对、导入别的系统。
* **HTML**  —— 用于「所见即所得」的预览与打印。预览 iframe 和最终打印走的是
  同一个渲染函数，所以模板改了什么，打出来就是什么样，不存在两套排版。

打印模板存在 ``print_template.content``（JSON）里，财务可以在前端设计器里
自助改版式，不需要改代码。
"""
from __future__ import annotations

import functools
import html
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.vouchers import _serialize
from app.core.db import get_db
from app.core.deps import get_book_link, get_current_user, get_my_book, require_book_write
from app.models import Book, Period, PrintTemplate, SysUser, UserBook, Voucher
from app.printing import excel as xl
from app.printing import preview as pv
from app.printing.template import (
    DEFAULT_TEMPLATES,
    PAPERS,
    SCOPES,
    render_table_page,
    render_voucher_sheet,
    split_voucher,
    validate_template,
    voucher_rows_per_page,
    voucher_slots_per_page,
)

router = APIRouter(prefix="/api/books/{book_id}/print", tags=["打印与导出"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _deep_merge(base, override):
    """模板补丁叠加：递归合并，两边都是 dict 时逐层合并。"""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else v
        return out
    return base if override is None else override


def _filename(name: str) -> str:
    return "attachment; filename*=UTF-8''" + urllib.parse.quote(name)


def _period(db: Session, book_id: int, period_id: int | None) -> Period:
    stmt = select(Period).where(Period.book_id == book_id)
    p = db.get(Period, period_id) if period_id else db.scalar(
        stmt.order_by(Period.ord.desc()).limit(1))
    if p is None:
        raise HTTPException(404, "会计期间不存在")
    return p


def _visible_templates(book_id: int):
    """这个账套**看得见**的模板：本账套的，加上全局兜底的。

    ``print_template.book_id IS NULL`` 是「全局兜底」：迁移之前建的存量模板
    没有任何一个账套真的拥有它们（见 ``models/printing.py``），把它们回填到
    任意一个账套等于替人断言归属。算进可见范围是让老数据继续能打；它们
    **不参与**「设为默认」的互相清除，是让新数据从一开始就有主。
    """
    # **不能用 ``book_id.in_([book_id, None])``** —— 这是本次实测踩到的坑：
    # SQL 是三值逻辑，``NULL IN (6, NULL)`` 求值成 ``NULL``（不是 true），
    # 于是全局兜底模板会被 WHERE **悄悄滤掉**，一条都不返回。
    # 那个写法只在「没有 NULL 行」时与本式等价，而本式的全部意义恰恰是那几行。
    return or_(PrintTemplate.book_id == book_id, PrintTemplate.book_id.is_(None))


def _scoped_template(db: Session, book: Book, template_id: int) -> PrintTemplate:
    """按 id 取模板，并且校验它属于当前账套（全局兜底模板也算可见）。

    ``template_id`` 是前端传来的任意整数，**不是授权凭据**：没有这一层校验，
    任何账套都能拿着别人的 id 进打印页。口径必须与 ``_template`` 的取用口径
    一致（同一份 ``_visible_templates``），否则会出现「列表里看不见、却能
    按 id 打出来」这种更糟的状态。
    """
    t = db.get(PrintTemplate, template_id)
    if t is None or t.book_id not in (book.id, None):
        # 跨账套与不存在返回同一个 404：不区分「没有」和「不是你的」，
        # 否则这条接口本身就成了一个跨账套的存在性探测器。
        raise HTTPException(404, "打印模板不存在")
    return t


def _template(db: Session, book: Book, scope: str, template_id: int | None) -> PrintTemplate:
    if template_id:
        return _scoped_template(db, book, template_id)
    t = db.scalar(
        select(PrintTemplate)
        .where(PrintTemplate.scope == scope, PrintTemplate.is_default.is_(True),
               _visible_templates(book.id))
        # 本账套模板优先、全局模板兜底。``book_id IS NULL`` 对 NULL 行算出
        # true、对本账套行算出 false，ASC 排序下 false 在前 —— 「先本账套、
        # 后全局」由排序保证，不依赖 id 大小这类偶然。
        .order_by(PrintTemplate.book_id.is_(None), PrintTemplate.id)
        .limit(1)
    )
    if t is None:
        # 兜底用内置默认值，保证任何时候都能打印
        class _Virtual:
            id = None
            name = "内置默认"
            remark = ""
            content = DEFAULT_TEMPLATES[scope]

        v = _Virtual()
        v.scope = scope          # 实例属性：validate_template 需要读 t.scope
        return v
    return t


def _tpl_dict(t) -> dict:
    return validate_template(t.scope, t.content or {})


# ===================================================================== 打印模板 CRUD


class TemplateIn(BaseModel):
    scope: str
    name: str
    paper: Optional[str] = None
    content: dict = Field(default_factory=dict)
    is_default: bool = False
    remark: str = ""


class TemplatePatch(BaseModel):
    name: Optional[str] = None
    paper: Optional[str] = None
    content: Optional[dict] = None
    is_default: Optional[bool] = None
    remark: Optional[str] = None


@router.get("/templates/meta")
def template_meta():
    """设计器需要的元信息：可选纸型、字段字典、默认模板。"""
    return {
        "scopes": list(SCOPES),
        "papers": [{"key": k, **v, "landscape": k.endswith("_LANDSCAPE")} for k, v in PAPERS.items()],
        "defaults": DEFAULT_TEMPLATES,
    }


@router.get("/templates")
def templates(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    # 按账套过滤：设计器列的是「这个账套能改的模板」。跨账套可见既会让
    # 别的账套看到本账套的版式，点下去也只能拿到 404（见 _scoped_template）。
    rows = db.scalars(
        select(PrintTemplate)
        .where(_visible_templates(book.id))
        .order_by(PrintTemplate.book_id.is_(None), PrintTemplate.scope, PrintTemplate.id)
    ).all()
    return [{"id": t.id, "scope": t.scope, "name": t.name, "paper": t.paper,
             "is_default": t.is_default, "remark": t.remark, "content": t.content,
             "book_id": t.book_id}
            for t in rows]


@router.get("/templates/{template_id}")
def template_detail(template_id: int, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db)):
    t = _scoped_template(db, book, template_id)
    merged = _tpl_dict(t)
    # book_id 与列表接口保持一致：设计器要能看出这张是本账套的模板还是
    # 全局兜底的老数据（后者改动会影响所有账套）。
    return {"id": t.id, "scope": t.scope, "name": t.name, "paper": t.paper,
            "is_default": t.is_default, "remark": t.remark, "content": merged,
            "book_id": t.book_id, "raw": t.content}


@router.post("/templates", status_code=201)
def create_template(payload: TemplateIn, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                    user: SysUser = Depends(get_current_user),
                    _link: UserBook = Depends(require_book_write)):
    # 门禁按「模板是全局资源」收紧（见 _require_global_template_write）；模板本身
    # 从这一版起归属本账套（book_id=book.id），门禁暂时不放宽。
    # require_book_write 仍然挂着：它让「每个写接口都挂了写依赖」保持机械可查
    # （tests/test_viewer_matrix.py 遍历路由表检查），与 books.py 的做法一致。
    _require_global_template_write(link, user)
    if payload.scope not in SCOPES:
        raise HTTPException(400, f"未知打印范围：{payload.scope}，可选：{'、'.join(SCOPES)}")
    try:
        content = validate_template(payload.scope, payload.content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    t = PrintTemplate(book_id=book.id, scope=payload.scope, name=payload.name,
                      paper=content.get("paper", "A4"),
                      content=content, is_default=payload.is_default, remark=payload.remark)
    db.add(t)
    db.flush()
    if t.is_default:
        _clear_other_defaults(db, t)
    _log(db, user, book.id, "CREATE_PRINT_TEMPLATE", t.id, f"新增打印模板 {t.name}")
    db.commit()
    db.refresh(t)
    return {"id": t.id, "scope": t.scope, "name": t.name, "content": t.content}


@router.put("/templates/{template_id}")
def update_template(template_id: int, payload: TemplatePatch, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                    user: SysUser = Depends(get_current_user),
                    _link: UserBook = Depends(require_book_write)):
    t = _scoped_template(db, book, template_id)
    _require_global_template_write(link, user)
    before = t.content
    # 局部更新：先把补丁叠到「已保存的模板」上，再叠加内置默认值兜底。
    # 直接把补丁丢给 validate_template 会让默认值里的 paper/margins 盖掉用户已保存的值。
    patched = _deep_merge(t.content or {}, payload.content) if payload.content is not None else (t.content or {})
    try:
        merged = validate_template(t.scope, patched)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if payload.name is not None:
        t.name = payload.name
    if payload.remark is not None:
        t.remark = payload.remark
    if payload.is_default is not None:
        t.is_default = payload.is_default
    t.content = merged
    t.paper = merged.get("paper", "A4")
    db.flush()
    if t.is_default:
        _clear_other_defaults(db, t)
    _log(db, user, book.id, "UPDATE_PRINT_TEMPLATE", t.id, f"修改打印模板 {t.name}",
         before={"content": before}, after={"content": merged})
    db.commit()
    return {"id": t.id, "content": t.content}


@router.delete("/templates/{template_id}")
def delete_template(template_id: int, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                    user: SysUser = Depends(get_current_user),
                    _link: UserBook = Depends(require_book_write)):
    t = _scoped_template(db, book, template_id)
    _require_global_template_write(link, user)
    name = t.name
    db.delete(t)
    _log(db, user, book.id, "DELETE_PRINT_TEMPLATE", template_id, f"删除打印模板 {name}")
    db.commit()
    return {"ok": True}


@router.post("/templates/{template_id}/copy")
def copy_template(template_id: int, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                  user: SysUser = Depends(get_current_user),
                  _link: UserBook = Depends(require_book_write)):
    """复制一份再改 —— 改模板前先留个底，财务自己就能做。"""
    src = _scoped_template(db, book, template_id)
    # 复制出来的仍是本账套的模板，同样按门禁收紧（否则「复制」就是个绕过点）。
    _require_global_template_write(link, user)
    t = PrintTemplate(book_id=book.id, scope=src.scope, name=f"{src.name} 副本",
                      paper=src.paper,
                      content=dict(src.content or {}), is_default=False,
                      remark=src.remark)
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "scope": t.scope, "name": t.name, "content": t.content}


def _clear_other_defaults(db: Session, keep: PrintTemplate) -> None:
    # 按 (book_id, scope) 分组：A 账套「设为默认」不该把 B 账套的默认模板
    # 一起清掉 —— 那等于「在 A 账套点一下，B 账套的版式就变回旧的了」。
    # book_id 为 NULL 的全局模板各自成组，跨账套互不影响。
    db.query(PrintTemplate).filter(
        PrintTemplate.scope == keep.scope, PrintTemplate.id != keep.id,
        PrintTemplate.is_default.is_(True),
        PrintTemplate.book_id == keep.book_id if keep.book_id is not None
        else PrintTemplate.book_id.is_(None)
    ).update({PrintTemplate.is_default: False}, synchronize_session=False)


def _log(db, user, book_id, action, entity_id, detail, before=None, after=None) -> None:
    from app.accounting.audit_helper import log

    log(db, action=action, entity="print_template", entity_id=entity_id, user=user,
        book_id=book_id, detail=detail, before=before, after=after)


def _require_global_template_write(link: UserBook, user: SysUser) -> None:
    """改打印模板需要本账套 OWNER（管理员放行）。

    **为什么不能只看「当前账套能不能写」**：模板内容进 ``<style>``（P0-2），
    改错一次，整批单据都要重打。``require_book_write`` 问的是「非 VIEWER 即可」，
    而「能改别人的模板」从来不是一个人被授权去做的事。对齐
    ``app/api/system.py`` 里 OWNER 的定义（「账套负责人，可改科目/模板/授权」）
    与 ``app/api/books.py::update_book`` 的先例。

    **``print_template.book_id`` 落地之后，这道门禁还不能放宽**，三个理由：

    1. **仍有全局模板。** 迁移之前的存量模板 ``book_id IS NULL``，它们照样流进
       每一个账套的打印页，而**任何一个**账套的 OWNER 都能改它们 —— 跨账套
       窃取这条路还开着，只是从「改一张」变成「改一张还没有主的」。
    2. **门禁本身不是这次新加的。** 它是上一批为了止血加的收紧；把它撤掉
       属于「拿掉一道已经生效的防线」，而放宽的前提是「已经有更好的那道」，
       不是「原来的问题看起来没那么严重」。
    3. **没有回归网。** 模板是 JSON 自由文本，样式注入只有渲染层二次夹取
       兜底；门禁是写入路径上唯一一道按**角色**判断的关卡。

    真正该放宽的时机是「全局模板退役」之后：所有 ``book_id IS NULL`` 的行
    都不再参与取用，写路径就真的只剩一个账套，可以回到「该账套 OWNER 以上」。

    fail-closed：角色认不出来一律当拒绝，不做「不是 VIEWER 就放行」的黑名单判断
    （见 ``frontend/src/stores/session.js::canWrite`` 里同一段推理）。
    """
    if user.is_admin:
        return
    if link.role != "OWNER":
        raise HTTPException(
            403, "打印模板是全局模板（所有账套的打印页都会用到），"
                 "只有账套所有者或管理员可以修改")


# ===================================================================== HTML 打印
#
# **预览和打印走的是同一个渲染函数 _render_scope()**，唯一区别是模板从哪来：
# 正式打印读数据库里已保存的模板，设计器预览直接吃前端传来的草稿 JSON。
# 这样「所见即所得」是结构上成立的 —— 世界上只有一套排版代码，没有第二处
# 可以跑偏。以前预览必须传 template_id 读已保存内容，所以改完版式要点保存
# 才看得到变化。


#: 打印页的 CSP。
#:
#: 打印页是**纯样式文档**：一份内联 ``<style>`` + 静态表格，没有脚本、没有外链
#: 资源、没有 ``<img>``/``<link>``/``<form>``。所以这里可以收得很紧：
#:
#: * ``default-src 'none'`` —— 除下面显式放行的，其余一律拒绝；
#: * ``style-src 'unsafe-inline'`` —— 版式全靠内联 ``<style>``（``_page_css``），
#:   ``'unsafe-inline'`` 是模板引擎的固有要求，不能去掉；
#: * ``script-src 'none'`` —— **这条是 P0-2 的兜底**：即使模板内容绕过校验
#:   （老数据、直接改库）注入进了 ``</style><script>…``，脚本也不会执行。
#:   模板校验是第一道防线，这一条是第二道，两道独立成立。
#:
#: ``frame-ancestors 'self'`` 防止打印页被外站 iframe 套娃。
#:
#: 注意 nginx 那层也该有全局 CSP（由部署工作包负责）。这一条是**应用层**的：
#: 即便反代没配、或有人直接用后端端口起服务，防护也仍然成立。
PRINT_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'none'; "
    "img-src 'none'; "
    "font-src 'none'; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)


def _html_response(doc: str) -> Response:
    """打印页响应。

    带 CSP 头（见 ``PRINT_CSP``）和 ``nosniff``。``nosniff`` 是配套的：
    没有 CSP 时它至少能挡住 MIME 嗅探这一条道，两者一起才是一道完整的门。
    """
    return Response(
        content=doc,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": PRINT_CSP,
            "X-Content-Type-Options": "nosniff",
            # 打印页内容随模板变，但不能被任何一层缓存住：模板改完重新预览必须
            # 立刻看到新版本，旧版式发出去才是账务事故。
            "Cache-Control": "no-store",
        },
    )


def _voucher_html(db: Session, book: Book, period: Period, tpl: dict,
                  status: Optional[str] = None, max_pages: Optional[int] = None,
                  note: str = "", voucher_ids: Optional[list[int]] = None) -> str:
    """记账凭证：A4 一页两张、A5 一页一张，每张最多 5 行摘要、超出续到下一张。

    ``max_pages`` 限的是**原始凭证张数**（只给预览用，保证改版式时的响应速度），
    不是分片数 —— 否则一张 12 行的凭证会把「只预览 3 张」变成只看它自己。
    """
    from app.accounting.vouchers import query_vouchers

    if voucher_ids is None:
        vouchers, _ = query_vouchers(db, book.id, from_period=period, to_period=period,
                                     status=status, page_size=5000)
    else:
        ids = list(dict.fromkeys(voucher_ids))
        vouchers = list(db.scalars(select(Voucher).where(
            Voucher.book_id == book.id, Voucher.id.in_(ids)
        )).all())
        if len(vouchers) != len(ids):
            raise HTTPException(404, "所选凭证不存在或不属于当前账套")
    vouchers = sorted(vouchers, key=lambda v: (v.voucher_date, v.voucher_no, v.id))
    shown = vouchers[:max_pages] if max_pages else vouchers

    period_codes = {}
    if voucher_ids is not None and shown:
        periods = db.scalars(select(Period).where(
                Period.book_id == book.id,
                Period.id.in_({v.period_id for v in shown}),
            )).all()
        period_codes = {p.id: p.code for p in periods}

    rows_per_page = voucher_rows_per_page(tpl)
    slots = voucher_slots_per_page(tpl)

    # 先分片再排版：一张 12 行的凭证会变成 记001 - 001/003 / 002/003 / 003/003，
    # 它们占三个槽位，可能跨物理页。分页编号的写法由模板的 split_format 决定。
    split_format = (tpl.get("voucher") or {}).get("split_format")
    pieces: list[dict] = []
    for v in shown:
        voucher = _serialize(db, v)
        voucher["period"] = period_codes.get(v.period_id, period.code)
        pieces.extend(split_voucher(voucher, rows_per_page, split_format))

    if not pieces:
        pieces = split_voucher({
            "voucher_no": "", "voucher_date": "", "entries": [], "total_debit": 0,
            "total_credit": 0, "remark": "", "attachments": 0,
        }, rows_per_page)

    sheets = [pieces[i:i + slots] for i in range(0, len(pieces), slots)]
    total_pages = len(sheets)

    ctx = pv.context(db, book, period)
    docs = [render_voucher_sheet(tpl, ctx, group, i, total_pages)
            for i, group in enumerate(sheets, start=1)]

    if len(docs) == 1:
        doc = docs[0]
    else:
        doc = docs[0].split("</head>")[0] + "</head><body>" + "".join(docs) + "</body></html>"

    return _with_note(doc, note)


def _with_note(doc: str, note: str) -> str:
    if not note:
        return doc
    return doc.replace('<div class="page">',
                       f'<p class="page-note">{html.escape(note)}</p><div class="page">', 1)


def _render_scope(
    db: Session,
    book: Book,
    scope: str,
    tpl: dict,
    *,
    period_id: Optional[int] = None,
    from_period_id: Optional[int] = None,
    subject_id: Optional[int] = None,
    from_subject_id: Optional[int] = None,
    to_subject_id: Optional[int] = None,
    report_template_id: Optional[int] = None,
    include_unposted: bool = False,
    leaf_only: bool = False,
    status: Optional[str] = None,
    voucher_ids: Optional[list[int]] = None,
    max_pages: Optional[int] = None,
    note: str = "",
) -> str:
    """把一个「打印范围」渲染成完整 HTML。五个 .html 接口和预览接口都调它。"""
    from app.ledger import queries

    period = _period(db, book.id, period_id)

    if scope == "VOUCHER":
        return _voucher_html(db, book, period, tpl, status=status,
                             max_pages=max_pages, note=note, voucher_ids=voucher_ids)

    if scope == "BALANCE":
        subjects = queries.subject_tree(db, book.id)
        rows, _check = queries.balance_sheet_of_accounts(db, book.id, subjects, period,
                                                          include_unposted)
        doc = render_table_page(tpl, pv.context(db, book, period),
                                pv.balance_rows(rows), 1, 1)
        return _with_note(doc, note)

    to_period = period
    from_period = _period(db, book.id, from_period_id) if from_period_id else to_period

    if scope == "LEDGER":
        subjects = queries.subject_tree(db, book.id)
        if leaf_only:
            subjects = [s_ for s_ in subjects if s_.is_leaf]
        rows = queries.general_ledger(db, book.id, subjects, from_period, to_period,
                                      include_unposted)
        ctx = pv.context(db, book, to_period,
                         period=f"{from_period.code} 至 {to_period.code}"
                         if from_period.id != to_period.id else to_period.code)
        doc = render_table_page(tpl, ctx, pv.general_rows(rows), 1, 1)
        return _with_note(doc, note)

    if scope == "DETAIL_LEDGER":
        if not subject_id:
            raise HTTPException(400, "明细账预览需要先选择一个科目")
        payload = queries.detail_ledger(
            db, book.id, subject_id, from_period, to_period, include_unposted,
            from_subject_id=from_subject_id, to_subject_id=to_subject_id,
        )
        subj = payload["subject"]
        subject_caption = f"{subj['code']} {subj['name']}"
        if payload.get("subject_range"):
            subject_caption += (
                f"（{payload['subject_range']['from']['code']} 至 "
                f"{payload['subject_range']['to']['code']}）"
            )
        ctx = pv.context(db, book, to_period,
                         subject=subject_caption,
                         period=f"{payload['from_period']} 至 {payload['to_period']}"
                         if payload["from_period"] != payload["to_period"] else payload["from_period"])
        doc = render_table_page(tpl, ctx, pv.detail_rows(payload), 1, 1)
        return _with_note(doc, note)

    if scope == "REPORT":
        from app.models import ReportTemplate
        from app.reports.evaluate import evaluate

        if not report_template_id:
            raise HTTPException(400, "报表预览需要先选择一张报表")
        rtpl = db.get(ReportTemplate, report_template_id)
        if rtpl is None:
            raise HTTPException(404, "报表模板不存在")
        payload = pv.visible_report_payload(evaluate(db, report_template_id, book, period, False))

        tpl = dict(tpl)
        tpl["title"] = {**tpl.get("title", {}), "text": rtpl.name}

        # 报表的列随「报表模板」走：第一列固定项目名，其余按模板列数展开
        cols = payload.get("cols", [])
        if cols:
            label_w = max(24.0, 46.0 - 4.0 * len(cols))
            tpl["columns"] = [
                {"key": "label", "label": "项　目", "width": label_w, "align": "left", "indent": 0},
            ] + [
                {"key": f"value{i}", "label": c.get("label", ""), "width": 10.0,
                 "align": "right", "indent": 0}
                for i, c in enumerate(cols)
            ]
            tpl = validate_template("REPORT", tpl)      # 重新归一化列宽到 100%

        ctx = pv.context(db, book, period)
        doc = render_table_page(tpl, ctx, pv.report_rows(payload), 1, 1)
        return _with_note(doc, note)

    raise HTTPException(400, f"未知的打印范围：{scope}")


@router.get("/vouchers.html")
def print_vouchers(book_id: int, period_id: Optional[int] = None,
                   template_id: Optional[int] = None, status: Optional[str] = None,
                   voucher_ids: Optional[str] = None,
                   book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """记账凭证：A4 每页 2 张、A5 每页 1 张，每张最多 5 行摘要。"""
    selected_ids = None
    if voucher_ids is not None:
        try:
            selected_ids = list(dict.fromkeys(
                int(value.strip()) for value in voucher_ids.split(',') if value.strip()
            ))
        except ValueError as exc:
            raise HTTPException(422, "所选凭证编号格式无效") from exc
        if not selected_ids or any(voucher_id <= 0 for voucher_id in selected_ids):
            raise HTTPException(422, "至少选择一张有效凭证")
    tpl = _tpl_dict(_template(db, book, "VOUCHER", template_id))
    return _html_response(_render_scope(db, book, "VOUCHER", tpl,
                                        period_id=period_id, status=status,
                                        voucher_ids=selected_ids))


@router.get("/balance.html")
def print_balance(book_id: int, period_id: Optional[int] = None,
                  template_id: Optional[int] = None, include_unposted: bool = Query(False),
                  book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    tpl = _tpl_dict(_template(db, book, "BALANCE", template_id))
    return _html_response(_render_scope(db, book, "BALANCE", tpl,
                                        period_id=period_id, include_unposted=include_unposted))


@router.get("/general-ledger.html")
def print_general(book_id: int, period_id: Optional[int] = None,
                  from_period_id: Optional[int] = None, template_id: Optional[int] = None,
                  include_unposted: bool = Query(False), leaf_only: bool = Query(False),
                  book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    tpl = _tpl_dict(_template(db, book, "LEDGER", template_id))
    return _html_response(_render_scope(db, book, "LEDGER", tpl, period_id=period_id,
                                        from_period_id=from_period_id,
                                        include_unposted=include_unposted,
                                        leaf_only=leaf_only))


@router.get("/detail-ledger.html")
def print_detail(book_id: int, subject_id: int, period_id: Optional[int] = None,
                 from_period_id: Optional[int] = None, template_id: Optional[int] = None,
                 from_subject_id: Optional[int] = None, to_subject_id: Optional[int] = None,
                 include_unposted: bool = Query(False),
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    tpl = _tpl_dict(_template(db, book, "DETAIL_LEDGER", template_id))
    return _html_response(_render_scope(db, book, "DETAIL_LEDGER", tpl, period_id=period_id,
                                        from_period_id=from_period_id, subject_id=subject_id,
                                        from_subject_id=from_subject_id,
                                        to_subject_id=to_subject_id,
                                        include_unposted=include_unposted))


@router.get("/report.html")
def print_report(book_id: int, template_id: int, period_id: int,
                 print_template_id: Optional[int] = None,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    tpl = _tpl_dict(_template(db, book, "REPORT", print_template_id))
    return _html_response(_render_scope(db, book, "REPORT", tpl, period_id=period_id,
                                        report_template_id=template_id))


# --------------------------------------------------------------- 设计器实时预览


class PreviewIn(BaseModel):
    """设计器预览请求。

    ``content`` 是**还没保存的草稿**，服务端只用来渲染、绝不落库 ——
    这样财务可以一边调版式一边看效果，满意了再点「保存模板」。
    """
    scope: str
    content: dict = Field(default_factory=dict)
    period_id: Optional[int] = None
    from_period_id: Optional[int] = None
    subject_id: Optional[int] = None
    from_subject_id: Optional[int] = None
    to_subject_id: Optional[int] = None
    report_template_id: Optional[int] = None


@router.post("/preview")
def preview_draft(payload: PreviewIn, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db)):
    """按草稿模板渲染一页 HTML。预览与正式打印共用 _render_scope，排版不会走样。"""
    if payload.scope not in SCOPES:
        raise HTTPException(400, f"未知打印范围：{payload.scope}")
    try:
        tpl = validate_template(payload.scope, payload.content)
    except ValueError as e:
        # 草稿正处于「半成品」状态（比如把所有列都删了）时不该 500。
        # 把原因回给前端，由它在预览区里就地提示，而不是弹一个红色报错。
        raise HTTPException(422, f"模板暂时无法预览：{e}")

    note = ""
    if payload.scope == "VOUCHER":
        note = "预览只显示前 3 张凭证，打印时输出该期间全部凭证。"
    try:
        doc = _render_scope(
            db, book, payload.scope, tpl,
            period_id=payload.period_id,
            from_period_id=payload.from_period_id,
            subject_id=payload.subject_id,
            from_subject_id=payload.from_subject_id,
            to_subject_id=payload.to_subject_id,
            report_template_id=payload.report_template_id,
            max_pages=3 if payload.scope == "VOUCHER" else None,
            note=note,
        )
    except HTTPException as e:
        # 缺期间 / 缺科目这类「参数没选全」不该让预览框变成红色报错
        raise HTTPException(422, e.detail)
    return _html_response(doc)



# ===================================================================== Excel 导出


def _xlsx(fn):
    """把 Excel 导出层的领域异常翻译成 HTTP 语义。

    * ``ExportTooLarge`` → 413 + 可操作提示（缩小范围再导出）。
      导出侧是全内存构建的（见 ``excel.MAX_EXPORT_ROWS``），超限时**必须**
      失败得明确：静默截断出来的账表缺行，交给审计方是错的。
    * 其它 ValueError → 400。这类值在 API 层已经校验过，这里是兜底，
      不该以 500 的形式漏给用户。

    装饰器而不是每个接口写一遍 try/except：六个导出接口的这段逻辑完全相同，
    复制六份迟早有一份漏掉。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except xl.ExportTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, f"导出失败：{exc}") from exc

    return wrapper


@router.get("/vouchers.xlsx")
@_xlsx
def export_vouchers(book_id: int, period_id: Optional[int] = None, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), status: Optional[str] = None):
    from app.accounting.vouchers import query_vouchers

    period = _period(db, book.id, period_id)
    rows, _ = query_vouchers(db, book.id, from_period=period, to_period=period,
                             status=status, page_size=5000)
    data = xl.vouchers_excel([_serialize(db, v) for v in rows], period=period.code)
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"记账凭证_{book.name}_{period.code}.xlsx")})


@router.get("/balance.xlsx")
@_xlsx
def export_balance(book_id: int, period_id: Optional[int] = None, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), include_unposted: bool = Query(False)):
    from app.ledger import queries

    period = _period(db, book.id, period_id)
    subjects = queries.subject_tree(db, book.id)
    rows, check = queries.balance_sheet_of_accounts(db, book.id, subjects, period, include_unposted)
    data = xl.balance_excel({"rows": rows, "check": check, "period": period.code},
                            company=book.company_name, book_name=book.name)
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"科目余额表_{period.code}.xlsx")})


@router.get("/general-ledger.xlsx")
@_xlsx
def export_general(book_id: int, period_id: Optional[int] = None,
                   from_period_id: Optional[int] = None,
                   book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                   include_unposted: bool = Query(False), leaf_only: bool = Query(False)):
    """总账导出。

    原来这个接口**只认 period_id**，把 from/to 都当成同一期：前端页面显示的
    是「起始月 ~ 结束月」这一段，下载下来的却是起始月到结束月的**全部**
    （因为 period_id 之外没有下界）。屏幕上是一页、文件里是全账，数字对不上，
    用户只会以为导出坏了。审计第 7.3 节把「总账导出：区间与页面显示一致」
    列为验收项。

    现在与 general-ledger.html 的口径一致：认 from_period_id，缺省则退化成
    单期导出（老调用方不受影响）。
    """
    from app.ledger import queries

    to_period = _period(db, book.id, period_id)
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
        if from_period.ord > to_period.ord:
            from_period, to_period = to_period, from_period
    else:
        from_period = to_period

    subjects = queries.subject_tree(db, book.id)
    if leaf_only:
        subjects = [s for s in subjects if s.is_leaf]
    rows = queries.general_ledger(db, book.id, subjects, from_period, to_period, include_unposted)
    data = xl.general_ledger_excel(
        {"rows": rows, "from_period": from_period.code, "to_period": to_period.code},
        company=book.company_name, book_name=book.name)
    span = from_period.code if from_period.id == to_period.id else f"{from_period.code}-{to_period.code}"
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"总账_{span}.xlsx")})


@router.get("/report.xlsx")
@_xlsx
def export_report(book_id: int, template_id: int, period_id: int, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db)):
    from app.models import ReportTemplate
    from app.reports.evaluate import evaluate

    period = _period(db, book.id, period_id)
    tpl = db.get(ReportTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "报表模板不存在")
    payload = pv.visible_report_payload(evaluate(db, template_id, book, period, False))
    payload["name"] = tpl.name
    data = xl.report_excel(payload, company=book.company_name, book_name=book.name)
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"{tpl.name}_{period.code}.xlsx")})


@router.get("/detail-ledger.xlsx")
@_xlsx
def export_detail_ledger(book_id: int, subject_id: int, period_id: Optional[int] = None,
                         from_period_id: Optional[int] = None,
                         from_subject_id: Optional[int] = None,
                         to_subject_id: Optional[int] = None,
                         include_unposted: bool = Query(False),
                         book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    from app.ledger import queries

    to_period = _period(db, book.id, period_id)
    if to_period.book_id != book.id:
        raise HTTPException(404, "会计期间不存在")
    if from_period_id:
        from_period = db.get(Period, from_period_id)
        if from_period is None or from_period.book_id != book.id:
            raise HTTPException(404, "起始期间不存在")
    else:
        from_period = to_period

    payload = queries.detail_ledger(
        db, book.id, subject_id, from_period, to_period, include_unposted,
        from_subject_id=from_subject_id, to_subject_id=to_subject_id,
    )
    data = xl.detail_ledger_excel(payload, book_name=book.name)
    span = from_period.code if from_period.id == to_period.id else f"{from_period.code}-{to_period.code}"
    code = payload["subject"]["code"]
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"明细账_{code}_{span}.xlsx")})
