"""打印模板引擎。

为什么用 HTML + CSS 而不是直接生成 PDF
--------------------------------------
会计打印的诉求是「任意纸型 + 模板可自助改」。CSS 的 ``@page size`` 可以直接写
``210mm 297mm``、``A4 landscape``、甚至 ``100mm 连续``，配合浏览器原生打印即可
同时满足「屏幕预览 = 打印结果」和「另存为 PDF」两个需求，不需要在服务器上维护
中文字体包和排版引擎。预览、打印、PDF 三者共用同一份 HTML，永远不会走样。

模板 JSON 结构
--------------
    paper     纸型与方向
    margins   页边距（单位 mm）
    title     大标题
    subtitle  副标题（常放期间）
    font      正文字体与字号
    show      各字段是否显示
    columns   列定义（key / label / width 权重 / align）
    meta      页眉信息项（公司、账套、制单、审核、出纳…）
    footer    页脚与页码

``width`` 用「权重」而非绝对单位，渲染时按 100% 归一化，改一列不会把整张表挤歪。
新增字段只要在 ``merge_defaults`` 的默认值里补上，旧模板即可继续渲染。
"""
from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from decimal import Decimal

# --------------------------------------------------------------------- 纸型

PAPERS: dict[str, dict] = {
    "A4": {"w": 210, "h": 297},
    "A3": {"w": 297, "h": 420},
    "A5": {"w": 148, "h": 210},
    "B5": {"w": 176, "h": 250},
    "A4_LANDSCAPE": {"w": 297, "h": 210},
    "CONTINUOUS": {"w": 240, "h": 140},   # 连续打印纸，一页一行
}

DIRECTIONS = ("portrait", "landscape")

ALIGN = ("left", "center", "right")


# --------------------------------------------------------------------- 默认模板

