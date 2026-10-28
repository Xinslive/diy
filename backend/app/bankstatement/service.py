"""流水对账的服务层：对账单台账、流水行、科目规则、格式画像与流水制证。

这一层把「解析器读出来的东西」变成「账上的东西」，中间隔着三件事：

1. **入库**（:func:`upload_statement`）—— 落原件、落行、落诊断；
2. **认账**（:func:`plan_vouchers`）—— 哪些流水已经有凭证了、哪些还没；
3. **记账**（:func:`commit_vouchers`）—— 一条流水一张凭证。

**方向在这里只有一处转换。** 解析层给出的 ``IN``/``OUT`` 是**账户余额**
的方向（收钱 = IN），而会计凭证上银行存款增加记**借方**。所以转换恒为：

    收入（IN） → 借 现金科目 / 贷 对方科目
    支出（OUT）→ 借 对方科目 / 贷 现金科目

**别把银行的「贷方发生额」当成会计的贷方** —— 那是整个模块最容易
搞错的一处，所以它写在 :mod:`app.models.bankstatement` 的模块 docstring 里，
这里再抄一遍。
"""
from __future__ import annotations

import copy
import logging
import os
import re
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.accounting import vouchers as voucher_service
from app.accounting.util import find_period, money
from app.bankstatement import formats, matching, semantics, storage
from app.models import (
    BankStatement,
    BankStatementConfig,
    BankStatementProfile,
    BankStatementRow,
    BankStatementRowSubject,
    BankStatementRule,
    Book,
    Entry,
    InvoiceRecord,
    Period,
    Subject,
    SysUser,
    Voucher,
)
from app.models.bankstatement import (
    DIRECTION_IN,
    DIRECTION_OUT,
    DIRECTION_UNKNOWN,
    PARSE_FAILED,
    PARSE_OK,
    PARSE_PARTIAL,
    PLAN_MANUAL,
    PLAN_NONE,
    PLAN_PENDING,
    PLAN_RULE,
    STATUS_IGNORED,
    STATUS_OPEN,
    STATUS_POSTED,
)

log = logging.getLogger("cwq")

#: 现金类科目的编码前缀。1001 库存现金、1002 银行存款、1012 其他货币资金。
#:
#: **为什么是前缀而不是「用户勾选这是现金科目」**：这两个前缀就是
#: 「钱放在哪」的三个去处，认错了会影响余额核对（拿存货跟银行流水比）。
#: 界面上仍然让用户**具体选一个明细科目** —— 前缀只用来做「这该不会是个
#: 费用科目吧」的提示，不代替用户的选择。
CASH_SUBJECT_PREFIXES = ("1001", "1002", "1012")

#: 去重窗口（天）：凭证日期在流水日前后这么多天之内、同一现金科目、
#: 同方向、同金额，就当成「这笔已经入过账了」。
#:
#: 为什么是 7 天：银行流水与发票/收付款单据之间本来就会差几天
#: （发票开了几天才付款、货款到了才开票），窗口太小会漏判成重复入账，
#: 窗口太大则会把「这个月付了两笔一样的钱」误判成同一笔 ——
#: 那会让真正该记的第二笔永远进不了账。
DEFAULT_DEDUPE_WINDOW_DAYS = 7

#: 流水行允许的最大条数。超了直接报错，不截断 ——
#: 截断出来的对账单会让余额对不上，而用户看不出少了尾巴。
MAX_STATEMENT_ROWS = 20000


class StatementParseError(ValueError):
    """解析失败但原件与失败记录已经写入当前事务。"""


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return money(value)
    except ValueError:
        return None


def _text(value: Any) -> str:
    return str(value or "").strip()


# ----------------------------------------------------------------------
# 科目校验
# ----------------------------------------------------------------------

def cash_subject(db: Session, book_id: int, subject_id: int) -> Subject:
    """取一个**可绑定的现金科目**，并给出人话的错误原因。

    三道校验，缺一不可：

    - 属于本账套 —— 跨账套绑科目等于把 A 公司的流水记到 B 公司的账上；
    - 末级 —— 绑到「1002 银行存款」这种上级科目上，凭证会被库层守卫拦下；
    - 启用 —— 停用科目是「不再用了」，往里记钱等于记进一口井里。
    """
    subject = db.get(Subject, subject_id)
    if subject is None or subject.book_id != book_id:
        raise ValueError("选择的科目不属于本账套")
    if not subject.is_leaf:
        raise ValueError(
            f"「{subject.code} {subject.name}」不是末级科目，"
            "对账单要绑到具体的明细科目上")
    if not subject.active:
        raise ValueError(f"「{subject.code} {subject.name}」已停用，不能绑定对账单")
    return subject


def _looks_cash(subject: Subject) -> bool:
    return any(subject.code.startswith(prefix) for prefix in CASH_SUBJECT_PREFIXES)


# ----------------------------------------------------------------------
# 上传与解析
# ----------------------------------------------------------------------

def _effective_period(parsed: semantics.ParsedStatement) -> tuple[Optional[date], Optional[date]]:
    """这份对账单覆盖哪一段时间。

    文件自报的期间优先；文件没写就**用流水行的日期兜底** ——
    银行那份对账单没有一行「本期范围」是常事，而流水日期是实打实的。

    用行日期兜底还有一个好处：**重复上传的守卫需要一把稳定的钥匙**。
    自报期间写「2026-09」这种含糊值时，解析出来的字符串可能每次都不同，
    于是同一份文件传两遍会绕过下面那道守卫。
    """
    start = parsed.meta.period_start
    end = parsed.meta.period_end
    dates = [row.tx_date for row in parsed.rows if row.tx_date]
    if dates:
        start = start or min(dates)
        end = end or max(dates)
    return start, end


def _opening_balance(parsed: semantics.ParsedStatement) -> Optional[Decimal]:
    """期初余额 = 期末余额 − 收入 + 支出。

    对账单通常只印期末余额，而期初余额是从「银行那边这个月一共动了多少」
    倒推出来的。倒推不出来（文件没给期末余额）就留空 ——
    **不猜**：期初余额是用来跟期初余额表对照的，猜出来的数字会让对照
    变成「自己跟自己比」。
    """
    closing = _dec(parsed.meta.closing_balance)
    if closing is None:
        return None
    return closing - parsed.income_total + parsed.expense_total


def _ocr_note(parsed: semantics.ParsedStatement) -> str:
    """OCR 参与过这一份解析吗 —— 参与过就要在界面上说出来。

    **为什么必须说**：OCR 是**识别**（会认错字），而文字层是精确取字；
    更要紧的是，OCR 这一路把**文件正文送出了本机**（交给外部 OCR 服务）。
    这两件事用户有权知道。

    **两种说法**：整份都是 OCR 认的（``via_ocr``），与两路合并、
    其中一半是 OCR 补的（``via_merge``）—— 后者更要说清楚，
    因为金额是精确取来的、方向是识别来的。
    """
    if parsed.via_merge:
        return "这一份是文字层与 OCR 合并识别的"
    if parsed.via_ocr:
        return "这一份是用 OCR 识别的"
    return ""


def _parse_message(parsed: semantics.ParsedStatement) -> str:
    """解析说明（界面上那段话）。

    三件事都在这儿说清楚：**哪里对不上**（自检的诊断）、**OCR 有没有参与**
    （正文出没出本机）、**两路认得有没有不一样**。
    """
    parts: list[str] = []
    if parsed.diagnosis:
        parts.append("；".join(parsed.diagnosis))
    note = _ocr_note(parsed)
    if note:
        parts.append(note)
    if parsed.merge_conflicts:
        parts.append(
            f"两路解析有 {len(parsed.merge_conflicts)} 处认得不一样，已按自检结果取舍")
    return "；".join(parts)


def _diagnosis_of(parsed: semantics.ParsedStatement) -> dict[str, Any]:
    return {
        "container": parsed.container,
        "sheet": parsed.sheet,
        "header_row": parsed.header_row,
        "header_cells": parsed.header_cells,
        "column_mapping": parsed.column_mapping,
        "notes": list(parsed.diagnosis),
        "merge_conflicts": list(parsed.merge_conflicts),
        "grid_preview": parsed.grid_preview,
        "fingerprint": parsed.fingerprint,
        "meta": {
            "account_no": parsed.meta.account_no,
            "account_name": parsed.meta.account_name,
            "bank_name": parsed.meta.bank_name,
            "currency": parsed.meta.currency,
            "closing_balance": (str(parsed.meta.closing_balance)
                                if parsed.meta.closing_balance is not None else ""),
            "declared_income_count": parsed.meta.declared_income_count,
            "declared_income_total": (str(parsed.meta.declared_income_total)
                                      if parsed.meta.declared_income_total is not None else ""),
            "declared_expense_count": parsed.meta.declared_expense_count,
            "declared_expense_total": (str(parsed.meta.declared_expense_total)
                                       if parsed.meta.declared_expense_total is not None else ""),
        },
    }


def _ocr_grid(db: Session, data: bytes) -> Optional[Any]:
    """扫描件对账单 → OCR → 网格。

    **什么时候走这里**：① 整份 PDF 没有文本层（扫描件）；② 文字层解析出来
    **自检没通过**（诊断非空）—— 2026-10-27 实测过第二种：Linux 上 pdfium
    算出来的字形框退化，表头认不出来，解析结果「18050.00」被截成「1805」
    却**不报错**。这时 OCR 是第二意见，它看的是渲染出来的图像，没有这个问题。

    没配 OCR 服务时返回 ``None``，由调用方给出**明确的报错**并保留原件：
    扫描件对账单在没有人来读的时候，是没法变成流水行的。
    """
    from app.core.system_settings import ocr_config
    from app.invoice.ocr_client import (OcrApiError, ocr_pdf_to_markdown,
                                        optional_payload_from_env)

    config = ocr_config(db)
    if not (config.get("api_url") and config.get("token")):
        return None

    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        handle.write(data)
        handle.close()
        try:
            pages = ocr_pdf_to_markdown(
                handle.name,
                job_url=config["api_url"],
                token=config["token"],
                model=config.get("model") or "PaddleOCR-VL-1.6",
                optional_payload=(config.get("optional_payload")
                                  or optional_payload_from_env("{}")),
                timeout_seconds=config.get("timeout_seconds", 600),
                poll_seconds=config.get("poll_seconds", 5.0),
            )
        except OcrApiError as exc:
            log.warning("对账单 OCR 兜底失败：%s", exc)
            return None
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    return _grid_from_ocr_markdown(pages)


def _grid_from_ocr_markdown(pages: list[str]) -> Optional[Any]:
    """OCR 给回来的 markdown（按页）→ 网格。

    **表格优先**：OCR 认出的是 HTML 表格，**格边界要原样保留** ——
    摊平成空格分隔的文字行会丢掉空格子（「序号」那一列常常是空的），
    列数于是与表头对不上，下游只能靠猜（实测光大那份的金额被猜成了
    「1.00」—— 序号那一列的值）。

    抽成独立函数是为了让**测试走同一条转换链** —— 测试自己拼一遍的链
    与生产不是同一条，就等于没测生产那条。
    """
    from app.bankstatement import pdf_table
    from app.invoice.ocr_client import markdown_html_tables, markdown_to_plain_text

    tables: list[str] = []
    meta_lines: list[str] = []
    footer_lines: list[str] = []
    for page in pages:
        text = str(page or "")
        found = markdown_html_tables(text)
        if not found:
            continue
        tables.extend(found)
        before, _sep, after = text.partition(found[0])
        meta_lines.extend(_plain_lines(markdown_to_plain_text(before)))
        footer_lines.extend(_plain_lines(markdown_to_plain_text(after)))
    if tables:
        grid = pdf_table.grid_from_html_tables(
            tables, meta_lines=meta_lines, footer_lines=footer_lines)
        if grid is not None:
            return grid

    lines = [line for page in pages for line in str(page or "").splitlines()
             if line.strip()]
    if not lines:
        return None
    return pdf_table.grid_from_text_lines(lines)


def _plain_lines(text: str) -> list[str]:
    """一段文字 → 去掉空行的文本行。"""
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _pdf_grid(db: Session, data: bytes) -> Any:
    """PDF → 网格。

    先按坐标还原（准），取不到就退回纯文本流切分（粗），
    再取不到就交给 OCR 兜底。
    """
    from app.bankstatement import pdf_table

    grid = pdf_table.extract_grid(data)
    if grid is None:
        grid = _ocr_grid(db, data)
    if grid is None:
        raise ValueError("这份 PDF 取不到表格（没有文本层，且 OCR 兜底也没成功）")
    return grid


def _ocr_parse(db: Session, data: bytes) -> Optional[semantics.ParsedStatement]:
    """OCR 兜底 → 解析结果。

    OCR 没配、调用失败、或者 OCR 之后仍然认不出表头时给 ``None``。
    """
    from app.bankstatement import semantics

    grid = _ocr_grid(db, data)
    if grid is None:
        return None
    try:
        parsed = semantics.parse_grid(grid)
    except Exception:  # noqa: BLE001 —— 兜底这条路崩了也不能带倒主路径
        return None
    if parsed.header_row < 0 and not parsed.rows:
        return None
    parsed.via_ocr = True
    return parsed


