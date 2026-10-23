"""凭证 Excel 导入。

采用「解析暂存 → 预览 → 确认入库」三段式，绝不边读边写：

    POST /vouchers/import            上传文件，解析并暂存，返回逐张凭证的校验结果
    POST /vouchers/import/{id}/commit 财务确认后真正写入
    POST /vouchers/import/{id}/discard 作废暂存

列名容错
--------
不同财务导出的表头写法差别很大（日期 / 制单日期 / voucher_date …），
因此每个字段都配了一组别名，全部小写去空格后匹配。

文件格式
--------
``.xlsx`` 走 openpyxl，用友等旧系统导出的 ``.xls`` 走 xlrd。**按文件内容分流，
文件名只作补充** —— 改了后缀的 .xls 必须照样读得开，反过来把 .xlsx 改名成 .xls
也不能被 xlrd 接着而报一个看不懂的错。分流函数本身不在这里：
:func:`app.accounting.subject_importer.iter_workbook_rows` 是全仓唯一的一份，
期初余额表与科目表导入已经共用它（见那里的 docstring）。

断行规则
--------
**普通表**（一张凭证几行分录，凭证头一行填日期与凭证号、后续行留空）：
日期或凭证号**非空**就算新的一张凭证，**留空**才算「接上一张」——
这正好对应 Excel 里一张凭证连着几行分录的常见排版。
（实现是 :func:`_parse_rows` 里的 ``starts_voucher``；这一段文档曾经
写反了 —— 写成「为空时接上一张」，与实现正好相反，审计 P3-1。）

**用友序时账**例外：它每行都重复完整的日期与凭证号数，所以那一排里
**日期与凭证号都和当前这张相同**才算接上一张（见下）。

用友序时账
----------
用友导出的**序时账**不是「一张凭证几行、后续行留空」，而是把每一条分录
都平铺成一行，**每行都重复完整的日期与凭证号数**（实测
``商管序时账.xls``：5579 行分录 = 556 张凭证，凭证号数逐行重复）。照上面
那条「非空即新凭证」的规则去读，一张 112 条分录的凭证会被拆成 112 张
每条都借贷不平衡的废凭证。

所以这里多认一种排版：表头里有「上级科目」这类序时账独有的列时（见
:func:`_looks_like_journal`），改成**日期与凭证号都和当前这张相同 = 接上一张**。
判据只能落在表头上 —— 另一种同样存在的写法（每行都填日期、凭证号留空交给
系统自动编号）在行内容上与序时账完全一样，靠行分不开，硬改规则会把那种表
的两张小凭证并成一张。

容量边界（审计 P2-02）
---------------------
导入是这套系统里唯一「一个请求能把整个进程的内存吃光」的入口：
解析器把每一张凭证、每一条分录、每一条错误都累积进列表，
最后整包写进 ``import_batch.payload``。因此设了三道闸 —— 而闸本身住在
``app/core/uploads.py``，**期初余额解析（openings/parse）共用同一套**
（审计 P1-4：同一种入口曾在这里有三道、在那里一道都没有）：

    1. 接口层限制**读取字节数**（不把大文件读进内存）；
    2. 解析前按 zip 目录里的声明大小预检**解压后体量**（xlsx 就是 zip，
       几百 KB 的文件解压成几百 MB 是常事，声明大小不真正解压就能看）；
    3. 解析过程中限制**行数**与**单批凭证数** —— 前两道都可以被伪造
       （zip 目录里的 file_size 是可以随便写的），行数不会。

确认入库或作废之后，完整 payload 当场清空，只留批次元数据与校验摘要：
已经处理完的工作簿内容没有继续留在暂存表里的理由，而那里面是
整份凭证的科目、金额、摘要。
"""
from __future__ import annotations

import itertools
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.accounting import amounts, subject_importer
from app.accounting.audit_helper import log
from app.accounting.util import is_summary_label, money
from app.core.config import settings
from app.core.uploads import (RowBudget, UploadTooLarge, check_archive_size,
                              read_upload)
from app.models import ImportBatch, Subject, SysUser

# 字段别名：内部键 -> 可接受的表头（小写、已去空格）
ALIASES: dict[str, tuple[str, ...]] = {
    "voucher_date": ("日期", "制单日期", "凭证日期", "业务日期", "voucherdate", "date"),
    "voucher_no": ("凭证号", "凭证字号", "凭证编号", "字号", "凭证号数", "voucherno", "no"),
    "summary": ("摘要", "业务摘要", "分录摘要", "说明", "summary", "memo"),
    "subject": ("科目编码", "科目代码", "科目编号", "科目", "subjectcode", "subject", "code"),
    "subject_name": ("科目名称", "科目全名", "subjectname"),
    "debit": ("借方", "借方金额", "借方发生额", "借", "debit", "dr"),
    "credit": ("贷方", "贷方金额", "贷方发生额", "贷", "credit", "cr"),
    "amount": ("金额", "发生额", "amount", "money"),
    # 「金额」单列模式必须有方向列才知道借贷。早期版本没注册这一项，
    # col("direction") 恒为 None，于是单列金额全部落到了贷方。
    "direction": ("方向", "借贷", "借贷方向", "借或贷", "direction", "drcr"),
    "attachments": ("附单据数", "附件数", "单据数", "附单据", "attachments", "attach"),
    "remark": ("备注", "说明", "remark", "note"),
    # 用友序时账多出来的三列。注册它们不是为了取值，而是为了**别再报成
    # 「未识别的表头」**：那三列是正常业务列，用户看到自己的文件被列进
    # 未识别清单只会怀疑是自己导出错了。
    #   parent_subject —— 明细科目的完整路径（如「预收账款/星悦荟商业/1层/112」）。
    #     它同时是 :func:`_looks_like_journal` 认序时账的主要判据，
    #     且科目对不上时能拿它说清「这张凭证记的是哪个明细」。
    #   quantity / foreign_amount —— 数量核算与外币核算两列，本表不取数
    #     （Entry 上没有对应字段，辅助明细走 aux_ref）。
    "parent_subject": ("上级科目", "科目全路径", "上级科目名称"),
    "quantity": ("数量", "qty", "quantity"),
    "foreign_amount": ("外币", "外币金额", "原币金额", "foreignamount", "fc"),
}