DEFAULT_TEMPLATES: dict[str, dict] = {
    "VOUCHER": {
        "paper": "A4", "orientation": "landscape",
        "margins": {"top": 10, "right": 8, "bottom": 10, "left": 8},
        "title": {"text": "记 账 凭 证", "size": 18, "bold": True, "align": "center"},
        "subtitle": {"text": "", "size": 11, "bold": False, "align": "center"},
        "font": {"family": "宋体", "size": 10},
        "show": {"period": True, "voucher_no": True, "date": True,
                 "attachments": True, "remark": True, "page_no": True},
        "columns": [
            {"key": "line_no", "label": "", "width": 5, "align": "center"},
            {"key": "summary", "label": "摘　要", "width": 26, "align": "left"},
            {"key": "subject", "label": "会计科目", "width": 26, "align": "left"},
            {"key": "debit", "label": "借方金额", "width": 14, "align": "right"},
            {"key": "credit", "label": "贷方金额", "width": 14, "align": "right"},
        ],
        "meta": {"company": True, "book": True, "period": True,
                 "voucher_no": True, "date": True, "attachments": True,
                 "accountant": True, "checker": True, "cashier": True},
        "footer": {"text": "", "show_page_no": True,
                   "format": "第 {page} 页　共 {total} 页"},
        "body": {"repeat_header": True, "border": True},
    },
    "LEDGER": {
        "paper": "A4", "orientation": "portrait",
        "margins": {"top": 15, "right": 10, "bottom": 15, "left": 12},
        "title": {"text": "总　账", "size": 16, "bold": True, "align": "center"},
        "subtitle": {"text": "", "size": 11, "align": "center"},
        "font": {"family": "宋体", "size": 10},
        "show": {"subject": True, "period": True, "page_no": True},
        "columns": [
            {"key": "date", "label": "日期", "width": 10, "align": "center"},
            {"key": "voucher_no", "label": "凭证字号", "width": 12, "align": "center"},
            {"key": "summary", "label": "摘　要", "width": 26, "align": "left"},
            {"key": "debit", "label": "借方金额", "width": 13, "align": "right"},
            {"key": "credit", "label": "贷方金额", "width": 13, "align": "right"},
            {"key": "direction", "label": "方向", "width": 8, "align": "center"},
            {"key": "balance", "label": "余额", "width": 13, "align": "right"},
        ],
        "meta": {"company": True, "book": True, "period": True},
        "footer": {"text": "", "show_page_no": True,
                   "format": "第 {page} 页　共 {total} 页"},
        "body": {"repeat_header": True, "border": True},
    },
    "DETAIL_LEDGER": {
        "paper": "A4", "orientation": "landscape",
        "margins": {"top": 12, "right": 8, "bottom": 12, "left": 8},
        "title": {"text": "明细分类账", "size": 15, "bold": True, "align": "center"},
        "subtitle": {"text": "", "size": 10, "align": "center"},
        "font": {"family": "宋体", "size": 9},
        "show": {"period": True, "page_no": True},
        "columns": [
            {"key": "date", "label": "日期", "width": 8, "align": "center"},
            {"key": "voucher_no", "label": "凭证字号", "width": 10, "align": "center"},
            {"key": "summary", "label": "摘　要", "width": 24, "align": "left"},
            {"key": "debit", "label": "借方金额", "width": 11, "align": "right"},
            {"key": "credit", "label": "贷方金额", "width": 11, "align": "right"},
            {"key": "direction", "label": "方向", "width": 7, "align": "center"},
            {"key": "balance", "label": "方向余额", "width": 12, "align": "right"},
        ],
        "meta": {"company": True, "book": True, "period": True},
        "footer": {"text": "", "show_page_no": True,
                   "format": "第 {page} 页　共 {total} 页"},
        "body": {"repeat_header": True, "border": True},
    },
    "BALANCE": {
        "paper": "A4", "orientation": "landscape",
        "margins": {"top": 12, "right": 8, "bottom": 12, "left": 8},
        "title": {"text": "科目余额表", "size": 16, "bold": True, "align": "center"},
        "subtitle": {"text": "", "size": 10, "align": "center"},
        "font": {"family": "宋体", "size": 9},
        "show": {"period": True, "page_no": True},
        "columns": [
            {"key": "code", "label": "科目编码", "width": 10, "align": "center"},
            {"key": "name", "label": "科目名称", "width": 20, "align": "left"},
            {"key": "level", "label": "级次", "width": 6, "align": "center"},
            {"key": "opening_debit", "label": "期初借方", "width": 11, "align": "right"},
            {"key": "opening_credit", "label": "期初贷方", "width": 11, "align": "right"},
            {"key": "period_debit", "label": "本期借方", "width": 11, "align": "right"},
            {"key": "period_credit", "label": "本期贷方", "width": 11, "align": "right"},
            {"key": "closing_debit", "label": "期末借方", "width": 11, "align": "right"},
            {"key": "closing_credit", "label": "期末贷方", "width": 11, "align": "right"},
        ],
        "meta": {"company": True, "book": True, "period": True},
        "footer": {"text": "", "show_page_no": True,
                   "format": "第 {page} 页　共 {total} 页"},
        "body": {"repeat_header": True, "border": True},
    },
    "REPORT": {
        "paper": "A4", "orientation": "portrait",
        "margins": {"top": 15, "right": 12, "bottom": 15, "left": 12},
        "title": {"text": "财务报表", "size": 16, "bold": True, "align": "center"},
        "subtitle": {"text": "", "size": 11, "align": "center"},
        "font": {"family": "宋体", "size": 10},
        "show": {"period": True, "page_no": True},
        "columns": [{"key": "label", "label": "项　目", "width": 34, "align": "left"}],
        "meta": {"company": True, "book": True, "period": True},
        "footer": {"text": "", "show_page_no": True,
                   "format": "第 {page} 页　共 {total} 页"},
        "body": {"repeat_header": True, "border": True},
    },
}

SCOPES = tuple(DEFAULT_TEMPLATES)


# --------------------------------------------------------------------- 合并默认值


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else v
        return out
    return base if override is None else override