#: 合并时逐行比对的字段。
#:
#: 方向、金额、余额、对方账号这四个**能自校验**（方向是 IN/OUT、金额与
#: 账号是数字串）—— 认错了当场能看出来。对方户名、摘要、用途是**自由
#: 文本**，没法自校验，所以从 OCR 补它们时要额外设一道闸：见
#: :data:`_FREE_TEXT_FIELDS`。
_MERGE_FIELDS = (
    "direction", "amount", "balance",
    "counterparty_account", "counterparty_name", "summary", "purpose",
)

#: 自由文本字段：从 OCR 补值时，**纯数字**的一律不收。
#:
#: **为什么**：OCR 认歪列时，错位的那一格通常是**数字**（金额、余额、
#: 账号）。光大那份实测就多识别出一个空格子，金额列整体右移一位 ——
#: 「对方名称」那一栏落进来的是一串余额数字。自由文本没法自校验，
#: 于是只能按形态拒掉最可疑的那一类。
_FREE_TEXT_FIELDS = ("counterparty_name", "summary", "purpose")

#: 纯数字（含千分位、小数点、负号、空格）。
_NUMERIC_ONLY = re.compile(r"^[\d.,\s\-]+$")

#: 字段的中文名（诊断里要给人看）。
_FIELD_LABELS = {
    "direction": "收支方向",
    "amount": "金额",
    "balance": "余额",
    "counterparty_account": "对方账号",
    "counterparty_name": "对方户名",
    "summary": "摘要",
    "purpose": "用途",
}

#: meta 里参与合并的字段（文件自己报出来的那些）。
_META_FIELDS = (
    "account_no", "account_name", "bank_name", "currency",
    "period_start", "period_end", "closing_balance",
    "declared_income_count", "declared_income_total",
    "declared_expense_count", "declared_expense_total",
    "declared_total_count",
)


def _field_value(row: semantics.StatementRow, field: str) -> Any:
    """一个字段的「有效值」—— 没有值时返回 ``None``。

    方向认不出时是 ``"UNKNOWN"``，那**不算**有值。
    """
    value = getattr(row, field)
    if field == "direction":
        return value if value not in (None, "", "UNKNOWN") else None
    if field in ("amount", "balance"):
        return value if value is not None else None
    text = str(value or "").strip()
    return text or None


def _meta_value(meta: semantics.StatementMeta, field: str) -> Any:
    value = getattr(meta, field)
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _merge_parsed(base: semantics.ParsedStatement,
                  other: semantics.ParsedStatement
                  ) -> tuple[semantics.ParsedStatement, bool]:
    """两路解析**逐行合并**：每行从「有那个字段」的那一路取值。

    **什么时候需要**：Linux 上文字层会**丢单字对象**（光大那份的「借/贷」
    是两个单字，pdfium 整个不进文字层），于是方向认不出；而 OCR 那一路把
    金额列认歪了（多识别出一个空格子，金额列整体右移一位），于是金额
    认不出。**两边各有一半**，合起来才是一行完整的流水。

    **对齐要求**：行数相同、**日期序列相同**。对不上就**不合并** —— 合并错
    行比缺一个方向更糟（那是把甲流水的金额记到乙头上）。

    **谁说了算：自检。** 合并有两种口径 ——「文字层优先」（精确取字，
    但 Linux 上可能被截断）与「OCR 优先」（识别，但可能把列认歪）。
    两种各建一份，**各自跑一遍自检，谁的诊断更少谁赢**（一样多时取
    文字层优先）。

    裁判当得起这个位子：自检拿的是**文件自己报的数字**（笔数、收支
    合计），那是外部的、不受我们影响的基准。实测：光大那份的 OCR 金额列
    整体错位一位，于是「OCR 优先」那份合并结果的合计与文件自称的对不上，
    而「文字层优先」那份分毫不差 —— 裁判选中的正是对的那一个。

    **返回值**：``(结果, 有没有真的合并)``。合并了才需要重跑自检。
    """
    if len(base.rows) != len(other.rows) or not base.rows:
        return base, False
    base_dates = [str(row.tx_date) for row in base.rows]
    other_dates = [str(row.tx_date) for row in other.rows]
    if base_dates != other_dates:
        return base, False

    best: Optional[tuple[int, semantics.ParsedStatement, bool]] = None
    for prefer_ocr in (False, True):
        candidate = copy.deepcopy(base)
        counterpart = copy.deepcopy(other)
        changed = _apply_merge(candidate, counterpart, prefer_ocr=prefer_ocr)
        semantics.self_check(candidate)
        # 只数**自检**的结论：解析期的结论（表头认不出）两份一样，
        # 拿来比较没有意义。
        score = candidate.check_notes_count
        if best is None or score < best[0]:
            best = (score, candidate, changed)
    assert best is not None
    return best[1], best[2]


def _apply_merge(base: semantics.ParsedStatement,
                 other: semantics.ParsedStatement,
                 *, prefer_ocr: bool) -> bool:
    """把另一路的值并进 ``base``（**原地改**）。返回「有没有真的补上东西」。

    ``prefer_ocr`` 决定同一字段上**谁优先**：``False`` = 文字层优先，
    ``True`` = OCR 优先。优先的那一路没有值时，另一路补上。

    **两路认得不一样时记一笔**：不是静默地挑一个 —— 差异要能被人看见
    （:attr:`ParsedStatement.merge_conflicts`）。但**不因此判这份解析不可信**：
    哪一份对，由自检说了算（见 :func:`_merge_parsed`）。
    """
    if base.via_ocr:
        text, ocr = other, base
    else:
        text, ocr = base, other
    primary, secondary = (ocr, text) if prefer_ocr else (text, ocr)

    conflicts: list[str] = []
    changed = False
    for index in range(len(base.rows)):
        primary_row = primary.rows[index]
        secondary_row = secondary.rows[index]
        target = base.rows[index]
        for field in _MERGE_FIELDS:
            tvalue = _field_value(text.rows[index], field)
            ovalue = _field_value(ocr.rows[index], field)
            if tvalue is not None and ovalue is not None and tvalue != ovalue:
                conflicts.append(
                    f"第 {index + 1} 行的{_FIELD_LABELS[field]}两路认得不一样"
                    f"（文字层 {tvalue}、OCR {ovalue}）")
            pvalue = _field_value(primary_row, field)
            svalue = _field_value(secondary_row, field)
            chosen = pvalue if pvalue is not None else svalue
            if chosen is None:
                continue
            # 自由文本字段**不收纯数字**：OCR 认歪列时，错位的那一格通常
            # 是数字（金额、余额、账号）。光大那份实测：对方名称那一栏
            # 落进来的是一串余额数字。
            from_ocr = chosen == ovalue and tvalue != ovalue
            if (from_ocr and field in _FREE_TEXT_FIELDS
                    and _NUMERIC_ONLY.match(str(chosen))):
                fallback = tvalue if tvalue is not None else None
                if fallback is None:
                    conflicts.append(
                        f"第 {index + 1} 行的{_FIELD_LABELS[field]}只在一路里有，"
                        f"而它是「{chosen}」——像串错列的数字，已略过")
                    continue
                chosen = fallback
                from_ocr = False
            if _field_value(target, field) != chosen:
                changed = True
            setattr(target, field, chosen)
            if field == "direction" and from_ocr:
                # 方向是从 OCR 认出来的 —— 依据要跟着换，否则界面上写的
                # 依据是文字层那一份（那一路根本没认出方向）。
                target.extra["direction_basis"] = "OCR 识别"

        # 行上的**问题清单**要跟着合并结果走：方向补齐了，「方向未知」这条
        # 就不能留着 —— 它会让这一行被标成「先定方向」，既不能制证也不能
        # 跟发票匹配（:_store_rows 就是按它判的）。
        target.problems = [problem for problem in target.problems
                           if problem not in ("方向未知", "没有金额", "金额为 0")]
        if target.amount is None:
            target.problems.append("没有金额")
        elif target.amount <= 0:
            target.problems.append("金额为 0")
        if target.direction == "UNKNOWN":
            target.problems.append("方向未知")

        # extra 里的零散字段（交易类型、记账日…）也互补：文字层的优先。
        for key, value in (ocr.rows[index].extra or {}).items():
            if key == "direction_basis":
                continue
            if not (target.extra or {}).get(key) and value:
                target.extra[key] = value
                changed = True

    # meta 也合并：文件自己报的数字是**判据**（自检拿它跟解析结果对账），
    # 缺了它，自检就少了一条能发现漏行的通路。
    merged_meta = semantics.StatementMeta()
    for field in _META_FIELDS:
        tvalue = _meta_value(text.meta, field)
        ovalue = _meta_value(ocr.meta, field)
        if tvalue is not None:
            setattr(merged_meta, field, tvalue)
            if ovalue is not None and ovalue != tvalue:
                conflicts.append(
                    f"文件自报的「{field}」两路认得不一样"
                    f"（文字层 {tvalue}、OCR {ovalue}）")
        elif ovalue is not None:
            setattr(merged_meta, field, ovalue)
            changed = True
    base.meta = merged_meta

    base.merge_conflicts = conflicts
    if changed:
        base.via_merge = True
    return changed

def _parse_pdf(db: Session, data: bytes) -> semantics.ParsedStatement:
    """解析一份 PDF 对账单 —— **两路解析 + 严格自检**。

    **为什么要有两路。** 文字层（按坐标还原）是本地的、精确取字、通常也更快；
    但它依赖 pdfium 算出来的**字形框**，而**同一份 pdfium 源码在不同平台上
    编出来的行为不一样**：2026-10-27 实测，Linux 上 CID 字体的字形框高度
    退化成 0，整张表的表头认不出来，于是**静默**退回「纯文本流切分」那一层，
    产出「18050.00」被截成「1805」、合计对不上、方向认不出这种结果 ——
    **而且不报错**。OCR 那一路没有这个问题（它看的是渲染出来的图像），
    实测同一份文件，OCR 给出的结果与金标准逐项吻合。

    **为什么 OCR 是「第二意见」而不是首选。** 文字层是精确取字（不猜），
    不花钱、不外发数据；OCR 是识别（会认错字），慢一些，正文要过一遍外部
    服务。OCR 的额度每天两万次、实际一个月用不到十次，所以「再问一次」的
    成本可以忽略 —— 但文字层已经自检通过时，没必要多走一趟。

    **严格自检。** 一份解析结果「可信」的判据是**它自己的诊断为空**：文件
    自称的笔数与收支合计对得上、没有认不出方向的行、余额接得上、至少解析出
    一行。诊断非空 = 某处对不上，**这时候不采信它**，换另一路；两路都不干净，
    就取**更完整**的那一份（字段齐全优先），并把诊断带出去（台账状态是
    「部分解析」，而制证那一关本来就会把认不出的行挡下来，不会让它变成凭证）。
    """
    from app.bankstatement import pdf_table

    text_parsed: Optional[semantics.ParsedStatement] = None
    has_text_layer = pdf_table.has_text_layer(data)
    if has_text_layer:
        try:
            text_parsed = semantics.parse_statement(data)
        except ValueError:
            # 文字层认不出表头 —— 不是终局，换 OCR 再问一次。
            text_parsed = None
        if text_parsed is not None and not text_parsed.diagnosis:
            return text_parsed

    ocr_parsed = _ocr_parse(db, data)
    if ocr_parsed is not None and not ocr_parsed.diagnosis:
        # OCR 这一路自检全过 —— 比不过它就别用文字层那份。
        return ocr_parsed

    # 两路都不干净（或只有一路）：取**更完整**的那一份，再逐行合并。
    if text_parsed is not None and ocr_parsed is not None:
        base = text_parsed if _completeness(text_parsed) >= _completeness(ocr_parsed) \
            else ocr_parsed
        other = ocr_parsed if base is text_parsed else text_parsed
        merged, changed = _merge_parsed(base, other)
        if changed:
            # 合并补上的字段可能让自检的结论变了样（方向补齐了，
            # 「有 N 行认不出收支方向」这条就该消失）—— **重跑一遍自检**。
            #
            # 这一步曾经是**空转**的：合并是原地改 ``base`` 并把它返回，
            # 于是「合并过没有」永远判不出来，自检从来没重跑过 ——
            # 诊断里一直挂着一条已经不成立的抱怨。
            semantics.self_check(merged)
        return merged
    if text_parsed is not None:
        return text_parsed
    if ocr_parsed is not None:
        return ocr_parsed

    # 报错要说清**卡在哪一步**：是压根没有文字层（扫描件），还是文字层
    # 在但表头认不出来 —— 这两件事用户要做的下一步不一样。
    reason = "文字层认不出表头" if has_text_layer else "没有文本层"
    raise ValueError(
        f"这份 PDF 解析不出来：{reason}，OCR 兜底也没成功"
        "（系统没有配置 OCR 服务，或 OCR 服务没认出表头）。"
        "可以把对账单导出成 Excel 再上传。")


def _completeness(parsed: semantics.ParsedStatement) -> int:
    """一份解析结果「有多完整」—— 用来在两路之间取舍。

    **字段齐全的行最值钱**（金额与方向都有，用户确认一下就能用）；
    其次是只有金额的（方向可以人工指定，比金额容易填）；
    再次是只有方向的；什么都没有的行最没用。
    """
    score = 0
    for row in parsed.rows:
        has_amount = row.amount is not None
        has_direction = row.direction != "UNKNOWN"
        if has_amount and has_direction:
            score += 4
        elif has_amount:
            score += 2
        elif has_direction:
            score += 1
    return score


