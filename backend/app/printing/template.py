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
import re
from datetime import datetime
from typing import Any

from decimal import ROUND_HALF_UP, Decimal

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

LINE_STYLES = ("solid", "dashed", "dotted")
ALIGN = ("left", "center", "right")
ELEMENT_TYPES = ("text", "table", "line", "rectangle")

#: 正文字体白名单。
#:
#: ``font.family`` 会被 ``_page_css`` 原样拼进 ``font-family: "…"``。
#: 这里拼的是**带引号**的值，但引号本身不能防注入：给宋体后面接一个 ``"`` 就能
#: 把字符串闭合掉，剩下的内容就成了 CSS 声明甚至 ``</style><script>``（模板内容
#: 是全局的、只读账号也会渲染它，见 ``api/printing.py::_template``）。
#:
#: 字体名对排版而言是个**封闭集合** —— 打印环境里能用的中文字体就那么几种，
#: 前端设计器也是让用户从下拉里选。认不出来的名字一律退回宋体，没有「让用户
#: 自己敲字体名」这个需求，因此不必为了灵活开一个字符串注入的口子。
FONT_FAMILIES = (
    # 中文
    "宋体", "SimSun", "黑体", "SimHei", "楷体", "KaiTi",
    "仿宋", "FangSong", "微软雅黑", "Microsoft YaHei",
    # 拉丁 / 通用
    "Arial", "Times New Roman", "serif", "sans-serif", "monospace",
)
#: 正文字号区间（pt）。与前端设计器输入框的 min/max 一致。
FONT_SIZE_RANGE = (6, 24)
#: 标题 / 副标题字号区间（pt）。比正文宽一点，允许做大标题。
TITLE_SIZE_RANGE = (8, 48)
#: 页边距区间（mm）。负数在 CSS 里会被浏览器丢弃、过大则整页空白，这里夹住。
MARGIN_RANGE = (0, 100)
ELEMENT_BINDS = {
    "title", "subtitle", "company", "book", "period", "voucher_no", "date",
    "attachments", "accountant", "checker", "cashier", "remark", "capital",
    "footer", "custom", "entries",
}


# --------------------------------------------------------------------- 默认模板

