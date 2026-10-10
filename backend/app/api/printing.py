"""打印与导出。

两条输出通道：

* **Excel** —— 用于二次加工、数据核对、导入别的系统。
* **HTML**  —— 用于「所见即所得」的预览与打印。预览 iframe 和最终打印走的是
  同一个渲染函数，所以模板改了什么，打出来就是什么样，不存在两套排版。

打印模板存在 ``print_template.content``（JSON）里，财务可以在前端设计器里
自助改版式，不需要改代码。
"""
from __future__ import annotations

import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.vouchers import _serialize
from app.core.db import get_db
from app.core.deps import get_book_link, get_current_user, get_my_book
from app.models import Book, Period, PrintTemplate, SysCompany, SysUser, UserBook
from app.printing import excel as xl
from app.printing import preview as pv
from app.printing.template import (
    DEFAULT_TEMPLATES,
    PAPERS,
    SCOPES,
    render_table_page,
    render_voucher_page,
    validate_template,
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
                    db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                    user: SysUser = Depends(get_current_user)):
    _require_write(link)
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
                    db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                    user: SysUser = Depends(get_current_user)):
    _require_write(link)
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
                    db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                    user: SysUser = Depends(get_current_user)):
    _require_write(link)
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
                  db: Session = Depends(get_db), link: UserBook = Depends(get_book_link),
                  user: SysUser = Depends(get_current_user)):
    """复制一份再改 —— 改模板前先留个底，财务自己就能做。"""
    _require_write(link)
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


def _require_write(link: UserBook) -> None:
    if link.role == "VIEWER":
        raise HTTPException(403, "只读账套不能修改打印模板")


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


def _html_response(doc: str) -> Response:
    return Response(content=doc, media_type="text/html; charset=utf-8")