def _parse_with_profile(db: Session, book_id: int, data: bytes,
                        ) -> tuple[semantics.ParsedStatement, Optional[BankStatementProfile]]:
    """解析一份文件，顺带套用**记住的格式画像**。

    顺序很要紧：先自动解析一遍拿到**指纹**（指纹只跟容器与表头有关，
    跟列映射认得准不准无关），再用指纹去找画像。找到就把那份人工指定的
    映射**重新套一遍**。

    为什么不用画像替代自动解析：自动解析认得出来的时候，它比人工指定更
    准（别名表会跟着银行改版更新）；画像只在自动解析**认不出来**的时候
    才是权威。所以这里先自动、再用画像覆盖 —— 画像里的映射会**整体替换**
    自动映射，不做逐字段合并（半自动半手工的结果没人能解释）。
    """
    container = formats.sniff(data)
    if container == formats.CONTAINER_PDF:
        parsed = _parse_pdf(db, data)
    else:
        parsed = semantics.parse_statement(data)
    profile = db.scalar(select(BankStatementProfile).where(
        BankStatementProfile.book_id == book_id,
        BankStatementProfile.fingerprint == parsed.fingerprint,
    ))
    if profile is not None and profile.column_mapping:
        mapping = {str(k): int(v) for k, v in profile.column_mapping.items()}
        if container == formats.CONTAINER_PDF:
            parsed = semantics.parse_grid(
                _pdf_grid(db, data), column_mapping=mapping)
        else:
            parsed = semantics.parse_statement(data, column_mapping=mapping)
        # 指纹是**表头**算出来的，重解析不会改它；但重解析可能换了网格
        # （多 sheet 时选中的那个），所以重新取一次，保证两处一致。
        parsed.fingerprint = profile.fingerprint
        profile.hit_count = (profile.hit_count or 0) + 1
    return parsed, profile


def _must_keep(row: BankStatementRow) -> bool:
    """这一行身上有没有**重建就丢不得**的东西。

    有三样：已经生成过凭证（``matched_voucher_id``）、已经被发票占用
    （``matched_invoice_record_id``）、用户点名不算（``IGNORED``）。

    重建行会把这三样冲掉 —— 凭证还在账上，那几行却变成了「来路不明」，
    人也就再也查不到那笔钱从哪来。
    """
    return (row.matched_voucher_id is not None
            or row.matched_invoice_record_id is not None
            or row.status == STATUS_IGNORED)


def _match_old_rows(keep_rows: list[BankStatementRow],
                    new_rows: list[Any]) -> Optional[dict[int, BankStatementRow]]:
    """旧行 → 新行（按 seq 对回去）。

    **按「日期 + 金额 + 方向」三项匹配**：这三样合起来在一份对账单里
    基本唯一。**匹配不唯一或者匹配不上，就返回 ``None``** ——
    宁可不做，也不能把凭证挂到别的行上：那比缺一个方向糟得多
    （那是把甲流水的金额记到乙头上）。

    返回 ``{新行的 seq: 旧行}``。
    """
    if not keep_rows:
        return {}
    index: dict[tuple[Any, ...], list[int]] = {}
    for row in new_rows:
        key = (row.tx_date, row.amount, row.direction)
        index.setdefault(key, []).append(row.seq)
    matched: dict[int, BankStatementRow] = {}
    used: set[int] = set()
    for old in keep_rows:
        key = (old.tx_date, old.amount, old.direction)
        candidates = [seq for seq in index.get(key, []) if seq not in used]
        if len(candidates) != 1:
            return None
        used.add(candidates[0])
        matched[candidates[0]] = old
    return matched


def _store_rows(db: Session, statement: BankStatement,
                parsed: semantics.ParsedStatement,
                rules: list[BankStatementRule],
                carry: Optional[dict[int, BankStatementRow]] = None
                ) -> None:
    """把解析出来的行写进库里。

    **行不丢。** 方向认不出来的行照样入库（标 ``problems``）——
    丢掉的话用户永远不知道自己漏了一笔钱，而「方向未知」在界面上
    一眼就能看见、人工一点就定好了。

    **``carry`` 是「重建行时对不回去的东西」**（重新解析用）：已经生成过
    凭证 / 已被发票占用 / 被用户忽略的行，把那些状态原样搬过去。
    """
    # **旧行的对方科目要先读出来**：下面那句 DELETE 会把子表里的行
    # 级联删掉 —— 晚一步读就全没了，而那些是用户手工指定的科目与金额。
    old_children = _children_of(db, [row.id for row in (carry or {}).values()])
    db.execute(delete(BankStatementRow).where(
        BankStatementRow.statement_id == statement.id))
    pending: list[tuple[BankStatementRow, list[BankStatementRowSubject]]] = []
    for row in parsed.rows:
        subject_id, plan_status = _rule_subject(rules, row)
        status = STATUS_OPEN
        if row.problems and any("方向未知" in p for p in row.problems):
            # 方向都认不出来，这一行既不能制证也不能被发票匹配 ——
            # 但它**必须在库里**：用户看到「有 1 行认不出方向」才知道
            # 有笔钱没对上。标成 NONE（不是 PENDING）是为了让界面把它
            # 归到「先定方向」而不是「先定科目」那两类里。
            plan_status = PLAN_NONE
        old = (carry or {}).get(row.seq)
        children: list[BankStatementRowSubject] = []
        if old is not None:
            # 搬回去：凭证、发票占用、忽略状态、对方科目（连金额一起搬）。
            status = old.status
            plan_status = old.plan_status
            children = list(old_children.get(old.id) or [])
        elif subject_id and row.amount is not None and row.amount > 0:
            # 规则命中的行：一条子表行，金额就是这一行的流水金额。
            #
            # **金额为 0 / 未知的行不给建子表行** —— 子表有 ``amount > 0``
            # 的 CHECK，而 0 金额的行压根进不了制证（库层
            # ``ck_entry_amount_nonzero`` 也会挡）。这种行标成 NONE：
            # 它等的是「金额」，不是「对方科目」。
            children = [BankStatementRowSubject(
                subject_id=subject_id, amount=row.amount,
                source=PLAN_RULE, sort_no=1)]
        elif subject_id:
            plan_status = PLAN_NONE
        new_row = BankStatementRow(
            statement_id=statement.id,
            book_id=statement.book_id,
            seq=row.seq,
            tx_date=row.tx_date,
            tx_time=row.tx_time,
            direction=row.direction,
            amount=row.amount,
            balance=row.balance,
            summary=row.summary,
            purpose=row.purpose,
            counterparty_name=row.counterparty_name,
            counterparty_account=row.counterparty_account,
            extra=dict(row.extra or {}),
            status=status,
            matched_voucher_id=(old.matched_voucher_id if old is not None else None),
            matched_invoice_record_id=(
                old.matched_invoice_record_id if old is not None else None),
            plan_status=plan_status,
            problems=list(row.problems or []),
        )
        db.add(new_row)
        pending.append((new_row, children))
    db.flush()
    # 子表行要等父行拿到 id 才建得出来。
    for new_row, children in pending:
        for index, child in enumerate(children, start=1):
            new_row.plan_subjects.append(BankStatementRowSubject(
                subject_id=child.subject_id, amount=child.amount,
                source=child.source, sort_no=index))
    db.flush()


def upload_statement(db: Session, book_id: int, user: SysUser, filename: str,
                     data: bytes, account_subject_id: int) -> dict[str, Any]:
    """上传一份对账单：落原件、解析、落库。"""
    subject = cash_subject(db, book_id, account_subject_id)
    if not data:
        raise ValueError("上传的文件是空的")
    container = formats.sniff(data)
    try:
        parsed, _profile = _parse_with_profile(db, book_id, data)
    except ValueError as exc:
        # **原件先落盘，再报错。**
        #
        # 扫描件对账单认不出来时，用户至少还能把原件下载下来手工处理 ——
        # 而「原件没了」是最没用的报错。台账里留一条 ``parse_status=FAILED``
        # 的记录，界面上看得见「这份没解析出来」，也能下载原件。
        _record_failed_statement(db, book_id, subject, filename, data,
                                  container, str(exc))
        raise StatementParseError(
            f"{exc}（原件已保留，可在对账单列表里下载后手工处理）") from exc
    if len(parsed.rows) > MAX_STATEMENT_ROWS:
        raise ValueError(
            f"这份对账单有 {len(parsed.rows)} 行，超过上限 {MAX_STATEMENT_ROWS} 行。"
            "请确认传的是对账单而不是明细流水")

    period_start, period_end = _effective_period(parsed)
    _guard_duplicate(db, book_id, account_subject_id, period_start, period_end)

    relative = storage.store(book_id, container, data)
    statement = BankStatement(
        book_id=book_id,
        account_subject_id=subject.id,
        account_subject_code=subject.code,
        filename=filename or "对账单",
        file_path=relative,
        container_format=container,
        bank_name=parsed.meta.bank_name,
        account_no=parsed.meta.account_no,
        account_name=parsed.meta.account_name,
        period_start=period_start,
        period_end=period_end,
        opening_balance=_opening_balance(parsed),
        closing_balance=_dec(parsed.meta.closing_balance),
        income_total=parsed.income_total,
        expense_total=parsed.expense_total,
        row_count=len(parsed.rows),
        parse_status=(PARSE_PARTIAL if parsed.diagnosis else PARSE_OK),
        parse_message=_parse_message(parsed),
        diagnosis=_diagnosis_of(parsed),
        uploaded_by=getattr(user, "username", "") or "",
    )
    db.add(statement)
    db.flush()
    rules = list_rules(db, book_id)
    _store_rows(db, statement, parsed, rules)
    db.flush()
    return serialize_statement(db, statement)


def _record_failed_statement(db: Session, book_id: int, subject: Subject,
                             filename: str, data: bytes, container: str,
                             message: str) -> None:
    """解析失败也要留一条记录（含原件）。

    **为什么失败也要留**：用户传了文件却什么都没发生，最怕的是「我明明
    传上去了」。台账里那条红色记录 + 一个能下载原件的入口，是用户唯一
    能确认「文件确实到了系统里」的地方。
    """
    relative = storage.store(book_id, container, data)
    statement = BankStatement(
        book_id=book_id,
        account_subject_id=subject.id,
        account_subject_code=subject.code,
        filename=filename or "对账单",
        file_path=relative,
        container_format=container,
        parse_status=PARSE_FAILED,
        parse_message=message,
        diagnosis={"notes": [message]},
        uploaded_by="",
    )
    db.add(statement)
    db.flush()


def _guard_duplicate(db: Session, book_id: int, account_subject_id: int,
                     period_start: Optional[date], period_end: Optional[date],
                     *, exclude_id: Optional[int] = None) -> None:
    """同一账户 + 同一期间，只能有一份对账单。

    这道拦截是**硬拦**：同一份流水传两遍会生成两遍凭证，钱在账上多记一次，
    而用户界面上看着「我明明只传了一次」。

    库里有唯一约束（``uq_bank_statement_book_subject_period``），但
    PostgreSQL 把 NULL 视作互不相等 —— 解析不出期间时约束不拦。
    所以这里再显式查一次，并且**给出人话**：告诉用户是哪一份重复了。

    ``exclude_id`` 是**它自己**（重新解析时）：重解析的那一份当然「已经
    有一份」—— 不排掉它的话，重新解析永远会被自己挡住。
    """
    if period_start is None or period_end is None:
        # 没有期间就对不了「同一份」：这时至少拦住「同一个文件再来一次」。
        return
    stmt = select(BankStatement).where(
        BankStatement.book_id == book_id,
        BankStatement.account_subject_id == account_subject_id,
        BankStatement.period_start == period_start,
        BankStatement.period_end == period_end,
    )
    if exclude_id is not None:
        stmt = stmt.where(BankStatement.id != int(exclude_id))
    existing = db.scalar(stmt)
    if existing is not None:
        raise ValueError(
            f"这个账户在 {period_start} ~ {period_end} 期间已经有一份对账单了"
            f"（{existing.filename}，{existing.row_count} 行）。"
            "同一份流水不能传两遍 —— 会记两遍账。")