# 「方向」列认得的写法。用友序时账写「借 / 贷」，另一些系统写全的「借方 / 贷方」。
DIRECTION_WORDS: dict[str, str] = {
    "借": "DEBIT", "借方": "DEBIT", "d": "DEBIT", "debit": "DEBIT", "dr": "DEBIT",
    "贷": "CREDIT", "贷方": "CREDIT", "c": "CREDIT", "credit": "CREDIT", "cr": "CREDIT",
}

# 序时账的表头判据。「上级科目」是它独有的列（明细科目的完整路径），
# 普通凭证导入表不会有这一列。
JOURNAL_HEADER_MARKERS = ("上级科目", "科目全路径", "上级科目名称")

#: 表头扫描深度（审查修复）。
#:
#: 外部系统导出常在表头上方带标题、制表日期、账套名 —— 写死「第 1 行是
#: 表头」对真实文件是错的，整份表都读不进来。期初余额导入早就改成扫描了
#: （``opening_importer`` 扫 20/15 行、还认双行合并表头），凭证导入这一条
#: 是漏的。15 行与期初那条同款。
VOUCHER_HEADER_SCAN_ROWS = 15


def _looks_like_journal(header: list[Any]) -> bool:
    """这份表是用友的序时账吗（逐条分录平铺、每行重复日期与凭证号数）。

    判据只能落在表头上，这一点值得说明白：序时账那种「每行都填日期与凭证号」
    的排版，和「每行填日期、凭证号留空交给系统自动编号」的排版，
    **在行内容上完全一样**，没有哪一列能把两者分开。区别只在整份表：

    * 序时账逐条分录都带完整的日期与凭证号数，凭证号形如 ``记-0001``；
    * 手工表是「一张凭证头一行、后续行留空」，凭证号常留空。

    要是按行内容无差别地把「日期与凭证号相同」当成接上一张，那种自动编号的
    写法就会被改掉 —— 每行都填日期的两张小凭证会被并成一张，而这种表
    **今天是可以正常导入的**（两张各自平，凭证号留空由系统编号）。
    宁可让序时账走一条单独的判据，也不改掉一条本来就对的规则。
    """
    names = {_norm(h) for h in header if h not in (None, "")}
    if names & set(JOURNAL_HEADER_MARKERS):
        return True
    # 没有「上级科目」时退一步：序时账必带「数量 / 外币」两个核算列，
    # 而普通凭证导入表只有借贷金额。要求两列同时出现，避免只勾了数量核算
    # 的表被误判。
    return "数量" in names and "外币" in names


def _norm(value: Any) -> str:
    return "".join(str(value).split()).lower() if value is not None else ""


def _cell_text(value: Any) -> str:
    """把单元格稳定转成文本（数字型编码 ``1001.0`` → ``1001``）。"""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value).strip()


def map_columns(header: list[Any]) -> tuple[dict[str, int], list[str]]:
    """把表头映射成内部字段 -> 列号。返回 (映射, 未识别的表头)。"""
    lookup: dict[str, str] = {}
    for key, names in ALIASES.items():
        for name in names:
            lookup[name] = key

    mapping: dict[str, int] = {}
    unknown: list[str] = []
    for idx, raw in enumerate(header):
        key = lookup.get(_norm(raw))
        if key is None:
            if raw not in (None, ""):
                unknown.append(str(raw))
        elif key not in mapping:  # 先出现的列优先
            mapping[key] = idx
    return mapping, unknown


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(value: Any) -> tuple[Decimal, str | None]:
    """把单元格解析成金额，返回 (金额, 错误说明)。

    原来这里是 ``except: return Decimal("0")`` —— 解析不了就当 0，
    不报错、不留痕。审计实测：一条含「非法金额行」的导入文件，
    5 条分录只入库 2 条，批次仍报 ok=true，错误列表**是空的**。
    用户以为凭证齐了，实际少记了金额，而且没有任何告警 ——
    这是财务软件里最容易造成实际损失的一类缺陷。

    所以规则改成：**解析不了必须说出来**。金额按 0 参与后续计算只是为了让
    流程继续走完，真正的处置权在调用方 —— 它必须把这条错误挂到凭证上。

    **写法归一交给共享解析器** :mod:`app.accounting.amounts`：全角数字、
    空格当千分位、U+2212 减号、括号负数在这里**一次**认全，凭证导入与
    期初导入走的是**同一份**代码（审计 P2-3）—— 之前两边各写一份，
    同一张表走凭证导入被拒、走期初导入却能读进来。
    """
    if value in (None, ""):
        return Decimal("0"), None
    if isinstance(value, (int, float, Decimal)):
        try:
            return money(value), None
        except Exception as exc:
            return Decimal("0"), f"{value!r} 不是合法金额（{exc}）"
    text = amounts.normalize_amount_text(value)
    if text.endswith("%"):
        # 原来直接返回 0。金额列写百分比，99% 的可能是从别处粘串了整列。
        return Decimal("0"), f"{text!r} 是百分比，金额列不该出现百分号"
    try:
        return amounts.parse_amount(value), None
    except Exception:
        return Decimal("0"), f"{text!r} 无法解析为金额"


