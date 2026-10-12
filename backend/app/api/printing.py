"""打印与导出。

两条输出通道：

* **Excel** —— 用于二次加工、数据核对、导入别的系统。
* **HTML**  —— 用于「所见即所得」的预览与打印。预览 iframe 和最终打印走的是
  同一个渲染函数，所以模板改了什么，打出来就是什么样，不存在两套排版。

打印模板存在 ``print_template.content``（JSON）里，财务可以在前端设计器里
自助改版式，不需要改代码。
"""
from __future__ import annotations

import html
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.vouchers import _serialize
from app.core.db import get_db
from app.core.deps import get_current_user, get_my_book, require_book_write
from app.models import Book, Period, PrintTemplate, SysUser, UserBook
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


def _template(db: Session, book: Book, scope: str, template_id: int | None) -> PrintTemplate:
    if template_id:
        t = db.get(PrintTemplate, template_id)
        if t is None:
            raise HTTPException(404, "打印模板不存在")
        return t
    t = db.scalar(
        select(PrintTemplate)
        .where(PrintTemplate.scope == scope, PrintTemplate.is_default.is_(True))
        .order_by(PrintTemplate.id)          # 多个默认时取最早创建的，保证结果确定
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
    rows = db.scalars(select(PrintTemplate).order_by(PrintTemplate.scope, PrintTemplate.id)).all()
    return [{"id": t.id, "scope": t.scope, "name": t.name, "paper": t.paper,
             "is_default": t.is_default, "remark": t.remark, "content": t.content}
            for t in rows]


@router.get("/templates/{template_id}")
def template_detail(template_id: int, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db)):
    t = db.get(PrintTemplate, template_id)
    if t is None:
        raise HTTPException(404, "打印模板不存在")
    merged = _tpl_dict(t)
    return {"id": t.id, "scope": t.scope, "name": t.name, "paper": t.paper,
            "is_default": t.is_default, "remark": t.remark, "content": merged,
            "raw": t.content}


@router.post("/templates", status_code=201)
def create_template(payload: TemplateIn, book: Book = Depends(get_my_book),
                    db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                    user: SysUser = Depends(get_current_user)):
    if payload.scope not in SCOPES:
        raise HTTPException(400, f"未知打印范围：{payload.scope}，可选：{'、'.join(SCOPES)}")
    try:
        content = validate_template(payload.scope, payload.content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    t = PrintTemplate(scope=payload.scope, name=payload.name, paper=content.get("paper", "A4"),
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
                    db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                    user: SysUser = Depends(get_current_user)):
    t = db.get(PrintTemplate, template_id)
    if t is None:
        raise HTTPException(404, "打印模板不存在")
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
                    db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                    user: SysUser = Depends(get_current_user)):
    t = db.get(PrintTemplate, template_id)
    if t is None:
        raise HTTPException(404, "打印模板不存在")
    name = t.name
    db.delete(t)
    _log(db, user, book.id, "DELETE_PRINT_TEMPLATE", template_id, f"删除打印模板 {name}")
    db.commit()
    return {"ok": True}


@router.post("/templates/{template_id}/copy")
def copy_template(template_id: int, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write),
                  user: SysUser = Depends(get_current_user)):
    """复制一份再改 —— 改模板前先留个底，财务自己就能做。"""
    src = db.get(PrintTemplate, template_id)
    if src is None:
        raise HTTPException(404, "打印模板不存在")
    t = PrintTemplate(scope=src.scope, name=f"{src.name} 副本", paper=src.paper,
                      content=dict(src.content or {}), is_default=False,
                      remark=src.remark)
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id, "scope": t.scope, "name": t.name, "content": t.content}


def _clear_other_defaults(db: Session, keep: PrintTemplate) -> None:
    db.query(PrintTemplate).filter(
        PrintTemplate.scope == keep.scope, PrintTemplate.id != keep.id,
        PrintTemplate.is_default.is_(True)
    ).update({PrintTemplate.is_default: False}, synchronize_session=False)