@router.get("/vouchers.html")
def print_vouchers(book_id: int, period_id: Optional[int] = None,
                   template_id: Optional[int] = None, status: Optional[str] = None,
                   book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    """记账凭证：一页一张。"""
    from app.accounting.vouchers import query_vouchers

    period = _period(db, book.id, period_id)
    t = _template(db, book, "VOUCHER", template_id)
    tpl = _tpl_dict(t)

    vouchers, _ = query_vouchers(db, book.id, from_period=period, to_period=period,
                                 status=status, page_size=5000)
    vouchers = sorted(vouchers, key=lambda v: (v.voucher_date, v.voucher_no))

    pages = []
    total = max(len(vouchers), 1)
    for i, v in enumerate(vouchers, start=1):
        payload = _serialize(db, v)
        ctx = pv.context(db, book, period)
        pages.append(render_voucher_page(tpl, ctx, payload, i, total))

    doc = pages[0] if pages else render_voucher_page(tpl, pv.context(db, book, period), {
        "voucher_no": "", "voucher_date": "", "entries": [], "total_debit": 0,
        "total_credit": 0, "remark": "", "attachments": 0,
    }, 1, 1)
    if len(pages) > 1:
        head = pages[0].split("</head>")[0] + "</head><body>"
        doc = head + "".join(pages) + "</body></html>"
    return _html_response(doc)


@router.get("/balance.html")
def print_balance(book_id: int, period_id: Optional[int] = None,
                  template_id: Optional[int] = None, include_unposted: bool = Query(False),
                  book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    from app.ledger import queries

    period = _period(db, book.id, period_id)
    tpl = _tpl_dict(_template(db, book, "BALANCE", template_id))
    subjects = queries.subject_tree(db, book.id)
    rows, _check = queries.balance_sheet_of_accounts(db, book.id, subjects, period, include_unposted)
    ctx = pv.context(db, book, period)
    return _html_response(render_table_page(tpl, ctx, pv.balance_rows(rows), 1, 1))


@router.get("/general-ledger.html")
def print_general(book_id: int, period_id: Optional[int] = None,
                  from_period_id: Optional[int] = None, template_id: Optional[int] = None,
                  include_unposted: bool = Query(False), leaf_only: bool = Query(False),
                  book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    from app.ledger import queries

    to_period = _period(db, book.id, period_id)
    from_period = _period(db, book.id, from_period_id) if from_period_id else to_period
    tpl = _tpl_dict(_template(db, book, "LEDGER", template_id))
    subjects = queries.subject_tree(db, book.id)
    if leaf_only:
        subjects = [s for s in subjects if s.is_leaf]
    rows = queries.general_ledger(db, book.id, subjects, from_period, to_period, include_unposted)
    ctx = pv.context(db, book, to_period,
                     period=f"{from_period.code} 至 {to_period.code}"
                     if from_period.id != to_period.id else to_period.code)
    return _html_response(render_table_page(tpl, ctx, pv.general_rows(rows), 1, 1))


@router.get("/detail-ledger.html")
def print_detail(book_id: int, subject_id: int, period_id: Optional[int] = None,
                 from_period_id: Optional[int] = None, template_id: Optional[int] = None,
                 include_unposted: bool = Query(False),
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    from app.ledger import queries

    to_period = _period(db, book.id, period_id)
    from_period = _period(db, book.id, from_period_id) if from_period_id else to_period
    tpl = _tpl_dict(_template(db, book, "DETAIL_LEDGER", template_id))
    payload = queries.detail_ledger(db, book.id, subject_id, from_period, to_period, include_unposted)
    subj = payload["subject"]
    ctx = pv.context(db, book, to_period,
                     subject=f"{subj['code']} {subj['name']}",
                     period=f"{payload['from_period']} 至 {payload['to_period']}"
                     if payload["from_period"] != payload["to_period"] else payload["from_period"])
    return _html_response(render_table_page(tpl, ctx, pv.detail_rows(payload), 1, 1))


@router.get("/report.html")
def print_report(book_id: int, template_id: int, period_id: int,
                 print_template_id: Optional[int] = None,
                 book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    from app.models import ReportTemplate
    from app.reports.evaluate import evaluate

    period = _period(db, book.id, period_id)
    rtpl = db.get(ReportTemplate, template_id)
    if rtpl is None:
        raise HTTPException(404, "报表模板不存在")
    payload = evaluate(db, template_id, book, period, False)

    tpl = _tpl_dict(_template(db, book, "REPORT", print_template_id))
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
    return _html_response(render_table_page(tpl, ctx, pv.report_rows(payload), 1, 1))


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
    company = db.get(SysCompany, book.company_id)
    data = xl.balance_excel({"rows": rows, "check": check, "period": period.code},
                            company=company.name, book_name=book.name)
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"科目余额表_{period.code}.xlsx")})


@router.get("/general-ledger.xlsx")
def export_general(book_id: int, period_id: Optional[int] = None, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), include_unposted: bool = Query(False)):
    from app.ledger import queries

    period = _period(db, book.id, period_id)
    subjects = queries.subject_tree(db, book.id)
    rows = queries.general_ledger(db, book.id, subjects, period, period, include_unposted)
    company = db.get(SysCompany, book.company_id)
    data = xl.general_ledger_excel({"rows": rows, "from_period": period.code, "to_period": period.code},
                                   company=company.name, book_name=book.name)
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"总账_{period.code}.xlsx")})


@router.get("/report.xlsx")
def export_report(book_id: int, template_id: int, period_id: int, book: Book = Depends(get_my_book),
                  db: Session = Depends(get_db)):
    from app.models import ReportTemplate
    from app.reports.evaluate import evaluate

    period = _period(db, book.id, period_id)
    tpl = db.get(ReportTemplate, template_id)
    if tpl is None:
        raise HTTPException(404, "报表模板不存在")
    payload = evaluate(db, template_id, book, period, False)
    payload["name"] = tpl.name
    company = db.get(SysCompany, book.company_id)
    data = xl.report_excel(payload, company=company.name, book_name=book.name)
    return Response(content=data, media_type=XLSX_MIME,
                    headers={"Content-Disposition": _filename(f"{tpl.name}_{period.code}.xlsx")})
