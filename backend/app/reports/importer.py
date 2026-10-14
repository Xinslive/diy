"""用友 UFO 5.10 / 8.00 的 .rep/.rbk 模板迁移。

OLE 的 UFO~REP 流包含主网格、字体、文本对象、公式对象及历史/缓存对象。
必须用网格内的对象 ID 和坐标定位当前公式，不能从二进制中搜索到一条就导入。
只迁移结构和公式；历史金额、编制单位及打印说明不成为新账套的财务数据。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import struct
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


def _grid(data: bytes) -> tuple[list[list[dict]], str]:
    if len(data) < 1816:
        raise TemplateImportError("模板结构不完整")
    row_count, col_count = struct.unpack_from("<HH", data, 452)
    font_count = struct.unpack_from("<I", data, 44)[0]
    if not (1 <= row_count <= MAX_ROWS and col_count in (4, 8) and 1 <= font_count <= 100):
        raise TemplateImportError("当前支持 4 列利润表和 8 列资产负债表，请使用这两类 UFO 模板")
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


def parse_ufo_report(raw: bytes, filename: str) -> dict[str, Any]:
    filename = (filename or "").replace("\\", "/").split("/")[-1]
    if Path(filename).suffix.lower() not in (".rep", ".rbk"):
        raise TemplateImportError("请选择用友 UFO 报表模板（.rep 或 .rbk）")
    grid, version = _grid(_stream(raw))
    col_count = len(grid[0])
    report_type = "BALANCE_SHEET" if col_count == 8 else "INCOME_SHEET"
    header_index = next((index for index, row in enumerate(grid[:12])
                         if re.sub(r"\s+", "", row[0]["text"]) in ("资产", "项目")
                         and any(cell["text"].strip() == "行次" for cell in row)), None)
    if header_index is None:
        raise TemplateImportError("未找到项目/行次表头，无法可靠识别报表主体")
    row_start = header_index + 2
    # 主体由固定行次定位，空的分节行会一起保留；表尾说明及缓存数据不导入。
    line_cols = (1, 5) if col_count == 8 else (1,)
    numbered = [index + 1 for index, row in enumerate(grid)
                if index > header_index and any(re.fullmatch(r"\d+", row[col]["text"].strip()) for col in line_cols)]
    if not numbered:
        raise TemplateImportError("报表没有可识别的项目行次")
    row_end = max(numbered)
    if col_count == 8:
        source_to_target = {column: column for column in range(1, 9)}
        cols = []
        for index, cell in enumerate(grid[header_index], 1):
            kind = "STATIC" if index in (1, 5) else "LINE_NO" if index in (2, 6) else (
                "BALANCE_CLOSING" if index in (3, 7) else "BALANCE_OPENING")
            cols.append({"col_no": index, "label": cell["text"].strip() or no_to_col(index), "kind": kind,
                         "width": 250 if kind == "STATIC" else 56 if kind == "LINE_NO" else 140, "hidden": False})
    else:
        # UFO 损益表的 B 列只是行次；当前系统的利润表没有这列，金额列要左移。
        source_to_target = {1: 1, 3: 2, 4: 3}
        header = grid[header_index]
        cols = [
            {"col_no": 1, "label": header[0]["text"].strip() or "项目", "kind": "STATIC", "width": 250, "hidden": False},
            {"col_no": 2, "label": header[2]["text"].strip() or "本期金额", "kind": "FORMULA", "width": 140, "hidden": False},
            {"col_no": 3, "label": header[3]["text"].strip() or "本年累计", "kind": "FORMULA", "width": 140, "hidden": False},
        ]
    rows, cells, issues = [], [], []
    source_count = 0
    for source_row in range(row_start, row_end + 1):
        row_no = source_row - row_start + 1
        raw_row = grid[source_row - 1]
        label, indent, bold, kind = _side(raw_row[0]["text"])
        style: dict[str, Any] = {"line_left": raw_row[1]["text"].strip() or None, "kind": kind, "source_row_no": source_row}
        if col_count == 8:
            right, right_indent, right_bold, right_kind = _side(raw_row[4]["text"])
            style.update(label_right=right, indent_right=right_indent, bold_right=right_bold,
                         kind_left=kind, kind_right=right_kind, line_right=raw_row[5]["text"].strip() or None)
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