def _log(db, user, book_id, action, entity_id, detail, before=None, after=None) -> None:
    from app.accounting.audit_helper import log

    log(db, action=action, entity="print_template", entity_id=entity_id, user=user,
        book_id=book_id, detail=detail, before=before, after=after)


# ===================================================================== HTML 打印
#
# **预览和打印走的是同一个渲染函数 _render_scope()**，唯一区别是模板从哪来：
# 正式打印读数据库里已保存的模板，设计器预览直接吃前端传来的草稿 JSON。
# 这样「所见即所得」是结构上成立的 —— 世界上只有一套排版代码，没有第二处
# 可以跑偏。以前预览必须传 template_id 读已保存内容，所以改完版式要点保存
# 才看得到变化。


def _html_response(doc: str) -> Response:
    return Response(content=doc, media_type="text/html; charset=utf-8")


def _voucher_html(db: Session, book: Book, period: Period, tpl: dict,
                  status: Optional[str] = None, max_pages: Optional[int] = None,
                  note: str = "") -> str:
    """记账凭证：A4 一页两张、A5 一页一张，每张最多 5 行摘要、超出续到下一张。

    ``max_pages`` 限的是**原始凭证张数**（只给预览用，保证改版式时的响应速度），
    不是分片数 —— 否则一张 12 行的凭证会把「只预览 3 张」变成只看它自己。
    """
    from app.accounting.vouchers import query_vouchers

    vouchers, _ = query_vouchers(db, book.id, from_period=period, to_period=period,
                                 status=status, page_size=5000)
    vouchers = sorted(vouchers, key=lambda v: (v.voucher_date, v.voucher_no))
    shown = vouchers[:max_pages] if max_pages else vouchers

    rows_per_page = voucher_rows_per_page(tpl)
    slots = voucher_slots_per_page(tpl)

    # 先分片再排版：一张 12 行的凭证会变成 记001 - 001/003 / 002/003 / 003/003，
    # 它们占三个槽位，可能跨物理页。分页编号的写法由模板的 split_format 决定。
    split_format = (tpl.get("voucher") or {}).get("split_format")
    pieces: list[dict] = []
    for v in shown:
        pieces.extend(split_voucher(_serialize(db, v), rows_per_page, split_format))

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
    report_template_id: Optional[int] = None,
    include_unposted: bool = False,
    leaf_only: bool = False,
    status: Optional[str] = None,
    max_pages: Optional[int] = None,
    note: str = "",
) -> str:
    """把一个「打印范围」渲染成完整 HTML。五个 .html 接口和预览接口都调它。"""
    from app.ledger import queries

    period = _period(db, book.id, period_id)

    if scope == "VOUCHER":
        return _voucher_html(db, book, period, tpl, status=status,
                             max_pages=max_pages, note=note)

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
        payload = queries.detail_ledger(db, book.id, subject_id, from_period, to_period,
                                        include_unposted)
        subj = payload["subject"]
        ctx = pv.context(db, book, to_period,
                         subject=f"{subj['code']} {subj['name']}",
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
                   book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """记账凭证：A4 每页 2 张、A5 每页 1 张，每张最多 5 行摘要。"""
    tpl = _tpl_dict(_template(db, book, "VOUCHER", template_id))
    return _html_response(_render_scope(db, book, "VOUCHER", tpl,
                                        period_id=period_id, status=status))


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
                 include_unposted: bool = Query(False),
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    tpl = _tpl_dict(_template(db, book, "DETAIL_LEDGER", template_id))
    return _html_response(_render_scope(db, book, "DETAIL_LEDGER", tpl, period_id=period_id,
                                        from_period_id=from_period_id, subject_id=subject_id,
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
            report_template_id=payload.report_template_id,
            max_pages=3 if payload.scope == "VOUCHER" else None,
            note=note,
        )
    except HTTPException as e:
        # 缺期间 / 缺科目这类「参数没选全」不该让预览框变成红色报错
        raise HTTPException(422, e.detail)
    return _html_response(doc)



# ===================================================================== Excel 导出


@router.get("/vouchers.xlsx")
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