def _as_decimal(value: Any) -> Decimal:
    """宽松版：只用于「有就用、没有就 0」的场合（如附单据数）。

    **不要**用它解析借贷方金额 —— 那样会把解析失败悄悄吞掉。
    金额一律走 :func:`_parse_amount`。
    """
    amount, _err = _parse_amount(value)
    return amount


def _current_accounting_year(db: Session, book_id: int) -> int:
    """这个账套当前在记的那一年（惰性 import，避免模块级的循环依赖）。"""
    from app.accounting.periods import current_accounting_year
    from app.models import Book

    book = db.get(Book, book_id)
    return current_accounting_year(db, book) if book is not None else 0


def _subject_index(db: Session, book_id: int, year: int
                   ) -> tuple[dict[str, Subject], dict[str, Subject]]:
    """返回某个年度的 (编码索引, 名称索引)。末级和上级都收进来：
    匹配到上级科目时才能说清「不是明细科目」，而不是含糊地报一句「找不到科目」——
    「1002 银行存款」在这个账套里明明存在，只是不能往它身上记。

    **年度是必填的**：科目表每个年度各有一份（迁移 0032），
    同一个编码在 2026 和 2027 是两行不同的记录。按编码匹配时必须
    先定下这一行属于哪一年 —— 序时账里跨年的分录各归各的表。
    """
    rows = db.scalars(
        select(Subject).where(
            Subject.book_id == book_id, Subject.year == year, Subject.active.is_(True))
    ).all()
    by_code = {s.code.strip(): s for s in rows}
    by_name: dict[str, Subject] = {}
    for s in rows:
        by_name.setdefault(s.name.strip(), s)
    return by_code, by_name


# --------------------------------------------------------------------- 容量边界


# 容量闸本身在 app/core/uploads.py —— 期初余额解析共用同一套（审计 P1-4）。
# 下面两个名字只为兼容既有用法与测试：``check_archive_size`` 曾是本模块的私有
# 函数，现在是共享工具的转出（同一个对象，不是复制品 —— 两条上传入口共用一把尺，
# 这一点必须靠「同一个函数」保证，而不是靠两处代码写得一样）。
__all__ = [
    "ALIASES", "UploadTooLarge", "check_archive_size", "commit", "discard",
    "map_columns", "parse_workbook", "purge_stale_batches", "read_upload",
    "stage",
]


# --------------------------------------------------------------------- 解析


def _read_error(exc: Exception) -> str:
    """把底层 reader 的报错并回本模块对外的那一句。

    分流用的是 ``subject_importer`` 里的两个 reader，它们各自报「无法读取 .xls
    文件：…」/「无法读取 .xlsx 文件：…」。而本模块对外一直是一句「无法读取
    Excel 文件：…」（``tests/test_import_limits.py`` 盯着这一句：不能因为多加了
    一道容量检查，就把已经对用户说清楚的报错口径换掉）。所以摘掉重复的前缀、
    把细节拼回去，一句话说完，不让人看到「无法读取 Excel 文件：无法读取
    .xlsx 文件：…」这种叠罗汉。
    """
    text = str(exc).strip()
    if text.startswith("Excel 里"):   # 「Excel 里没有工作表」本身就是完整报错
        return text
    for prefix in ("无法读取 .xls 文件：", "无法读取 .xlsx 文件："):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return f"无法读取 Excel 文件：{text}"


def parse_workbook(data: bytes, book_id: int, db: Session,
                   filename: str | None = None) -> dict:
    """解析 Excel 字节流（``.xlsx`` 或用友的 ``.xls``），返回可 JSON 化的暂存包。

    不写库。``filename`` 只在文件内容看不出格式时兜底，所以它是可选参数 ——
    既有调用方（测试里成片的 ``parse_workbook(data, book.id, db)``）不用改。
    """
    check_archive_size(data)
    try:
        _sheet, rows_iter, _fmt = subject_importer.iter_workbook_rows(
            data, filename or "")
    except ValueError as exc:
        raise ValueError(_read_error(exc)) from exc

    try:
        return _parse_rows(rows_iter, book_id, db)
    finally:
        # reader 把「关工作簿」放在行生成器的 finally 里（.xls 的
        # release_resources / .xlsx 的 close）。正常路径会把生成器耗尽而触发它，
        # 但异常路径（撞上限、金额非法）不会 —— 不显式关就是漏一个句柄。
        rows_iter.close()


