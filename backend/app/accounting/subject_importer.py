"""外部科目表解析。

目前重点兼容用友导出的 ``会计科目.xls``：

    类型、级次、科目编码、科目名称、助记码、外币币种、计量单位、
    辅助账类型、账页格式、余额方向

解析阶段只生成可 JSON 化的预览，不接触数据库。科目覆盖的删除和写入由
``api.subjects`` 在一个事务内完成。
"""
from __future__ import annotations

import io
import math
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from app.core.config import settings
from app.core.uploads import RowBudget, check_archive_size

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"

_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "type": ("类型", "科目类型", "类别", "type", "category"),
    "level": ("级次", "级别", "层级", "level", "level_no"),
    "code": ("科目编码", "科目代码", "编码", "科目编号", "code", "subject_code"),
    "name": ("科目名称", "名称", "subject_name", "name"),
    "mnemonic": ("助记码", "助记符", "mnemonic"),
    "foreign_currency": ("外币币种", "外币", "币种", "foreign_currency", "currency"),
    "unit": ("计量单位", "单位", "unit"),
    "auxiliary_type": ("辅助账类型", "辅助核算类型", "辅助类型", "auxiliary_type"),
    "page_format": ("账页格式", "账页", "page_format"),
    "direction": ("余额方向", "方向", "direction"),
}

_FIELD_LABELS = {
    "type": "类型",
    "level": "级次",
    "code": "科目编码",
    "name": "科目名称",
    "direction": "余额方向",
}

_CATEGORY_MAP = {
    "资产": "ASSET",
    "资产类": "ASSET",
    "负债": "LIABILITY",
    "负债类": "LIABILITY",
    "权益": "EQUITY",
    "权益类": "EQUITY",
    "所有者权益": "EQUITY",
    "所有者权益类": "EQUITY",
    "成本": "COST",
    "成本类": "COST",
    "损益": "PROFIT",
    "损益类": "PROFIT",
}
_DIRECTION_MAP = {"借": "DEBIT", "借方": "DEBIT", "贷": "CREDIT", "贷方": "CREDIT"}


def _text(value: Any) -> str:
    """把 Excel 单元格稳定地转成文本，避免 ``1001.0`` 污染科目编码。"""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value).strip()


def _header_key(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value)).lower()


def _column_mapping(header: Iterable[Any]) -> tuple[dict[str, int], list[str]]:
    aliases = {
        _header_key(alias): key
        for key, names in _HEADER_ALIASES.items()
        for alias in names
    }
    mapping: dict[str, int] = {}
    unknown: list[str] = []
    for index, value in enumerate(header):
        label = _text(value)
        if not label:
            continue
        key = aliases.get(_header_key(label))
        if key is None:
            unknown.append(label)
        elif key not in mapping:
            mapping[key] = index
    return mapping, unknown


def _is_blank(row: Iterable[Any]) -> bool:
    return all(_text(value) == "" for value in row)


def _iter_xls(data: bytes):
    try:
        import xlrd
    except ModuleNotFoundError as exc:  # pragma: no cover - deployment misconfiguration
        raise ValueError("服务器缺少 .xls 解析组件 xlrd，请重新安装后端依赖") from exc

    try:
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
        if workbook.nsheets == 0:
            raise ValueError("Excel 里没有工作表")
        sheet = workbook.sheet_by_index(0)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无法读取 .xls 文件：{exc}") from exc

    def rows():
        try:
            for row_no in range(sheet.nrows):
                yield sheet.row_values(row_no)
        finally:
            workbook.release_resources()

    return sheet.name, rows(), "xls"


def _iter_xlsx(data: bytes):
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        sheet = workbook.active
        if sheet is None:
            raise ValueError("Excel 里没有工作表")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无法读取 .xlsx 文件：{exc}") from exc

    def rows():
        try:
            yield from sheet.iter_rows(values_only=True)
        finally:
            workbook.close()

    return sheet.title, rows(), "xlsx"


