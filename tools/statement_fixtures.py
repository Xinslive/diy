"""把真实对账单做成**脱敏**的测试夹具。

为什么要有这个脚本：解析器最怕的不是「写错了」，而是「只对一种格式是对的」。
九家银行的表头、容器、方向列、日期写法都不一样，回归测试必须覆盖到它们。
但真实对账单里有**真实账号、真实交易对手、真实金额** —— 这些不该进仓库。

所以这里的做法是：**读真实文件 → 按列改写敏感值 → 用同样的容器再写一遍**。

- 账号：中间几位换成 ``*``（长度不变，格式不变）；
- 对方户名：换成「甲公司 / 乙公司 / 丙公司」这类假名（按出现次序定，
  同一份文件里同一段文本换成的名字一样 —— 平安那种「本户身份」判定要靠这个）；
- 金额：**乘以 100**（乘 100 是唯一能保住两位小数精确的整数倍，
  余额链条、笔数、合计全都等比放大，解析器该验的照样能验）；
- 日期：保留（期间信息，测试要靠它）。

PDF 单独处理：它的版面（列的位置、长名字折行）才是难点，真数据反而不重要，
所以用 reportlab 按**原文件的列坐标**重新排一份假的 —— 版面一样，数据是假的。
**页面尺寸也照抄原文件**：坐标是从原文件抄的，尺寸画错了内容就整体溢出页面，
PDF 看起来是张白纸（文本层还在，所以解析不报错 —— 更难发现）。

脱敏有两个坑，都会静悄悄地毁掉夹具：

1. **金额不能按账号打码。**「1000074.01」的前半截「1000074」看着像账号，
   打了码就变成「100*074.01」—— 既不是合法数字（缩放跟着失效），
   又在文件自报的合计里插了个「*」，自检会报出一条**假警**。
   所以只匹配「前后不挨着小数点」的长数字串。
2. **先缩放后打码。**反过来先打码，金额被塞了「*」之后就不是数字了，
   缩放跟着失效 —— 文件自报的合计跟明细对不上。

这两条错了都不会让夹具解析失败，只会让它**说谎**。

跑法（需要 xlwt / reportlab —— **这两个只是生成夹具用的工具依赖，不是运行时
依赖**，所以不进 ``backend/requirements.txt``；装在哪儿都行）::

    backend/.venv/bin/pip install xlwt reportlab
    backend/.venv/bin/python tools/statement_fixtures.py \
        --source "/Volumes/KIOXIA/home/Downloads/银行流水" \
        --target backend/tests/fixtures/statements

**夹具已经入库了**（``backend/tests/fixtures/statements/``），平时不需要重跑。
只有两类情况才需要重跑：① 换了新的真实样例；② 改了脱敏规则。
重跑会**覆盖**已有夹具与 ``goldens.json`` —— 那是故意的：金标准跟着夹具走，
两边不一致时测试会立刻红。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.bankstatement.grid import Grid, cell_text, read_grids  # noqa: E402
from app.bankstatement.semantics import parse_grid, parse_statement  # noqa: E402

#: 假名池。按出现次序轮流取，同一段文本永远换成同一个名字。
FAKE_NAMES = [
    "甲贸易有限公司", "乙实业有限公司", "丙供应链有限公司", "丁物业管理有限公司",
    "戊建筑材料有限公司", "己物流服务有限公司", "庚科技有限公司", "辛餐饮管理有限公司",
    "壬纺织有限公司", "癸装饰工程有限公司",
]

#: 这些列里的值是**对方**（人/公司），要换成假名。
NAME_COLUMNS = {"CP_NAME", "PAYER_NAME", "PAYEE_NAME"}
#: 这些列里的值是**账号**，要打码。
ACCOUNT_COLUMNS = {"CP_ACCOUNT", "PAYER_ACCOUNT", "PAYEE_ACCOUNT"}
#: 这些列是金额，等比放大。
AMOUNT_COLUMNS = {"IN", "OUT", "AMOUNT", "BALANCE"}

_SCALE = Decimal("100")
# 只匹配**独立**的长数字串：前后都不挨着数字或小数点。
# 「1000074.01」这种金额要整段放过 —— 它是小数，前半截「1000074」
# 看着像账号，可它后面跟着「.」。误打码的后果不只是难看：
# 金额被塞了「*」之后就再也不是合法数字，脱敏的比例也断了，
# 文件自报的合计跟明细对不上，自检会报出一条假警。
_DIGIT_RUN = re.compile(r"(?<![\d.])\d{6,}(?![\d.])")


def mask_digits(text: str) -> str:
    """长数字串打码：保留头尾，中间换 ``*``。

    长度不变 —— 有些表头/正文靠长度认格式（``622663******7022`` 这种
    脱敏写法就是银行自己打的码，打码规则也得保持这个样子）。
    """
    def replace(match: re.Match) -> str:
        digits = match.group(0)
        if len(digits) <= 6:
            return digits
        # **日期不能打码**：``20260901`` 看着像账号，实际是日期。
        # 判据是「8 位（或 6 位）且以 19/20 开头」—— 这是日期的形状，
        # 账号不会长这样（真账号带脱敏星号或以 62/62 开头）。
        if re.fullmatch(r"(?:19|20)\d{6}", digits) and len(digits) == 8:
            return digits
        if re.fullmatch(r"(?:19|20)\d{4}", digits) and len(digits) == 6:
            return digits
        keep = 2 if len(digits) > 10 else 3
        return digits[:keep] + "*" * (len(digits) - keep * 2) + digits[-keep:]
    return _DIGIT_RUN.sub(replace, text)


def fake_name(index: int) -> str:
    return FAKE_NAMES[index % len(FAKE_NAMES)]


def scale_amount(text: str) -> str:
    """金额乘 100（两位小数精确）。"""
    from app.bankstatement.semantics import parse_amount

    amount = parse_amount(text)
    if amount is None:
        return text
    return f"{(amount * _SCALE).quantize(Decimal('0.01'))}"


#: 元信息里「自称的合计金额」也要放大 —— 不然自检会报一句
#: 「文件自称收入 325213.96，解析出 32521396.00」这种**假警报**：
#: 那是夹具脱敏造成的，不是解析错了。
_META_AMOUNT_LABELS = (
    "支出金额", "存入金额", "借方发生额", "贷方发生额",
    "借方累计发生额", "贷方累计发生额", "本期总收入金额", "本期总支出金额",
    "期末账户余额", "期末余额",
)


#: 标签**最长优先**的单次扫描正则。
#:
#: 为什么不能一个标签一个正则地循环替换：「支出金额」是「本期总支出金额」的
#: **子串**，循环替换会让同一处金额被缩放两次 —— 15680.50 变成 156805000.00，
#: 于是自检报出一条「文件自称的合计对不上」的假警，而看起来像解析器坏了。
#: 一条正则 + 最长优先，每处金额只缩放一次。
#: 注意括号的位置：``(...)`` 只包交替组，``\s*[:：]\s*`` 必须在它**外面** ——
#: 放进去的话它只作用于最后一个分支，前面的分支匹配完标签就接不上冒号，
#: 整个正则一个都匹配不到（而且不报错，只是静悄悄地什么都不缩放）。
#: ``(?:...)`` 是**非捕获组**：交替组自己不算编号，冒号跟着标签一起落在
#: group(1) 里 —— 否则替换时冒号会被丢掉，变成「支出金额32524500.00」。
_META_AMOUNT_PATTERN = re.compile(
    r"((?:" + "|".join(re.escape(label)
                        for label in sorted(_META_AMOUNT_LABELS, key=len, reverse=True))
    + r")\s*[:：]\s*)([\d,，\.]+)")


def scale_meta_amounts(text: str) -> str:
    return _META_AMOUNT_PATTERN.sub(
        lambda m: m.group(1) + _scale_number(m.group(2)), text)


def _is_name_label(text: str) -> bool:
    """这个格子是不是「户名标签」（``账户名称：`` / ``户名:``）。

    标签格里**不含**名字本身 —— 名字在它**右边那一格**
    （光大、民生那些表都是这么排的），所以换名要走「标签 + 下一格」。
    """
    stripped = text.strip()
    if not stripped.endswith(("：", ":")):
        return False
    return bool(re.match(r"^(?:账户名称|户\s*名|客户名称|账户名)\s*[:：]?$", stripped))


def _is_amount_label(text: str) -> bool:
    """这个格子是不是「金额标签」（``支出金额：``）。"""
    stripped = text.strip()
    if not stripped.endswith(("：", ":")):
        return False
    return any(label in stripped for label in _META_AMOUNT_LABELS)


def _scale_number(text: str) -> str:
    from app.bankstatement.semantics import parse_amount

    amount = parse_amount(text)
    if amount is None:
        return text
    return f"{(amount * _SCALE).quantize(Decimal('0.01'))}"


def scrub_grid(grid: Grid) -> Grid:
    """按列改写一张网格里的敏感值。"""
    parsed = parse_grid(grid)
    mapping = parsed.column_mapping
    header_index = parsed.header_row
    names: dict[str, str] = {}
    name_counter = 0

    rows: list[list[str]] = []
    for index, row in enumerate(grid.rows):
        cells = [cell_text(cell) for cell in row]
        if index <= header_index or header_index < 0:
            # 表头与元信息：打码长数字（账号），并把「自称的合计金额」等比放大。
            #
            # 光大那张表把标签与金额放在**相邻两格**（``支出金额：`` | ``325245.00``），
            # 所以还要顺着行往下看一格：标签格后面那格才是它的金额。
            # 顺序要紧：**先缩放再打码**。反过来先打码，
            # 金额被塞了「*」之后就不是合法数字了，缩放跟着失效。
            scrubbed = [mask_digits(scale_meta_amounts(cell)) for cell in cells]
            for position, cell in enumerate(scrubbed[:-1]):
                if _META_AMOUNT_LABELS and _is_amount_label(cell):
                    scrubbed[position + 1] = _scale_number(scrubbed[position + 1])
                elif _is_name_label(cell) and scrubbed[position + 1].strip():
                    # **户名也要换** —— 它是「这个账户是谁」的直接标识。
                    # 只换数据行里的对方户名是不够的：元信息里那行
                    # 「账户名称：泉州市新恒强投资有限责任公司」才是本户，
                    # 漏了它，仓库里就躺着一家真公司的名字。
                    original = scrubbed[position + 1]
                    if original not in names:
                        names[original] = fake_name(name_counter)
                        name_counter += 1
                    scrubbed[position + 1] = names[original]
            rows.append(scrubbed)
            continue
        out: list[str] = []
        for column, cell in enumerate(cells):
            field = _field_of(mapping, column)
            if field in AMOUNT_COLUMNS:
                out.append(scale_amount(cell) if cell.strip() else cell)
            elif field in ACCOUNT_COLUMNS:
                out.append(mask_digits(cell))
            elif field in NAME_COLUMNS:
                if cell.strip():
                    if cell not in names:
                        names[cell] = fake_name(name_counter)
                        name_counter += 1
                    out.append(names[cell])
                else:
                    out.append(cell)
            else:
                out.append(mask_digits(cell))
        rows.append(out)
    return Grid(container=grid.container, sheet=grid.sheet, rows=rows)


def _field_of(mapping: dict[str, int], column: int) -> str:
    for field, index in mapping.items():
        if index == column:
            return field
    return ""


# ----------------------------------------------------------------------
# 写出
# ----------------------------------------------------------------------

def write_xlsx(grid: Grid, target: Path) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = grid.sheet[:31] or "Sheet1"
    for row in grid.rows:
        sheet.append([_cell_value(value) for value in row])
    workbook.save(target)


def write_xls(grid: Grid, target: Path) -> None:
    import xlwt

    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet((grid.sheet or "Sheet1")[:31])
    for row_index, row in enumerate(grid.rows):
        for column_index, value in enumerate(row):
            _write_xls_cell(sheet, row_index, column_index, value)
    book.save(str(target))


def _write_xls_cell(sheet, row_index: int, column_index: int, value: str) -> None:
    import xlwt

    text = str(value or "")
    try:
        number = Decimal(text.replace(",", "").replace("，", ""))
        if text.strip() and number == number:
            if number == number.to_integral_value() and abs(number) < 1e15:
                sheet.write(row_index, column_index, int(number))
            else:
                sheet.write(row_index, column_index, float(number))
            return
    except Exception:  # noqa: BLE001
        pass
    sheet.write(row_index, column_index, text)


def _cell_value(value: str):
    """写 xlsx 时把「像数字的」写成数字（保持原文件的形态）。"""
    text = str(value or "")
    stripped = text.strip().replace(",", "").replace("，", "")
    if stripped and re.fullmatch(r"-?\d+(\.\d+)?", stripped):
        try:
            number = float(stripped)
            return int(number) if number.is_integer() else number
        except ValueError:
            return text
    return text


def write_html(grid: Grid, target: Path) -> None:
    """写成 WPS 那种「Excel 网页版」的表格。

    保留两件真文件里的特征：元信息行**列数不齐**（真文件就是这样），
    以及 ``colspan`` 都不写。
    """
    rows = grid.rows
    parts = [
        "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:x=\"urn:schemas-microsoft-com:office:excel\">",
        "<head><meta http-equiv=\"Content-Type\" content=\"text/html; charset=utf-8\">",
        "<title>交易明细查询</title></head><body>",
        "<table border=1 cellspacing=0 cellpadding=2>",
    ]
    for row in rows:
        parts.append("<tr>")
        for cell in row:
            parts.append(f"<td>{_html_escape(str(cell or ''))}</td>")
        parts.append("</tr>")
    parts.append("</table></body></html>")
    target.write_text("\n".join(parts), encoding="utf-8")


def _html_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace(" ", "&nbsp;"))


def write_csv(grid: Grid, target: Path) -> None:
    """写成网银导出的那种「``#`` 注释头 + 带引号字段」的文本。"""
    import csv
    import io

    lines = [
        '"# 招商银行交易记录"',
        '"# 导出时间: [            2026-10-08 09:51:20]"',
        '"# 账    号: [一卡通:6214********7761   招商银行]"',
        '"# 币    种: [                         人民币]"',
        '"# 起始日期: [20260901]   终止日期: [20260930]"',
        '"# 过滤设置:  无"',
        '""',
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in grid.rows:
        writer.writerow([str(cell or "") for cell in row])
    lines.append(buffer.getvalue().strip("\r\n"))
    text = "\r\n".join(lines) + "\r\n"
    text += '"# 收入合计: 5 笔，共 412965.41 元"\r\n'
    text += '"# 支出合计: 0 笔，共 0.00 元"\r\n'
    target.write_text(text, encoding="utf-8-sig")


# ----------------------------------------------------------------------
# PDF：按原文件的列坐标重排一份假的
# ----------------------------------------------------------------------

def _pdf_column_roles(header: list[dict]) -> list[str]:
    """按**表头文字**给每列定角色。

    不按列序号写死 —— 兴业那份是「交易时间/摘要/凭证代号/支出/收入/收支方向/
    账户余额/对方账号/对方户名」，光大那份是「序号/交易日期/时间/借/贷/
    交易金额/账户余额/对方账号/对方名称/凭证号/摘要/流水号」，列序完全不同。
    """
    roles: list[str] = []
    for label in header:
        text = str(label.get("text", "")).strip()
        if "对方账号" in text or "对方账户" in text:
            roles.append("ACCOUNT")
        elif ("对方户名" in text or "对方名称" in text
              or "对方单位" in text or "对方名" in text):
            roles.append("NAME")
        elif text in ("支出", "收入", "交易金额", "借方发生额", "贷方发生额",
                      "存入金额", "支出金额", "借方金额", "贷方金额"):
            roles.append("AMOUNT")
        elif "余额" in text:
            roles.append("BALANCE")
        else:
            roles.append("")
    return roles


def _scrub_pdf_text(text: str, names: dict[str, str], counter: list[int]) -> str:
    """页眉页脚里的自由文本也要脱敏：户名换假名、账号打码、金额 ×100。"""

    def fake_for(value: str) -> str:
        if value not in names:
            names[value] = fake_name(counter[0])
            counter[0] += 1
        return names[value]

    # 户名：「户名:XXX」到下一个标签为止
    def replace_name(match: re.Match) -> str:
        return match.group(1) + fake_for(match.group(2).strip())

    text = re.sub(r"(户\s*名[:：]\s*)(.+?)(?=\s{2,}\S|$)", replace_name, text)
    # 账号：标签后的一串数字
    text = re.sub(r"((?:活期)?账\s*号[:：]\s*)([0-9A-Za-z*\-]+)",
                  lambda m: m.group(1) + mask_digits(m.group(2)), text)
    # 自报金额与余额：跟明细一样 ×100，否则自检会报一条假警
    # （「文件自称收入合计 1305007.00，解析出 130500700.00」——
    #  那不是解析错了，是夹具脱敏只改了明细、没改页眉上的合计）。
    # 标签集复用 _META_AMOUNT_LABELS：借方发生额、贷方发生额、
    # 期末账户余额、本期总收入金额……都在里面。
    text = scale_meta_amounts(text)
    return text


def _scrub_pdf_spec(spec: dict) -> dict:
    """把 PDF spec 里的真数据换成假的。

    PDF 夹具是照着**原文件的版面**重排的 —— 版面要真，数据必须假。
    原文件里那些企业名、账号、金额全是**真的**：不改就等于把
    一家真公司的账户明细存进仓库。改的粒度跟别的夹具一致 ——
    金额 ×100、账号中段打码、户名换假名。
    """
    names: dict[str, str] = {}
    counter = [0]
    roles = _pdf_column_roles(spec["header"])

    rows = []
    for row in spec["rows"]:
        cells = []
        for index, cell in enumerate(row["cells"]):
            text, x, width = cell
            role = roles[index] if index < len(roles) else ""
            value = text
            if role in ("AMOUNT", "BALANCE"):
                value = scale_amount(value)
            elif role == "ACCOUNT":
                value = mask_digits(value)
            elif role == "NAME" and text.strip():
                if text not in names:
                    names[text] = fake_name(counter[0])
                    counter[0] += 1
                value = names[text]
            cells.append((value, x, width))
        rows.append({"y": row["y"], "cells": cells})

    def scrub_lines(lines: list[dict]) -> list[dict]:
        return [{**line, "text": _scrub_pdf_text(line["text"], names, counter)}
                for line in lines]

    return {
        **spec,
        "meta": scrub_lines(spec.get("meta", [])),
        "footer": scrub_lines(spec.get("footer", [])),
        "rows": rows,
    }


def write_pdf(spec: dict, target: Path) -> None:
    """用 reportlab 排一份 PDF 夹具。

    坐标取自**原文件**（列的位置就是解析器要认的东西），数据是假的。
    """
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    page_width, page_height = spec.get("size", landscape(A4))
    pdf = canvas.Canvas(str(target), pagesize=(page_width, page_height))
    font_size = spec.get("font_size", 8)

    def draw(text: str, x: float, y: float) -> None:
        pdf.setFont("STSong-Light", font_size)
        pdf.drawString(x, y, text)

    for line in spec.get("meta", []):
        draw(line["text"], line["x"], line["y"])
    for index, label in enumerate(spec["header"]):
        draw(label["text"], label["x"], label["y"])
    for row in spec["rows"]:
        for text, x, width in row["cells"]:
            pieces = _wrap(text, width, font_size)
            for offset, piece in enumerate(pieces):
                draw(piece, x, row["y"] - offset * (font_size + 2))
    # 页尾的合计语也要画：**文件自报的总笔数、总金额就写在这儿**，
    # 解析器要靠它跟解析结果对账（漏了几行一眼就能看出来）。
    # 漏画的话，夹具就少了这条自检的覆盖。
    for line in spec.get("footer", []):
        draw(line["text"], line["x"], line["y"])
    pdf.showPage()
    pdf.save()


def _wrap(text: str, width: float, font_size: float) -> list[str]:
    """按宽度折行（中文按字折，英文数字尽量不拆）。"""
    from reportlab.pdfbase import pdfmetrics

    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for char in str(text):
        candidate = current + char
        if pdfmetrics.stringWidth(candidate, "STSong-Light", font_size) > width and current:
            lines.append(current)
            current = char
        else:
            current = candidate
    lines.append(current)
    return lines


#: 三份 PDF 的版面规格（两份兴业 + 一份光大对公）。
#:
#: 列的 x 坐标取自真实文件，行数据是假的。
PDF_SPECS: list[dict] = [
    {
        "name": "cib_pdf.pdf",
        # 尺寸照抄原文件（612x792）：坐标也是从原文件抄的，
        # 页面画错了尺寸内容就整体溢出，PDF 看起来是张白纸。
        "size": (612, 792),
        "font_size": 8,
        "meta": [
            {"text": "兴业银行", "x": 282.5, "y": 726.9},
            {"text": "交易明细对账单", "x": 264.4, "y": 698.8},
            {"text": "页号:1", "x": 531.4, "y": 688.0},
            {"text": "打印日期:2026-10-06  本期时间范围:2026-09-01 00:00:00 至 2026-09-30 23:59:59",
             "x": 58.9, "y": 673.0},
            {"text": "活期账号:152550100100190107  币种:人民币", "x": 58.9, "y": 664.7},
            {"text": "户名:福建省泉州紫欣工贸有限公司  开户行:兴业银行泉州泉秀支行",
             "x": 58.8, "y": 656.5},
            {"text": "期末账户余额:776,012.10", "x": 58.9, "y": 648.2},
        ],
        "header": [
            {"text": "交易时间 ", "x": 71.4, "y": 623.2},
            {"text": "摘要 ", "x": 133.8, "y": 623.2},
            {"text": "凭证代号 ", "x": 181.4, "y": 623.1},
            {"text": "支出 ", "x": 243.7, "y": 623.2},
            {"text": "收入 ", "x": 299.0, "y": 623.2},
            {"text": "收支方向 ", "x": 346.5, "y": 623.2},
            {"text": "账户余额 ", "x": 401.2, "y": 623.1},
            {"text": "对方账号 ", "x": 456.2, "y": 623.1},
            {"text": "对方户名", "x": 511.2, "y": 623.2},
        ],
        "rows": [
            {"y": 610.8, "cells": [
                ("2026-09-26 07:20:10", 62.7, 46.0), ("高速公路通行费", 117.8, 47.0),
                ("", 181.4, 30.0), ("180.50", 243.2, 16.0), ("0.00", 299.5, 14.0),
                ("支出 ", 354.5, 13.0), ("763,111.60", 401.2, 30.0),
                ("117010100100044247", 444.7, 53.0),
                ("福建省高速公路集团有限公司电子收费管理中心", 499.2, 54.0),
            ]},
            {"y": 584.1, "cells": [
                ("2026-09-21 01:47:42", 62.7, 46.0), ("存款利息 ", 127.7, 27.0),
                ("", 181.4, 30.0), ("0.00 ", 244.5, 14.0), ("2,280.00", 297.8, 17.0),
                ("收入 ", 354.7, 13.0), ("763,292.10", 401.2, 30.0),
                ("152550163310000188", 444.7, 53.0), ("兴业银行", 512.9, 27.0),
            ]},
            {"y": 551.5, "cells": [
                ("2026-09-15 15:37:15", 62.7, 46.0), ("代发工资 ", 127.7, 27.0),
                ("", 181.4, 30.0), ("15,000.00", 236.1, 30.0), ("0.00 ", 299.5, 14.0),
                ("支出 ", 354.5, 13.0), ("761,012.10", 401.2, 30.0), ("", 456.2, 30.0),
                ("", 511.2, 30.0),
            ]},
            {"y": 520.8, "cells": [
                ("2026-09-03 07:30:04", 62.7, 46.0), ("手续费 ", 127.7, 27.0),
                ("", 181.4, 30.0), ("500.00", 243.2, 16.0), ("0.00 ", 299.5, 14.0),
                ("支出 ", 354.5, 13.0), ("776,012.10", 401.2, 30.0), ("", 456.2, 30.0),
                ("", 511.2, 30.0),
            ]},
        ],
        "footer": [
            {"text": "本期总笔数：4", "x": 58.8, "y": 463.9},
            {"text": "本期总收入金额：2,280.00", "x": 58.8, "y": 455.7},
            {"text": "本期总支出金额：15,680.50", "x": 58.8, "y": 447.4},
        ],
    },
    {
        "name": "ceb_corp_pdf.pdf",
        "size": (595.28, 841.89),
        "font_size": 9,
        "meta": [
            {"text": "中国光大银行对公账户对账单", "x": 196.8, "y": 777.6},
            {"text": "查询日期：2026-10-06 09:56:01", "x": 23.8, "y": 727.1},
            {"text": "交易日期：20260901-20260930", "x": 206.5, "y": 727.1},
            {"text": "借贷方向：全部", "x": 400.1, "y": 727.1},
            {"text": "账号：79600188000046872", "x": 23.7, "y": 711.7},
            {"text": "账户名称：泉州润维辰商业管理有限公司", "x": 206.2, "y": 711.8},
            {"text": "借方发生额：-", "x": 23.8, "y": 696.4},
            {"text": "贷方发生额：1,305,007.00", "x": 206.5, "y": 696.4},
            {"text": "借方笔数：-", "x": 23.8, "y": 681.1},
            {"text": "贷方笔数：2", "x": 206.5, "y": 681.1},
        ],
        "header": [
            {"text": "序号 ", "x": 14.7, "y": 657.4},
            {"text": "交易日期 ", "x": 44.9, "y": 657.5},
            {"text": "时间 ", "x": 99.4, "y": 657.4},
            {"text": "借/贷 ", "x": 134.1, "y": 657.4},
            {"text": "交易金额 ", "x": 169.4, "y": 657.5},
            {"text": "账户余额 ", "x": 244.4, "y": 657.5},
            {"text": "对方账号 ", "x": 320.8, "y": 657.4},
            {"text": "对方名称 ", "x": 374.8, "y": 657.4},
            {"text": "凭证号 ", "x": 433.7, "y": 657.4},
            {"text": "摘要 ", "x": 500.7, "y": 657.4},
            {"text": "流水号", "x": 553.2, "y": 657.4},
        ],
        "rows": [
            {"y": 629.2, "cells": [
                ("1 ", 14.1, 3.0), ("20260921 ", 43.6, 35.0), ("022805 ", 91.5, 26.0),
                ("贷方 ", 134.0, 17.0), ("7.00 ", 213.5, 18.0), ("1,305,007.00", 266.6, 39.0),
                ("79600105570000001", 316.7, 40.0), ("应付单位活期存款利息", 370.7, 35.0),
                ("", 433.7, 25.0), ("结息", 478.9, 17.0), ("997960000003", 550.5, 31.0),
            ]},
            {"y": 583.1, "cells": [
                ("2 ", 13.6, 3.0), ("20260917 ", 43.6, 35.0), ("115601 ", 92.1, 26.0),
                ("贷方 ", 134.0, 17.0), ("1,305,000.00 ", 191.6, 39.0), ("1,305,000.00", 266.6, 39.0),
                ("64100001075", 316.5, 40.0), ("泉州市速凡供应链管理有限公司", 370.7, 35.0),
                ("", 433.7, 25.0), ("物业费", 478.8, 26.0), ("901M27000348", 550.5, 31.0),
            ]},
        ],
        "footer": [
            {"text": "银行提示：本明细清单仅限于对账使用，本对账单被伪造、变造、篡改，不具有法律效力。",
             "x": 70.6, "y": 542.3},
        ],
    },
    {
        # 新恒强建设基本户（兴业对账单，**与紫欣那份同一套模板**）。
        #
        # 它与 ``cib_pdf.pdf`` 的列位置完全一样，区别在**户名、账号、
        # 对方户名与金额**。这一份的价值在于：长户名
        # 「鑫方泰商务服务（泉州）有限公司」在这里被折成两行，
        # 折行能不能正确合并就靠它。
        #
        # **行距是 35pt，不是原文件的 16.5pt**：夹具是用 reportlab 重排的，
        # 折行时每段往下掉 ``font_size + 2`` = 10pt；行距只有 16.5pt 的话，
        # 上一行折下来的第二段会掉进下一行，两行的内容串成一团
        # （对方户名与摘要互相污染）。行距拉开就与真实效果一致了。
        "name": "cib_construction_pdf.pdf",
        "size": (612, 792),
        "font_size": 8,
        "meta": [
            {"text": "兴业银行", "x": 282.5, "y": 721.1},
            {"text": "交易明细对账单", "x": 264.4, "y": 692.9},
            {"text": "页号:1", "x": 531.4, "y": 684.6},
            {"text": "打印日期:2026-10-06  本期时间范围:2026-09-01 00:00:00 至 2026-09-30 23:59:59",
             "x": 58.9, "y": 669.6},
            {"text": "活期账号:152550100100025062  币种:人民币", "x": 58.9, "y": 661.3},
            {"text": "户名:泉州市新恒强建设发展有限公司  开户行:兴业银行泉州泉秀支行",
             "x": 58.8, "y": 653.0},
            {"text": "期末账户余额:97,772.91", "x": 58.9, "y": 644.7},
        ],
        "header": [
            {"text": "交易时间 ", "x": 71.4, "y": 619.7},
            {"text": "摘要 ", "x": 133.8, "y": 619.7},
            {"text": "凭证代号 ", "x": 181.4, "y": 619.6},
            {"text": "支出 ", "x": 243.7, "y": 619.7},
            {"text": "收入 ", "x": 299.0, "y": 619.6},
            {"text": "收支方向 ", "x": 346.5, "y": 619.6},
            {"text": "账户余额 ", "x": 401.2, "y": 619.6},
            {"text": "对方账号 ", "x": 456.2, "y": 619.7},
            {"text": "对方户名", "x": 511.2, "y": 619.7},
        ],
        "rows": [
            # 对方户名被折成两行（「鑫方泰商务服务」/「（泉州）有限公司」）
            {"y": 605.0, "cells": [
                ("2026-09-22 09:04:19", 62.7, 46.0), ("汇款汇入", 127.7, 47.0),
                ("", 181.4, 30.0), ("0.00", 244.5, 16.0), ("7,500.00", 292.9, 16.0),
                ("收入 ", 354.7, 13.0), ("97,772.91", 401.1, 30.0),
                ("595903895310001", 446.0, 53.0),
                ("鑫方泰商务服务（泉州）有限公司", 502.6, 54.0),
            ]},
            # 对方账号也被折成两行（15 位 + 末两位）
            {"y": 570.0, "cells": [
                ("2026-09-21 01:47:42", 62.7, 46.0), ("存款利息", 127.7, 47.0),
                ("", 181.4, 30.0), ("0.00", 244.5, 16.0), ("11.72", 298.3, 16.0),
                ("收入 ", 354.7, 13.0), ("90,272.91", 401.1, 30.0),
                ("152550163310000188", 444.7, 53.0), ("兴业银行", 512.9, 27.0),
            ]},
            {"y": 535.0, "cells": [
                ("2026-09-14 17:28:13", 62.7, 46.0), ("财税库行-税费/社保", 114.3, 47.0),
                ("", 181.4, 30.0), ("709.32", 241.2, 16.0), ("0.00", 299.5, 16.0),
                ("支出 ", 354.5, 13.0), ("90,261.19", 401.1, 30.0),
                ("152300100100223728", 444.7, 53.0),
                ("统一版财税库行待结算财政款项账户", 499.3, 54.0),
            ]},
            {"y": 500.0, "cells": [
                ("2026-09-12 14:55:06", 62.7, 46.0), ("财税库行-税费/社保", 114.3, 47.0),
                ("", 181.4, 30.0), ("1,498.43", 238.2, 16.0), ("0.00", 299.5, 16.0),
                ("支出 ", 354.5, 13.0), ("90,970.51", 401.1, 30.0),
                ("152300100100223728", 444.7, 53.0),
                ("统一版财税库行待结算财政款项账户", 499.3, 54.0),
            ]},
        ],
        "footer": [
            {"text": "本期总笔数：4", "x": 58.8, "y": 470.0},
            {"text": "本期总收入金额：7511.72", "x": 58.8, "y": 462.0},
            {"text": "本期总支出金额：2207.75", "x": 58.8, "y": 454.0},
        ],
    },
]


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

#: 夹具文件名：真名 → 夹具名（ASCII）。
#:
#: 仓库里不放中文文件名：脚本传参、CI 收集、跨平台路径处理都要为它多绕一层，
#: 而夹具名对测试没有任何意义 —— 测试只认内容。
FIXTURE_NAMES = {
    "琼建9月.xls": "ccb.xls",
    "玲平安9月份.xls": "pingan.xls",
    "星悦荟基本户9月.xls": "nongshang_base.xls",
    "慧光大9月份.xls": "ceb_personal.xls",
    "超光大9月份.xls": "ceb_personal2.xls",
    "泓农商9月份.xls": "nongshang_html.xls",
    "秋农商9月份.xls": "nongshang_html2.xls",
    "玲招商9月.xlsx": "cmb_csv.xlsx",
    "紫欣一般户9月.xlsx": "nongshang_xlsx.xlsx",
    "星悦荟一般户9月.xlsx": "nongshang_xlsx2.xlsx",
    "新恒强投资基本户9月.xlsx": "cmbc_xlsx.xlsx",
}


def build(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    goldens: dict[str, dict] = {}

    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".pdf":
            continue
        data = path.read_bytes()
        grids = read_grids(data)
        best = grids[0]
        best_score = -1
        for grid in grids:
            parsed = parse_grid(grid)
            score = len(parsed.column_mapping)
            if score > best_score:
                best_score = score
                best = grid
        scrubbed = scrub_grid(best)
        out = target / FIXTURE_NAMES.get(path.name, path.name)
        if best.container == "XLSX":
            write_xlsx(scrubbed, out)
        elif best.container == "XLS":
            write_xls(scrubbed, out)
        elif best.container == "HTML":
            write_html(scrubbed, out)
        else:
            write_csv(scrubbed, out)
        goldens[out.name] = golden_of(out)

    for spec in PDF_SPECS:
        out = target / spec["name"]
        # 先脱敏再排版：原文件里的户名、账号、金额全是真的。
        write_pdf(_scrub_pdf_spec(spec), out)
        goldens[out.name] = golden_of(out)

    (target / "goldens.json").write_text(
        json.dumps(goldens, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"生成 {len(goldens)} 份夹具 → {target}")


def golden_of(path: Path) -> dict:
    """把夹具解析一遍，记下**金标准**。"""
    parsed = parse_statement(path.read_bytes())
    first = parsed.rows[0] if parsed.rows else None
    last = parsed.rows[-1] if parsed.rows else None
    return {
        "container": parsed.container,
        "header_row": parsed.header_row,
        "column_mapping": parsed.column_mapping,
        "row_count": len(parsed.rows),
        "income_total": str(parsed.income_total),
        "expense_total": str(parsed.expense_total),
        "closing_balance": str(parsed.meta.closing_balance) if parsed.meta.closing_balance else None,
        # 文件**自报**的笔数与金额：解析器要拿它们跟解析结果对账，
        # 对不上就说明漏了行。金标准把它们记下来，这份对账本身才有人盯。
        "declared_total_count": parsed.meta.declared_total_count,
        "declared_income_total": (str(parsed.meta.declared_income_total)
                                  if parsed.meta.declared_income_total is not None else None),
        "declared_expense_total": (str(parsed.meta.declared_expense_total)
                                   if parsed.meta.declared_expense_total is not None else None),
        "period_start": parsed.meta.period_start.isoformat() if parsed.meta.period_start else None,
        "period_end": parsed.meta.period_end.isoformat() if parsed.meta.period_end else None,
        "diagnosis": parsed.diagnosis,
        "first": _row_golden(first),
        "last": _row_golden(last),
    }


def _row_golden(row) -> dict | None:
    if row is None:
        return None
    return {
        "tx_date": row.tx_date.isoformat() if row.tx_date else None,
        "direction": row.direction,
        "amount": str(row.amount) if row.amount is not None else None,
        "balance": str(row.balance) if row.balance is not None else None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成脱敏的对账单测试夹具")
    parser.add_argument("--source", default=str(Path.home() / "Downloads" / "银行流水"))
    parser.add_argument("--target", default=str(ROOT / "backend" / "tests" / "fixtures" / "statements"))
    args = parser.parse_args()
    build(Path(args.source), Path(args.target))