def reparse_statement(db: Session, book_id: int, statement_id: int, *,
                      column_mapping: dict[str, int] | None = None,
                      header_row: int | None = None) -> dict[str, Any]:
    """重新解析一份对账单。

    两条路：

    - ``column_mapping`` 给了 → 按用户手工指定的列重跑；
    - 没给 → 重读原件自动重跑（解析器改了规则之后用）。

    **重跑会重建全部行。** 已经生成过凭证的行带着 ``matched_voucher_id``，
    重建就没了 —— 账上那几张凭证就变成了「来路不明」。所以那些行要**对回去**：
    按「日期 + 金额 + 方向」三项唯一匹配（见 :func:`_match_old_rows`），
    把「已生成凭证 / 已被发票占用 / 被用户忽略」这些状态原样搬回新行。

    **对不回去就整次拒绝** —— 宁可不做，也不能把凭证挂到别的行上。
    （2026-10-27 之前这里是「一律拒绝」：用户有一份对账单，9 行已经生成过
    凭证，于是连「刷新一下期末余额」都做不了 —— 而他要的正是这个。）
    """
    statement = _statement(db, book_id, statement_id)
    old_rows = db.scalars(select(BankStatementRow).where(
        BankStatementRow.statement_id == statement.id)).all()
    keep = [row for row in old_rows if _must_keep(row)]
    if not storage.exists(statement.file_path):
        raise ValueError("原件已不在磁盘上，无法重新解析")

    data = storage.read(statement.file_path)
    if formats.sniff(data) == formats.CONTAINER_PDF:
        parsed = semantics.parse_grid(
            _pdf_grid(db, data), column_mapping=column_mapping,
            header_row=header_row)
    else:
        parsed = semantics.parse_statement(
            data, column_mapping=column_mapping, header_row=header_row)
    # **把「重建就丢不得」的行对回去。** 重建行会冲掉「已生成凭证 /
    # 已被发票占用 / 被用户忽略」这些标记 —— 凭证还在账上，那几行却变成
    # 了「来路不明」。对回去的依据是「日期 + 金额 + 方向」三项唯一匹配；
    # **对不上就整次拒绝**，不硬挂（挂错了等于把甲流水的金额记到乙头上）。
    carry = _match_old_rows(keep, parsed.rows)
    if carry is None:
        raise ValueError(
            "重新解析之后，账上已经生成过凭证（或已被发票占用、或被你点名"
            "忽略）的那些流水对不回去了 —— 新解析出来的行里找不到同一天、"
            "同金额、同方向的那一行。不能硬挂：挂错了等于把甲流水的金额"
            "记到乙头上。请把这份对账单删掉重新上传（原文件还在），"
            "或者先把对应的凭证删掉。")
    period_start, period_end = _effective_period(parsed)
    # 排掉它自己：重解析的那一份当然「已经有一份」。
    _guard_duplicate(db, book_id, statement.account_subject_id,
                     period_start, period_end, exclude_id=statement.id)

    statement.container_format = parsed.container
    statement.bank_name = parsed.meta.bank_name or statement.bank_name
    statement.account_no = parsed.meta.account_no or statement.account_no
    statement.account_name = parsed.meta.account_name or statement.account_name
    statement.period_start = period_start
    statement.period_end = period_end
    statement.closing_balance = _dec(parsed.meta.closing_balance)
    statement.income_total = parsed.income_total
    statement.expense_total = parsed.expense_total
    statement.row_count = len(parsed.rows)
    statement.parse_status = (PARSE_PARTIAL if parsed.diagnosis else PARSE_OK)
    statement.parse_message = _parse_message(parsed)
    statement.diagnosis = _diagnosis_of(parsed)
    rules = list_rules(db, book_id)
    _store_rows(db, statement, parsed, rules, carry=carry)
    db.flush()
    return serialize_statement(db, statement)


def delete_statement(db: Session, book_id: int, statement_id: int) -> dict[str, Any]:
    """删掉一份对账单。

    **已经有凭证的行不许删** —— 凭证在账上，对账单一删就查不到那笔钱从哪来。
    这里点名告诉用户哪几条挡住了。
    """
    statement = _statement(db, book_id, statement_id)
    blocked = db.scalars(select(BankStatementRow).where(
        BankStatementRow.statement_id == statement.id,
        BankStatementRow.matched_voucher_id.is_not(None),
    ).limit(5)).all()
    if blocked:
        names = "、".join(f"第 {row.seq} 行" for row in blocked)
        raise ValueError(
            f"这份对账单里的流水已经生成了凭证（{names}），不能删除。"
            "请先删掉对应的凭证。")
    storage.delete(statement.file_path)
    db.delete(statement)
    db.flush()
    return {"deleted": True, "id": statement_id}


# ----------------------------------------------------------------------
# 流水科目规则
# ----------------------------------------------------------------------

#: 规则拿哪一列去匹配关键词。
RULE_FIELDS = {
    "SUMMARY": "摘要",
    "PURPOSE": "用途",
    "CP_NAME": "对方户名",
}


def list_rules(db: Session, book_id: int) -> list[BankStatementRule]:
    return list(db.scalars(
        select(BankStatementRule)
        .where(BankStatementRule.book_id == book_id)
        .order_by(BankStatementRule.priority.desc(),
                  BankStatementRule.id.desc())
    ).all())


def _pseudo_row(row: BankStatementRow) -> semantics.StatementRow:
    """把库里的一行流水变成「规则匹配要的那种行」。

    规则匹配看的是摘要 / 用途 / 对方户名三列，而这三列在库里直接就是
    同名字段 —— 中间这一层转换写在一处，免得两处各写一遍、改了一处忘了
    另一处（那会让「规则命中了但制证没按规则走」这种错看起来像随机故障）。
    """
    return semantics.StatementRow(
        seq=row.seq,
        summary=row.summary,
        purpose=row.purpose,
        counterparty_name=row.counterparty_name,
    )


def _rule_hit(rules: Iterable[BankStatementRule],
              row: semantics.StatementRow) -> Optional[BankStatementRule]:
    """命中的那一条规则（没有命中就是 ``None``）。

    **同优先级时取更长的关键词**：「手续费」与「跨行转账手续费」优先级一样时，
    长的那个更具体。「跨行转账手续费」里也含「手续费」，但前者更可能是
    用户真正想表达的那个。这条规则让用户不必给每条规则都排优先级。
    """
    best: Optional[BankStatementRule] = None
    for rule in rules:
        if not rule.active:
            continue
        text = {
            "SUMMARY": row.summary,
            "PURPOSE": row.purpose,
            "CP_NAME": row.counterparty_name,
        }.get(rule.match_field, "")
        if not text or not rule.keyword:
            continue
        if rule.keyword not in text:
            continue
        if best is None:
            best = rule
            continue
        if rule.priority > best.priority:
            best = rule
        elif rule.priority == best.priority and len(rule.keyword) > len(best.keyword):
            best = rule
    return best


def _rule_subject(rules: Iterable[BankStatementRule],
                  row: semantics.StatementRow) -> tuple[Optional[int], str]:
    """这一行该记到哪个对方科目。

    命中规则 → ``(科目 id, RULE)``；没命中 → ``(None, PENDING)``。
    """
    best = _rule_hit(rules, row)
    if best is None:
        return None, PLAN_PENDING
    return best.subject_id, PLAN_RULE


def create_rule(db: Session, book_id: int, user: SysUser, payload: dict[str, Any]) -> dict:
    match_field = payload.get("match_field") or "SUMMARY"
    if match_field not in RULE_FIELDS:
        raise ValueError(f"匹配字段只能是 {'、'.join(RULE_FIELDS)}")
    keyword = _text(payload.get("keyword"))
    if not keyword:
        raise ValueError("关键词不能为空")
    subject_id = payload.get("subject_id")
    if subject_id is None:
        raise ValueError("请选择对方科目")
    subject = db.get(Subject, int(subject_id))
    if subject is None or subject.book_id != book_id:
        raise ValueError("选择的科目不属于本账套")
    if not subject.is_leaf:
        raise ValueError(f"「{subject.code} {subject.name}」不是末级科目")
    exists = db.scalar(select(BankStatementRule).where(
        BankStatementRule.book_id == book_id,
        BankStatementRule.match_field == match_field,
        BankStatementRule.keyword == keyword,
    ))
    if exists is not None:
        raise ValueError(f"「{RULE_FIELDS[match_field]}」里已经有「{keyword}」这条规则了")
    rule = BankStatementRule(
        book_id=book_id,
        match_field=match_field,
        keyword=keyword,
        subject_id=subject.id,
        priority=int(payload.get("priority") or 0),
        active=bool(payload.get("active", True)),
        summary_template=_summary_template_of(payload),
        created_by=getattr(user, "username", "") or "",
    )
    db.add(rule)
    db.flush()
    _replan_rules(db, book_id)
    return serialize_rule(db, rule)


def _summary_template_of(payload: dict[str, Any]) -> str:
    """取摘要模板并校验。

    **模板写错不许存**：一个不认识的占位符会让渲染退回内置口径，
    而那正是用户以为「我设了模板但没生效」时最难受的一种情况 ——
    与其存下来让人看不出来，不如当场说清「这个占位符不认识」。
    """
    template = _text(payload.get("summary_template"))
    if not template:
        return ""
    if len(template) > 256:
        raise ValueError("摘要模板太长了（最多 256 个字）")
    for field in re.findall(r"\{([^{}]*)\}", template):
        if field not in SUMMARY_TEMPLATE_FIELDS:
            raise ValueError(
                f"摘要模板里的「{{{field}}}」不是可用的占位符。"
                f"可用的是：{'、'.join('{' + f + '}' for f in SUMMARY_TEMPLATE_FIELDS)}")
    return template


def update_rule(db: Session, book_id: int, rule_id: int,
                payload: dict[str, Any]) -> dict:
    rule = db.get(BankStatementRule, rule_id)
    if rule is None or rule.book_id != book_id:
        raise ValueError("规则不存在")
    if "match_field" in payload and payload["match_field"]:
        if payload["match_field"] not in RULE_FIELDS:
            raise ValueError(f"匹配字段只能是 {'、'.join(RULE_FIELDS)}")
        rule.match_field = payload["match_field"]
    if "keyword" in payload and payload["keyword"]:
        rule.keyword = _text(payload["keyword"])
    if "subject_id" in payload and payload["subject_id"]:
        subject = db.get(Subject, int(payload["subject_id"]))
        if subject is None or subject.book_id != book_id:
            raise ValueError("选择的科目不属于本账套")
        if not subject.is_leaf:
            raise ValueError(f"「{subject.code} {subject.name}」不是末级科目")
        rule.subject_id = subject.id
    if "priority" in payload and payload["priority"] is not None:
        rule.priority = int(payload["priority"])
    if "active" in payload and payload["active"] is not None:
        rule.active = bool(payload["active"])
    if "summary_template" in payload:
        rule.summary_template = _summary_template_of(payload)
    db.flush()
    _replan_rules(db, book_id)
    return serialize_rule(db, rule)


def delete_rule(db: Session, book_id: int, rule_id: int) -> dict:
    rule = db.get(BankStatementRule, rule_id)
    if rule is None or rule.book_id != book_id:
        raise ValueError("规则不存在")
    db.delete(rule)
    db.flush()
    _replan_rules(db, book_id)
    return {"deleted": True, "id": rule_id}


def _replan_rules(db: Session, book_id: int) -> None:
    """规则变了，重算所有行的对方科目。

    只重算**还没定下来**的行：人工指定过的（``MANUAL``）不动 ——
    那是用户亲手挑的，不该被一条新规则覆盖掉。

    **已经生成过凭证的行也不重算** —— 凭证里写着那个科目，重算会把流水行
    改成别的科目，两边对不上且看不出来（与 :func:`update_row` 那道闸同一件事：
    已入账的对方科目由凭证定死）。规则改的是「还没入账的行该怎么记」，
    碰不到已经记好的账。

    规则命中给的是**一条**对方科目（金额就是这一行的流水金额）。
    一条规则只有一个科目 —— 规则没有金额，配不出「800 房租 + 200 押金」
    这种拆分来，而拆分的金额只有用户知道（凭发票、凭单据）。
    要拆就人工指定。
    """
    rules = list_rules(db, book_id)
    rows = db.scalars(select(BankStatementRow).where(
        BankStatementRow.book_id == book_id,
        BankStatementRow.plan_status != PLAN_MANUAL,
        BankStatementRow.matched_voucher_id.is_(None),
    )).all()
    children = _children_of(db, [row.id for row in rows])
    for row in rows:
        pseudo = _pseudo_row(row)
        subject_id, plan_status = _rule_subject(rules, pseudo)
        if row.direction == DIRECTION_UNKNOWN:
            # 方向都没定，「对方科目待确认」这句话是错的 —— 它等的是方向。
            plan_status = PLAN_NONE
        items: list[dict[str, Any]] = []
        if subject_id and row.amount is not None and row.amount > 0:
            items = [{"subject_id": subject_id, "amount": row.amount,
                      "source": PLAN_RULE}]
        elif subject_id:
            plan_status = PLAN_NONE
        _write_plan_subjects(db, row, items, PLAN_RULE,
                             existing=children.get(row.id) or [])
        row.plan_status = plan_status
    db.flush()


def serialize_rule(db: Session, rule: BankStatementRule) -> dict[str, Any]:
    subject = db.get(Subject, rule.subject_id)
    return {
        "id": rule.id,
        "match_field": rule.match_field,
        "match_label": RULE_FIELDS.get(rule.match_field, rule.match_field),
        "keyword": rule.keyword,
        "subject_id": rule.subject_id,
        "subject_code": subject.code if subject else "",
        "subject_name": subject.name if subject else "",
        "priority": rule.priority,
        "active": rule.active,
        "summary_template": rule.summary_template or "",
    }


# ----------------------------------------------------------------------
# 账套级配置（凭证摘要模板）
# ----------------------------------------------------------------------

def get_config(db: Session, book_id: int) -> Optional[BankStatementConfig]:
    """这个账套的流水配置。**没有就返回 ``None``** —— 那表示「还没配过」，
    不是「配了个空的」。"""
    return db.scalar(select(BankStatementConfig).where(
        BankStatementConfig.book_id == book_id))


def serialize_config(config: BankStatementConfig | None) -> dict[str, Any]:
    return {
        "book_id": config.book_id if config else 0,
        "summary_template": (config.summary_template if config else "") or "",
        "template_fields": list(SUMMARY_TEMPLATE_FIELDS),
    }


