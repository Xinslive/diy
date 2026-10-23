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

#: 类别的中文名（报错用）。
_CATEGORY_LABELS = {
    "ASSET": "资产类",
    "LIABILITY": "负债类",
    "EQUITY": "所有者权益类",
    "COST": "成本类",
    "PROFIT": "损益类",
}

#: 科目树的**层级深度上限**（审查修复）。
#:
#: 账簿引擎 ``LedgerEngine._roll_up_openings`` 里有个 16 层的**保险丝**：
#: 超过 16 层的子树合计会被**静默截断** —— 期初上卷少算一层，
#: 余额对不上，而**没有任何报错**。
#:
#: 导入是科目树唯一的批量入口，所以这里在导入边界上把这条上限**写出来**：
#: 超限时明确报错，而不是让它一路走到引擎里被悄悄截断。
#:
#: 为什么现在不会触发：这份解析器已经有一道「级次超过系统支持的 8 级」的
#: 检查（更严），而父子关系是按「级次 + 编码前缀」认的，树深不会超过级次。
#: 这条检查的作用是**把上限显式化**：将来若放宽级次上限，这里不会变成
#: 静默截断的入口。标准科目表只有 3 级。
MAX_SUBJECT_DEPTH = 16


def _canonical_prefix_categories() -> dict[str, str]:
    """本账套编码体系里，**科目编码首位（大类码）** → 类别。

    这张表**不是拍脑袋写的**，是从内置标准科目表
    （``app/seed/chart_of_accounts.py``，用友 4-2-2）里读出来的 ——
    那一级科目才是「大类码」的定义处（子科目编码是父级编码的前缀，
    首位与父级一致）。

    为什么「编码首位」是一条**独立于「类型」列**的证据：「类型」列是外部
    系统自己写的、写错一个字就错；而编码首位是编码规则决定的 ——
    1002 开头是 1，而 1 在这套体系里就是资产类。**两份证据对不上，
    就是这份表有问题。**
    """
    from app.seed.chart_of_accounts import CHART

    out: dict[str, str] = {}
    for row in CHART:
        code, category = row[0], row[2]
        if code and category:
            out.setdefault(str(code)[0], category)
    return out


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


def _xls_row(sheet, row_no: int) -> list[Any]:
    """一行 .xls 单元格，**日期单元格还原成 datetime**。

    xlrd 对 .xls 的日期单元格返回的是**浮点序列号**（``46027.0``），
    而 ``row_values()`` 只给原始值、不看单元格类型；.xlsx 那条路
    （openpyxl）会自己把日期转成 ``datetime``。两条路径的日期类型因此
    不一致 —— 用友导出的 .xls 序时账**整份导不进**：每一张凭证都报
    「日期无法识别：46027.0」（审计 P1-4）。

    还原要按**工作簿的 datemode**：1900 系统与 1904 系统差 1462 天，
    照搬序列号会把日期整整差四年。

    数据本身没坏，坏的是读取器没做这次类型判断 —— 所以这里补的就是
    这一次判断，不改任何别的行为（非日期单元格的取值与
    ``row_values()`` 完全一致）。
    """
    import xlrd

    row: list[Any] = []
    for col in range(sheet.ncols):
        cell = sheet.cell(row_no, col)
        if cell.ctype == xlrd.XL_CELL_DATE:
            row.append(xlrd.xldate_as_datetime(cell.value, sheet.book.datemode))
        else:
            row.append(cell.value)
    return row


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
                yield _xls_row(sheet, row_no)
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


def iter_workbook_rows(data: bytes, filename: str):
    """按文件内容选择解析器，文件名只作为兼容补充。

    对外公开：期初余额表解析（``opening_importer``）要读同一批文件，而 .xls 的
    OLE 与 .xlsx 的 zip 分流、以及 xlrd 缺失时那句中文提示都只应该存在一份 ——
    以前这里是私有的 ``_workbook_rows``，期初表那条路于是自己 openpyxl 硬读，
    用友导出的 .xls 到那里必然读不开（审计发现的老坑）。
    """
    lower_name = (filename or "").lower()
    if data.startswith(_OLE_MAGIC) or (lower_name.endswith(".xls") and not data.startswith(_ZIP_MAGIC)):
        return _iter_xls(data)
    return _iter_xlsx(data)


# 旧名保留成别名：既有测试按私有名调用，改名会让它们静默变成「测不到任何东西」。
_workbook_rows = iter_workbook_rows


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


def _parent_chain_depth(item: dict, by_code: dict[str, dict]) -> int:
    """这个科目在父子链上有多深（自己算第 1 级）。

    按 ``parent_code`` 一路上溯；遇到环或找不到父级就停（那两道另有检查
    会报错），这里只负责算深度。
    """
    depth = 1
    cur = item
    seen = {item.get("code")}
    while cur.get("parent_code"):
        parent = by_code.get(cur["parent_code"])
        if parent is None or parent.get("code") in seen:
            break
        seen.add(parent.get("code"))
        depth += 1
        cur = parent
    return depth