def merge_defaults(scope: str, content: dict | None) -> dict:
    """用默认值补齐模板里缺失的键。

    这样以后往默认模板里加新配置项，老模板不需要改也能正常渲染，
    这正是「框架不能妨碍后期迭代」的一个具体落点。
    """
    base = DEFAULT_TEMPLATES.get(scope)
    if base is None:
        raise ValueError(f"未知的打印范围：{scope}")
    return _deep_merge(base, content or {})


def validate_template(scope: str, content: dict | None) -> dict:
    """校验并归一化模板内容。返回合并默认值后的模板。"""
    merged = merge_defaults(scope, content)

    paper = merged.get("paper", "A4")
    if isinstance(paper, dict):          # 兼容早期写法 {"w":..,"h":..}
        spec = paper
    else:
        spec = PAPERS.get(str(paper))
        if spec is None:
            raise ValueError(f"未知纸型「{paper}」，可选：{'、'.join(PAPERS)}")
    if isinstance(paper, str):
        merged["paper"] = paper

    if str(paper).endswith("_LANDSCAPE") and merged.get("orientation") != "landscape":
        merged["orientation"] = "landscape"

    if merged.get("orientation") not in DIRECTIONS:
        merged["orientation"] = "portrait"

    margins = merged.setdefault("margins", {})
    for side in ("top", "right", "bottom", "left"):
        try:
            margins[side] = max(0.0, float(margins.get(side, 10)))
        except (TypeError, ValueError):
            margins[side] = 10.0

    cols = merged.get("columns") or []
    if not isinstance(cols, list) or not cols:
        raise ValueError("模板至少要有一列")
    cleaned: list[dict] = []
    for i, c in enumerate(cols):
        if not isinstance(c, dict) or not c.get("key"):
            raise ValueError(f"第 {i + 1} 列缺少 key")
        align = c.get("align", "left")
        cleaned.append(
            {
                "key": str(c["key"]),
                "label": str(c.get("label", "")),
                "width": max(0.5, float(c.get("width", 10) or 10)),
                "align": align if align in ALIGN else "left",
                "indent": int(c.get("indent", 0) or 0),
            }
        )
    total = sum(c["width"] for c in cleaned) or 1.0
    for c in cleaned:
        c["width"] = round(c["width"] / total * 100.0, 4)   # 归一化到 100%
    merged["columns"] = cleaned
    return merged


# --------------------------------------------------------------------- 渲染工具

_CN_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_CN_UNITS = ("", "拾", "佰", "仟")
_CN_GROUPS = ("", "万", "亿", "兆")