DEFAULT_TEMPLATES: dict[str, dict] = {
    "VOUCHER": {
        "paper": "A4", "orientation": "portrait",
        "margins": {"top": 10, "right": 8, "bottom": 10, "left": 8},
        "title": {"text": "记 账 凭 证", "size": 18, "bold": True, "align": "center"},
        "subtitle": {"text": "", "size": 11, "bold": False, "align": "center"},
        "font": {"family": "宋体", "size": 10},
        "voucher": {"rows_per_page": 5, "split_number": True, "fill_rows": True},
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
        # 凭证专用的可视化布局。坐标均为当前凭证格的百分比，前端可以直接拖拽。
        "elements": [
            {"id": "title", "type": "text", "bind": "title", "text": "记 账 凭 证",
             "x": 32, "y": 3, "w": 36, "h": 8, "font_size": 18, "bold": True, "align": "center"},
            {"id": "company", "type": "text", "bind": "company", "text": "单位：{company}",
             "x": 3, "y": 12, "w": 32, "h": 5, "font_size": 9, "align": "left"},
            {"id": "period", "type": "text", "bind": "period", "text": "期间：{period}",
             "x": 36, "y": 12, "w": 28, "h": 5, "font_size": 9, "align": "center"},
            {"id": "voucher_no", "type": "text", "bind": "voucher_no", "text": "凭证字号：{voucher_no}",
             "x": 66, "y": 12, "w": 31, "h": 5, "font_size": 9, "align": "right"},
            {"id": "date", "type": "text", "bind": "date", "text": "日期：{date}",
             "x": 66, "y": 17, "w": 31, "h": 5, "font_size": 9, "align": "right"},
            {"id": "table", "type": "table", "bind": "entries", "text": "分录表格",
             "x": 3, "y": 23, "w": 94, "h": 57, "font_size": 9, "border": True},
            {"id": "capital", "type": "text", "bind": "capital", "text": "合计大写：{capital}",
             "x": 3, "y": 82, "w": 64, "h": 5, "font_size": 9, "align": "left"},
            {"id": "attachments", "type": "text", "bind": "attachments", "text": "附单据：{attachments}",
             "x": 68, "y": 82, "w": 29, "h": 5, "font_size": 9, "align": "right"},
            {"id": "remark", "type": "text", "bind": "remark", "text": "备注：{remark}",
             "x": 3, "y": 88, "w": 94, "h": 5, "font_size": 9, "align": "left"},
            {"id": "footer", "type": "text", "bind": "footer", "text": "第 {page} 页　共 {total} 页",
             "x": 3, "y": 94, "w": 94, "h": 4, "font_size": 8, "align": "right"},
        ],
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
            {"key": "month", "label": "月", "width": 5, "align": "center"},
            {"key": "day", "label": "日", "width": 5, "align": "center"},
            {"key": "voucher_no", "label": "凭证字号", "width": 10, "align": "center"},
            {"key": "code", "label": "科目编码", "width": 10, "align": "center"},
            {"key": "name", "label": "科目名称", "width": 16, "align": "left"},
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


def _correct_voucher_title(text: str) -> str:
    return re.sub(r"记\s*帐\s*凭\s*证", "记 账 凭 证", text)


def _num(value: Any, default: float, low: float, high: float) -> float:
    """把任意输入夹成 ``[low, high]`` 区间内的浮点数。

    兜底 ``float(...)`` 抛异常时用默认值：模板里写坏了不该让整个预览 500。
    返回值一律是 ``float``：这些数字直接拼进 CSS，统一类型免得调用方
    拿到 int 时还要再判一次。
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(default)
    if value != value or value in (float("inf"), float("-inf")):   # NaN / 无穷
        value = float(default)
    return max(low, min(high, value))


def _safe_font(value: Any) -> str:
    """正文字体名过白名单。认不出来退回宋体（见 ``FONT_FAMILIES`` 的说明）。"""
    text = str(value or "").strip()
    return text if text in FONT_FAMILIES else "宋体"


def _safe_align(value: Any, default: str = "center") -> str:
    """对齐方式过白名单 —— 会无引号拼进 ``text-align:{}``，必须过白名单。"""
    return value if value in ALIGN else default


def _clean_title_like(merged: dict, key: str, size_range: tuple[float, float],
                      default_size: float) -> None:
    """归一化 title / subtitle 这两组「会进 CSS 的标题字段」。

    ``align`` 会无引号拼进 ``text-align:``、``size`` 会拼进 ``font-size:{}pt``，
    两处都不像 ``font-family`` 那样自带引号兜底，是四组注入面里最好利用的
    两个。任何进入 ``<style>`` 的字段都必须在这里过白名单或夹区间。
    """
    raw = merged.get(key)
    if not isinstance(raw, dict):
        merged[key] = {}
        raw = merged[key]
    raw["align"] = _safe_align(raw.get("align"))
    raw["size"] = _num(raw.get("size"), default_size, *size_range)
    raw["bold"] = bool(raw.get("bold", False))
    raw["text"] = str(raw.get("text", ""))


def validate_template(scope: str, content: dict | None) -> dict:
    """校验并归一化模板内容。返回合并默认值后的模板。"""
    merged = merge_defaults(scope, content)
    if scope == "VOUCHER" and isinstance(merged.get("title"), dict):
        merged["title"]["text"] = _correct_voucher_title(
            str(merged["title"].get("text", "")))

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

    # font.family 会拼进 font-family，font.size 会拼进 font-size ——
    # 与下面 title/subtitle 同理，会进 <style> 的字段一律过白名单或夹区间。
    font = merged.get("font")
    if not isinstance(font, dict):
        font = {}
    font["family"] = _safe_font(font.get("family"))
    font["size"] = _num(font.get("size"), 10, *FONT_SIZE_RANGE)
    merged["font"] = font

    # title 与 subtitle 用各自的默认字号（16 / 11），别让写坏的值把副标题
    # 悄悄变成大标题那种字号 —— 副标题比正文小是版式约定。
    _clean_title_like(merged, "title", TITLE_SIZE_RANGE, 16)
    _clean_title_like(merged, "subtitle", TITLE_SIZE_RANGE, 11)

    # 页边距是原有 float 兜底，那一处已经足够：float() 对任何非数字串都抛
    # ValueError，走 except 退回 10.0，因此不可能把非数字原样拼进 @page。
    # 这里补的是**区间**——负数/超大值虽然注入不了，却能让整张纸空白或错位。
    margins = merged.setdefault("margins", {})
    if not isinstance(margins, dict):
        margins = {}
    for side in ("top", "right", "bottom", "left"):
        margins[side] = _num(margins.get(side), 10.0, *MARGIN_RANGE)
    merged["margins"] = margins

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

    if scope == "VOUCHER":
        voucher = merged.setdefault("voucher", {})
        try:
            rows_per_page = int(voucher.get("rows_per_page", 5) or 5)
        except (TypeError, ValueError):
            rows_per_page = 5
        # 会计凭证的物理版式固定最多五行，不能通过模板把分录挤到下一张纸上。
        voucher["rows_per_page"] = max(1, min(5, rows_per_page))
        voucher["split_number"] = bool(voucher.get("split_number", True))
        voucher["fill_rows"] = bool(voucher.get("fill_rows", True))
        # 拆页后凭证号的写法，默认「记001 - 001/006」。空串等于退回默认写法，
        # 免得有人把输入框清空之后凭证号就变成了光秃秃的「记001」。
        split_format = str(voucher.get("split_format") or "").strip()
        voucher["split_format"] = split_format or SPLIT_NUMBER_FORMAT

        elements = merged.get("elements") or []
        clean_elements: list[dict] = []
        for i, raw in enumerate(elements):
            if not isinstance(raw, dict):
                continue
            element_type = str(raw.get("type", "text"))
            if element_type not in ELEMENT_TYPES:
                element_type = "text"
            bind = str(raw.get("bind", "custom"))
            if bind not in ELEMENT_BINDS:
                bind = "custom"
            text = str(raw.get("text", ""))
            if bind == "title":
                text = _correct_voucher_title(text)

            def _number(key: str, default: float, low: float, high: float) -> float:
                # 与页边距/字号共用 _num：元素坐标与 font_size 同样会被拼进
                # style 属性（_element_style），NaN/Infinity 进去会让整张凭证塌掉。
                return _num(raw.get(key, default), default, low, high)

            clean_elements.append(
                {
                    "id": str(raw.get("id") or f"element-{i + 1}"),
                    "type": element_type,
                    "bind": bind,
                    "text": text,
                    "x": _number("x", 5, 0, 100),
                    "y": _number("y", 5, 0, 100),
                    "w": _number("w", 30, 1, 100),
                    "h": _number("h", 6, 1, 100),
                    "font_size": _number("font_size", merged["font"].get("size", 10), 6, 40),
                    "bold": bool(raw.get("bold", False)),
                    "align": raw.get("align") if raw.get("align") in ALIGN else "left",
                    "visible": bool(raw.get("visible", True)),
                    "border": bool(raw.get("border", True)),
                    # 表格专用：表头可以不印，高度（占表格的百分比）也归模板管
                    "show_header": bool(raw.get("show_header", True)),
                    "header_height": _number("header_height", VOUCHER_HEAD_PCT, 0, 100),
                    "total_label": str(raw.get("total_label", VOUCHER_TOTAL_LABEL)),
                    # 线宽/线型会拼进 style 属性，必须过白名单，不能原样透传
                    "line_width": _number("line_width", 0.3, 0.1, 3),
                    "line_style": raw.get("line_style") if raw.get("line_style") in LINE_STYLES else "solid",
                }
            )
        merged["elements"] = clean_elements
    return merged


# --------------------------------------------------------------------- 渲染工具

_CN_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_CN_UNITS = ("", "拾", "佰", "仟")
_CN_GROUPS = ("", "万", "亿", "兆")


def to_cn_capital(amount: Decimal | float | int | str) -> str:
    """人民币金额转中文大写（凭证合计行用）。

    例：1234.56 -> 壹仟贰佰叁拾肆元伍角陆分；305.00 -> 叁佰零伍元整。
    整数部分按 4 位一节处理，节内补「零」、节间补「万/亿/兆」。

    **舍入必须是 ROUND_HALF_UP**：中文大写金额是凭证的法定要素，必须和
    ``accounting/util.py::money`` 的口径一致（银行/四舍五入习惯）。
    ``Decimal.quantize`` 默认是 ROUND_HALF_EVEN（银行家舍入），二者会分叉：
    ``0.125`` 在这里是「零元壹角叁分」，在账上却是 0.13；``1.005`` 同理。
    同一张凭证，合计小写写 0.13、大写印「壹角贰分」，财务当场就能看出不对。
    """
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
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


#: 打印页内联的 CSP（``<meta http-equiv>`` 形式）。
#:
#: **为什么必须有 meta 而不是只靠响应头**：这份 HTML 不是被浏览器当「文档」直接
#: 加载的。前端用 axios 取回源码，再交给 ``iframe.srcdoc`` 或
#: ``window.open()`` + ``document.write()``（见 ``frontend/src/api/print.js``，
#: 因为 token 在 localStorage、``iframe.src`` 带不上 Authorization）。
#: 走这条链路时 **响应头全部丢失** —— CSP 头只在浏览器直接导航到该 URL 时生效。
#: meta 标签是文档的一部分，跟着字符串走，因此三种加载方式都覆盖得到。
#:
#: ``style-src 'unsafe-inline'`` 不能去掉：版式全靠 ``_page_css`` 拼的内联
#: ``<style>``。``script-src 'none'`` 才是重点 —— 模板校验是第一道防线，
#: 这条是第二道，两道独立成立。
PRINT_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; "
    "img-src 'none'; font-src 'none'; connect-src 'none'; "
    "form-action 'none'; base-uri 'none'"
    '">'
)


# --------------------------------------------------------------------- 页面骨架


def _page_css(tpl: dict) -> str:
    """拼出整份页面的 CSS。

    **这里的每一个模板取值都要在拼进 CSS 之前重新过一遍白名单/区间**，
    不因为 ``validate_template`` 已经校验过就省掉。理由有两个：

    1. 模板内容可能来自 ``validate_template`` 上线**之前**写入的老数据，
       或者有人直接改库 —— 渲染层不能假设自己拿到的字典是干净的；
    2. 渲染函数是公开入口，将来新增调用点时不必记得先校验。

    拼装方式本身也遵循同一条规则：只有「白名单里的枚举字面量」和
    「已经夹过区间的数字」才允许出现在本函数的返回值里。前者不可能包含
    ``;``/``}``/``<``，后者经 ``_num`` 转换后只可能是有限小数，
    因此 ``</style><script>`` 这类载荷在结构上就进不来。
    """
    paper = str(tpl.get("paper", "A4"))
    spec = PAPERS.get(paper, PAPERS["A4"])
    landscape = tpl.get("orientation") == "landscape"
    w, h = (spec["h"], spec["w"]) if landscape else (spec["w"], spec["h"])

    size_line = f"{w}mm {h}mm"
    if paper == "A4":
        size_line = "A4 landscape" if landscape else "A4"
    elif paper == "A5":
        size_line = "A5 landscape" if landscape else "A5"

    border = "1px solid #333" if tpl.get("body", {}).get("border", True) else "none"
    # 屏幕预览必须用**同一套**纸型与页边距，否则「所见即所得」是假的：
    # 用户在左边把 A4 改成 A5、把页边距调小，右边预览纹丝不动，
    # 等真打印出来才发现版式不对 —— 那就失去了预览的意义。
    # 边距同样重新夹区间：老数据里存过负数或超大值时不该整页空白。
    m = tpl.get("margins", {})
    if not isinstance(m, dict):
        m = {}
    mt, mr, mb, ml = (
        _num(m.get(k), 10.0, *MARGIN_RANGE) for k in ("top", "right", "bottom", "left")
    )

    # font.family / font.size / title.align / title.size / subtitle.align /
    # subtitle.size —— 六处会进 <style> 的字段，与 validate_template 里
    # 「必须过白名单」的注释是同一套约束，只是执行点不同。
    font = tpl.get("font")
    if not isinstance(font, dict):
        font = {}
    family = _safe_font(font.get("family"))
    fsize = _num(font.get("size"), 10, *FONT_SIZE_RANGE)

    def _title_like(key: str, default_size: float) -> tuple[str, float]:
        raw = tpl.get(key)
        if not isinstance(raw, dict):
            raw = {}
        return (
            _safe_align(raw.get("align")),
            _num(raw.get("size"), default_size, *TITLE_SIZE_RANGE),
        )

    title_align, title_size = _title_like("title", 16)
    subtitle_align, subtitle_size = _title_like("subtitle", 11)
    title_bold = "700" if (tpl.get("title") or {}).get("bold", True) else "400"

    return f"""
@page {{ size: {size_line}; margin: {mt}mm {mr}mm {mb}mm {ml}mm; }}
* {{ box-sizing: border-box; }}
html, body {{ margin:0; padding:0; }}
body {{
  font-family: "{family}", "SimSun", "Songti SC", serif;
  font-size: {fsize}pt;
  color:#000;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}}
.doc-title {{ text-align:{title_align};
  font-size:{title_size}pt;
  font-weight:{title_bold};
  margin:0 0 4px; letter-spacing:2px; }}
.doc-subtitle {{ text-align:{subtitle_align};
  font-size:{subtitle_size}pt; margin:0 0 8px; }}
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
/* 凭证按「槽位」铺在纸上：A4 一页两个槽位，A5 一个。
   槽位是元素的定位参照（.voucher-element 的百分比坐标都相对它），
   因此槽位高度必须等于纸面去掉上下边距后的净高，否则内容会溢出到第二张纸。 */
.voucher-sheet-page {{ width:100%; height:calc({h}mm - {mt}mm - {mb}mm); overflow:hidden; }}
.voucher-sheet {{ display:grid; gap:3mm; height:100%; }}
.voucher-sheet.slots-1 {{ grid-template-columns:minmax(0,1fr); grid-template-rows:minmax(0,1fr); }}
.voucher-sheet.slots-2.flow-rows {{ grid-template-columns:minmax(0,1fr); grid-template-rows:repeat(2,minmax(0,1fr)); }}
.voucher-sheet.slots-2.flow-columns {{ grid-template-columns:repeat(2,minmax(0,1fr)); grid-template-rows:minmax(0,1fr); }}
.voucher-slot {{ position:relative; min-width:0; min-height:0; overflow:hidden; }}
.voucher-element {{ position:absolute; overflow:hidden; white-space:pre-wrap;
  word-break:break-word; line-height:1.3; padding:0 1mm; }}
.voucher-element.line {{ height:0 !important; padding:0; border-top:0.3mm solid #000; }}
.voucher-element.rectangle {{ border:0.3mm solid #000; padding:0; }}
.voucher-element.table {{ padding:0; white-space:normal; }}
/* 关框线只去线，不去表格：行列位置和行高还得留着，否则整张凭证会塌一块。 */
.voucher-element.table.no-border table.grid th,
.voucher-element.table.no-border table.grid td {{ border:none; }}
/* 凭证是固定版式：表头 +（分录行 + 合计行）必须**恰好**占满表格，所以每一行
   都显式给同一个高度。不能只写 height:100% 让浏览器自己分配 —— 它不会平均分给
   tbody 里的各行，而是把余量塞给最后一行，于是合计行变成一条肥带、空行又比
   有字的分录行矮胖不一。--vrow 由 _voucher_table_html 按行数算好后内联给出。 */
.voucher-element.table table.grid {{ height:100%; font-size:inherit; }}
.voucher-element.table table.grid th {{ height:var(--vhead); }}
.voucher-element.table table.grid td {{ height:var(--vrow); }}
.voucher-element.table table.grid th,
.voucher-element.table table.grid td {{ padding:0.5mm 1mm; vertical-align:middle;
  overflow:hidden; text-overflow:ellipsis; }}
@media screen {{
  /* 左右不能留内边距：预览 iframe 的宽度就等于纸宽（A5=559px），
     body 一旦 padding:16px，内容区只剩 527px 装不下 559px 的纸 ——
     左边凭空多一道灰边，右边还会被裁掉 16px。页与页之间靠 margin 分隔。 */
  body {{ background:#f0f0f0; padding:0; }}
  /* 宽度=纸型宽，内容区=纸型宽-左右边距，与打印时的 @page 完全一致 */
  .page {{ background:#fff; margin:0 0 16px;
    box-sizing:border-box; width:{w}mm; min-height:{h}mm;
    padding:{mt}mm {mr}mm {mb}mm {ml}mm;
    box-shadow:0 2px 12px rgba(0,0,0,.18); }}
  .page-note {{ text-align:center; color:#888; font-size:12px; margin:10px 0; }}
}}
@media print {{
  body {{ background:#fff; padding:0; }}
  .page {{ box-shadow:none; padding:0; margin:0; }}
  .voucher-sheet-page {{ break-after:page; page-break-after:always; }}
  .voucher-sheet-page:last-child {{ break-after:auto; page-break-after:auto; }}
}}
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
{PRINT_CSP_META}
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


# --------------------------------------------------------------------- 凭证版式
#
# 会计凭证是**固定版式**，不是可以随便塞内容的表格：
#
#   * 一张凭证最多 5 行摘要，超出的行续到下一张凭证上打印
#     （记001号 - 001/006 / 记001号 - 002/006）；
#   * A4 一页 2 张（A4 纵向上下排、A4 横向左右排），A5 一页 1 张；
#   * 每一张的位置、字号由模板的 elements 决定 —— 坐标是「当前槽位」的百分比，
#     所以同一份元素在不同纸型上自动等比缩放，模板不必为 A4/A5 各存一份。

MAX_VOUCHER_ROWS = 5
#: 表头占表格高度的比例。前端算行高毫米数时用同一个值，改这里要同步改前端。
VOUCHER_HEAD_PCT = 14.0
#: 合计行里那个标签的默认文字。老模板没有这个字段，按这个显示。
VOUCHER_TOTAL_LABEL = "合　计"
#: 拆页后的凭证号写法：{voucher_no} - {page}/{pages} → 记001号 - 001/006。
#: 模板可以改这个格式串，占位符见 _split_number。
SPLIT_NUMBER_FORMAT = "{voucher_no} - {page}/{pages}"
#: 一页能放两张凭证的纸型。其余（A5/B5/连续纸）一张。
TWO_UP_PAPERS = {"A4", "A3", "A4_LANDSCAPE"}

#: 元素没写死文字时，各字段的默认写法。
ELEMENT_DEFAULT_TEXT = {
    "title": "记 账 凭 证",
    "subtitle": "{subtitle}",
    "company": "单位：{company}",
    "book": "账套：{book}",
    "period": "期间：{period}",
    "voucher_no": "凭证字号：{voucher_no}",
    "date": "日期：{date}",
    "attachments": "附单据：{attachments}",
    "accountant": "制单：{accountant}",
    "checker": "审核：{checker}",
    "cashier": "出纳：{cashier}",
    "remark": "备注：{remark}",
    "capital": "合计大写：{capital}",
    "footer": "第 {page} 页　共 {total} 页",
    "entries": "",
}


def voucher_rows_per_page(tpl: dict) -> int:
    """一张凭证最多几行。validate_template 已经把越界的值夹回 1~5。"""
    try:
        n = int((tpl.get("voucher") or {}).get("rows_per_page", MAX_VOUCHER_ROWS))
    except (TypeError, ValueError):
        return MAX_VOUCHER_ROWS
    return max(1, min(MAX_VOUCHER_ROWS, n))


def voucher_slots_per_page(tpl: dict) -> int:
    """一张物理纸放几张凭证。"""
    return 2 if str(tpl.get("paper", "A4")) in TWO_UP_PAPERS else 1


def _entry_amount(entry: dict) -> Decimal:
    raw = entry.get("amount")
    if raw in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _slice_total(entries: list[dict], direction: str) -> Decimal:
    """拆出来的每一张自己那几行的合计，口径跟凭证上的合计一致：
    红字（负数）算到对面去 —— 拆页是排版，不是改账。"""
    dr = cr = Decimal("0")
    for e in entries:
        amt = _entry_amount(e)
        if amt > 0:
            if e.get("direction") == "DEBIT":
                dr += amt
            else:
                cr += amt
        elif amt < 0:
            if e.get("direction") == "DEBIT":
                cr += -amt
            else:
                dr += -amt
    return dr if direction == "DEBIT" else cr


#: 分页编号格式串里的占位符。
_SPLIT_TOKEN = re.compile(r"\{(\w+)\}")


def split_number(voucher_no: str, index: int, total: int,
                 first: int, last: int, lines: int, fmt: str | None = None) -> str:
    """把分页信息套进模板给的格式串。

    ``记001`` + 第 2 张、共 6 张 → ``记001 - 002/006``。数字统一补零到三位
    （跟凭证号本身的记001 一样宽），页数超过 999 位会自己加宽。
    """
    voucher_no = str(voucher_no or "")
    if not voucher_no.strip():
        return ""            # 空凭证（空数据占位）不需要编号
    fmt = fmt or SPLIT_NUMBER_FORMAT
    width = max(3, len(str(max(total, lines))))
    values = {
        "voucher_no": voucher_no,
        "page": str(index).zfill(width),
        "pages": str(total).zfill(width),
        "first": str(first).zfill(width),
        "last": str(last).zfill(width),
        "lines": str(lines).zfill(width),
    }
    return _SPLIT_TOKEN.sub(lambda m: values.get(m.group(1), ""), str(fmt))


def split_voucher(voucher: dict, rows_per_page: int, fmt: str | None = None) -> list[dict]:
    """把一张凭证按最多 ``rows_per_page`` 行拆成若干张可独立打印的凭证。

    * 凭证号一律按模板格式带分页号：不拆的是「记001号 - 001/001」，拆开的
      是「记001号 - 001/006」。撕开的几张凭这个认得出是同一张凭证，没拆的那张
      也和拆过的长得一样，不用先数张数再对号。
    * 拆页只是排版，账上的凭证号一个字都不改。
    * 合计是**累计**的：第二张印的是前两张加起来的数，最后一张等于整张凭证的合计。
      拆页只是排版，不会把一张凭证的账算成好几张 —— 只按本页几行重算的话，
      每张都印着「合　计」却对不上整张凭证的数，撕开就对不了账。
    """
    entries = voucher.get("entries") or []
    rows_per_page = max(1, int(rows_per_page))
    if len(entries) <= rows_per_page:
        piece = dict(voucher)
        piece["slice_index"] = 1
        piece["slice_total"] = 1
        piece["voucher_no"] = split_number(voucher.get("voucher_no", ""), 1, 1,
                                           1, len(entries), len(entries), fmt)
        return [piece]

    chunks = [entries[i:i + rows_per_page] for i in range(0, len(entries), rows_per_page)]
    total_pages = len(chunks)
    lines = len(entries)
    voucher_no = voucher.get("voucher_no", "")
    run_dr = run_cr = Decimal("0")
    out: list[dict] = []
    for i, chunk in enumerate(chunks, start=1):
        piece = dict(voucher)
        piece["entries"] = chunk
        run_dr += _slice_total(chunk, "DEBIT")
        run_cr += _slice_total(chunk, "CREDIT")
        piece["total_debit"] = str(run_dr)
        piece["total_credit"] = str(run_cr)
        first = (i - 1) * rows_per_page + 1
        piece["voucher_no"] = split_number(voucher_no, i, total_pages,
                                          first, first + len(chunk) - 1, lines, fmt)
        piece["slice_index"] = i
        piece["slice_total"] = total_pages
        out.append(piece)
    return out


def _cn_capital_safe(value: Any) -> str:
    try:
        return to_cn_capital(Decimal(str(value or 0)))
    except Exception:  # noqa: BLE001
        return ""


def cn_date(value: Any) -> str:
    """``2026-01-05`` -> ``2026年01月05日``。

    中文记账凭证上的日期就是这么写的；不是 ISO 日期就原样返回，别把
    空值或者别的东西掰成一段假日期。
    """
    text = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if not m:
        return text
    return f"{m.group(1)}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"


def _element_values(tpl: dict, ctx: dict, voucher: dict, page: int, total_pages: int) -> dict:
    """元素占位符能取到的值。凭证字段来自这张（分片后的）凭证，其余来自账套上下文。"""
    period = voucher.get("period") or ctx.get("period", "")
    return {
        "title": (tpl.get("title") or {}).get("text", ""),
        "subtitle": (tpl.get("subtitle") or {}).get("text", "") or period,
        "company": ctx.get("company", ""),
        "book": ctx.get("book", ""),
        "period": period,
        "voucher_no": voucher.get("voucher_no", ""),
        "date": cn_date(voucher.get("voucher_date", "")),
        "attachments": voucher.get("attachments") or "",
        "accountant": voucher.get("accountant", ""),
        "checker": voucher.get("checker", ""),
        "cashier": voucher.get("cashier", ""),
        "remark": voucher.get("remark") or "",
        "capital": _cn_capital_safe(voucher.get("total_debit")),
        "page": page,
        "total": total_pages,
    }


def _fill(text: str, values: dict) -> str:
    """替换 {field} 占位符。字段缺失时留空，不把占位符原样印到纸上。"""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value if value is not None else ""))
    return text


def _element_enabled(tpl: dict, bind: str) -> bool:
    """旧模板用 meta / show 开关控制页眉显示，元素化之后这两组开关继续有效。"""
    meta = tpl.get("meta") or {}
    show = tpl.get("show") or {}
    if bind in META_LABELS and not meta.get(bind, True):
        return False
    if bind in ("period", "voucher_no", "date", "attachments", "remark"):
        if not show.get(bind, True):
            return False
    if bind == "footer" and not show.get("page_no", True):
        return False
    return True


def _voucher_total_row(tpl: dict, voucher: dict, el: dict | None = None) -> str:
    """合计行。借/贷方金额放在对应的列上，金额列不存在时退回旧的「倒数第三列」。

    「合　计」三个字由表格元素的 total_label 决定，不同模板可以写「合计」「小计」。
    """
    cols = tpl["columns"]
    n = len(cols)
    dr_i = next((i for i, c in enumerate(cols) if c["key"] == "debit"), None)
    cr_i = next((i for i, c in enumerate(cols) if c["key"] == "credit"), None)
    dr = _fmt(voucher.get("total_debit") or 0)
    cr = _fmt(voucher.get("total_credit") or 0)
    label_at = dr_i - 1 if dr_i else max(0, n - 3)
    label = (el or {}).get("total_label", VOUCHER_TOTAL_LABEL)
    cells = []
    for i, c in enumerate(cols):
        cls = "num" if c["align"] == "right" else ("ctr" if c["align"] == "center" else "")
        if dr_i is not None and i == dr_i:
            text = dr
        elif cr_i is not None and i == cr_i:
            text = cr
        elif i == label_at:
            text = label
        else:
            text = ""
        cells.append(f'<td class="{cls}">{_esc(text)}</td>')
    return '<tr class="total">' + "".join(cells) + "</tr>"


def table_head_pct(el: dict | None) -> float:
    """表头占表格高度的百分比。表头关掉就是 0，高度全给分录行和合计行。"""
    if not el or el.get("show_header") is False:
        return 0.0
    try:
        pct = float(el.get("header_height", VOUCHER_HEAD_PCT))
    except (TypeError, ValueError):
        pct = VOUCHER_HEAD_PCT
    return max(0.0, min(100.0, pct))


def _voucher_table_html(tpl: dict, voucher: dict, el: dict | None = None) -> str:
    """分录表格。表头、金额千分位、合计行与旧版一致，只是行数由外层分片保证。

    表头本身也是模板的一部分：可以不显示，高度（占表格的百分比）也归模板管，
    关掉表头之后剩下的高度全部分给分录行和合计行。
    """
    cols = tpl["columns"]
    colgroup = "".join(f'<col style="width:{c["width"]}%" />' for c in cols)
    head = "".join(
        f'<th style="text-align:{c["align"]}">{_esc(c["label"])}</th>' for c in cols
    )

    rows = []
    for e in voucher.get("entries") or []:
        tds = []
        for c in cols:
            key = c["key"]
            if key == "subject":
                text = f"{e.get('subject_code','')} {e.get('subject_name','')}".strip()
            elif key in ("debit", "credit"):
                # 分录只有 direction + amount，borrow/credit 两列要按方向取金额，
                # 方向对不上就留空（不能把同一笔金额同时印在借方和贷方）。
                text = e.get("amount", "") if e.get("direction") == key.upper() else ""
            else:
                text = e.get(key, "")
            cls = "num" if c["align"] == "right" else ("ctr" if c["align"] == "center" else "")
            shown = _fmt(text) if c["align"] == "right" else text
            tds.append(f'<td class="{cls}">{_esc(shown)}</td>')
        rows.append("<tr>" + "".join(tds) + "</tr>")

    if (tpl.get("voucher") or {}).get("fill_rows", True):
        # 补足空行：凭证是固定版式，3 行和 5 行的凭证高度必须一样，
        # 否则叠放的两张在纸上的分界线会错位。
        blank = "<td></td>" * len(cols)
        rows += ["<tr>" + blank + "</tr>"] * max(0, voucher_rows_per_page(tpl) - len(rows))

    head_pct = table_head_pct(el)
    row_pct = (100.0 - head_pct) / (voucher_rows_per_page(tpl) + 1)
    heights = f' style="--vhead:{head_pct:g}%;--vrow:{row_pct:g}%"'
    # 老模板没有 show_header，按「有表头」处理，否则升级后所有凭证的表头会消失
    show_head = not el or el.get("show_header") is not False
    thead = f"<thead><tr>{head}</tr></thead>" if show_head else ""
    return (
        '<table class="grid"' + heights + "><colgroup>" + colgroup + "</colgroup>"
        + thead
        + "<tbody>" + "".join(rows) + _voucher_total_row(tpl, voucher, el) + "</tbody></table>"
    )


def _element_style(el: dict) -> str:
    """元素的行内样式。

    与 ``_page_css`` 同一个道理：``x/y/w/h/font_size/align`` 会无引号拼进
    ``style="…"``，渲染层再夹一次，不因为 ``validate_template`` 校验过就省掉。
    """
    return (
        f"left:{_num(el.get('x'), 5, 0, 100)}%;top:{_num(el.get('y'), 5, 0, 100)}%;"
        f"width:{_num(el.get('w'), 30, 1, 100)}%;height:{_num(el.get('h'), 6, 1, 100)}%;"
        f"font-size:{_num(el.get('font_size'), 10, 6, 40)}pt;"
        f"text-align:{_safe_align(el.get('align'), 'left')};"
        + ("font-weight:700;" if el.get("bold") else "")
    )


def _element_border(el: dict) -> str:
    """线条/矩形的边框。线宽线型由模板控制，写死成 0.3mm 实线就没法改了。"""
    # 同样是无引号拼进 style 属性：线宽夹区间、线型过白名单。
    width = _num(el.get("line_width", 0.3), 0.3, 0.1, 3)
    style = el.get("line_style") if el.get("line_style") in LINE_STYLES else "solid"
    return f"{width}mm {style} #000"


def _voucher_element_html(tpl: dict, ctx: dict, voucher: dict, el: dict,
                          page: int, total_pages: int) -> str:
    # 元素自己的显隐（画布上的「显示」开关 / 元素列表的隐藏按钮）
    if not el.get("visible", True):
        return ""
    etype = el.get("type", "text")
    style = _element_style(el)
    if etype == "line":
        return (f'<div class="voucher-element line" style="{style}border-top:{_element_border(el)};'
                f'border-right:0;border-bottom:0;border-left:0"></div>')
    if etype == "rectangle":
        return f'<div class="voucher-element rectangle" style="{style}border:{_element_border(el)}"></div>'
    if etype == "table":
        # 框线是每个表格自己的开关，不跟全局「表格边框」设置混在一起
        cls = "voucher-element table" if el.get("border", True) else "voucher-element table no-border"
        return f'<div class="{cls}" style="{style}">{_voucher_table_html(tpl, voucher, el)}</div>'

    bind = el.get("bind", "custom")
    if not _element_enabled(tpl, bind):
        return ""
    text = _fill(el.get("text") or ELEMENT_DEFAULT_TEXT.get(bind, ""),
                 _element_values(tpl, ctx, voucher, page, total_pages))
    if not text.strip():
        return ""
    return f'<div class="voucher-element text" style="{style}">{_esc(text)}</div>'


def render_voucher_sheet(tpl: dict, ctx: dict, vouchers: list[dict],
                         page: int, total_pages: int) -> str:
    """一张**物理纸**。``vouchers`` 是要放进本页槽位的凭证（分片后）。"""
    slots = voucher_slots_per_page(tpl)
    # A4 横向时上下两张会非常扁，横排更符合直觉；纵向则上下叠。
    flow = "flow-columns" if slots == 2 and tpl.get("orientation") == "landscape" else "flow-rows"
    inner = ""
    for v in vouchers:
        elements = "".join(
            _voucher_element_html(tpl, ctx, v, el, page, total_pages)
            for el in (tpl.get("elements") or []) if el.get("visible", True)
        )
        inner += f'<div class="voucher-slot">{elements}</div>'

    doc_title = _esc((tpl.get("title") or {}).get("text", "") or "记 账 凭 证")
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
{PRINT_CSP_META}
<title>{doc_title}</title>
<style>{_page_css(tpl)}</style></head><body>
<div class="page">
  <div class="voucher-sheet-page">
    <div class="voucher-sheet slots-{slots} {flow}">{inner}</div>
  </div>
</div></body></html>"""


def render_voucher_page(tpl: dict, ctx: dict, voucher: dict, page: int, total: int) -> str:
    """单张凭证占一整张纸（兼容旧调用与空数据占位）。"""
    return render_voucher_sheet(tpl, ctx, [voucher], page, total)


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
