"""用友 UFO 5.10 / 8.00 的 .rep/.rbk 模板迁移。

OLE 的 UFO~REP 流包含主网格、字体、文本对象、公式对象及历史/缓存对象。
必须用网格内的对象 ID 和坐标定位当前公式，不能从二进制中搜索到一条就导入。
只迁移结构和公式；历史金额、编制单位及打印说明不成为新账套的财务数据。

表体的边界有三条互相独立的判据（见 parse_ufo_report 里的注释）：列按尾部空列裁剪，
行按「最后一个带行次的行」「最后一个带金额公式的行」「最后一个带金额数值的行」取靠后者。
行次列里大类行走的是中文数字（「一、二、三、四」），只认阿拉伯数字会把「四、利润总额」
那一行整行切掉 —— 而且不报错，导进来的是半张表；行次列整列是空的模板更常见，
光认行次连表体到哪儿都定位不到。所以边界最终落在「金额列有没有内容」上。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import unicodedata
from pathlib import Path
from typing import Any

import olefile

from app.reports.formula import ADDR_RE, FormulaError, Parser, col_to_no, no_to_col

MAX_TEMPLATE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 500
MAX_COLS = 32
_ALLOWED_FUNCTIONS = {"科目", "合计", "SUM", "上期", "单元格", "IF", "四舍五入", "ROUND", "ABS", "MAX", "MIN", "绝对值"}
_SUBJECT = re.compile(r'科目\("([^"\\]+)",')
_UFO_CALL = re.compile(r"(?<![A-Za-z0-9_])(QC|QM|FS|LFS)\(([^()]*)\)", re.I)
# 行次列里的行号标记。明细行用阿拉伯数字，**大类行用中文数字**（「一、二、三、四」），
# 所以两种写法都得认 —— 只认阿拉伯数字会把最后一行大类（通常是「四、利润总额」）
# 连同它后面的行一起切掉，导进来只剩半张表。
#
# 写法上还有一堆变体：带括号「（1）」、带序号点「1.」「1、」、带子项号「1-1」。
# 行次列是模板作者手写的，认得越宽，表体边界越不容易被切错。
_LINE_NO = re.compile(r"[（(]?\s*(?:\d+(?:[-—]\d+)*|[〇零一二三四五六七八九十百千]+)\s*[)）]?[\.、,，:：]?")
# 金额格里的数值文本。表体边界要认「有内容」，而内容有两种来源：公式和手填的数。
_AMOUNT_TEXT = re.compile(r"[+-]?\d+(?:\.\d+)?")
# 列名里的字眼。行次列的表头未必写「行次」（也有「行号」「序号」），金额列的表头
# 未必写「金额」（也有「本期数」「本年累计数」）。认字眼比死认位置宽，
# 但认不准时宁可拒收 —— 宁可人拿不到模板，也不能让人拿着对不上的报表去做账。
_LINE_HEADER_WORDS = ("行次", "行号", "序号")
_AMOUNT_HEADER_WORDS = ("金额", "余额", "累计", "本期", "本月", "数量", "合计")


class TemplateImportError(ValueError):
    """外部模板不能可靠转换。"""


def subject_codes(cells: list[dict]) -> list[str]:
    return sorted({code for cell in cells for code in _SUBJECT.findall(cell["formula"])})


def validate_formula(formula: str, rows: set[int], cols: set[int]) -> None:
    """除语法外校验函数及引用，防止不支持的函数存入后才报错。"""
    parser = Parser(formula)
    parser.parse()
    for index, token in enumerate(parser.tokens):
        if token.kind == "IDENT" and parser.tokens[index + 1].value == "(":
            if token.value not in _ALLOWED_FUNCTIONS:
                raise FormulaError(f"未知函数：{token.value}")
        if token.kind == "CELL":
            col, row = token.value
            if row not in rows or col not in cols:
                raise FormulaError(f"引用 {no_to_col(col)}{row} 超出报表范围")


def _stream(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_TEMPLATE_BYTES:
        raise TemplateImportError("模板不能为空，单个文件最大 5 MB")
    try:
        with olefile.OleFileIO(io.BytesIO(raw)) as ole:
            if not ole.exists("UFO~REP"):
                raise TemplateImportError("文件中没有 UFO 报表数据，请选择原系统导出的 .rep 或 .rbk 模板")
            # 声明的流大小也必须过容量闸，避免畸形目录驱动超大内存分配。
            if ole.get_size("UFO~REP") > MAX_TEMPLATE_BYTES:
                raise TemplateImportError("UFO 报表内容超过 5 MB 上限")
            data = ole.openstream("UFO~REP").read()
    except TemplateImportError:
        raise
    except (OSError, ValueError, IndexError, struct.error) as exc:
        raise TemplateImportError("无法读取模板，文件可能损坏或不是用友 UFO 格式") from exc
    if data[:9] not in (b"\x00UFO5.10\x00", b"\x00UFO8.00\x00"):
        raise TemplateImportError("当前支持 UFO 5.10 / 8.00 模板，此文件版本无法识别")
    return data


def _last_used_column(grid: list[list[dict]]) -> int:
    """最后一个真正有内容的列号（1 起）。整张表都没有内容时返回 0。

    **UFO 的「列数」是网格预留的宽度，不是「用了几列」。** 从更宽的模板里
    复制/另存出来的利润表常见 5/6/7 列，而第 5 列往后一个格子都没有内容 ——
    那不是另一种版式，只是留白。把这种模板按「7 列」拒掉，理由（不支持）
    站不住：它和 4 列那张在结构上完全一样。

    所以判版式前先裁掉**尾部连续空列**。中间或首部的空列不裁 —— 那种模板的
    列序里可能还夹着别的语义（比较数、上年同期），悄悄跳过一列等于丢掉一列数。
    """
    last = 0
    for row in grid:
        for index, cell in enumerate(row):
            if cell["text"].strip() or cell["formula"].strip():
                last = max(last, index + 1)
    return last


def _grid(data: bytes) -> tuple[list[list[dict]], str]:
    if len(data) < 1816:
        raise TemplateImportError("模板结构不完整")
    row_count, col_count = struct.unpack_from("<HH", data, 452)
    font_count = struct.unpack_from("<I", data, 44)[0]
    # 这里只挡畸形与超宽网格（下面的逐格循环要按 col_count 开数组）；
    # 「是不是 4 列 / 8 列」要等网格解析完、裁掉尾部空列之后才能判。
    if not (1 <= row_count <= MAX_ROWS and 1 <= col_count <= MAX_COLS and 1 <= font_count <= 100):
        raise TemplateImportError("模板结构超出支持范围，请使用原系统导出的标准 UFO 模板")
    grid_start = 1816 + row_count * 16 + col_count * 12
    text_start = grid_start + row_count * col_count * 12 + font_count * 360
    if text_start >= len(data):
        raise TemplateImportError("模板行列或字体结构不完整")

    # 公式对象：ID(4) + 状态(2) + 行列(4) + 状态(2) + 行列(4)
    #           + 标志(4，固定 1) + 字节长度(2) + GBK 正文。
    formulas: dict[tuple[int, int, int], str] = {}
    formula_start = len(data)
    for marker in re.finditer(b"\x01\x00\x00\x00", data[text_start:]):
        offset = text_start + marker.start()
        if offset < text_start + 16 or offset + 6 > len(data):
            continue
        ident, _, row, col, _, row2, col2, _, length = struct.unpack_from("<IHHHHHHIH", data, offset - 16)
        if not (row == row2 and col == col2 and 1 <= row <= row_count and 1 <= col <= col_count
                and 0 < ident < 100_000 and 0 < length <= 4000 and offset + 6 + length <= len(data)):
            continue
        raw_formula = data[offset + 6:offset + 6 + length]
        try:
            source = raw_formula.decode("gbk")
        except UnicodeDecodeError:
            continue
        if any(ord(char) < 32 for char in source):
            continue
        formula_start = min(formula_start, offset - 16)
        formulas[(ident, row, col)] = source

    if not formulas or (formula_start - text_start) % 76:
        raise TemplateImportError("无法定位主报表公式对象，模板可能使用了不同的 UFO 存储版本")
    texts: dict[int, str] = {}
    for offset in range(text_start, formula_start, 76):
        ident = struct.unpack_from("<I", data, offset)[0]
        raw_text = data[offset + 4:offset + 76].split(b"\x00", 1)[0]
        try:
            text = raw_text.decode("gbk")
        except UnicodeDecodeError:
            raise TemplateImportError("模板文本编码无法识别") from None
        texts[ident] = text

    grid: list[list[dict]] = []
    for row in range(1, row_count + 1):
        cells = []
        for col in range(1, col_count + 1):
            ident, flags, _ = struct.unpack_from("<III", data, grid_start + ((row - 1) * col_count + col - 1) * 12)
            is_formula = bool(flags & 0x4000)
            text = texts.get(ident, "") if ident and not is_formula else ""
            formula = formulas.get((ident, row, col), "") if ident and is_formula else ""
            cells.append({"text": text, "formula": formula, "missing": bool(ident and is_formula and not formula)})
        grid.append(cells)
    # 版式判定放在解析之后：先裁掉尾部空列（见 _last_used_column 的说明）。
    columns = _last_used_column(grid)
    if columns not in (4, 8):
        raise TemplateImportError("当前支持 4 列利润表和 8 列资产负债表，请使用这两类 UFO 模板")
    if columns < col_count:
        grid = [row[:columns] for row in grid]
    return grid, data[1:8].decode("ascii")


def _args(text: str) -> list[str]:
    # UFO 取数参数没有嵌套；逗号在双引号内不作为分隔符。
    return [part.strip() for part in re.split(r',(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', text)]


def _translate_call(match: re.Match) -> str:
    name = match.group(1).upper()
    args = _args(match.group(2))
    code = args[0].strip('"') if args else ""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", code):
        raise TemplateImportError("无法识别科目编码")
    period = args[1] if len(args) > 1 else ""
    direction = args[2].strip('"') if len(args) > 2 else ""
    if period not in ("", "月", "全年") or direction not in ("", "借", "贷"):
        raise TemplateImportError("指定期间或取数方向需要人工配置")
    # 第 4 参数为外部账套，第 5 参数为年度；拒绝跨账套/固定年度及辅助核算参数。
    if len(args) > 3 and args[3]:
        raise TemplateImportError("包含外部账套或辅助核算条件")
    if len(args) > 4 and args[4] not in ("", "年"):
        raise TemplateImportError("包含固定年度或辅助核算条件")
    if any(args[5:]):
        raise TemplateImportError("包含辅助核算、外币或其他扩展参数")
    if name in ("FS", "LFS"):
        if direction not in ("借", "贷") or period not in ("", "月"):
            raise TemplateImportError("发生额公式需要明确借贷方向和当前期间")
        kind = (("本期借方" if direction == "借" else "本期贷方") if name == "FS"
                else ("借方累计" if direction == "借" else "贷方累计"))
        return f'科目("{code}","{kind}",{{{{本期}}}})'
    if name == "QC":
        kind, placeholder = "期初余额", "{{年初}}" if period == "全年" else "{{本期}}"
        signed_kind = "期初"
    else:
        if period == "全年":
            raise TemplateImportError("全年期末余额需要人工配置")
        kind, placeholder, signed_kind = "期末余额", "{{本期}}", "期末"
    if direction:
        signed = f'科目("{code}","{signed_kind}",{placeholder})'
        return f'MAX({"-" if direction == "贷" else ""}{signed},0)'
    return f'科目("{code}","{kind}",{placeholder})'


def _translate(source: str, row_start: int, row_end: int, col_count: int, source_to_target: dict[int, int] | None = None) -> str:
    if "?" in source or re.search(r"\bselect\s*\(", source, re.I):
        raise TemplateImportError("包含 UFO 跨表页 select() 逻辑，需要人工配置")
    formula = _UFO_CALL.sub(_translate_call, source)
    column_map = source_to_target or {column: column for column in range(1, col_count + 1)}
    target_col_count = max(column_map.values())
    # 去掉表头后重编号，$ 锁定引用也要迁移到新坐标；字符串和期间占位符不改。
    parts = re.split(r'("(?:[^"\\]|\\.)*"|\{\{.*?\}\})', formula)
    for index in range(0, len(parts), 2):
        def move(match: re.Match) -> str:
            source_col = col_to_no(match.group(2))
            row = int(match.group(4))
            target_col = column_map.get(source_col)
            if target_col is None or not (row_start <= row <= row_end):
                raise TemplateImportError(f"引用 {match.group(0)} 位于表头、固定列或报表范围外")
            return f'{match.group(1)}{no_to_col(target_col)}{match.group(3)}{row - row_start + 1}'
        parts[index] = ADDR_RE.sub(move, parts[index])
    translated = "".join(parts)
    validate_formula(translated, set(range(1, row_end - row_start + 2)), set(range(1, target_col_count + 1)))
    return translated


def _side(text: str) -> tuple[str, int, bool, str]:
    label = text.strip()
    indent = min(4, (len(text) - len(text.lstrip())) // 2)
    bold = any(word in label for word in ("合计", "总计", "营业利润", "利润总额", "净利润"))
    kind = "SHADE" if label in ("流动资产：", "非流动资产：", "流动负债：", "非流动负债：", "资产总计", "负债合计", "负债和所有者权益（或股东权益）总计") else "ITEM"
    return label, indent, bold, kind


def _is_header_label(text: str) -> bool:
    """表头格里是不是列名。

    认「行次类」或「金额类」的字眼，用来确认这一行确实是表头。
    死认「行次」两个字太窄：行次列表头写成「行号」「序号」，或者整列空着
    （自建模板常见），都会让整张模板在进入表体判定之前就被拒掉。
    """
    label = re.sub(r"\s+", "", text)
    return any(word in label for word in _LINE_HEADER_WORDS + _AMOUNT_HEADER_WORDS)


def _is_amount_text(text: str) -> bool:
    """金额格里的文本是不是一个数。

    UFO 的格分两种：**标了公式的**（内容在公式对象里）和**没标公式的**（内容是一段文本）。
    导入只迁公式，但表体边界不能因此把没公式的行切掉 —— 自建模板的金额多半是手填的数，
    那也是表体的一部分。所以边界判定要认数值文本。

    认「像数字的」而不是「非空的」：表尾的签字说明（「单位负责人：会计主管：」）也在格子里，
    那是文字不是金额，认进来表体就会多出一行假行。数值要容忍千分位、货币符号、
    全角字符和括号负数 —— 模板里手填的数什么写法都有。
    """
    raw = unicodedata.normalize("NFKC", text).strip()
    if not raw:
        return False
    negative = raw.startswith("(") and raw.endswith(")")
    if negative:
        raw = raw[1:-1].strip()
    raw = raw.lstrip("¥$").replace(",", "").replace(" ", "").rstrip("元").strip()
    return bool(_AMOUNT_TEXT.fullmatch(raw))


def _amount_cell_has_content(cell: dict) -> bool:
    """金额格算不算「有内容」。公式、数值、以及标了公式但没读出公式的格都算。"""
    if cell["formula"].strip() or cell["missing"]:
        return True
    return _is_amount_text(cell["text"])


def parse_ufo_report(raw: bytes, filename: str) -> dict[str, Any]:
    filename = (filename or "").replace("\\", "/").split("/")[-1]
    if Path(filename).suffix.lower() not in (".rep", ".rbk"):
        raise TemplateImportError("请选择用友 UFO 报表模板（.rep 或 .rbk）")
    grid, version = _grid(_stream(raw))
    col_count = len(grid[0])
    report_type = "BALANCE_SHEET" if col_count == 8 else "INCOME_SHEET"
    # 表头行：第一列写着「项目」或「资产」，而且这一行带着列名。
    #
    # 死认「行次」两个字太窄：行次列表头写成「行号」「序号」，或者整列空着
    # （自建模板常见），都会让整张模板在进入表体判定之前就被拒掉。
    header_index = next((index for index, row in enumerate(grid[:12])
                         if re.sub(r"\s+", "", row[0]["text"]) in ("资产", "项目")
                         and any(_is_header_label(cell["text"]) for cell in row)), None)
    if header_index is None:
        raise TemplateImportError("未找到项目/行次表头，无法可靠识别报表主体")
    row_start = header_index + 2
    # 主体由表体的内容定位，空的分节行会一起保留；表尾说明及缓存数据不导入。
    header = grid[header_index]
    header_labels = {column: re.sub(r"\s+", "", header[column - 1]["text"])
                     for column in range(1, col_count + 1)}
    # 行次列（界面上那一列行号）：资产负债表的位置是固定的（源第 2、6 列），
    # 利润表靠表头认 —— 见下面的说明。
    header_line_cols = {column for column, label in header_labels.items()
                        if column != 1 and any(word in label for word in _LINE_HEADER_WORDS)}
    if col_count == 8:
        # 资产负债表是固定的双栏版式：左栏 1..4（资产/行次/期末/年初）、
        # 右栏 5..8（负债/行次/期末/年初），位置不随模板变。
        line_source_cols = {2, 6}
        amount_source_cols = {3, 4, 7, 8}
        # 两处取并集 —— 只认表头的话，右边栏表头写成「行号」就会连右边的行次一起丢掉。
        line_cols = tuple(sorted({column - 1 for column in header_line_cols} | {1, 5}))
        source_to_target = {column: column for column in range(1, 9)}
        cols = []
        for index, cell in enumerate(grid[header_index], 1):
            kind = "STATIC" if index in (1, 5) else "LINE_NO" if index in (2, 6) else (
                "BALANCE_CLOSING" if index in (3, 7) else "BALANCE_OPENING")
            cols.append({"col_no": index, "label": cell["text"].strip() or no_to_col(index), "kind": kind,
                         "width": 250 if kind == "STATIC" else 56 if kind == "LINE_NO" else 140, "hidden": False})
    else:
        # 利润表 4 列：项目 / 行次 / 本期 / 累计。
        #
        # **三列的语义不能按固定位置写死**。照 UFO 固定位置（行次=源第 2 列、
        # 金额=源第 3、4 列）映射，遇到把「行次」写在别的列的模板，会把真正的金额列
        # 当行次丢掉：少一列数，而且不报任何错。
        #
        # 所以按表头认：写着「行次/行号/序号」的那列是行次列；表头一个行次字眼都没有
        # 时（整列是空的行次列，或多出来一列「备注」），取「不是项目列、也不是金额列」
        # 的那一列；再认不出来才回落 UFO 固定位置。金额列同理，认不满两列就拒收。
        amount_like = {column for column, label in header_labels.items()
                       if column != 1 and any(word in label for word in _AMOUNT_HEADER_WORDS)}
        line_source_cols = {column for column in header_line_cols if column != 1}
        if not line_source_cols and len(amount_like) == 2:
            line_source_cols = ({column for column in header_labels if column != 1} - amount_like)
        if not line_source_cols:
            line_source_cols = {2}
        if len(line_source_cols) != 1:
            raise TemplateImportError("无法确定利润表的行次列，请检查模板版式")
        line_column = next(iter(line_source_cols))
        if len(amount_like) == 1:
            raise TemplateImportError("模板只有一个金额列，当前系统的利润表需要本期与本年累计两列")
        if len(amount_like) > 2:
            raise TemplateImportError("模板有多于两个金额列，无法确定哪两列导入当前系统")
        amount_source_cols = sorted({column for column in range(1, col_count + 1)
                                     if column != 1 and column != line_column})
        if len(amount_source_cols) != 2:
            raise TemplateImportError("无法确定利润表的两个金额列，请检查模板版式")
        line_cols = (line_column - 1,)
        source_to_target = {1: 1, amount_source_cols[0]: 2, amount_source_cols[1]: 3}
        cols = [
            {"col_no": 1, "label": header[0]["text"].strip() or "项目", "kind": "STATIC", "width": 250, "hidden": False},
            {"col_no": 2, "label": header[amount_source_cols[0] - 1]["text"].strip() or "本期金额", "kind": "FORMULA", "width": 140, "hidden": False},
            {"col_no": 3, "label": header[amount_source_cols[1] - 1]["text"].strip() or "本年累计", "kind": "FORMULA", "width": 140, "hidden": False},
        ]
    # 行次列里出现公式，说明这一列其实是金额列 —— 照着行次丢掉就是在静默丢一列数。
    # 宁可拒收让人把模板改对，也不能让人拿着一张少了一列的报表去做账。
    for source_col in sorted(line_source_cols):
        if any(row[source_col - 1]["formula"].strip() for row in grid):
            raise TemplateImportError(f"第 {source_col} 列被当成了行次列，可列里有公式，列的含义认不准，请检查模板版式")
    # 表体到哪儿为止：三条互相独立的判据，取靠后的那个。
    #
    # 1) 行次列里有行号标记的行；
    # 2) 金额列里有公式的行；
    # 3) 金额列里有数值的行（手填的数）。
    #
    # **行次从来不是表体的可靠边界**。UFO 的行次列里大类行走中文数字
    # （「一、二、三、四」），明细行才走阿拉伯数字；标准《会小企02表 利润表》的大类行
    # （一、主营业务收入 / 减：1、管理费用 / 减：2、财务费用 / 四、利润总额）全是中文数字，
    # 只认 `\d+` 的话末行大类落在 row_end 之后，会被整行切掉 —— 「利润总额」那一行凭空消失，
    # 导进来的是半张表，而且**不报任何错**。自建模板更常见：行次列整列是空的。
    #
    # 只认公式也不够。UFO 的格分两种：标了公式的（内容在公式对象里）和没标公式的
    # （内容是一段文本）。自建模板的金额格常常是**手填的数**，没有公式可读 ——
    # 光看 formula 会把这类整行切掉，连表尾手填的合计行一起丢掉。
    # 所以边界必须以「**金额列有内容**」为准：有公式、有数值、或标了公式但没读出公式，都算。
    numbered = [index + 1 for index, row in enumerate(grid)
                if index > header_index and any(_LINE_NO.fullmatch(row[col]["text"].strip()) for col in line_cols)]
    with_amount = [index + 1 for index, row in enumerate(grid)
                   if index + 1 >= row_start and any(_amount_cell_has_content(row[col - 1]) for col in amount_source_cols)]
    if not numbered and not with_amount:
        raise TemplateImportError("没有找到报表内容行：行次列、金额公式和金额数值都是空的")
    row_end = max(numbered + with_amount)
    rows, cells, issues = [], [], []
    source_count = 0
    for source_row in range(row_start, row_end + 1):
        row_no = source_row - row_start + 1
        raw_row = grid[source_row - 1]
        label, indent, bold, kind = _side(raw_row[0]["text"])
        style: dict[str, Any] = {"line_left": raw_row[line_cols[0]]["text"].strip() or None,
                                 "kind": kind, "source_row_no": source_row}
        if col_count == 8:
            right, right_indent, right_bold, right_kind = _side(raw_row[4]["text"])
            style.update(label_right=right, indent_right=right_indent, bold_right=right_bold,
                         kind_left=kind, kind_right=right_kind,
                         line_right=raw_row[line_cols[1]]["text"].strip() or None)
        rows.append({"row_no": row_no, "label": label, "indent": indent, "bold": bold, "hidden": False, "style": style})
        for source_col_no, raw_cell in enumerate(raw_row, 1):
            target_col_no = source_to_target.get(source_col_no)
            if target_col_no is None:
                continue
            col = cols[target_col_no - 1]
            if col["kind"] in ("STATIC", "LINE_NO"):
                continue
            source = raw_cell["formula"]
            if source:
                source_count += 1
                try:
                    formula = _translate(source, row_start, row_end, col_count, source_to_target)
                except (TemplateImportError, FormulaError) as exc:
                    issues.append({"row_no": row_no, "col_no": target_col_no, "source_address": f'{no_to_col(source_col_no)}{source_row}',
                                   "source_formula": source, "reason": str(exc), "label": label})
                    continue
                cells.append({"row_no": row_no, "col_no": target_col_no, "formula": formula})
            elif raw_cell["missing"] or raw_cell["text"]:
                issues.append({"row_no": row_no, "col_no": target_col_no, "source_address": f'{no_to_col(source_col_no)}{source_row}',
                               "source_formula": raw_cell["text"], "reason": "原模板金额格没有可读取的公式，导入后留空", "label": label})
    normalized = {"type": report_type, "rows": rows, "cols": cols, "cells": cells, "issues": issues}
    fingerprint = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    return {"name": Path(filename).stem[:128], **normalized,
            "source": {"filename": filename, "format": f"用友 {version}", "fingerprint": fingerprint},
            "stats": {"rows": len(rows), "cols": len(cols), "formulas": source_count,
                      "converted_formulas": len(cells), "unsupported_formulas": len(issues), "subject_codes": len(subject_codes(cells))},
            "warnings": (["发生额已按原模板的借/贷方向转换为当前期间和本年累计口径；导入后按当前系统的报表规则重新计算。"] if col_count == 4 else []),
            "subject_codes": subject_codes(cells)}