def _merge_header(first: list[Any], second: list[Any]) -> list[str]:
    """把上下两行表头按列拼起来（用友某些版本把表头分两行）。"""
    width = max(len(first), len(second))
    out: list[str] = []
    for index in range(width):
        top = _cell_text(first[index]) if index < len(first) else ""
        bottom = _cell_text(second[index]) if index < len(second) else ""
        out.append(f"{top}{bottom}")
    return out


def _header_score(mapping: dict[str, int]) -> int:
    """这个表头认出了几样**必需**的东西（满分 3）。

    必需的三样：日期、能定位科目的列（编码或名称）、金额列（借/贷/金额）。
    打分而不是「认不认得出」：一份表头可能只认得出两样 —— 那时候仍然
    是它，缺的那一样由下面那句报错说清楚。
    """
    score = 0
    if "voucher_date" in mapping:
        score += 1
    if "subject" in mapping or "subject_name" in mapping:
        score += 1
    if {"debit", "credit", "amount"} & set(mapping):
        score += 1
    return score


def _scan_header(rows: list[list[Any]]) -> tuple[int, list[Any], int]:
    """在前若干行里找表头，返回 ``(表头首行下标, 表头内容, 表头占几行)``。

    找不到就返回 ``(0, rows[0], 1)`` —— 让下面那句「缺少必需的表头列」
    照原样报出去（用户看到的是他第 1 行的内容）。

    双行合并表头：用友某些版本把「日期」「凭证号」这类列名分两行放，
    单行必然认不全 —— 合并下一行才认得出。这种表头**占两行**，
    数据从第三行开始（行号基准也跟着走，见 ``_parse_rows``）。
    """
    best_score = -1
    best = (0, rows[0], 1)
    for index, row in enumerate(rows):
        mapping, _unknown = map_columns(row)
        score = _header_score(mapping)
        if score > best_score:
            best_score, best = score, (index, row, 1)
        # 只在「这一行自己已经认出点东西」时才去拼下面那行：
        # 否则标题行（「序时账」「2026 年 10 月」）会和真正的表头拼在一起。
        if score >= 1 and index + 1 < len(rows):
            merged = _merge_header(row, rows[index + 1])
            merged_mapping, _unknown = map_columns(merged)
            merged_score = _header_score(merged_mapping)
            if merged_score > best_score:
                best_score, best = merged_score, (index, merged, 2)
    return best