def to_cn_capital(amount: Decimal | float | int | str) -> str:
    """人民币金额转中文大写（凭证合计行用）。

    例：1234.56 -> 壹仟贰佰叁拾肆元伍角陆分；305.00 -> 叁佰零伍元整。
    整数部分按 4 位一节处理，节内补「零」、节间补「万/亿/兆」。
    """
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001
        return ""
    neg = value < 0
    value = abs(value)
    yuan = int(value)
    cents = int((value - yuan) * 100)

    if yuan == 0 and cents == 0:
        return "零元整"

    # ---- 整数部分 ----
    sections: list[int] = []
    n = yuan
    while n > 0:
        sections.append(n % 10000)
        n //= 10000

    parts: list[str] = []
    for gi in range(len(sections) - 1, -1, -1):   # 高位在前
        g = sections[gi]
        seg = ""
        zero_flag = False
        for pos in range(3, -1, -1):             # 千位 → 个位
            d = (g // (10 ** pos)) % 10
            if d == 0:
                if seg:
                    zero_flag = True
            else:
                if zero_flag:
                    seg += "零"
                    zero_flag = False
                seg += _CN_DIGITS[d] + _CN_UNITS[pos]

        if seg:
            # 低位节不足四位且上面已有内容 -> 节间补「零」，如 100001 -> 壹拾万零壹
            if parts and len(seg) < 4 and not parts[-1].endswith("零"):
                parts.append("零")
            parts.append(seg + _CN_GROUPS[gi])
        elif parts and not parts[-1].endswith("零"):
            parts.append("零")                    # 整节为 0，如 100000

    int_text = ("零元" if yuan == 0 else "".join(parts).rstrip("零") + "元")

    # ---- 小数部分 ----
    if cents == 0:
        frac = "整"
    elif cents % 10 == 0:
        frac = _CN_DIGITS[cents // 10] + "角整"
    else:
        fen = _CN_DIGITS[cents % 10] + "分"
        if cents // 10:
            frac = _CN_DIGITS[cents // 10] + "角" + fen
        else:
            # 角位为 0：有整数补「零角」(20000.07 -> 贰万元零角柒分)，
            # 无整数则直接写分 (0.05 -> 零元伍分)
            frac = ("零角" if yuan > 0 else "") + fen

    out = int_text + frac
    return ("负" + out) if neg else out


def _fmt(value: Any) -> str:
    """右对齐列的金额格式化。非数字原样返回。"""
    if value in (None, ""):
        return ""
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    if isinstance(value, bool):          # bool 是 int 的子类，必须挡在前面
        return str(value)
    if isinstance(value, (int, float)):
        return f"{Decimal(str(value)):,.2f}"
    text = str(value).strip().replace(",", "").replace("，", "")
    try:                                  # 数字字符串也统一格式，避免 "200000" 这种漏网
        return f"{Decimal(text):,.2f}"
    except Exception:  # noqa: BLE001
        return str(value)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


# --------------------------------------------------------------------- 页面骨架


def _page_css(tpl: dict) -> str:
    paper = str(tpl.get("paper", "A4"))
    spec = PAPERS.get(paper, PAPERS["A4"])
    landscape = tpl.get("orientation") == "landscape"
    w, h = (spec["h"], spec["w"]) if landscape else (spec["w"], spec["h"])
    m = tpl.get("margins", {})
    font = tpl.get("font", {})

    size_line = f"{w}mm {h}mm"
    if paper == "A4":
        size_line = "A4 landscape" if landscape else "A4"
    elif paper == "A5":
        size_line = "A5 landscape" if landscape else "A5"

    border = "1px solid #333" if tpl.get("body", {}).get("border", True) else "none"
    return f"""
@page {{ size: {size_line}; margin: {m.get('top',10)}mm {m.get('right',10)}mm {m.get('bottom',10)}mm {m.get('left',10)}mm; }}
* {{ box-sizing: border-box; }}
html, body {{ margin:0; padding:0; }}
body {{
  font-family: "{font.get('family','宋体')}", "SimSun", "Songti SC", serif;
  font-size: {font.get('size',10)}pt;
  color:#000;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}}
.doc-title {{ text-align:{tpl.get('title',{}).get('align','center')};
  font-size:{tpl.get('title',{}).get('size',16)}pt;
  font-weight:{'700' if tpl.get('title',{}).get('bold',True) else '400'};
  margin:0 0 4px; letter-spacing:2px; }}
.doc-subtitle {{ text-align:{tpl.get('subtitle',{}).get('align','center')};
  font-size:{tpl.get('subtitle',{}).get('size',11)}pt; margin:0 0 8px; }}
.doc-meta {{ display:flex; flex-wrap:wrap; gap:6px 18px; font-size:10pt; margin-bottom:6px; }}
.doc-meta b {{ font-weight:600; }}
table.grid {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
table.grid th, table.grid td {{ border:{border}; padding:3px 5px;
  vertical-align:top; word-break:break-word; }}
table.grid thead {{ display:{'table-header-group' if tpl.get('body',{}).get('repeat_header',True) else 'table-row-group'}; }}
table.grid td.num {{ text-align:right; white-space:nowrap; }}
table.grid td.ctr {{ text-align:center; }}
table.grid tr.total td {{ font-weight:700; }}
table.grid tfoot td {{ font-weight:700; }}
.doc-footer {{ margin-top:8px; font-size:9pt; color:#333;
  display:flex; justify-content:space-between; }}
@media screen {{
  body {{ background:#f0f0f0; padding:16px; }}
  .page {{ background:#fff; margin:0 auto 16px; padding:12mm;
    box-shadow:0 2px 12px rgba(0,0,0,.18); width:fit-content; }}
  .page-note {{ text-align:center; color:#888; font-size:12px; margin:10px 0; }}
}}
@media print {{ body {{ background:#fff; padding:0; }} .page {{ box-shadow:none; padding:0; margin:0; }} }}
"""


#: 页眉可显示的信息项。模板用 meta 子字典逐项开关。
META_LABELS = {
    "company": "单位", "book": "账套", "period": "期间",
    "accountant": "制单", "checker": "审核", "cashier": "出纳",
    "voucher_no": "凭证字号", "date": "日期", "attachments": "附单据",
    "remark": "备注",
}


def _meta_html(tpl: dict, ctx: dict) -> str:
    flags = tpl.get("meta", {}) or {}
    bits = []
    for key, label in META_LABELS.items():
        if not flags.get(key):
            continue
        value = ctx.get(key)
        if value in (None, ""):
            continue
        bits.append(f"<span><b>{_esc(label)}：</b>{_esc(value)}</span>")
    return f'<div class="doc-meta">{"".join(bits)}</div>' if bits else ""


def _footer_html(tpl: dict, page: int, total: int) -> str:
    f = tpl.get("footer", {}) or {}
    left = _esc(f.get("text", ""))
    right = ""
    if f.get("show_page_no", True):
        fmt = f.get("format") or "第 {page} 页　共 {total} 页"
        right = _esc(fmt.replace("{page}", str(page)).replace("{total}", str(total)))
    if not left and not right:
        return ""
    return f'<div class="doc-footer"><span>{left}</span><span>{right}</span></div>'


def render_table_page(tpl: dict, ctx: dict, rows: list[dict], page: int, total: int) -> str:
    """通用表格页：总账 / 明细账 / 余额表 / 报表共用。"""
    cols = tpl["columns"]
    colgroup = "".join(f'<col style="width:{c["width"]}%" />' for c in cols)
    head = "".join(
        f'<th style="text-align:{c["align"]}">{_esc(c["label"])}</th>' for c in cols
    )
    body_rows: list[str] = []
    for r in rows:
        tds = []
        is_total = str(r.get("__kind", "")) in ("TOTAL", "SUBTOTAL", "GROUP")
        for c in cols:
            raw = r.get(c["key"], "")
            cls = ""
            if c["align"] == "right":
                cls = "num"
            elif c["align"] == "center":
                cls = "ctr"
            style = ""
            if c["align"] == "left":
                # 行级 indent（报表用）优先于列级 indent
                depth = r.get("indent")
                if depth is None or depth == "":
                    depth = c.get("indent", 0)
                try:
                    depth = int(depth)
                except (TypeError, ValueError):
                    depth = 0
                if depth:
                    style = f' style="padding-left:{5 + depth * 14}px"'
                if r.get("bold"):
                    style += " font-weight:700;"
            # 只对右对齐（金额）列做千分位格式化；居中列放的是级次、日期这类
            # 非金额字段，套 _fmt 会把 1 变成 "1.00"
            txt = _esc(_fmt(raw) if c["align"] == "right" and raw not in (None, "") else raw)
            tds.append(f'<td class="{cls}"{style}>{txt}</td>')
        tr_cls = ' class="total"' if is_total else ""
        body_rows.append(f"<tr{tr_cls}>{''.join(tds)}</tr>")

    if not body_rows:
        body_rows = ['<tr><td colspan="%d" class="ctr">（无数据）</td></tr>' % len(cols)]

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(ctx.get('doc_title') or tpl.get('title',{}).get('text','打印预览'))}</title>
<style>{_page_css(tpl)}</style></head><body>
<div class="page">
  <h1 class="doc-title">{_esc(tpl.get('title',{}).get('text',''))}</h1>
  <p class="doc-subtitle">{_esc(tpl.get('subtitle',{}).get('text','') or ctx.get('period',''))}</p>
  {_meta_html(tpl, ctx)}
  <table class="grid"><colgroup>{colgroup}</colgroup>
    <thead><tr>{head}</tr></thead>
    <tbody>{''.join(body_rows)}</tbody>
  </table>
  {_footer_html(tpl, page, total)}
</div></body></html>"""


def render_voucher_page(tpl: dict, ctx: dict, voucher: dict, page: int, total: int) -> str:
    """记账凭证：一页一张，含附件数、合计行与金额大写。"""
    cols = tpl["columns"]
    colgroup = "".join(f'<col style="width:{c["width"]}%" />' for c in cols)
    head = "".join(
        f'<th style="text-align:{c["align"]}">{_esc(c["label"])}</th>' for c in cols
    )

    rows = []
    entries = voucher.get("entries", [])
    for e in entries:
        tds = []
        for c in cols:
            key = c["key"]
            if key == "subject":
                text = f"{e.get('subject_code','')} {e.get('subject_name','')}".strip()
            else:
                text = e.get(key, "")
            cls = "num" if c["align"] == "right" else ("ctr" if c["align"] == "center" else "")
            tds.append(f'<td class="{cls}">{_esc(_fmt(text) if c["align"]=="right" else text)}</td>')
        rows.append(f"<tr>{''.join(tds)}</tr>")

    dr = voucher.get("total_debit") or 0
    cr = voucher.get("total_credit") or 0
    total_label = "合　计"
    tail: list[str] = []
    if cols:
        last_two = cols[-2:]
        span = len(cols) - 2
        head_cells = "".join(
            f'<td class="{"num" if c["align"]=="right" else ("ctr" if c["align"]=="center" else "")}">'
            f'{_esc(total_label) if i == max(0, span - 1) else ""}</td>'
            for i, c in enumerate(cols)
        )
        tail.append(f"<tr class='total'>{head_cells}</tr>")
    else:
        tail = []

    show = tpl.get("show", {}) or {}
    remark_row = ""
    if show.get("remark", True) and voucher.get("remark"):
        remark_row = (
            f'<div style="margin-top:6px;font-size:10pt">'
            f'<b>备注：</b>{_esc(voucher.get("remark"))}</div>'
        )

    meta = dict(ctx)
    meta.update(
        {
            "voucher_no": voucher.get("voucher_no", ""),
            "date": voucher.get("voucher_date", ""),
            "attachments": voucher.get("attachments") or "",
        }
    )
    capital = (
        f'<div style="margin-top:4px;font-size:10pt"><b>合计大写：</b>'
        f'{_esc(to_cn_capital(Decimal(str(dr))))}</div>'
        if to_cn_capital(Decimal(str(dr)))
        else ""
    )

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(voucher.get('voucher_no','记账凭证'))}</title>
<style>{_page_css(tpl)}</style></head><body>
<div class="page">
  <h1 class="doc-title">{_esc(tpl.get('title',{}).get('text','记 账 凭 证'))}</h1>
  <p class="doc-subtitle">{_esc(tpl.get('subtitle',{}).get('text',''))}</p>
  {_meta_html(tpl, meta)}
  <table class="grid"><colgroup>{colgroup}</colgroup>
    <thead><tr>{head}</tr></thead>
    <tbody>{''.join(rows)}{''.join(tail)}</tbody>
    <tfoot><tr>
      <td colspan="{len(cols)}">{capital}</td>
    </tr></tfoot>
  </table>
  {remark_row}
  {_footer_html(tpl, page, total)}
</div></body></html>"""


def wrap_pages(pages: list[str], note: str = "") -> str:
    """把多个单页 HTML 包成一份连续文档，浏览器一次打印全部。"""
    if not pages:
        return render_table_page(validate_template("LEDGER", {}), {}, [], 1, 1)
    if len(pages) == 1:
        return pages[0]
    head = pages[0].split("</head>")[0] + "</head><body>"
    body = head + "".join(pages) + "</body></html>"
    note_html = f'<p class="page-note">{_esc(note)}</p>' if note else ""
    return body.replace("<div class=\"page\">", note_html + "<div class=\"page\">", 1)
