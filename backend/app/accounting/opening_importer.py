"""期初余额表解析（只解析、不落库）。

这个模块替掉了 ``api/books.py`` 里内联的 ``parse_openings()``：那 170 行逻辑直接
写在路由里，于是**只认得本系统自己导出的四列表**（科目编码 | 科目名称 | 方向 |
期初余额），用友 U8/畅捷通导出的 .xls「科目余额表」读不开 —— 客户拿真实账套的
第一份期初余额文件就能把它顶死。

两类来源，**故意不合成一个解析器**：

* **A. 用友科目余额表**：借贷**分列**（期初借方 / 期初贷方），带「资产小计 /
  合计」等汇总行，几百行全零数据行，科目名称带前导空格表示级次。
* **B. 本系统导出的四列表**：借贷**合列**，方向由独立的「方向」列给出，没有
  汇总行。

两者的金额语义不同源（分列 vs 合列），硬合成一个分支只会让两边都难懂，所以
这里各自一条解析路径，**共用**表头扫描入口、科目匹配与统计收尾。

关于「全零行要不要返回」的刻意不对称（A 跳过、B 保留）
---------------------------------------------------------
真实用友余额表 546 个数据行里 328 行借贷都是 0.0 —— 这些行塞进预览框没法看，
用户要在一屏零行里找自己那 200 来笔有数的。而 B 是本系统自己导出的文件，导出
侧已经用「非末级不导出」筛过，0 值行很少，且**前端录入页靠 rows 逐行回填**、
靠 ``error`` 字段过滤掉问题行；历史上 0 值行是照常返回的（``amount="0"``、
``error=""``），把它一并去掉会让「导出的表再导入」这个闭环悄悄变形。
所以：**A 跳过全零行并计入 ``skipped``，B 原样返回**。

关于「非明细科目」为什么两类都报错
----------------------------------
用友余额表虽然只有末级科目，但那是那份文件的性质，不是格式保证 —— 别的导出
（尤其按级次汇总过的）会带父级行。期初余额只能录在末级：录到父级上，汇总时
会与子级重复计入，借贷合计当场就不平了。这一条原来只写在 B 的分支里，
A 的分支若不照做就是同一个数据完整性缺口，只是换了个文件名。

模块**不认识 Session**：科目从 ``subjects_by_code`` / ``subjects_by_name``
两个字典进来（值是 ``Subject`` 或轻量命名空间，取 ``id``/``direction``/
``is_leaf``/``name``）。解析失败抛 ``ValueError``（路由层翻成 400），
容量超限让 ``UploadTooLarge``（继承 ``ValueError``）自己往上走（路由翻成 413）。
这里**不出现 HTTPException**：解析逻辑同样会被服务层和测试直接调用，
那里没有 HTTP 语义。
"""
from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal
from typing import Any, Iterable, Sequence

from app.accounting.subject_importer import iter_workbook_rows
from app.accounting.util import SUMMARY_LABELS as util_summary_labels
from app.accounting.util import is_summary_label, money
from app.core.config import settings
from app.core.uploads import RowBudget, check_archive_size

# 表头识别要扫多少行。用友导出常在表头上方带几行标题/制表日期，
# 而本系统导出的四列表走的是另一套（更紧的）判定，两者的扫描深度不同。
UFIDA_HEADER_SCAN_ROWS = 20
EXPORT_HEADER_SCAN_ROWS = 15

SOURCE_UFIDA = "用友余额表"
SOURCE_EXPORT = "Excel 期初表"

# 汇总行标签。**定义在 app.accounting.util**（审计 P2-13）——
# 凭证导入那边也用得到，这里保留一个别名只是不想把本文件里其余引用一起改掉。
SUMMARY_LABELS = util_summary_labels

CODE_KEYWORDS = ("编码", "代码", "编号")
NAME_KEYWORDS = ("名称", "科目全名")
PERIOD_KEYWORD = "期间"