def _parse_rows(rows_iter, book_id: int, db: Session) -> dict:
    # ---- 表头扫描（审查修复）-----------------------------------------
    #
    # 原来这里只认「第 1 行就是表头」（``header = next(rows_iter)``）。
    # 外部系统导出常在表头上方带标题、制表日期、账套名（用友导出就是
    # 这样），于是**整份表读不进来** —— 而期初余额导入早就有同款扫描
    # （``opening_importer`` 扫 20/15 行、还认双行合并表头）。凭证导入
    # 这一条是漏的。
    #
    # 表头不在第 1 行时，**行号基准同步修正**（下面 ``line_no`` 的初值）——
    # 否则报错里说的「第 N 行」指的是「第 N 个数据行」，用户在 Excel 里
    # 按行号找会找错地方。
    buffered: list[list[Any]] = []
    index = 0
    while index < VOUCHER_HEADER_SCAN_ROWS:
        try:
            row = next(rows_iter)
        except StopIteration:
            break
        buffered.append(list(row))
        index += 1
    if not buffered:
        raise ValueError("Excel 是空文件")

    header_index, header, header_rows = _scan_header(buffered)
    mapping, unknown = map_columns(list(header))
    missing = [
        name
        for key, name in (("voucher_date", "日期"), ("subject", "科目编码"))
        if key not in mapping
    ]
    if missing:
        raise ValueError(
            f"缺少必需的表头列：{'、'.join(missing)}。"
            f"请使用「凭证导入模板」下载标准表头。识别到的表头：{[str(h) for h in header]}"
        )
    has_amount = any(k in mapping for k in ("debit", "credit", "amount"))
    if not has_amount:
        raise ValueError("缺少金额列（借方 / 贷方 / 金额 三选一）")

    # 科目表每个年度各有一份 —— 一份序时账可能跨年，而每年的编码表
    # 未必相同。所以索引**按年度建、按这一行的日期选**。
    # 一年建一次就够：真实文件一年几十行，索引查一次是几条 SQL 的事。
    _indexes: dict[int, tuple[dict[str, Subject], dict[str, Subject]]] = {}

    def _index_for(year: int) -> tuple[dict[str, Subject], dict[str, Subject]]:
        if year not in _indexes:
            _indexes[year] = _subject_index(db, book_id, year)
        return _indexes[year]

    # 日期认不出来的行（日期已经报错了）退回到账套当前在记的那一年 ——
    # 那张凭证本来就不会进库，用哪一年的表去认科目只影响多报不多报一条错。
    _fallback_year = _current_accounting_year(db, book_id)
    unknown_subjects: dict[str, str] = {}
    journal = _looks_like_journal(header)
    footer_rows = 0

    def col(key: str, row: list[Any]) -> Any:
        idx = mapping.get(key)
        return row[idx] if idx is not None and idx < len(row) else None

    vouchers: list[dict] = []
    errors: list[dict] = []
    current: dict | None = None
    # **行号基准跟着表头走**（审查修复）：表头在第 1 行时初值是 1，
    # 循环里先加 1 再处理，于是第一个数据行是「第 2 行」—— 与 Excel
    # 里的真实行号一致。表头不在第 1 行时（上面扫描出来的 ``header_index``），
    # 初值同步平移，报错里说的「第 N 行」才是用户在 Excel 里看到的那一行。
    line_no = header_index + header_rows
    rows_budget = RowBudget(
        settings.import_max_rows,
        hint="请按期间或按凭证类别拆成多份分别导入")

    def close_current() -> None:
        nonlocal current
        if current is None:
            return
        # 分录金额在 payload 里是字符串（保证 JSON 可序列化），这里按 Decimal 累加
        dr = sum((Decimal(e["amount"]) for e in current["entries"]
                  if e["direction"] == "DEBIT"), Decimal("0"))
        cr = sum((Decimal(e["amount"]) for e in current["entries"]
                  if e["direction"] == "CREDIT"), Decimal("0"))
        current["total_debit"] = str(dr)
        current["total_credit"] = str(cr)
        if not current["entries"]:
            current["errors"].append("没有任何分录")
        elif dr != cr:
            current["errors"].append(f"借贷不平衡：借 {dr} / 贷 {cr}")
        # 这里**不再**挡「借贷合计都是 0」。
        #
        # 原来挡的是 ``dr == 0 and cr == 0``，注释写着「只挡全零，不挡负数」，
        # 可它拿**合计**代替了「有没有记东西」，于是把同侧相抵的凭证一起误杀：
        # 用友导出的商管序时账 556 张里有 6 张是这种（结息、团体险分摊、
        # 工会经费返还 —— 银行账户几笔正数 + 费用一笔等额负数，借方自己抵平），
        # 报出来是「这张凭证什么也没记」，恰恰说反了。
        #
        # 而「一条都没填」这件事上面那句「没有任何分录」已经拦住了：两条取
        # 金额的路（借贷双列、单金额列）都以「解析出来是不是非零」为准，
        # 金额 0 的行根本不生成分录，所以**分录的金额永远不为 0** ——
        # 再加一道「每条都是 0」的判断只能是走不到的死代码。
        current["ok"] = not current["errors"]
        vouchers.append(current)
        current = None
        # 凭证数上限：行数上限挡不住「一张凭证几十分录」的放大 ——
        # 行数合规但凭证条数失控时，累积的列表依然能吃掉大量内存。
        if len(vouchers) > settings.import_max_vouchers:
            raise ValueError(
                f"这份表解析出超过 {settings.import_max_vouchers} 张凭证，超过单次导入上限。"
                "请拆成多份分别导入")

    # 表头前面那几行（标题、制表日期）**不是数据**，直接丢掉；
    # 表头占的那几行也丢掉。剩下的行先从缓冲区里取，再接上文件剩下的部分。
    data_start = header_index + header_rows
    pending_rows = iter(buffered[data_start:])
    for row in itertools.chain(pending_rows, rows_iter):
        line_no += 1
        # 行数与凭证数的上限在**遍历过程中**判，而不是先 count 一遍：
        # 先 count 照样会把整个工作表读进内存，那正是要防的事。
        # 触发上限时立刻中止解析并抛错，调用方回滚，暂存表里不留残骸。
        rows_budget.tick()
        if row is None or all(c in (None, "") for c in row):
            continue

        raw_date = col("voucher_date", row)
        raw_no = col("voucher_no", row)
        row_date = str(raw_date).strip() if raw_date not in (None, "") else ""
        row_no = str(raw_no).strip() if raw_no not in (None, "") else ""
        row_subject = col("subject", row)
        if row_subject in (None, ""):
            row_subject = col("subject_name", row)

        # 表尾的「合计」行：没有日期、没有凭证号、也没有科目，却带着金额。
        # 用友序时账必带这种行（借贷两行合计全部发生额），而它本来会命中下面
        # 的「接上一张」规则，被挂到上一张**真实**凭证头上 ——
        # 实测那份商管序时账的合计是 14,538,288.54，够把一张凭证直接撑爆。
        # 所以在这里挡掉，并且计数上报，不静默吞掉。
        #
        # 只认「三项全空」这一个形状：这种行在任何格式下都不可能是分录，
        # 判据不含糊。曾经的处理是让它报「有金额但没填科目」并把上一张凭证
        # 标成不通过 —— 那是把表尾的合计误算到别人的账上。
        if not (row_date or row_no or (row_subject not in (None, ""))):
            # 全空行在上一段已经 continue 过了，走到这里的必然有内容。
            footer_rows += 1
            continue

        # 汇总行：**标签写进了科目列**的那种（审计 P2-13）。
        #
        # 上面那条只认「三项全空」这一个形状，而真实文件还有另一种：表尾
        # 「合计」行把标签写进「科目名称」列、日期与凭证号留空。此时
        # row_subject 非空，上面那条判据放它过去，接着它会在科目查找里
        # 失败，于是「找不到科目「合计」」被挂到**上一张真实凭证**头上 ——
        # 那张凭证因此被标成不通过、静默地不进库。
        #
        # 后果不脏数据但很冤：用户什么也没做错，一张正经凭证凭表尾一行
        # 就没导进去。期初导入早就按标签剔过这类行（opening_importer），
        # 凭证导入却只有结构性判据 —— 现在两处共用 util.is_summary_label。
        if is_summary_label(col("subject_code", row), row_subject):
            footer_rows += 1
            continue

        # 换一张凭证的判据。
        #
        #   * 普通表：日期或凭证号非空 = 新的一张（一张凭证几行，后续行留空）。
        #   * 序时账：每行都重复完整的日期与凭证号数，所以**与当前这张相同
        #     才算接上一张**。少了这个区分，一张 112 条分录的凭证会被拆成
        #     112 张每条都借贷不平衡的废凭证 —— 而且它不是报错，是「成功导入
        #     0 张可用凭证」，最容易把人骗过去。
        #
        # 顺带把凭证号原样留着（用友是「记-0001」这种），因为系统的唯一约束是
        # (期间, 凭证号)：用友每个月都从 0001 重新编号，跨月重号是正常的，
        # 落在不同期间里本来就不冲突；而按期间分组这件事 create_voucher
        # 会用日期去找，不依赖这里。
        same_as_current = (
            current is not None and row_date and row_no
            and current.get("voucher_date_raw") == row_date
            and current.get("voucher_no") == row_no
        )
        starts_voucher = bool(row_date or row_no) and not same_as_current
        if starts_voucher:
            close_current()
            current = {
                "line": line_no,
                "voucher_date": _as_date(raw_date).isoformat() if _as_date(raw_date) else None,
                "voucher_date_raw": row_date,
                "voucher_no": row_no or None,
                "remark": str(col("remark", row) or ""),
                "attachments": int(_as_decimal(col("attachments", row)) or 0),
                "entries": [],
                "errors": [],
            }
            if current["voucher_date"] is None:
                current["errors"].append(f"日期无法识别：{raw_date!r}")

        if current is None:
            continue  # 表头之后的空行 / 无日期无凭证号的散行

        subject = None
        if row_subject not in (None, ""):
            key = str(row_subject).strip()
            # 用**这一行所属年度**的科目表去认编码/名称
            _vdate = current.get("voucher_date")
            _year = int(_vdate[:4]) if _vdate else _fallback_year
            by_code, by_name = _index_for(_year)
            subject = by_code.get(key) or by_name.get(key)
            # 序时账的「上级科目」是明细科目的完整路径，科目对不上时它就是
            # 最有用的那句线索：用户能据此判断是本账套缺了这个明细，还是
            # 自己在别的模块录了同一件事。没有这一列时不提。
            path_hint = ""
            path = col("parent_subject", row)
            if path not in (None, "") and journal:
                path_hint = f"，该分录的上级科目路径是「{str(path).strip()}」"
            if subject is None:
                unknown_subjects[key] = "科目编码或名称在本账套不存在"
                current["errors"].append(
                    f"第 {line_no} 行：找不到科目「{key}」{path_hint}")
            elif not subject.is_leaf:
                # 表里写的是上级科目。它存在，只是不能往它身上记：
                # 说清楚是哪一行、哪个科目，人改表还是改科目决定权都在自己手上
                unknown_subjects[key] = "不是明细科目，凭证要记在明细级"
                current["errors"].append(
                    f"第 {line_no} 行：{subject.code} {subject.name} 不是明细科目，凭证要记在明细级{path_hint}")
                subject = None

        # 借贷方向由「值填在哪一列」决定，**不能靠正负猜**。
        #
        # 原来写的是 ``direction = "DEBIT" if dr > 0 else "CREDIT"``，
        # 由此产生两个后果：
        #   · 红字（负数）分录在这一行被 ``dr <= 0 and cr <= 0`` 静默丢弃 ——
        #     冲销一张记错的凭证正是记账的日常，它恰恰是最该保真的场景；
        #   · 同一行借贷两列都填了值时，只按借方算，贷方那栏的值凭空消失，
        #     连报错都没有。
        raw_dr = col("debit", row)
        raw_cr = col("credit", row)
        dr, dr_err = _parse_amount(raw_dr)
        cr, cr_err = _parse_amount(raw_cr)
        for msg in (dr_err, cr_err):
            if msg:
                current["errors"].append(f"第 {line_no} 行：{msg}")

        direction: str | None = None
        amount = Decimal("0")
        amount_raw = ""

        # 「填了」的标准是**解析出来是不是非零**，不是单元格空不空：
        # 财务导出的表常在对面那栏填 0 而不是留空（测试数据就是这个样子），
        # 按「非空」判定会把每一行都误判成「借贷都填了」。
        dr_filled = raw_dr not in (None, "") and dr != 0
        cr_filled = raw_cr not in (None, "") and cr != 0

        if dr_filled and cr_filled:
            current["errors"].append(
                f"第 {line_no} 行：借方和贷方都填了金额"
                f"（借 {raw_dr} / 贷 {raw_cr}），一行只能有一条分录")
        elif dr_filled:
            direction, amount, amount_raw = "DEBIT", dr, str(raw_dr)
        elif cr_filled:
            direction, amount, amount_raw = "CREDIT", cr, str(raw_cr)
        elif "amount" in mapping and col("amount", row) not in (None, ""):
            # 单金额 + 方向列的老表格式。用友序时账走的正是这一路：
            # 一列「金额」配一列「方向」（写「借」或「贷」）。
            raw_amt = col("amount", row)
            amt, amt_err = _parse_amount(raw_amt)
            if amt_err:
                current["errors"].append(f"第 {line_no} 行：{amt_err}")
            raw_dir = str(col("direction", row) or "").strip()
            d = DIRECTION_WORDS.get(_norm(raw_dir))
            if amt == 0:
                pass                      # 金额 0：不生成行（错误若已记过就够了）
            elif d is not None:
                direction, amount, amount_raw = d, amt, str(raw_amt)
            else:
                current["errors"].append(
                    f"第 {line_no} 行：金额 {raw_amt!r} 无法判断方向，"
                    f"方向列填「{raw_dir or '空'}」，只认 借/贷 或 DEBIT/CREDIT")

        if direction is None:
            # 这一行没有金额：表头、空行、说明文字都走这条路，不该报错。
            # 真正的非法金额（乱码、百分比）已经在上面记进 errors 了。
            continue
        if subject is None:
            # 找不到科目 / 不是明细科目：原因上面已经记过，这里只补「压根没填科目」
            if row_subject in (None, ""):
                current["errors"].append(f"第 {line_no} 行：有金额但没填科目")
            continue

        current["entries"].append(
            {
                "subject_id": subject.id,
                "subject_code": subject.code,
                "subject_name": subject.name,
                "direction": direction,
                "amount": str(amount),
                "amount_raw": amount_raw,     # 留原始值：出问题时能对上源文件
                "summary": str(col("summary", row) or ""),
            }
        )

    close_current()

    if not vouchers:
        raise ValueError("没有解析出任何凭证行")

    for v in vouchers:
        if not v["ok"]:
            errors.append({"line": v["line"], "voucher_no": v["voucher_no"], "errors": v["errors"]})

    return {
        "vouchers": vouchers,
        "errors": errors,
        "unknown_subjects": unknown_subjects,
        "unknown_headers": unknown,
        "journal": journal,
        "footer_rows": footer_rows,
    }