def save_config(db: Session, book_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """存这个账套的流水配置。**存过一次之后就用那一份**（不覆盖成空）。"""
    template = _summary_template_of(payload)
    config = get_config(db, book_id)
    if config is None:
        config = BankStatementConfig(book_id=book_id, summary_template=template)
        db.add(config)
    else:
        config.summary_template = template
    db.flush()
    return serialize_config(config)


# ----------------------------------------------------------------------
# 格式画像
# ----------------------------------------------------------------------

def list_profiles(db: Session, book_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(select(BankStatementProfile).where(
        BankStatementProfile.book_id == book_id
    ).order_by(BankStatementProfile.id)).all()
    return [{
        "id": row.id,
        "fingerprint": row.fingerprint,
        "column_mapping": row.column_mapping,
        "hit_count": row.hit_count,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    } for row in rows]


def save_profile(db: Session, book_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """记住一种格式的列映射。

    指纹 = 容器类型 + 归一化后的表头签名。同一账套内同一个指纹只留一条 ——
    银行的导出格式改了会换指纹，旧画像自然失效，不会被「更新」抹掉历史。
    """
    fingerprint = _text(payload.get("fingerprint"))
    if not fingerprint:
        raise ValueError("缺少格式指纹")
    mapping = payload.get("column_mapping") or {}
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("列映射不能为空")
    clean: dict[str, int] = {}
    for field, index in mapping.items():
        if not field:
            continue
        try:
            clean[str(field)] = int(index)
        except (TypeError, ValueError):
            raise ValueError("列映射里的列号必须是整数")
    profile = db.scalar(select(BankStatementProfile).where(
        BankStatementProfile.book_id == book_id,
        BankStatementProfile.fingerprint == fingerprint,
    ))
    if profile is None:
        profile = BankStatementProfile(
            book_id=book_id, fingerprint=fingerprint, column_mapping=clean)
        db.add(profile)
    else:
        profile.column_mapping = clean
    db.flush()
    return {
        "id": profile.id,
        "fingerprint": profile.fingerprint,
        "column_mapping": profile.column_mapping,
        "hit_count": profile.hit_count,
    }


def delete_profile(db: Session, book_id: int, profile_id: int) -> dict[str, Any]:
    profile = db.get(BankStatementProfile, profile_id)
    if profile is None or profile.book_id != book_id:
        raise ValueError("格式画像不存在")
    db.delete(profile)
    db.flush()
    return {"deleted": True, "id": profile_id}


# ----------------------------------------------------------------------
# 读取与序列化
# ----------------------------------------------------------------------

def _statement(db: Session, book_id: int, statement_id: int) -> BankStatement:
    statement = db.get(BankStatement, statement_id)
    if statement is None or statement.book_id != book_id:
        raise ValueError("对账单不存在")
    return statement


def _subjects_of(db: Session, book_id: int,
                 ids: Iterable[int]) -> dict[int, Subject]:
    wanted = {int(i) for i in ids if i}
    if not wanted:
        return {}
    rows = db.scalars(select(Subject).where(
        Subject.book_id == book_id, Subject.id.in_(wanted))).all()
    return {row.id: row for row in rows}


# ----------------------------------------------------------------------
# 对方科目（可能不止一个）
# ----------------------------------------------------------------------

def _children_of(db: Session,
                 row_ids: Iterable[int]) -> dict[int, list[BankStatementRowSubject]]:
    """一次读完若干行的对方科目，按行分组。

    **一次查询，不是一行一次。** 流水明细页一次要显示几百行，逐行去查
    对方科目就是几百次往返 —— 那是最容易把页面拖慢的一种写法，而这里的
    数据结构（行 → 若干条）天生就该一次读完。
    """
    ids = [int(i) for i in row_ids if i]
    if not ids:
        return {}
    rows = db.scalars(
        select(BankStatementRowSubject)
        .where(BankStatementRowSubject.row_id.in_(ids))
        .order_by(BankStatementRowSubject.row_id, BankStatementRowSubject.sort_no,
                  BankStatementRowSubject.id)
    ).all()
    grouped: dict[int, list[BankStatementRowSubject]] = {}
    for item in rows:
        grouped.setdefault(item.row_id, []).append(item)
    return grouped


def _plan_total(items: Iterable[BankStatementRowSubject]) -> Decimal:
    return sum((Decimal(str(item.amount)) for item in items), Decimal("0"))


def _serialize_plan_subjects(items: list[BankStatementRowSubject],
                             subjects: dict[int, Subject]) -> list[dict[str, Any]]:
    """对方科目列表 → 界面上要的形状。

    ``summary_override`` 是**这一条对方科目**的人工摘要（没有人工写就是
    空串）。界面上那 N 个摘要框靠它回填 —— 写过的要显示出来，
    没写过的显示规则算出来的那句（那是行级的 ``default_summary``）。
    """
    out: list[dict[str, Any]] = []
    for item in items:
        subject = subjects.get(item.subject_id)
        out.append({
            "subject_id": item.subject_id,
            "subject_code": subject.code if subject else "",
            "subject_name": subject.name if subject else "",
            "amount": str(item.amount),
            "source": item.source,
            "summary_override": item.summary_override or "",
        })
    return out


def _write_plan_subjects(db: Session, row: BankStatementRow,
                         items: list[dict[str, Any]], source: str,
                         existing: list[BankStatementRowSubject] | None = None
                         ) -> None:
    """把一行的对方科目整个换掉。

    **整体替换，不做增量合并。** 理由与接口层的 ``exclude_unset=True``
    同款：增量合并一旦漏了「删掉其中一条」这条路，就会出现「界面上删了、
    库里还在」的幽灵科目 —— 而它会被算进制证金额里。

    调用方负责校验（科目合法、金额为正、合计等于流水金额）。

    **唯一被「带过去」的是人工摘要 —— 而且只跟着同一个科目走。**
    请求体里没带 ``summary`` 的那一条，沿用**它原来那个科目**上的人工
    摘要；换了个科目（从 6602 改成 6601）就**不带走** —— 那是另一个科目
    的话，不该接着上一个人写的那句。

    **这不是一般的增量合并**：科目、金额、来源一律整体替换，只有
    「同一科目上那句人工写的摘要」跟着走。因为它是**这一笔**的结论，
    而结论不该因为界面上重开了一次弹窗就丢掉。

    **写完把关系属性 expire 掉**：会话里那份 ``row.plan_subjects`` 可能还
    留着刚删掉的旧行（它是被 ``db.delete`` 删的，不是从集合里摘掉的），
    谁再读它就会读到已经不在库里的行。expire 之后下次访问重新读库。
    """
    olds = list(existing) if existing is not None else list(row.plan_subjects)
    # **先记下旧的人工摘要，再删** —— 删完就查不到了。
    kept: dict[int, str] = {}
    for old in olds:
        text = (old.summary_override or "").strip()
        if text:
            kept[old.subject_id] = text
    for old in olds:
        db.delete(old)
    db.flush()
    for index, item in enumerate(items, start=1):
        subject_id = int(item["subject_id"])
        # ``summary`` **没给** = 沿用同一科目上原来那句（``None``）；
        # **给了空串** = 用户清空了它 = 交回规则去算。
        raw = item.get("summary")
        if raw is None:
            summary = kept.get(subject_id, "")
        else:
            summary = _text(raw)
        db.add(BankStatementRowSubject(
            row_id=row.id,
            subject_id=subject_id,
            amount=money(item["amount"]),
            source=item.get("source") or source,
            sort_no=index,
            summary_override=summary,
        ))
    db.flush()
    db.expire(row, ["plan_subjects"])


def _plan_status_of(items: list[Any],
                    direction: str) -> str:
    """这一行的对方科目状态。

    - 一条都没有 → 方向还没定就是 ``NONE``，方向定了就是 ``PENDING``；
    - 全是规则命中 → ``RULE``；
    - 只要有人工指定过 → ``MANUAL``（人工优先：用户亲手挑过的不许被规则盖掉）。

    ``items`` 可以是子表行，也可以是 ``{subject_id, amount, source}`` 那种
    字典 —— 两种形状在两个地方用（读的时候是行，写的时候是字典）。
    """
    if not items:
        return PLAN_NONE if direction == DIRECTION_UNKNOWN else PLAN_PENDING
    sources = [item.get("source") if isinstance(item, dict) else item.source
               for item in items]
    if PLAN_MANUAL in sources:
        return PLAN_MANUAL
    return PLAN_RULE


def serialize_statement(db: Session, statement: BankStatement,
                        engine: Any = None) -> dict[str, Any]:
    subject = db.get(Subject, statement.account_subject_id)
    reconcile = reconcile_balance(db, statement.book_id, statement.id, engine=engine)
    return {
        "id": statement.id,
        "book_id": statement.book_id,
        "account_subject_id": statement.account_subject_id,
        "account_subject_code": statement.account_subject_code,
        "account_subject_name": subject.name if subject else "",
        "filename": statement.filename,
        "container_format": statement.container_format,
        "container_label": formats.CONTAINER_LABELS.get(
            statement.container_format, statement.container_format),
        "bank_name": statement.bank_name,
        "account_no": statement.account_no,
        "account_name": statement.account_name,
        "period_start": statement.period_start.isoformat() if statement.period_start else "",
        "period_end": statement.period_end.isoformat() if statement.period_end else "",
        "opening_balance": str(statement.opening_balance) if statement.opening_balance is not None else "",
        "closing_balance": str(statement.closing_balance) if statement.closing_balance is not None else "",
        "income_total": str(statement.income_total) if statement.income_total is not None else "",
        "expense_total": str(statement.expense_total) if statement.expense_total is not None else "",
        "row_count": statement.row_count,
        "parse_status": statement.parse_status,
        "parse_message": statement.parse_message,
        "diagnosis": statement.diagnosis or {},
        "uploaded_by": statement.uploaded_by,
        "created_at": statement.created_at.isoformat() if statement.created_at else "",
        "reconcile": reconcile,
    }


def list_statements(db: Session, book_id: int) -> list[dict[str, Any]]:
    statements = db.scalars(select(BankStatement).where(
        BankStatement.book_id == book_id
    ).order_by(BankStatement.period_start.desc().nullslast(),
               BankStatement.id.desc())).all()
    if not statements:
        return []
    # **共用一个账簿引擎**：引擎把账套的期初与分录聚合缓存下来，
    # 一次算完整本账。列表页要给每份对账单算「账面余额」，
    # 一份建一个引擎等于把整本账重算 N 遍。
    from app.ledger.engine import LedgerEngine

    engine = LedgerEngine.for_ledger(db, book_id, include_unposted=True)
    result = []
    for statement in statements:
        item = serialize_statement(db, statement, engine=engine)
        counters = db.execute(
            select(BankStatementRow.status, func.count())
            .where(BankStatementRow.statement_id == statement.id)
            .group_by(BankStatementRow.status)).all()
        counts = {status: int(count) for status, count in counters}
        item["status_counts"] = {
            "OPEN": counts.get(STATUS_OPEN, 0),
            "POSTED": counts.get(STATUS_POSTED, 0),
            "IGNORED": counts.get(STATUS_IGNORED, 0),
        }
        pending = db.scalar(select(func.count()).select_from(BankStatementRow).where(
            BankStatementRow.statement_id == statement.id,
            BankStatementRow.plan_status == PLAN_PENDING,
            BankStatementRow.status == STATUS_OPEN,
        )) or 0
        item["pending_count"] = int(pending)
        result.append(item)
    return result


def list_rows(db: Session, book_id: int, statement_id: int, *,
              statement: BankStatement | None = None) -> list[dict[str, Any]]:
    statement = statement or _statement(db, book_id, statement_id)
    rows = db.scalars(select(BankStatementRow).where(
        BankStatementRow.statement_id == statement.id
    ).order_by(BankStatementRow.seq)).all()
    children = _children_of(db, [row.id for row in rows])
    subject_ids = {item.subject_id for items in children.values() for item in items}
    subjects = _subjects_of(db, book_id, subject_ids)
    # **一次查询出全部「可以改科目的分录」候选**（不是一行一次）。
    candidates = matching.reclassify_candidates(db, book_id, statement, rows)
    # 规则与账套配置**各读一次**：算每一行「按规则算出来的摘要」要用，
    # 一行一次查询就是几百次往返。
    rules, config = _summary_context(db, book_id)
    out: list[dict[str, Any]] = []
    for row in rows:
        items = children.get(row.id) or []
        out.append(serialize_row(
            db, row, subjects, items, candidates.get(row.id) or [],
            default_summary=_default_summary_for(row, items, rules, config)))
    return out


def serialize_row(db: Session, row: BankStatementRow,
                  subjects: dict[int, Subject] | None = None,
                  plan_subjects: list[BankStatementRowSubject] | None = None,
                  reclassify: list[dict[str, Any]] | None = None,
                  default_summary: str = "",
                  ) -> dict[str, Any]:
    subjects = subjects or {}
    items = list(row.plan_subjects) if plan_subjects is None else list(plan_subjects)
    plans: list[dict[str, Any]] = []
    for item in reclassify or []:
        reason = matching.reclassify_block_reason(item)
        plans.append({**item, "changeable": not reason, "block_reason": reason})
    return {        "id": row.id,
        "statement_id": row.statement_id,
        "seq": row.seq,
        "tx_date": row.tx_date.isoformat() if row.tx_date else "",
        "tx_time": row.tx_time,
        "direction": row.direction,
        "amount": str(row.amount) if row.amount is not None else "",
        "balance": str(row.balance) if row.balance is not None else "",
        "summary": row.summary,
        "purpose": row.purpose,
        "counterparty_name": row.counterparty_name,
        "counterparty_account": row.counterparty_account,
        "extra": row.extra or {},
        "status": row.status,
        "matched_voucher_id": row.matched_voucher_id,
        "matched_invoice_record_id": row.matched_invoice_record_id,
        #: 对方科目（可能不止一个）。每条带自己的金额。
        "plan_subjects": _serialize_plan_subjects(items, subjects),
        #: 对方科目金额合计。**制证时它必须等于流水金额。**
        "plan_subject_total": str(_plan_total(items)),
        "plan_status": row.plan_status,
        "problems": row.problems or [],
        "voucher_state": _voucher_state(row),
        #: **按规则算出来的那个摘要** —— 界面上给每一个对方科目的摘要框
        #: 当默认值，让用户看到「不改的话会是什么」，改了就用他写的那个。
        #: **它是行级的**：模板管的是「这一类流水通常怎么写摘要」，
        #: 而对方科目的那几句各自写在 ``plan_subjects`` 里。
        "default_summary": default_summary,
        #: 账上「可能与这一行是同一笔」的分录（记在别的现金科目上的）。
        #: 有候选 = 这笔钱可能已经入过账了，只是记错了账户。
        "reclassify_candidates": plans,
    }


def _voucher_state(row: BankStatementRow,
                   plan_subjects: list[BankStatementRowSubject] | None = None
                   ) -> str:
    """这一行现在能不能制证，为什么不能。

    界面上用**颜色 + 文字**把四种情况分开，因为它们要做的动作不同：
    定方向 / 定科目 / 已被发票占用 / 已经入账。
    """
    if row.status == STATUS_IGNORED:
        return "IGNORED"
    if row.status == STATUS_POSTED:
        return "POSTED"
    if row.matched_invoice_record_id:
        return "INVOICE"
    if row.direction == DIRECTION_UNKNOWN:
        return "UNKNOWN"
    if row.amount is None or row.amount <= 0:
        return "NOAMOUNT"
    if row.plan_status == PLAN_PENDING:
        return "PENDING"
    items = list(row.plan_subjects) if plan_subjects is None else list(plan_subjects)
    if row.plan_status in (PLAN_RULE, PLAN_MANUAL) and items:
        return "READY"
    return "NONE"


# ----------------------------------------------------------------------
# 逐行操作
# ----------------------------------------------------------------------

def _parse_plan_payload(db: Session, book_id: int, row: BankStatementRow,
                         payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把「对方科目」这一段请求体变成子表行。

    **两种写法**：

    - ``plan_subjects`` —— 列表，每项 ``{subject_id, amount}``。这是正规
      写法，一行流水可以拆到好几个科目上。
    - ``plan_subject_id`` —— 单个科目 id，金额 = 这一行的流水金额。
      这是「就一个科目」的简写，留着是因为它太常用了（界面上改一个科目
      不该被迫凑一个列表出来）。

    两种都给了要报错 —— 那说明调用方自己也没想清楚要哪个。

    **每一项还可以带自己的 ``summary``** —— 这一条对方科目的凭证摘要。
    **没带**（``None``）= 沿用这个科目上原来那句；**带了空串** = 用户清空了
    它 = 交回规则去算。这两者的区别不能糊掉：糊掉的话「清空摘要」与
    「没碰摘要框」会变成同一件事。
    """
    has_list = "plan_subjects" in payload and payload["plan_subjects"] is not None
    has_single = "plan_subject_id" in payload
    if has_list and has_single:
        raise ValueError("对方科目只能给一种：plan_subjects（列表）或 plan_subject_id（单个）")
    raw: Any = None
    if has_list:
        raw = payload.get("plan_subjects")
    elif has_single:
        single = payload.get("plan_subject_id")
        if single in (None, "", 0):
            raw = []
        else:
            raw = [{"subject_id": single, "amount": str(row.amount or "")}]
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("对方科目必须是一个列表")
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    total = Decimal("0")
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("对方科目的每一项都要有科目与金额")
        subject_id = entry.get("subject_id")
        if subject_id in (None, "", 0):
            raise ValueError("对方科目不能为空")
        try:
            subject = db.get(Subject, int(subject_id))
        except (TypeError, ValueError):
            raise ValueError("对方科目的 id 必须是整数")
        if subject is None or subject.book_id != book_id:
            raise ValueError("选择的科目不属于本账套")
        if not subject.is_leaf:
            raise ValueError(f"「{subject.code} {subject.name}」不是末级科目")
        if not subject.active:
            raise ValueError(f"「{subject.code} {subject.name}」已停用")
        if subject.id in seen:
            raise ValueError(
                f"「{subject.code} {subject.name}」在这一行里出现了两次 —— "
                "要记两笔请把金额合并到一条上")
        seen.add(subject.id)
        amount = _dec(entry.get("amount"))
        if amount is None or amount <= 0:
            raise ValueError(f"「{subject.code} {subject.name}」的金额必须大于 0")
        # 摘要：**没给就是 ``None``**（沿用原来那句），给空串就是「交回规则」。
        summary = entry.get("summary")
        if summary is not None and not isinstance(summary, str):
            raise ValueError("凭证摘要必须是字符串")
        items.append({"subject_id": subject.id, "amount": amount,
                      "source": PLAN_MANUAL, "summary": summary})
        total += amount
    # **合计必须等于流水金额** —— 凭证借贷不平，或者更糟：平了但钱记在
    # 错误的科目上。流水金额为 0 / 未知的行不校验（它压根进不了制证）。
    if items and row.amount is not None and row.amount > 0 and total != row.amount:
        raise ValueError(
            f"对方科目金额合计 {total} 与流水金额 {row.amount} 不一致 —— "
            "凭证会不平衡。请调整各科目的金额")
    return items


def update_row(db: Session, book_id: int, row_id: int,
               payload: dict[str, Any]) -> dict[str, Any]:
    """改一行：指定对方科目（可能好几个），或忽略 / 恢复这一行。

    **摘要跟着对方科目走**：每个对方科目可以带自己的 ``summary``，
    写在 ``plan_subjects`` 的每一项里（见 :func:`_parse_plan_payload`）。
    一行流水只有一个「整张凭证的摘要」那种写法已经被去掉了 ——
    原因见 :func:`_make_voucher_for_row`。
    """
    row = db.get(BankStatementRow, row_id)
    if row is None or row.book_id != book_id:
        raise ValueError("流水行不存在")
    # **旧的行级摘要写法，明确报错而不是当没看见。**
    #
    # 请求体在进到这里之前已经被 pydantic 按 schema 过滤过了
    # （``model_dump(exclude_unset=True)``），所以一个「不认识」的字段
    # 会被**静默丢掉** —— 界面上写了一句摘要、接口回 200、库里什么都没变。
    # 「写了却看不见，等于没写」。所以这里把它接住并说清楚该往哪儿写。
    if "summary" in payload and payload["summary"] is not None:
        raise ValueError(
            "凭证摘要现在按对方科目分别设置 —— 请把摘要写在 plan_subjects "
            "的每一项里（每个科目一句）。")
    if "plan_subjects" in payload or "plan_subject_id" in payload:
        # **已经生成过凭证的行，对方科目不许再改。**
        #
        # 凭证已经把那个科目定死了（分录里写着）。只改流水行、不改凭证，
        # 两边就对不上 —— 而且**看不出来**：界面上写着「对方科目 210101
        # 郑紫臻」，账上的凭证却是 210202，直到对账时才发现。改科目是**改
        # 凭证**的动作，不是改流水行的动作。
        #
        # 出路有两条，都在这一页上：改那张凭证本身；或用「可改科目」——
        # 它走 ``accounting.vouchers.update_voucher``，凭证与流水一起改。
        #
        # 这条闸与前端那一条**同一个判据**（``matched_voucher_id``）：
        # 两边各判一次的话，迟早会出现「界面能点、点了被拒」或反过来的
        # 那种最难查的毛病。
        #
        # **摘要也在这道闸里面**：摘要是那条分录上的字段，凭证已经写死了它，
        # 改流水行**不会**回头改凭证，两边就对不上（同一个道理）。
        if row.matched_voucher_id:
            raise ValueError(
                "这一行已经生成了凭证，对方科目由凭证定死 —— "
                "要改请改那张凭证（或用「可改科目」）。")
        items = _parse_plan_payload(db, book_id, row, payload)
        _write_plan_subjects(db, row, items, PLAN_MANUAL)
        row.plan_status = _plan_status_of(items, row.direction)
    if "status" in payload and payload["status"]:
        status = payload["status"]
        if status not in (STATUS_OPEN, STATUS_IGNORED):
            raise ValueError("状态只能是 OPEN 或 IGNORED")
        if row.matched_voucher_id:
            raise ValueError("这一行已经生成了凭证，不能忽略")
        row.status = status
    db.flush()
    # 返回时把「按规则算出来的那个摘要」也带上：界面上那几个摘要输入框
    # 要显示**当前**该用的摘要 —— 人工改了对方科目之后，规则级的模板可能
    # 不再适用（规则级模板只在「这条科目是规则定的」时候算数），
    # 默认值得跟着变。
    rules, config = _summary_context(db, book_id)
    return serialize_row(
        db, row,
        default_summary=_default_summary_for(
            row, list(row.plan_subjects), rules, config))


def set_row_direction(db: Session, book_id: int, row_id: int,
                      direction: str) -> dict[str, Any]:
    """人工指定一行的收支方向。

    只有**认不出方向**的行才需要这一步。指定之后，这一行才进入
    发票匹配与自动制证的候选池。
    """
    if direction not in (DIRECTION_IN, DIRECTION_OUT):
        raise ValueError("方向只能是 IN（收入）或 OUT（支出）")
    row = db.get(BankStatementRow, row_id)
    if row is None or row.book_id != book_id:
        raise ValueError("流水行不存在")
    if row.matched_voucher_id:
        raise ValueError("这一行已经生成了凭证，不能改方向")
    row.direction = direction
    row.problems = [p for p in (row.problems or []) if "方向未知" not in p]
    if row.amount is not None and row.amount > 0 and row.plan_status == PLAN_NONE:
        row.plan_status = PLAN_PENDING
    db.flush()
    return serialize_row(db, row)


# ----------------------------------------------------------------------
# 去重与制证
# ----------------------------------------------------------------------

def _voucher_matches(db: Session, book_id: int, subject_id: int,
                      amount: Decimal, direction: str,
                      when: Optional[date], window_days: int) -> list[tuple[int, date]]:
    """这个科目上有没有「可能就是这一笔」的凭证。

    判据四件套：**同一现金科目 + 同方向 + 金额相等 + 日期在 ±N 天内**。

    为什么要日期窗口：同一天同金额同科目的两笔款是常事（付两笔一样的
    运费），没有窗口就会把第二笔误判成重复，于是它永远进不了账。
    窗口取 7 天是因为发票与付款之间差几天是常态。

    **未记账的凭证也算** —— 用户往往先把凭证录进来再去核对，
    「已入账」不等于「已记账」。
    """
    if when is None:
        return []
    entry_direction = "DEBIT" if direction == DIRECTION_IN else "CREDIT"
    lo = when - timedelta(days=window_days)
    hi = when + timedelta(days=window_days)
    rows = db.execute(
        select(Voucher.id, Voucher.voucher_date)
        .join(Entry, Entry.voucher_id == Voucher.id)
        .where(
            Voucher.book_id == book_id,
            Voucher.status.in_(("POSTED", "UNPOSTED")),
            Entry.subject_id == subject_id,
            Entry.direction == entry_direction,
            Entry.amount == amount,
            Voucher.voucher_date >= lo,
            Voucher.voucher_date <= hi,
        )
        .distinct()
    ).all()
    return [(int(vid), vdate) for vid, vdate in rows]


def _voucher_covers_row(db: Session, book_id: int, statement: BankStatement,
                        row: BankStatementRow,
                        voucher_id: Optional[int]) -> bool:
    """这张凭证是不是还「覆盖」这一行流水。

    「覆盖」= 凭证上有一条分录同时满足三件事：**科目就是这份对账单绑定的
    账户**、**方向对得上**、**金额相等**。

    **为什么按科目编码比，不按 id**：科目表每个年度各有一份（2026 的 1002 与
    2027 的 1002 是两行不同的记录），跨年搬科目时 id 会变
    （见 :func:`_remap_subjects`）。按 id 比，跨年那一笔就认不出自己刚标过
    的那张凭证。

    **为什么不看日期**：日期窗口是「这一行对应哪张凭证」的**启发式** ——
    同一天同金额同科目的两笔款是常事（付两笔一样的运费），用窗口把两者
    分开。但用户已经通过「可改科目」**人工确认过**是哪一张凭证了，这时再拿
    启发式去推翻它，就会把人工确认的结果吞掉：跨期 16~30 天的候选一改完，
    那张凭证在 ±7 天的扫描里根本看不见，标记当场被收回，行状态从「已入账」
    退回「待确认科目」—— 用户明明刚确认过，看着却像没改成。

    所以回收标记之前先问一句「**你标的那张凭证还在不在、还对不对得上**」。
    凭证被删了、金额改了、现金侧又挪走了 —— 那才该收回。
    """
    if not voucher_id:
        return False
    voucher = db.get(Voucher, voucher_id)
    if voucher is None or voucher.book_id != book_id:
        return False
    if voucher.status not in ("POSTED", "UNPOSTED"):
        return False
    if row.amount is None or row.direction not in (DIRECTION_IN, DIRECTION_OUT):
        return False
    account = db.get(Subject, statement.account_subject_id)
    if account is None:
        return False
    # **银行账的方向与会计凭证相反**：流水 IN（收钱）= 现金增加 = 借方。
    entry_direction = "DEBIT" if row.direction == DIRECTION_IN else "CREDIT"
    hit = db.execute(
        select(Entry.id)
        .join(Subject, Subject.id == Entry.subject_id)
        .where(
            Entry.voucher_id == voucher.id,
            Subject.book_id == book_id,
            Subject.code == account.code,
            Entry.direction == entry_direction,
            Entry.amount == row.amount,
        )
        .limit(1)
    ).first()
    return hit is not None


def plan_vouchers(db: Session, book_id: int, statement_id: int,
                  *, window_days: int = DEFAULT_DEDUPE_WINDOW_DAYS) -> dict[str, Any]:
    """算一份对账单里哪些流水可以制证、哪些已经入过账。

    **顺带把「已入账」这件事落库**（``status=POSTED`` + ``matched_voucher_id``）：
    它是「不再制这张凭证」的依据，也是界面上「这一行已经对上账了」的标记。

    落库的同时也会**回收**过期的标记：用户把那张凭证删了之后，
    这一行应该重新变成候选。这里每次都重新扫一遍，所以删凭证再点一次
    「生成流水凭证」就能恢复。
    """
    statement = _statement(db, book_id, statement_id)
    rows = db.scalars(select(BankStatementRow).where(
        BankStatementRow.statement_id == statement.id
    ).order_by(BankStatementRow.seq)).all()
    children = _children_of(db, [row.id for row in rows])
    subject_ids = {item.subject_id for items in children.values() for item in items}
    subjects = _subjects_of(db, book_id, subject_ids)

    # 规则与账套配置各读一次（算「按规则算出来的摘要」要用）。
    rules, config = _summary_context(db, book_id)

    def _ser(row: BankStatementRow, items: list[BankStatementRowSubject]) -> dict[str, Any]:
        return serialize_row(
            db, row, subjects, items,
            default_summary=_default_summary_for(row, items, rules, config))

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        items = children.get(row.id) or []
        if row.status == STATUS_IGNORED:
            skipped.append({**_ser(row, items),
                            "reason": "已忽略"})
            continue
        # ---- 去重（已有凭证）----
        # **人工确认过的标记优先**：这一行已经标着「已入账」且指着某张凭证时，
        # 先核实那张凭证**是不是还覆盖这一行** —— 覆盖了就保留它，不让下面
        # 那次启发式扫描去顶掉用户的确认。
        #
        # 为什么要先核实：扫描靠日期窗口（±7 天）找「哪张凭证」，而用户点
        # 「可改科目」时已经**人工确认过**是哪一张了。跨期 16~30 天的候选
        # 尤其明显 —— 改完之后那张凭证在 ±7 天的扫描里根本看不见，标记当场
        # 被收回，行状态从「已入账」退回「待确认科目」。用户明明刚确认过，
        # 看着却像没改成。
        covered = bool(
            row.status == STATUS_POSTED and row.matched_voucher_id
            and _voucher_covers_row(db, book_id, statement, row,
                                    row.matched_voucher_id))
        if not covered:
            matched = _voucher_matches(
                db, book_id, statement.account_subject_id,
                row.amount or Decimal("0"), row.direction, row.tx_date, window_days)
            if matched:
                voucher_id, voucher_date = matched[0]
                row.status = STATUS_POSTED
                row.matched_voucher_id = voucher_id
            elif row.status == STATUS_POSTED:
                # 标记还在，但那张凭证已经对不上这一行了（多半是被删了，
                # 也可能金额改了、现金侧又挪走了）—— 收回标记，重新判一次。
                row.status = STATUS_OPEN
                row.matched_voucher_id = None
        if row.status == STATUS_POSTED:
            skipped.append({
                **_ser(row, items),
                "reason": f"已入账（凭证 #{row.matched_voucher_id}）",
            })
            continue
        if row.matched_invoice_record_id:
            skipped.append({**_ser(row, items),
                            "reason": "已被发票占用"})
            continue
        if row.direction == DIRECTION_UNKNOWN:
            skipped.append({**_ser(row, items),
                            "reason": "方向未知"})
            continue
        if row.amount is None or row.amount <= 0:
            skipped.append({**_ser(row, items),
                            "reason": "金额为 0"})
            continue
        if row.plan_status == PLAN_PENDING or not items:
            skipped.append({**_ser(row, items),
                            "reason": "对方科目待确认"})
            continue
        # **对方科目金额合计必须等于流水金额** —— 不等的话凭证会不平衡。
        # 正常情况下写入口就挡住了（:func:`_parse_plan_payload`），
        # 这里是第二道：金额改过了、行是旧的、或者别处写进去的。
        if _plan_total(items) != row.amount:
            skipped.append({**_ser(row, items),
                            "reason": "对方科目金额与流水金额对不上"})
            continue
        candidates.append(_ser(row, items))
    db.flush()
    return {
        "statement_id": statement.id,
        "account_subject_id": statement.account_subject_id,
        "candidates": candidates,
        "skipped": skipped,
        "candidate_count": len(candidates),
        "pending_count": len([s for s in skipped if s["reason"] == "对方科目待确认"]),
        "unknown_count": len([s for s in skipped if s["reason"] == "方向未知"]),
        "window_days": window_days,
    }


def _default_summary(row: BankStatementRow) -> str:
    """内置的凭证摘要口径：**用途优先，摘要兜底**（用户定的口径，2026-10-27）。

    **为什么**：对账单上「摘要」与「用途」两列装的不是一回事 ——
    **摘要是「怎么走的」，用途是「为什么」**。农商行那份就是：

    | 摘要 | 用途 |
    |---|---|
    | 转入 | 办公室租金 |
    | 支取 | 归还借款 |
    | 短信收费 | （空） |

    凭证摘要是给人看的：「办公室租金」比「转入」有用得多；而「转入」这种
    话在同一个月的几十张凭证里全都一样，翻凭证时认不出是哪笔。

    **没有用途的行照旧用摘要** —— 平安那份两列都有，摘要='快捷支付'、
    用途='支付宝-消费刘硕妮'，后者更具体；光大那份压根没有用途列，
    于是摘要照旧顶上。
    """
    if row.purpose:
        return row.purpose
    if row.summary:
        return row.summary
    who = row.counterparty_name or "对方"
    return f"银行{'收' if row.direction == DIRECTION_IN else '付'} {who}"


#: 摘要模板里能用的占位符。
#:
#: **刻意用中文**：模板是给记账的人写的，而他们看到的是对账单上的中文列名
#: （摘要 / 用途 / 对方户名）。占位符跟界面上看到的字对得上，才不用去查
#: 一份「变量表」。
SUMMARY_TEMPLATE_FIELDS = ("摘要", "用途", "对方户名", "日期", "金额", "方向", "序号")


class _TemplateValues(dict):
    """模板取值。**不认识的占位符直接报错** —— 那样才能退回内置口径，
    而不是悄悄渲染成半截文字。"""

    def __missing__(self, key):
        raise KeyError(key)


def render_summary(row: BankStatementRow, template: str = "") -> str:
    """按模板渲染这一行的凭证摘要。

    **模板写错就退回内置口径**（:func:`_default_summary`），不让整批制证
    失败 —— 一条流水的摘要模板写错了，不该让这一份对账单的其他流水都
    生成不出凭证。
    """
    text = _text(template)
    if text:
        values = _TemplateValues({
            "摘要": row.summary or "",
            "用途": row.purpose or "",
            "对方户名": row.counterparty_name or "",
            "日期": row.tx_date.isoformat() if row.tx_date else "",
            "金额": str(row.amount) if row.amount is not None else "",
            "方向": ("收入" if row.direction == DIRECTION_IN
                     else "支出" if row.direction == DIRECTION_OUT else ""),
            "序号": str(row.seq),
        })
        try:
            out = text.format_map(values).strip()
        except (KeyError, IndexError, ValueError):
            out = ""
        if out:
            return out
    return _default_summary(row)


def _subject_summary(item: BankStatementRowSubject, row: BankStatementRow,
                     template: str = "") -> str:
    """**这一条对方科目**的凭证摘要**最终用哪一句**。

    **人工写的优先**（``summary_override``），没写才按模板算。

    **为什么人工的优先，而不是模板的优先**：模板是**账套级 / 规则级**的
    口径，它管的是「这一类流水通常该怎么写摘要」；而人工写的那一句是
    **这一个科目**的 —— 「这笔记到 6602、摘要写『备用金』」是常见搭配，
    而同一笔里的另一个科目（押金、运费）该写的话不一样。用户既然明确
    写了，就该用他写的那句 —— 这也正是界面上那个输入框存在的意义。

    **留空 = 交回规则**：清空那个输入框 = 不想改 = 跟着规则走（不是
    「摘要留空」）。
    """
    override = (item.summary_override or "").strip()
    if override:
        return override
    return render_summary(row, template)


def _row_summary(row: BankStatementRow, template: str = "") -> str:
    return render_summary(row, template)


def _summary_context(db: Session, book_id: int) -> tuple[
        list[BankStatementRule], Optional[BankStatementConfig]]:
    """算摘要要用的两样东西：**规则**与**账套配置**。

    **各读一次，不是一行一次**：一份对账单几百行，一行一次查询就是几百次
    往返 —— 而这两样东西整份对账单是同一份。
    """
    return list_rules(db, book_id), get_config(db, book_id)


def _default_summary_for(row: BankStatementRow,
                          items: list[BankStatementRowSubject],
                          rules: list[BankStatementRule],
                          config: Optional[BankStatementConfig]) -> str:
    """这一行**按规则算出来的**凭证摘要。

    **人工没写摘要时制证用的就是它**；界面上它也是「摘要」输入框的默认值
    —— 让用户先看见「不改会是什么」，改了就用他写的那个。

    取值序（规则级 > 账套级 > 内置口径）与制证时**完全一致**：
    :func:`_summary_template_for` 是同一段代码，不另写一份。
    """
    return render_summary(row, _summary_template_for(rules, config, row, items))


def _summary_template_for(rules: list[BankStatementRule],
                           config: Optional[BankStatementConfig],
                           row: BankStatementRow,
                           items: list[BankStatementRowSubject]) -> str:
    """这一行用哪个摘要模板。

    **取值序 = 规则级 > 账套级 > 内置口径。**

    规则级只在「这条流水是**那条规则**定的科目」时才算数 —— 人工指定过
    的行（``MANUAL``）不该被某条规则的摘要模板改掉，那是用户自己挑的科目、
    自己写的摘要。
    """
    if any(item.source == PLAN_RULE for item in items):
        rule = _rule_hit(rules, _pseudo_row(row))
        if rule is not None and rule.summary_template:
            return rule.summary_template
    if config is not None and config.summary_template:
        return config.summary_template
    return ""


def _remap_subjects(db: Session, book_id: int, year: int,
                    subject_ids: list[int]) -> tuple[dict[int, int], list[str]]:
    """把科目按**编码**搬到目标年度。

    科目表每个年度各有一份（2026 的 1002 与 2027 的 1002 是两行不同的记录），
    而流水日期可能落在别的年度。不搬的话库层守卫
    （``fn_entry_subject_year_guard``）会把分录挡下来。
    """
    ids = {int(s) for s in subject_ids if s}
    if not ids:
        return {}, []
    src = db.scalars(select(Subject).where(
        Subject.book_id == book_id, Subject.id.in_(ids))).all()
    mapping = {s.id: s.id for s in src}
    need = [s for s in src if s.year != year]
    if not need:
        return mapping, []
    dst = db.scalars(select(Subject).where(
        Subject.book_id == book_id, Subject.year == year)).all()
    by_code = {s.code: s for s in dst}
    missing = []
    for s in need:
        hit = by_code.get(s.code)
        if hit is None:
            missing.append(f"{s.code} {s.name}")
        else:
            mapping[s.id] = hit.id
    return mapping, missing


def commit_vouchers(db: Session, book_id: int, statement_id: int,
                    user: SysUser) -> dict[str, Any]:
    """把候选流水生成凭证。**一张流水一张凭证。**

    **逐行隔离**：每一行用一次 SAVEPOINT，单行失败只回滚那一行，
    其余照常生成。原因很实际：一批流水里总有一两行是「那个月已经结账了」
    或「对方科目被停用了」，为这一行把整批都赔进去不划算 ——
    用户能看到哪几行没成、为什么没成，改完再点一次就行。

    **幂等**：先锁住流水行，再在锁内只处理 ``status == OPEN`` 的候选，
    保证重复提交不会生成第二张凭证。
    """
    statement = _statement(db, book_id, statement_id)
    # 两个请求可能同时拿到同一份 plan。先锁住整份对账单的流水行，再重算
    # 清单；后到的请求会等前一个事务提交，随后看到 POSTED 状态而不再制证。
    db.scalars(select(BankStatementRow).where(
        BankStatementRow.statement_id == statement.id
    ).with_for_update()).all()
    plan = plan_vouchers(db, book_id, statement_id)
    created: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    # 规则与账套配置**各读一次**，不是一行读一次：这一份对账单可能有几百行，
    # 一行一次查询就是几百次往返。
    rules = list_rules(db, book_id)
    config = get_config(db, book_id)
    children = _children_of(db, [item["id"] for item in plan["candidates"]])

    for item in plan["candidates"]:
        row = db.get(BankStatementRow, item["id"])
        if row is None or row.status != STATUS_OPEN:
            continue
        savepoint = db.begin_nested()
        try:
            plan_items = children.get(row.id) or []
            voucher = _make_voucher_for_row(
                db, book_id, statement, row, user,
                plan_subjects=plan_items,
                summary_template=_summary_template_for(rules, config, row, plan_items))
            row.status = STATUS_POSTED
            row.matched_voucher_id = voucher.id
            savepoint.commit()
            created.append({
                "row_id": row.id,
                "seq": row.seq,
                "voucher_id": voucher.id,
                "voucher_no": voucher.voucher_no,
            })
        except Exception as exc:  # noqa: BLE001 —— 单行失败不许连累整批
            savepoint.rollback()
            message = str(exc) or exc.__class__.__name__
            row.plan_status = PLAN_PENDING
            row.problems = [*(row.problems or []), f"制证失败：{message}"]
            failures.append({"row_id": row.id, "seq": row.seq, "reason": message})
    db.flush()
    return {
        "statement_id": statement.id,
        "created": created,
        "created_count": len(created),
        "failures": failures,
    }


def _make_voucher_for_row(db: Session, book_id: int, statement: BankStatement,
                          row: BankStatementRow, user: SysUser, *,
                          plan_subjects: list[BankStatementRowSubject] | None = None,
                          summary_template: str = ""):
    """一条流水 → 一张凭证。

    **对方科目可以有好几个**，所以分录不止两条：现金那一条 + 对方那几条。
    对方各条自己的金额加起来正好等于流水金额（写入口已经校验过，
    :func:`_parse_plan_payload`），所以这张凭证必然借贷平。

    **每条分录用自己的摘要。** 对方那几条各有各的一句（人工写过的优先，
    否则按模板算）——「这笔记到 6602、摘要写『备用金』」与「同一笔记到
    6601、摘要写『收取押金』」是两个科目的事，不该被一句话管住。

    **现金那一条用行级的那句**（规则/模板算出来的）：它是**整笔**的描述
    ——钱进出了这个账户，而账户这一侧没有「按科目分开」这回事。要改它，
    去凭证里改那一条分录（凭证编辑页里逐条可改）。
    """
    if row.tx_date is None:
        raise ValueError("这一行没有日期，定不了凭证日期")
    items = list(plan_subjects) if plan_subjects is not None else list(row.plan_subjects)
    if not items:
        raise ValueError("对方科目还没确定")
    period = find_period(db, book_id, row.tx_date)
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能新增凭证")

    mapping, missing = _remap_subjects(
        db, book_id, period.year,
        [statement.account_subject_id] + [item.subject_id for item in items])
    if missing:
        raise ValueError(
            f"这些科目在 {period.year} 年度的科目表里不存在：{'、'.join(missing)}。"
            "请在流水科目规则里改成本年度的科目")
    cash_id = mapping.get(statement.account_subject_id, statement.account_subject_id)

    # **现金那一条**：整笔的描述，按模板算。
    cash_summary = render_summary(row, summary_template)
    entries: list[dict[str, Any]] = []
    if row.direction == DIRECTION_IN:
        # 收钱：现金增加记**借方**（会计口径），对方记贷方。
        entries.append({"subject_id": cash_id, "direction": "DEBIT",
                        "amount": money(row.amount), "summary": cash_summary})
        for item in items:
            other_id = mapping.get(item.subject_id, item.subject_id)
            entries.append({"subject_id": other_id, "direction": "CREDIT",
                            "amount": money(item.amount),
                            "summary": _subject_summary(item, row, summary_template)})
    else:
        for item in items:
            other_id = mapping.get(item.subject_id, item.subject_id)
            entries.append({"subject_id": other_id, "direction": "DEBIT",
                            "amount": money(item.amount),
                            "summary": _subject_summary(item, row, summary_template)})
        entries.append({"subject_id": cash_id, "direction": "CREDIT",
                        "amount": money(row.amount), "summary": cash_summary})
    return voucher_service.create_voucher(
        db,
        book_id=book_id,
        user=user,
        voucher_date=row.tx_date,
        entries=entries,
        remark=f"流水对账 {statement.filename}#{row.seq}",
        attachments=1,
        # **source 用既有的 IMPORT**：``ck_voucher_source`` 只允许
        # MANUAL / IMPORT / INVOICE / CARRY_FORWARD。流水是从外部单据导入的，
        # 归到 IMPORT 与它本来的意思一致 —— 不改那个 CHECK 约束。
        source="IMPORT",
        commit=False,
    )


def reclassify_row_subject(db: Session, book_id: int, row_id: int,
                            entry_id: int, user: SysUser) -> dict[str, Any]:
    """把一条流水对应的那条分录的科目改掉。

    **只改现金那一侧**：把「记在 1001 库存现金上的那笔」改成「记在这份
    对账单绑定的账户（1002 银行存款）上」。对方侧一个字都不动。

    **为什么只改现金那一侧**：银行流水只说「转了 5000 块」，不说是买什么，
    所以流水这一侧判不出对方科目该怎么改；而钱在哪个账户上，流水说得
    死死的 —— 这份对账单绑的就是那个账户。用户要改对方侧，去凭证里改。

    **为什么目标科目只能是这份对账单绑定的账户**：改成别的账户的话，这笔
    钱就不在这个账户上了，而这份对账单的期末余额就是拿这个账户去核对的
    —— 改了之后差额会凭空冒出来，且这笔流水还会被当成「没入账」再制一张
    凭证（同一笔钱记两次）。所以这里不做「随便改成哪个科目」的灵活，
    只做「记错账户 → 挪回这个账户」这一件确定的事。

    **改的是一张真凭证，所以走凭证编辑那条路**
    （:func:`app.accounting.vouchers.update_voucher`）：期间已结账、
    凭证已记账、科目年度不对、并发改动 —— 这些闸门与审计留痕都在那儿，
    自己另写一条 UPDATE 就等于把它们全绕过去了。
    """
    row = db.get(BankStatementRow, row_id)
    if row is None or row.book_id != book_id:
        raise ValueError("流水行不存在")
    statement = db.get(BankStatement, row.statement_id)
    if statement is None or statement.book_id != book_id:
        raise ValueError("对账单不存在")
    entry = db.get(Entry, entry_id)
    if entry is None or entry.book_id != book_id:
        raise ValueError("这条分录不存在")
    voucher = db.get(Voucher, entry.voucher_id)
    if voucher is None or voucher.book_id != book_id:
        raise ValueError("这条分录所属的凭证不存在")
    old_subject = db.get(Subject, entry.subject_id)
    if old_subject is None or not _looks_cash(old_subject):
        raise ValueError("这条分录不是记在现金科目上的 —— 改它没有意义")
    target = db.get(Subject, statement.account_subject_id)
    if target is None or target.book_id != book_id:
        raise ValueError("这份对账单绑定的账户科目不属于本账套")
    if not target.is_leaf:
        raise ValueError("这份对账单绑定的账户科目不是末级科目")
    if not target.active:
        raise ValueError("这份对账单绑定的账户科目已停用")
    if entry.subject_id == target.id:
        raise ValueError("这条分录已经记在这个账户上了 —— 它已经是入账状态")
    if voucher.status != "UNPOSTED":
        raise ValueError(
            f"凭证 {voucher.voucher_no} 已记账，不能改。"
            "请先到「期末结账」对本期执行「反结账」后再改。")
    period = db.get(Period, voucher.period_id)
    if period is None:
        raise ValueError("这张凭证所属的会计期间不存在")
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能修改凭证")

    # 科目表每个年度各有一份 —— 目标科目要搬到这张凭证所在的那一年。
    mapping, missing = _remap_subjects(db, book_id, period.year, [target.id])
    if missing:
        raise ValueError(
            f"这些科目在 {period.year} 年度的科目表里不存在：{'、'.join(missing)}")
    new_target_id = mapping.get(target.id, target.id)

    entries: list[dict[str, Any]] = []
    for item in sorted(voucher.entries, key=lambda e: e.line_no):
        entries.append({
            "subject_id": (new_target_id if item.id == entry.id
                           else int(item.subject_id)),
            "direction": item.direction,
            "amount": money(item.amount),
            "summary": item.summary or "",
            "aux_ref": item.aux_ref or {},
        })
    # **带乐观锁令牌**：并发改同一张凭证时，后一个会拿到 409 而不是把前一个
    # 的改动覆盖回去（那张凭证的分录会被整体重建，不比对就会丢改动）。
    token = voucher_service.version_token(voucher.updated_at)
    voucher_service.update_voucher(db, voucher, user, {"entries": entries},
                                   expected_version=token)
    # **这一行现在就是「已入账」了**：钱已经在本账户上了。
    #
    # 不等下一次 ``plan_vouchers`` 去推 —— 那条推法是「扫一遍有没有同账户
    # 同金额的分录」，推得出来，但用户刚点完「改科目」、界面上却还显示着
    # 「待确认科目」，会以为没改成。
    row.status = STATUS_POSTED
    row.matched_voucher_id = voucher.id
    db.flush()
    return {
        "row_id": row.id,
        "voucher_id": voucher.id,
        "voucher_no": voucher.voucher_no,
        "from_subject": {"id": old_subject.id, "code": old_subject.code,
                         "name": old_subject.name},
        "to_subject": {"id": new_target_id, "code": target.code,
                       "name": target.name},
    }


# ----------------------------------------------------------------------
# 期末余额对账
# ----------------------------------------------------------------------

def reconcile_balance(db: Session, book_id: int, statement_id: int,
                      engine: Any = None) -> dict[str, Any]:
    """对账单期末余额 vs 账面该科目余额。

    差额 = 对账单期末余额 − 账面余额（**含未记账**）。

    差额用「本账户还没入账的流水净额」解释：那笔还没入账的流水净额
    就是「银行那边已经变了、账上还没变」的那部分。预览阶段暂时被发票
    认领的行仍算未入账，只有对应发票已经提交并生成凭证后才排除。两者
    近似相等时，这笔差额**有了解释**；否则就是「原因待查」。

    对账单算不出期末余额时**不报红** —— 「对账单没有可用的期末余额，
    无法核对」是一条事实陈述，不是错误。

    **「算不出」比「没有」常见**：绝大多数对账单**压根不写「期末余额」**
    （只有流水），所以这个数是从**余额列**推出来的 —— 见
    :func:`app.bankstatement.semantics._closing_from_rows`。推不出来时
    （余额链接不上、或者文件自报的笔数对不上）才真的没有。
    """
    statement = db.get(BankStatement, statement_id)
    if statement is None or statement.book_id != book_id:
        return {"available": False, "reason": "对账单不存在"}
    if statement.closing_balance is None:
        return {
            "available": False,
            "reason": "对账单没有可用的期末余额，无法核对",
        }
    subject_id = statement.account_subject_id
    when = statement.period_end or statement.period_start
    if when is None:
        return {"available": False, "reason": "对账单没有期间信息，无法核对"}
    try:
        period = find_period(db, book_id, when)
    except ValueError:
        return {
            "available": False,
            "reason": f"{when.isoformat()} 不在本账套的会计期间范围内，无法取账面余额",
        }
    book = db.get(Book, book_id)
    if book is None:
        return {"available": False, "reason": "账套不存在"}

    if engine is None:
        from app.ledger.engine import LedgerEngine

        engine = LedgerEngine.for_ledger(db, book_id, include_unposted=True)
    ord_ = (period.year - book.start_year) * 12 + period.month
    book_balance = engine.closing(subject_id, ord_)
    difference = money(statement.closing_balance) - book_balance

    # 未入账流水的净额：收入减支出。**只算这个账户的**。
    open_rows = db.scalars(select(BankStatementRow).where(
        BankStatementRow.book_id == book_id,
        BankStatementRow.statement_id == statement.id,
        BankStatementRow.status == STATUS_OPEN,
        BankStatementRow.direction.in_((DIRECTION_IN, DIRECTION_OUT)),
        BankStatementRow.matched_voucher_id.is_(None),
    )).all()
    claimed_invoice_ids = {
        int(row.matched_invoice_record_id)
        for row in open_rows
        if row.matched_invoice_record_id is not None
    }
    committed_invoice_ids: set[int] = set()
    if claimed_invoice_ids:
        committed_invoice_ids = set(db.scalars(select(InvoiceRecord.id).where(
            InvoiceRecord.id.in_(claimed_invoice_ids),
            InvoiceRecord.status == "COMMITTED",
        )).all())
    # 预览阶段的发票认领仍未入账，必须继续算入未入账净额；只有发票已
    # 提交并生成凭证后，流水才被账面覆盖。
    open_rows = [row for row in open_rows
                 if row.matched_invoice_record_id not in committed_invoice_ids]
    net = Decimal("0")
    for row in open_rows:
        if row.amount is None:
            continue
        net += row.amount if row.direction == DIRECTION_IN else -row.amount

    if difference == 0:
        verdict = "MATCH"
        explanation = "账面余额与对账单期末余额一致"
    elif abs(difference - net) <= Decimal("0.05"):
        verdict = "EXPLAINED"
        explanation = f"差额由 {len(open_rows)} 条未入账流水解释"
    else:
        verdict = "UNEXPLAINED"
        explanation = "差额原因待查"
    return {
        "available": True,
        "statement_balance": str(statement.closing_balance),
        "book_balance": str(book_balance),
        "difference": str(difference),
        "unposted_net": str(net),
        "unposted_count": len(open_rows),
        "verdict": verdict,
        "explanation": explanation,
        "as_of": period.code,
    }