def _workbook_rows(data: bytes, filename: str):
    """按文件内容选择解析器，文件名只作为兼容补充。"""
    lower_name = (filename or "").lower()
    if data.startswith(_OLE_MAGIC) or (lower_name.endswith(".xls") and not data.startswith(_ZIP_MAGIC)):
        return _iter_xls(data)
    return _iter_xlsx(data)


def _as_level(raw: str) -> int | None:
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if value != value.to_integral_value():
        return None
    level = int(value)
    return level if level > 0 else None


def _add_error(item: dict, message: str) -> None:
    if message not in item["errors"]:
        item["errors"].append(message)


def _error_list(items: list[dict]) -> list[dict]:
    return [
        {"line": item["line"], "code": item.get("code", ""), "message": message}
        for item in items
        for message in item["errors"]
    ]


def parse_subject_workbook(data: bytes, filename: str = "") -> dict:
    """解析外部科目表，返回预览数据，不写数据库。"""
    if not data:
        raise ValueError("科目表文件为空")

    # .xls 是 OLE 文件，不会被 zip 检查读取；.xlsx 则复用上传入口的解压体积闸。
    check_archive_size(data, label="科目表")
    sheet_name, rows_iter, file_format = _workbook_rows(data, filename)
    budget = RowBudget(
        settings.import_max_rows,
        what="行",
        hint="请删除空白行或拆成多份分别导入",
    )
    try:
        rows = budget.guard_rows(rows_iter)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"读取科目表失败：{exc}") from exc

    if not rows:
        raise ValueError("Excel 是空文件")

    header_index = None
    mapping: dict[str, int] = {}
    unknown_headers: list[str] = []
    # 外部系统偶尔会在表头前放标题，允许跳过少量说明行。
    for index, row in enumerate(rows[:20]):
        candidate, unknown = _column_mapping(row)
        if {"code", "name", "level"}.issubset(candidate):
            header_index = index
            mapping = candidate
            unknown_headers = unknown
            break
    if header_index is None:
        raise ValueError(
            "无法识别科目表表头。需要包含：类型、级次、科目编码、科目名称、余额方向"
        )

    missing = [key for key in _FIELD_LABELS if key not in mapping]
    if missing:
        raise ValueError("缺少必需的表头列：" + "、".join(_FIELD_LABELS[key] for key in missing))

    def value(row: list[Any] | tuple[Any, ...], key: str) -> str:
        index = mapping.get(key)
        return _text(row[index]) if index is not None and index < len(row) else ""

    subjects: list[dict] = []
    for row_index, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if _is_blank(row):
            continue
        raw_type = value(row, "type")
        raw_level = value(row, "level")
        code = value(row, "code")
        name = value(row, "name")
        raw_direction = value(row, "direction")
        category = _CATEGORY_MAP.get(raw_type.replace(" ", ""))
        direction = _DIRECTION_MAP.get(raw_direction.replace(" ", ""))
        level = _as_level(raw_level)
        optional = {
            "mnemonic": value(row, "mnemonic"),
            "foreign_currency": value(row, "foreign_currency"),
            "unit": value(row, "unit"),
            "auxiliary_type": value(row, "auxiliary_type"),
            "page_format": value(row, "page_format"),
        }
        item = {
            "line": row_index,
            "code": code,
            "name": name,
            "category": category or "",
            "direction": direction or "",
            "level": level or 0,
            "parent_code": "",
            "is_leaf": True,
            "carry_forward": category == "PROFIT",
            "qty_calc": bool(optional["unit"]),
            "aux_calc": bool(optional["auxiliary_type"]),
            "fx_calc": bool(optional["foreign_currency"]),
            "sort_no": (len(subjects) + 1) * 10,
            "ext_attrs": {
                "source_type": raw_type,
                "source_level": raw_level,
                **{key: val for key, val in optional.items() if val},
            },
            "errors": [],
        }
        if not code:
            _add_error(item, "科目编码不能为空")
        elif len(code) > 32 or re.search(r"\s", code):
            _add_error(item, "科目编码长度不能超过 32 个字符且不能包含空白")
        if not name:
            _add_error(item, "科目名称不能为空")
        if category is None:
            _add_error(item, f"不支持的科目类型：{raw_type or '空'}")
        if level is None:
            _add_error(item, f"级次不是正整数：{raw_level or '空'}")
        if direction is None:
            _add_error(item, f"不支持的余额方向：{raw_direction or '空'}")
        subjects.append(item)

    if not subjects:
        raise ValueError("科目表没有数据行")

    by_code: dict[str, dict] = {}
    for item in subjects:
        code = item["code"]
        if not code:
            continue
        previous = by_code.get(code)
        if previous is not None:
            _add_error(previous, f"科目编码重复：{code}")
            _add_error(item, f"科目编码重复：{code}")
        else:
            by_code[code] = item

    by_level: dict[int, list[dict]] = defaultdict(list)
    for item in subjects:
        if item["level"] > 0:
            by_level[item["level"]].append(item)

    child_codes: set[str] = set()
    for item in subjects:
        level = item["level"]
        code = item["code"]
        if not code or level <= 1:
            if level == 1 and code:
                continue
            if level > 1:
                _add_error(item, "级次必须从 1 开始")
            continue
        candidates = [
            parent
            for parent in by_level.get(level - 1, [])
            if parent["code"] and code.startswith(parent["code"]) and parent["code"] != code
        ]
        parent = max(candidates, key=lambda row: len(row["code"]), default=None)
        if parent is None:
            _add_error(item, f"找不到级次为 {level - 1} 且编码为上级前缀的父科目")
        else:
            item["parent_code"] = parent["code"]
            child_codes.add(parent["code"])

    for item in subjects:
        item["is_leaf"] = item["code"] not in child_codes

    widths: dict[int, int] = {}
    for item in subjects:
        level = item["level"]
        code = item["code"]
        if not code or level <= 0 or (level > 1 and not item["parent_code"]):
            continue
        parent_length = len(item["parent_code"]) if level > 1 else 0
        width = len(code) - parent_length
        if width <= 0 or width > 8:
            _add_error(item, f"第 {level} 级编码段长度 {width} 不支持（应为 1-8）")
            continue
        if level in widths and widths[level] != width:
            _add_error(item, f"第 {level} 级编码段长度不一致：已有 {widths[level]}，当前为 {width}")
        else:
            widths[level] = width

    max_level = max((item["level"] for item in subjects), default=0)
    if max_level > 8:
        for item in subjects:
            if item["level"] > 8:
                _add_error(item, "科目级次超过系统支持的 8 级")
    if max_level and set(widths) != set(range(1, max_level + 1)):
        missing_levels = sorted(set(range(1, max_level + 1)) - set(widths))
        if missing_levels:
            for item in subjects:
                if item["level"] in missing_levels:
                    _add_error(item, f"缺少第 {item['level']} 级的有效编码段")

    errors = _error_list(subjects)
    for item in subjects:
        # 错误详情是预览用的；不把内部错误列表写入确认 payload。
        item.pop("errors", None)
    code_rule = "-".join(str(widths[level]) for level in range(1, max_level + 1)) if widths else ""
    return {
        "ok": not errors,
        "file_format": file_format,
        "sheet_name": sheet_name,
        "headers": [_text(cell) for cell in rows[header_index]],
        "unknown_headers": unknown_headers,
        "code_rule": code_rule,
        "total": len(subjects),
        "ok_count": len(subjects) - len({error["line"] for error in errors}),
        "error_count": len(errors),
        "errors": errors,
        "subjects": subjects,
    }


__all__ = ["parse_subject_workbook"]