# --------------------------------------------------------------------- 暂存 / 入库


# 摘要里最多留多少条错误：够人回源文件定位，又不至于把整份表又存一遍。
_SUMMARY_ERROR_LIMIT = 200


def _summarized_payload(payload: dict | None) -> dict:
    """确认/作废之后替换 payload 用：只留摘要，不留凭证正文（审计 P2-02）。

    ``payload`` 里装的是**整份工作簿解析结果**：每张凭证的每一条分录，
    科目编码、名称、金额、原始字符串、摘要文字。批次已经入库或作废之后，
    这些内容没有任何用途 —— 凭证在 ``voucher`` / ``entry`` 表里，错误在审计
    日志与用户手上的源文件里。留着只有一个后果：一张几万行的导入表，
    它的科目与金额会一直躺在暂存表里，而那张表本来该在上确认那一刻就消失。

    保留的仍然够复盘用：失败在哪一行、错在什么科目、这次导了多少张。
    """
    payload = payload or {}
    errors = list(payload.get("errors") or [])[:_SUMMARY_ERROR_LIMIT]
    dropped = max(0, len(payload.get("errors") or []) - len(errors))
    summary = {
        "cleared": True,
        "total": len(payload.get("vouchers") or []),
        "ok": sum(1 for v in payload.get("vouchers") or [] if v.get("ok")),
        "errors": errors,
        "unknown_subjects": payload.get("unknown_subjects") or {},
        "unknown_headers": payload.get("unknown_headers") or [],
        # 这两项是「这份表长什么样」的描述，不含凭证正文，留在摘要里正好
        # 让事后复盘知道当初是不是走的序时账排版、跳过了几行合计。
        "journal": bool(payload.get("journal")),
        "footer_rows": int(payload.get("footer_rows") or 0),
    }
    if dropped:
        summary["errors_truncated"] = dropped
    return summary