# basis -> 该口径的列名关键词。用友表期初/期末两套列都在，「期末」两栏还存在时
# 前端要给用户切换，所以列名是「含关键词」而不是全等（「期初 借方」也算）。
BASIS_KEYWORDS = {"opening": ("期初",), "closing": ("期末",)}

# B 类表的「方向」列取值。老格式导出写中文，历史上也收过 DEBIT/D。
DEBIT_TOKENS = frozenset({"借", "借方", "DEBIT", "D"})
CREDIT_TOKENS = frozenset({"贷", "贷方", "CREDIT", "C"})

ERR_SUBJECT_MISSING = "科目在本账套不存在"
ERR_NOT_LEAF = "不是明细科目，期初应录在明细级"
ERR_BOTH_SIDES = "借方和贷方同时有金额"

_CURRENCY_PREFIX = re.compile(r"^[¥￥]|^(人民币|rmb)", re.IGNORECASE)


def _cell_text(value: Any) -> str:
    """把单元格稳定转成文本。

    数字型编码（``1001``）会被 Excel 存成 ``1001.0``，不这么处理就永远匹配不上
    账套里的 ``"1001"``。逻辑照抄 ``subject_importer._text``，不在那里 import
    私有名：那是它的实现细节，而这个模块只借它的**行为约定**。
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value).strip()


def _header_key(value: Any) -> str:
    """表头归一：去所有空白 + 小写。``"期初 借方"`` 与 ``"期初借方"`` 必须是同一个键。"""
    return re.sub(r"\s+", "", _cell_text(value)).lower()


def _amount(raw: Any) -> Decimal:
    """把一个金额单元格归一到两位小数。

    外部表的金额形态比本系统导出脏得多，实测见过：``None`` / ``""`` / ``0`` /
    ``0.0`` / ``3056.43`` / ``-598.50`` / ``"3,056.43"`` / ``"￥598.5"`` /
    全角数字。NFKC 一次性把全角数字与 ``￥`` 转成半角（比逐字符映射表好维护），
    千分位逗号与货币前缀再单独去掉，然后交给 ``util.money()`` —— **精度口径
    必须是全系统唯一的那一份**（两位小数、四舍五入、NaN/无穷/超量级一律拒），
    在这里另写一套 Decimal 解析只会多一个和库里对不上的地方。
    """
    if raw is None:
        return Decimal("0")
    if isinstance(raw, bool):
        raise ValueError(f"{raw!r} 不是合法金额")
    if isinstance(raw, (int, float, Decimal)):
        return money(raw)
    text = unicodedata.normalize("NFKC", _cell_text(raw))
    text = _CURRENCY_PREFIX.sub("", text.replace(",", "").replace(" ", "")).strip()
    if not text:
        return Decimal("0")
    return money(text)


def _amount_text(value: Decimal) -> str:
    """金额字段的展示文本。零一律写 ``"0"``。

    写归一后的两位小数而不是原始单元格文本：预览框要回填、要参与借贷合计，
    原样文本里的千分位/全角数字前端和用户都读不懂，而期初余额本身就是两位小数。
    """
    return "0" if value == 0 else str(value)


def _is_summary_row(code: str, name: str) -> bool:
    """汇总行（资产小计/合计…）整行跳过 —— 它不是科目，也不是错误。

    判据与标签都搬到 ``util`` 了（审计 P2-13）：凭证导入也需要同一条判据，
    两边各留一份迟早再次对不上。
    """
    return is_summary_label(code, name)


def _side_columns(labels: Sequence[str], keywords: Iterable[str],
                  *, skip: Iterable[int] = ()) -> tuple[int | None, int | None]:
    """在一行表头里找「借方列 / 贷方列」。方向**由值所在的列决定**，不靠正负猜。

    用友余额表里借贷是分列的，两列必有且只有一列非零；按金额正负猜方向会在
    「红字」场景（负数借方）上把方向判反。

    三级判定，因为用友的表头布局不止一种：

    1. **同行**：「期初借方」这样一格里既有口径词又有方向词 —— 最常见的写法；
    2. **口径词与方向词分两行**：第一行只写「期初」，第二行才写「借方 / 贷方」，
       拼起来就成了「期初」「贷方」「期末借方」「贷方」这种**半空**的串。
       此时改成：定位到口径词所在的那一段，在这一段里找第一个「借」和第一个「贷」；
       遇到另一种口径词（要期初却撞上「期末」）就停 —— 后面是另一套数了；
    3. **压根没有口径词**：表里只有一套借贷分列，取第一个「借」与第一个「贷」，
       比认不出强（``basis`` 字段仍会如实告诉用户读的是哪一栏）。

    ``skip`` 是**科目列**（编码 / 名称）—— 整表扫「借」「贷」两个字时必须排掉它们：
    「短期借款」「应付账款」这类科目名称里本来就有借/贷，当成方向列的话，
    一张四列表的「方向」列会被挤掉，四列表反被认成用友表
    （实测：合并表头后「科目名称短期借款」被当成借方列，整张表读成全是零行）。
    """
    skip = set(skip)
    debit = credit = None
    for index, label in enumerate(labels):
        if index in skip or not label or not any(kw in label for kw in keywords):
            continue
        if "借" in label and debit is None:
            debit = index
        elif "贷" in label and credit is None:
            credit = index
    if debit is not None and credit is not None:
        return debit, credit

    wanted = list(keywords)
    others = [kw for other in BASIS_KEYWORDS.values() for kw in other
              if kw not in wanted]
    start = next((index for index, label in enumerate(labels)
                  if index not in skip and any(kw in label for kw in wanted)), None)
    if start is not None:
        for index in range(start, len(labels)):
            label = labels[index]
            if index in skip:
                continue
            if index != start and any(kw in label for kw in others):
                break                      # 撞上另一套口径：那一段不是本次要的数
            if "借" in label and debit is None:
                debit = index
            elif "贷" in label and credit is None:
                credit = index
    if debit is None or credit is None:
        for index, label in enumerate(labels):
            if index in skip:
                continue
            if "借" in label and debit is None:
                debit = index
            elif "贷" in label and credit is None:
                credit = index
    return debit, credit


def _ufida_roles(header: Sequence[Any], *, basis: str) -> dict[str, int]:
    """把一行表头解析成列角色（认不出的角色就不给这个键）。

    「科目名称」优先于「外币名称」：用友余额表里两者同时存在，
    谁在后面谁覆盖会让名称列悄悄变成外币币种列。
    """
    labels = [_header_key(cell) for cell in header]
    roles: dict[str, int] = {}
    for index, label in enumerate(labels):
        if not label:
            continue
        if "code" not in roles and any(keyword in label for keyword in CODE_KEYWORDS):
            roles["code"] = index
        if "name" not in roles and "科目" in label and "名称" in label:
            roles["name"] = index
        if "period" not in roles and PERIOD_KEYWORD in label:
            roles["period"] = index

    wanted = BASIS_KEYWORDS.get(basis) or BASIS_KEYWORDS["opening"]
    subject_cols = {index for key in ("code", "name")
                    if (index := roles.get(key)) is not None}
    debit, credit = _side_columns(labels, wanted, skip=subject_cols)
    if debit is None or credit is None:
        # 目标是期初但表里只有期末（或反过来）：换成另一套口径也比认不出强，
        # 解析结果里的 basis 字段仍然如实告诉用户读的是哪一栏。
        other = BASIS_KEYWORDS["closing" if basis == "opening" else "opening"]
        alt_debit, alt_credit = _side_columns(labels, other, skip=subject_cols)
        debit = debit if debit is not None else alt_debit
        credit = credit if credit is not None else alt_credit
    if debit is not None:
        roles["debit"] = debit
    if credit is not None:
        roles["credit"] = credit

    # 弱匹配兜底：名称列只写了「名称」而没有「科目」二字时（有的版本如此）。
    if "name" not in roles:
        for index, label in enumerate(labels):
            if any(keyword in label for keyword in NAME_KEYWORDS):
                roles["name"] = index
                break
    return roles


def _ufida_ready(roles: dict[str, int]) -> bool:
    """借贷两栏 + 至少一个能定位科目的列，才算认出了用友表头。"""
    return (
        roles.get("debit") is not None
        and roles.get("credit") is not None
        and (roles.get("code") is not None or roles.get("name") is not None)
    )


def _merged_header(first: Sequence[Any], second: Sequence[Any]) -> list[str]:
    """把上下两行表头按列拼起来（用友某些版本把「期初」和「借方」分两行）。"""
    width = max(len(first), len(second))
    out: list[str] = []
    for index in range(width):
        top = _cell_text(first[index]) if index < len(first) else ""
        bottom = _cell_text(second[index]) if index < len(second) else ""
        out.append(f"{top}{bottom}")
    return out


def _find_ufida_header(rows: Sequence[Sequence[Any]], basis: str) -> tuple[int, int, dict[str, int]]:
    """找用友余额表的表头，返回 ``(首行下标, 末行下标, 列角色)``；认不出返回 ``(-1, -1, {})``。

    为什么可以放宽到前 20 行：用友导出常在表头上方带标题、制表日期、账套名，
    写死「第 1 行是表头」对真实文件是错的 —— 这正是老实现只吃自己导出文件的原因。
    """
    limit = min(len(rows), UFIDA_HEADER_SCAN_ROWS)
    for index in range(limit):
        roles = _ufida_roles(rows[index], basis=basis)
        if _ufida_ready(roles):
            return index, index, roles
        # 表头横跨两行时，「期初」「借方」分处两行，单行必然认不出借贷列。
        #
        # 只在这一行**自己认出了科目列**时才去拼下面那行：否则标题行
        # （「科目余额表」「单位：某某公司 2026年10月」）会和真正的表头
        # 被拼成一行 —— 于是表头行本身被当成数据读进来，
        # 报出来的是「第 1 行 会计年度：科目在本账套不存在」。
        has_subject_col = roles.get("code") is not None or roles.get("name") is not None
        if not has_subject_col:
            continue
        for extra in (1, 2):
            end = index + extra
            if end >= limit:
                break
            merged = _ufida_roles(_merged_header(rows[index], rows[end]), basis=basis)
            if _ufida_ready(merged):
                return index, end, merged
    return -1, -1, {}


def _has_closing_columns(header: Sequence[Any]) -> bool:
    """表里有没有「期末」两栏。前端据此决定要不要显示期初/期末切换。"""
    labels = [_header_key(cell) for cell in header]
    closing = BASIS_KEYWORDS["closing"]
    roles = _ufida_roles(header, basis="closing")
    subject_cols = {index for key in ("code", "name")
                    if (index := roles.get(key)) is not None}
    debit, credit = _side_columns(labels, closing, skip=subject_cols)
    return debit is not None and credit is not None


def _find_export_header(grid: Sequence[Sequence[Any]]) -> tuple[int | None, dict[str, int]]:
    """本系统四列表的表头识别 —— **从 ``api/books.py`` 原样搬过来**，一行都没改。

    判定条件（既含「科目」又含 方向/余额/金额）与按列判角色的 elif 链都是老逻辑，
    照搬的理由不是偷懒：这条路径的输出被前端录入页逐行回填，识别规则的细微
    差别会变成「有几行金额不见了」这种查不出来的现象。改它需要独立的验收口径，
    不能夹带在「顺便支持用友格式」这次改动里。
    """
    roles = {"code": None, "name": None, "direction": None, "amount": None}
    for idx, row in enumerate(grid[:EXPORT_HEADER_SCAN_ROWS], start=1):
        texts = [str(c).strip() for c in row if c not in (None, "")]
        if not any("科目" in t for t in texts):
            continue
        if not any(("方向" in t) or ("余额" in t) or ("金额" in t) for t in texts):
            continue
        for col, cell in enumerate(row):
            t = str(cell).strip() if cell not in (None, "") else ""
            if not t:
                continue
            if ("编码" in t) or ("代码" in t):
                roles["code"] = col
            elif "名称" in t:
                roles["name"] = col
            elif ("方向" in t) or ("借贷" in t):
                roles["direction"] = col
            elif ("余额" in t) or ("金额" in t) or ("数" in t):
                roles["amount"] = col
        if roles["amount"] is not None:
            return idx, roles
    return None, roles


def _lookup(code: str, name: str,
            subjects_by_code: dict[str, Any],
            subjects_by_name: dict[str, Any]) -> Any | None:
    """先按编码、再按名称匹配科目。

    名称必须 strip：用友用前导空格表示级次（``"    水费"``），不 strip 就永远
    匹配不上账套里的「水费」，而这类文件里恰恰全是带级次缩进的名称。
    """
    return (subjects_by_code.get(code) or subjects_by_name.get(name)) if code or name else None


def _subject_attr(subject: Any, name: str, default: Any = None) -> Any:
    """取科目属性，缺省值让它容忍轻量替身。

    传进来的可能是 ``Subject`` ORM 对象，也可能是只带几个字段的命名空间
    （测试、服务层）。用 ``getattr`` 拿而不是直接 ``subject.name``，
    是为了让「非明细科目」这种判定在缺 ``is_leaf`` 的替身上默认按明细处理，
    而不是抛 AttributeError 变成 500。
    """
    return getattr(subject, name, default)


def _first_period_label(rows: Sequence[Sequence[Any]], header_end: int, role: int | None) -> str:
    """取「会计期间」列里第一个非空值（如 ``月份：2026.10-2026.10``）。

    只在表头认出了期间列时才有；认不出就返回空串，前端自行兜底成当前期间。
    """
    if role is None:
        return ""
    for row in rows[header_end + 1:]:
        if role < len(row):
            label = _cell_text(row[role])
            if label:
                return label
    return ""


def _summarize(entries: list[dict], errors: list[str], *,
               skipped: int, source: str, file_format: str, basis: str,
               period_label: str, has_closing: bool, header_row: int) -> dict:
    """收尾：借贷合计、匹配统计、预览 payload。

    借贷合计**只累加算得出 ``signed`` 的行**：方向定不下来的行（借贷两栏同时
    有数、非明细科目）进 ``errors``，不进合计 —— 合计是给用户核对「平不平」的，
    把定不了方向的行硬塞进去只会让差额变成一个查不出原因的数字。
    """
    debit = credit = Decimal("0")
    matched = 0
    unmatched = 0
    for entry in entries:
        if entry.get("subject_id") is None:
            unmatched += 1
        signed = entry.get("signed")
        if signed:
            value = Decimal(signed)
            if value != 0:
                matched += 1
                if value > 0:
                    debit += abs(value)
                else:
                    credit += abs(value)
    return {
        "rows": entries,
        "errors": errors,
        "total_debit": str(debit),
        "total_credit": str(credit),
        "balanced": debit == credit,
        "source": source,
        "file_format": file_format,
        "basis": basis,
        "period_label": period_label,
        "has_closing": has_closing,
        "matched": matched,
        "unmatched": unmatched,
        "skipped": skipped,
        "header_row": header_row,
    }


def _parse_ufida_rows(rows: Sequence[Sequence[Any]], roles: dict[str, int], *,
                      subjects_by_code: dict[str, Any], subjects_by_name: dict[str, Any],
                      header_end: int) -> tuple[list[dict], list[str], int]:
    """用友科目余额表：借贷分列 + 汇总行 + 全零行。"""
    entries: list[dict] = []
    errors: list[str] = []
    skipped = 0

    def cell(row: Sequence[Any], role: str) -> Any:
        index = roles.get(role)
        return row[index] if index is not None and index < len(row) else None

    # header_end 是表头**最后**一行的 0 基下标，数据从它下一行开始，
    # Excel 行号同样 1 基 —— 差一格就会静默吞掉第一行数据（老路由注释记的就是这个坑）。
    for line, raw_row in enumerate(rows[header_end + 1:], start=header_end + 2):
        if raw_row is None or all(c in (None, "") for c in raw_row):
            skipped += 1
            continue
        code = _cell_text(cell(raw_row, "code"))
        name = _cell_text(cell(raw_row, "name"))
        if not code and not name:
            skipped += 1
            continue
        if _is_summary_row(code, name):
            skipped += 1
            continue

        error = ""
        amount_text = ""
        raw_debit = cell(raw_row, "debit")
        raw_credit = cell(raw_row, "credit")
        try:
            opening_debit = _amount(raw_debit)
            opening_credit = _amount(raw_credit)
        except ValueError:
            opening_debit = opening_credit = Decimal("0")
            offending = _cell_text(raw_debit) or _cell_text(raw_credit)
            amount_text = offending
            error = f"金额无法识别：{offending!r}"

        has_debit = opening_debit > 0
        has_credit = opening_credit > 0
        if not error:
            if has_debit and has_credit:
                # 两栏同时有数在这套表里不可能是真实余额（贷方余额是另一行的事），
                # 多半是导出串了行；当成有方向的单边余额猜一个，会把借贷合计算平。
                error = ERR_BOTH_SIDES
                amount_text = f"{_amount_text(opening_debit)}/{_amount_text(opening_credit)}"
            elif not has_debit and not has_credit:
                # 全零行：不是错误，也不该进预览框（见模块 docstring 的说明）。
                skipped += 1
                continue

        subject = _lookup(code, name, subjects_by_code, subjects_by_name)
        entry: dict[str, Any] = {
            "line": line,
            "code": code,
            "name": name or str(_subject_attr(subject, "name", "") or ""),
            "amount": amount_text,
            "subject_id": _subject_attr(subject, "id") if subject else None,
            "direction": _subject_attr(subject, "direction") if subject else None,
            # 用友余额表没有「方向」列：方向由值所在的列决定，只体现在 display_side。
            "natural": "",
            "error": error,
        }
        if not amount_text and (has_debit or has_credit):
            amount_text = _amount_text(opening_debit if has_debit else opening_credit)
            entry["amount"] = amount_text

        if subject is None and not error:
            error = entry["error"] = ERR_SUBJECT_MISSING
        elif subject is not None and not error and not _subject_attr(subject, "is_leaf", True):
            error = entry["error"] = ERR_NOT_LEAF

        # 只有「匹配到科目 + 单边有数 + 是明细」才有资格定方向并进合计。
        if not error:
            entry["signed"] = str(opening_debit if has_debit else -opening_credit)
            entry["display_side"] = "借" if has_debit else "贷"

        entries.append(entry)
        if error:
            errors.append(f"第{line}行 {code or name}：{error}")
    return entries, errors, skipped


def _parse_export_rows(rows: Sequence[Sequence[Any]], cols: dict[str, int], *, head_idx: int,
                       subjects_by_code: dict[str, Any],
                       subjects_by_name: dict[str, Any]) -> tuple[list[dict], list[str], int, int]:
    """本系统导出的四列表 —— **行为逐字保留**（老 ``api/books.py`` 的循环体）。

    与老路由的差异只有三处，且都不改变已有行的输出：多一个 ``skipped`` 计数
    （老代码对空行是直接 continue，没地方记）、方向为空的科目改用 ``getattr``
    兜底、以及不再直接抛 ``HTTPException``（没有解析出行时抛 ``ValueError``，
    由路由翻成 400）。
    """
    entries: list[dict] = []
    errors: list[str] = []
    skipped = 0
    for i, raw_row in enumerate(rows[head_idx:], start=head_idx + 1):
        if raw_row is None or all(c in (None, "") for c in raw_row):
            skipped += 1
            continue

        def cell(role):
            c = cols.get(role)
            return raw_row[c] if c is not None and c < len(raw_row) else None

        code, name, direction, amount = cell("code"), cell("name"), cell("direction"), cell("amount")
        if code in (None, "") and name in (None, ""):
            skipped += 1
            continue
        subject = _lookup(str(code).strip() or "", str(name).strip() or "",
                          subjects_by_code, subjects_by_name)
        entry = {
            "line": i,
            "code": str(code or "").strip(),
            "name": str(name or (_subject_attr(subject, "name", "") or "")).strip(),
            "amount": str(amount if amount not in (None, "") else ""),
            "subject_id": _subject_attr(subject, "id") if subject else None,
            "direction": _subject_attr(subject, "direction") if subject else None,
            "natural": str(direction or "").strip(),
            "error": "",
        }
        if subject is None:
            entry["error"] = ERR_SUBJECT_MISSING
        elif not _subject_attr(subject, "is_leaf", True):
            entry["error"] = ERR_NOT_LEAF
        else:
            try:
                amt = _amount(amount)
            except ValueError:
                entry["error"] = f"金额无法识别：{amount!r}"
                amt = None
            if amt is not None:
                if amt == 0:
                    entry["error"] = ""
                    entry["amount"] = "0"
                else:
                    # 方向以「表里写的」为准，缺省才按科目属性推：
                    # 同一科目做反向调整是常事，按属性强推会把红字余额记反。
                    nat = entry["natural"]
                    if nat in DEBIT_TOKENS:
                        signed = abs(amt)
                    elif nat in CREDIT_TOKENS:
                        signed = -abs(amt)
                    else:
                        signed = abs(amt) if entry["direction"] == "DEBIT" else -abs(amt)
                    entry["signed"] = str(signed)
                    entry["display_side"] = "借" if signed > 0 else "贷"
        entries.append(entry)
        if entry["error"]:
            errors.append(f"第{i}行 {entry['code'] or entry['name']}：{entry['error']}")
    return entries, errors, skipped, head_idx


def parse_opening_rows(
    rows: Iterable[Sequence[Any]],
    *,
    subjects_by_code: dict[str, Any],
    subjects_by_name: dict[str, Any],
    basis: str = "opening",
    filename: str = "",
) -> dict:
    """解析期初余额表的工作表行（含表头，按 Excel 原顺序），返回预览 payload。

    ``basis`` 选哪一栏：``"opening"`` 读期初（初始化用），``"closing"`` 读期末。
    行号一律 1 基 —— 报给用户的「第 i 行」要能在源文件里直接对上。

    ``file_format`` 在这个入口只能按文件名推断（拿不到字节）：``.xls`` 之外的
    一切（含无扩展名）都报 ``"xlsx"``，宁可猜成常见的那种，也不要给前端第三种值。
    """
    grid = list(rows)
    if not grid:
        raise ValueError("Excel 是空文件")
    # 口径先校验：认不出的 basis 会静默退回某一栏，用户看着「读进来了」
    # 却拿到另一套数，比直接报错难查得多。
    if basis not in BASIS_KEYWORDS:
        raise ValueError(
            f"不支持的取数口径：{basis!r}（只认 {' / '.join(BASIS_KEYWORDS)}）")

    file_format = "xls" if (filename or "").lower().endswith(".xls") else "xlsx"
    header_index, header_end, roles = _find_ufida_header(grid, basis)
    if header_index >= 0:
        entries, errors, skipped = _parse_ufida_rows(
            grid, roles,
            subjects_by_code=subjects_by_code, subjects_by_name=subjects_by_name,
            header_end=header_end,
        )
        return _summarize(
            entries, errors, skipped=skipped, source=SOURCE_UFIDA, file_format=file_format,
            basis=basis, period_label=_first_period_label(grid, header_end, roles.get("period")),
            has_closing=_has_closing_columns(_merged_header(grid[header_index], grid[header_end])),
            header_row=header_index + 1,
        )

    head_idx, cols = _find_export_header(grid)
    positional_fallback = head_idx is None
    if positional_fallback:
        # 认不出表头才退回按位置取 A/B/C/D。老路由就是这么兜的，
        # 而这张表还得继续报错给用户看，所以不能在这里直接抛「无法识别表头」。
        head_idx, cols = 0, {"code": 0, "name": 1, "direction": 2, "amount": 3}
    entries, errors, skipped, header_row = _parse_export_rows(
        grid, cols, head_idx=head_idx,
        subjects_by_code=subjects_by_code, subjects_by_name=subjects_by_name,
    )
    return _summarize(
        entries, errors, skipped=skipped, source=SOURCE_EXPORT, file_format=file_format,
        basis=basis,
        period_label=_first_period_label(grid, head_idx - 1, _period_role(grid[head_idx - 1] if head_idx else grid[0])),
        has_closing=_has_closing_columns(grid[head_idx - 1] if head_idx else grid[0]),
        # 按位置兜底时没有「认出的表头」，报 0 让前端知道这行数不可信。
        header_row=0 if positional_fallback else header_row,
    )


def _period_role(header: Sequence[Any]) -> int | None:
    """本系统四列表里顺带认一下「会计期间」列。

    老表本来没这个概念，但用户自己整理的表经常带一列期间，写进
    ``period_label`` 比留空有用；老四列表没有这一列，所以老行为一个字不变。
    """
    for index, cell in enumerate(header or ()):
        if PERIOD_KEYWORD in _header_key(cell):
            return index
    return None


def parse_opening_workbook(
    data: bytes,
    filename: str,
    *,
    subjects_by_code: dict[str, Any],
    subjects_by_name: dict[str, Any],
    basis: str = "opening",
) -> dict:
    """字节流入口：读工作簿（xls/xlsx 自动分流）→ 行预算计数 → :func:`parse_opening_rows`。

    读取器直接用 ``subject_importer.iter_workbook_rows``：.xls 的 OLE 与 .xlsx 的
    zip 分流、以及 xlrd 缺失时那句中文提示都只保留一份 —— 老路由自己 openpyxl
    硬读，正是「用友的 .xls 打不开」这件事的根因（openpyxl 不吃 OLE）。

    两道闸都是**共用的**、不是新写的：解压体积走 ``check_archive_size``，
    行数走 ``RowBudget``（``settings.import_max_rows``）。行数闸必须
    **边读边数**：先 count 一遍照样要把整张表读进内存，那正是要防的事。
    路由层（``read_upload``）还有字节数那道，不在这里重复 —— 但服务层调用时
    至少还留着体积与行数两道。
    """
    if not data:
        raise ValueError("期初余额文件为空")

    check_archive_size(data, label="期初余额表")
    # 工作表名在这里用不上（不进 payload，见下面的说明），只取行与格式。
    _sheet_name, rows_iter, file_format = iter_workbook_rows(data, filename)
    budget = RowBudget(
        settings.import_max_rows,
        hint="请拆成多份分别导入，或先删除无关工作表",
    )
    try:
        rows = budget.guard_rows(rows_iter)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"读取期初余额表失败：{exc}") from exc

    if not rows:
        raise ValueError("Excel 是空文件")

    result = parse_opening_rows(
        rows,
        subjects_by_code=subjects_by_code,
        subjects_by_name=subjects_by_name,
        basis=basis,
        filename=filename,
    )
    # 以字节流判定的格式为准：文件名可以随便起，magic 字节不会骗人。
    # **不**再加工作表名之类的字段：两个入口必须返回同一个结构，路由与前端
    # 才能只认一套 payload（``subject_importer`` 那种带 sheet_name 的 payload
    # 是它自己的形状，这里不跟着开特例）。
    result["file_format"] = file_format
    if not result["rows"]:
        raise ValueError("没有解析出任何数据行")
    return result


__all__ = ["parse_opening_workbook", "parse_opening_rows"]