def _check_prefix_categories(subjects: list[dict]) -> None:
    """**科目编码首位（大类码）与「类型」必须一致**（独立于「类型」列的校验）。

    「类型」列是外部系统自己写的，写错一个字就错；而**编码首位是编码规则
    决定的**：本账套用友 4-2-2 体系里，第一级科目是 4 位大类码
    （1002 开头是 1），子科目编码是父级编码的前缀、首位与父级一致。
    两份证据对不上，就是这份表有问题。

    为什么值得拦：损益类科目会被自动置上 ``carry_forward``
    （见下面 ``carry_forward = category == "PROFIT"``），而月末结转会把
    带这个标记的科目的余额**整体搬进本年利润** —— 钱凭空消失，
    而**借贷仍然平衡，系统自检抓不到**。

    ---- 什么时候**不做**这道校验（重要）------------------------------
    导入的是**外部系统的科目表**，它的编码体系可能与本账套不同
    （存量账里还有上一版《企业会计准则》编号：4001 实收资本是所有者权益类、
    6001 主营业务收入是损益类）。所以：

    * 首位**不在**本账套体系里（比如 6、7 开头）→ 不校验，无从判断。
    * 首位在，但这份表把它用在了**另一个类别**上（同一首位的所有科目都
      不是本账套给这个首位的类别）→ 不校验：这份表用的是另一套体系，
      拿本账套的规则去判它就是误伤。

    只有「这个首位在这份表里已经按本账套的体系用起来」时，才拿它去校验
    同一位位的**个别**科目 —— 那才是真正的「类型列写错了」。
    """
    canonical = _canonical_prefix_categories()
    if not canonical:
        return
    used: dict[str, set[str]] = defaultdict(set)
    for item in subjects:
        code = item.get("code") or ""
        category = item.get("category")
        if code and category:
            used[code[0]].add(category)
    checkable = {
        digit for digit, cats in used.items()
        if digit in canonical and canonical[digit] in cats
    }
    if not checkable:
        return
    for item in subjects:
        code = item.get("code") or ""
        category = item.get("category")
        if not code or not category:
            continue
        digit = code[0]
        if digit not in checkable or category == canonical[digit]:
            continue
        _add_error(item, (
            f"科目编码首位与「类型」不一致：{code} {item.get('name', '')} 的编码首位"
            f"「{digit}」在本账套的编码体系里是"
            f"{_CATEGORY_LABELS.get(canonical[digit], canonical[digit])}，"
            f"而这张表把它标成了{_CATEGORY_LABELS.get(category, category)}。"
            f"请核对外部科目表的「类型」列 —— 类型写错会让结转损益搬错科目的余额，"
            f"而借贷仍然平衡、系统自检查不出来。"
        ))


def parse_subject_workbook(data: bytes, filename: str = "") -> dict:
    """解析外部科目表，返回预览数据，不写数据库。"""
    if not data:
        raise ValueError("科目表文件为空")

    # .xls 是 OLE 文件，不会被 zip 检查读取；.xlsx 则复用上传入口的解压体积闸。
    check_archive_size(data, label="科目表")
    sheet_name, rows_iter, file_format = iter_workbook_rows(data, filename)
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
            # **「类型」列推出来的**，不是「编码首位」推出来的 —— 所以它
            # 只对了一半：类型列写错时，这里跟着错。导入路径上还有一道
            # **独立于类型列**的校验（``_check_prefix_categories``：编码
            # 首位的大类码必须与类别对得上），两道证据不一致就报错。
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

    # ---- 科目树深度上限（审查修复）------------------------------------
    #
    # 引擎的期初上卷有个 16 层保险丝，超过就**静默截断**子树合计。这里在
    # 导入边界上把同一条上限写出来：超限时明确报错，别让它走到引擎里被
    # 悄悄少算一层。深度按「声明的级次」与「实际父子链」两者取大。
    for item in subjects:
        depth = max(int(item.get("level") or 0), _parent_chain_depth(item, by_code))
        if depth > MAX_SUBJECT_DEPTH:
            _add_error(
                item,
                f"科目层级深度 {depth} 超过上限 {MAX_SUBJECT_DEPTH} 级 —— "
                f"账簿引擎的期初上卷只处理到 {MAX_SUBJECT_DEPTH} 层，"
                f"再深的子树合计会被静默截断（余额对不上且不报错）。"
                f"请把科目表拆浅一些再导入。"
            )

    # ---- 编码首位（大类码）与「类型」必须一致（审查修复）--------------
    #
    # 这一道**独立于「类型」列**：编码首位是编码规则决定的（大类码），
    # 「类型」是外部系统自己写的。两者对不上，就是这份表有问题 ——
    # 而「类型」写错的后果不是显示难看：损益类科目会被自动置上
    # ``carry_forward``，月末结转会把它的余额整体搬进「本年利润」，
    # 钱凭空消失，而借贷仍然平衡、系统自检抓不到。
    _check_prefix_categories(subjects)

    errors = _error_list(subjects)
    for item in subjects:
        # 错误详情是预览用的；不把内部错误列表写入确认 payload。
        item.pop("errors", None)
    # ``widths`` 可能缺某一级（上一段刚为它记过「缺少第 N 级的有效编码段」）。
    # 这里原来直接 ``widths[level]``：一张级次不全的表走到这里就是 KeyError，
    # 于是本该给用户看的那份校验清单变成一句「数据库内部错误」——
    # 恰恰是最需要说清楚的时候说不清楚。缺的那级跳过即可：这份表本来
    # 就 ok=False，压根导入不了，编码规则给不给全都不影响结论。
    code_rule = (
        "-".join(str(widths[level]) for level in range(1, max_level + 1) if level in widths)
        if widths else ""
    )
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


__all__ = ["parse_subject_workbook", "iter_workbook_rows"]