def stage(db: Session, book_id: int, user: SysUser, filename: str, data: bytes) -> ImportBatch:
    parsed = parse_workbook(data, book_id, db, filename=filename)
    ok = sum(1 for v in parsed["vouchers"] if v["ok"])
    batch = ImportBatch(
        book_id=book_id,
        filename=filename or "未命名.xlsx",
        status="PENDING",
        total=len(parsed["vouchers"]),
        ok_count=ok,
        error_count=len(parsed["vouchers"]) - ok,
        payload=parsed,
        created_by=user.username,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def purge_stale_batches(db: Session, book_id: int | None = None,
                        ttl_days: int | None = None) -> int:
    """清掉久未确认的暂存批次，返回条数（审计 P2-02）。

    确认/作废时 payload 已经当场清空，但这解决不了「传完就忘了点确认」的
    那些批次：它们永远是 PENDING，payload 永远是完整的一份工作簿。
    列表接口的 ``LIMIT 100`` 只限制返回条数，不会删除任何东西 ——
    多次导入的批次会一直累积在表里。

    只删 PENDING：COMMITTED / DISCARDED 的批次元数据（谁在什么时候导了多少张）
    是有价值的追溯记录，而 payload 已经清空了，占不了什么空间。
    """
    ttl = settings.import_batch_ttl_days if ttl_days is None else ttl_days
    if ttl <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl)
    stmt = delete(ImportBatch).where(
        ImportBatch.status == "PENDING",
        ImportBatch.created_at < cutoff,
    )
    if book_id is not None:
        stmt = stmt.where(ImportBatch.book_id == book_id)
    result = db.execute(stmt)
    db.commit()
    return int(result.rowcount or 0)


def commit(db: Session, batch: ImportBatch, user: SysUser) -> dict:
    """把暂存包里通过校验的凭证写入。任一张失败则整体回滚。"""
    from app.accounting import vouchers as svc

    if batch.status != "PENDING":
        raise ValueError(f"该导入批次已是「{batch.status}」状态，不能重复提交")

    created, failed = 0, []
    for v in batch.payload.get("vouchers", []):
        if not v.get("ok"):
            continue
        try:
            svc.create_voucher(
                db,
                book_id=batch.book_id,
                user=user,
                voucher_date=date.fromisoformat(v["voucher_date"]),
                entries=[
                    {
                        "subject_id": e["subject_id"],
                        "direction": e["direction"],
                        "amount": e["amount"],
                        "summary": e.get("summary", ""),
                    }
                    for e in v["entries"]
                ],
                remark=v.get("remark", ""),
                attachments=int(v.get("attachments") or 0),
                source="IMPORT",
                voucher_no=v.get("voucher_no"),
                commit=False,
            )
            created += 1
        except (ValueError, ArithmeticError) as exc:
            # ArithmeticError 是 Decimal 异常族的公共祖先（InvalidOperation /
            # DivisionByZero / Overflow 都从它出），**不是 ValueError 的子类**。
            # 这里原来只接 ValueError，于是金额侧的 Decimal 异常直接穿透到
            # 500。服务层的 money() 已经把它们翻译成 ValueError 了，这一行是
            # 第二道：将来有人绕过 money() 直接 Decimal(...)，也不会 500。
            #
            # 凭证号撞车也走这条路：``create_voucher`` 里那次占用检查没有锁，
            # 并发时两个人会拿到同一个号，数据库抛 IntegrityError，
            # 由服务层翻成「凭证号已经有一张了」的人话（审计 P1-3）。
            failed.append({"line": v["line"], "voucher_no": v.get("voucher_no"), "error": str(exc)})

    if failed:
        db.rollback()
        return {
            "ok": False,
            "created": 0,
            "failed": failed,
            "message": f"{len(failed)} 张凭证导入失败，已整体回滚，未写入任何数据",
        }

    batch.status = "COMMITTED"
    batch.committed_count = created
    # 时间一律写 UTC（审计 P2-23）。原来是 ``datetime.now()`` —— 本地 naive 时间，
    # 而列是 ``DateTime(timezone=True)``：psycopg 会按数据库会话的 TimeZone 把它
    # 当成本地时间解释，同一份数据在不同部署的时区下会偏掉 8 小时。
    # ``created_at`` 由 server_default now() 给（UTC），``purge_stale_batches``
    # 的 TTL 清理也拿 UTC 的 cutoff 比 —— 两边必须同一个时区，否则过期批次
    # 要么永远清不掉、要么刚上传就被清。
    batch.committed_at = datetime.now(timezone.utc)
    batch.committed_by = user.username
    # P2-02：凭证已经入库，payload 里的分录正文没有存在理由了。
    # 先算摘要再清：下面的 log() 不用 payload，但将来有人想在这里加统计，
    # 也不会发现数据已经没了 —— 这个顺序要写在注释里，否则早晚会有人调换。
    batch.payload = _summarized_payload(batch.payload)
    log(
        db,
        action="IMPORT",
        entity="voucher",
        entity_id=str(batch.id),
        user=user,
        book_id=batch.book_id,
        detail=f"Excel 导入凭证 {created} 张（{batch.filename}）",
        after={"batch_id": batch.id, "created": created},
    )
    db.commit()
    return {"ok": True, "created": created, "failed": [], "message": f"成功导入 {created} 张凭证"}


def discard(db: Session, batch: ImportBatch) -> None:
    if batch.status != "PENDING":
        raise ValueError(f"该导入批次已是「{batch.status}」状态")
    batch.status = "DISCARDED"
    # P2-02：作废同样清空正文。这批数据没人要了，但「作废了却还留着一整份
    # 凭证内容」是最坏的一种残留 —— 界面上看不见，库里一直在。
    batch.payload = _summarized_payload(batch.payload)
    db.commit()
