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

import logging
import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.accounting import vouchers as voucher_service
from app.accounting.util import find_period, money
from app.bankstatement import formats, semantics, storage
from app.models import (
    BankStatement,
    BankStatementProfile,
    BankStatementRow,
    BankStatementRule,
    Book,
    Entry,
    InvoiceRecord,
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


def _diagnosis_of(parsed: semantics.ParsedStatement) -> dict[str, Any]:
    return {
        "container": parsed.container,
        "sheet": parsed.sheet,
        "header_row": parsed.header_row,
        "header_cells": parsed.header_cells,
        "column_mapping": parsed.column_mapping,
        "notes": list(parsed.diagnosis),
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
    from app.invoice.ocr_client import (OcrApiError, ocr_pdf_to_texts,
                                        optional_payload_from_env)

    config = ocr_config(db)
    if not (config.get("api_url") and config.get("token")):
        return None
    from app.bankstatement import pdf_table

    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        handle.write(data)
        handle.close()
        try:
            pages = ocr_pdf_to_texts(
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
    lines = [line for page in pages for line in str(page or "").splitlines()
             if line.strip()]
    if not lines:
        return None
    return pdf_table.grid_from_text_lines(lines)


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
    就把诊断带出去（台账状态是「部分解析」，而制证那一关本来就会把认不出的
    行挡下来，不会让它变成凭证）。
    """
    from app.bankstatement import pdf_table

    text_parsed: Optional[semantics.ParsedStatement] = None
    if pdf_table.has_text_layer(data):
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

    # 两路都不干净（或只有一路）：取**问题更少**的那一份，并把诊断带出去。
    if text_parsed is not None and ocr_parsed is not None:
        return text_parsed if len(text_parsed.diagnosis) <= len(ocr_parsed.diagnosis) \
            else ocr_parsed
    if text_parsed is not None:
        return text_parsed
    if ocr_parsed is not None:
        return ocr_parsed

    raise ValueError(
        "这份 PDF 解析不出来：文字层认不出表头，OCR 兜底也没成功"
        "（系统没有配置 OCR 服务，或 OCR 服务没认出表头）。"
        "可以把对账单导出成 Excel 再上传。")


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


def _store_rows(db: Session, statement: BankStatement,
                parsed: semantics.ParsedStatement,
                rules: list[BankStatementRule]) -> None:
    """把解析出来的行写进库里。

    **行不丢。** 方向认不出来的行照样入库（标 ``problems``）——
    丢掉的话用户永远不知道自己漏了一笔钱，而「方向未知」在界面上
    一眼就能看见、人工一点就定好了。
    """
    db.execute(delete(BankStatementRow).where(
        BankStatementRow.statement_id == statement.id))
    for row in parsed.rows:
        subject_id, plan_status = _rule_subject(rules, row)
        status = STATUS_OPEN
        if row.problems and any("方向未知" in p for p in row.problems):
            # 方向都认不出来，这一行既不能制证也不能被发票匹配 ——
            # 但它**必须在库里**：用户看到「有 1 行认不出方向」才知道
            # 有笔钱没对上。标成 NONE（不是 PENDING）是为了让界面把它
            # 归到「先定方向」而不是「先定科目」那两类里。
            plan_status = PLAN_NONE
        db.add(BankStatementRow(
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
            plan_subject_id=subject_id,
            plan_status=plan_status,
            problems=list(row.problems or []),
        ))


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
        parse_message=("；".join(parsed.diagnosis)
                       + ("（这一份是用 OCR 识别的）" if parsed.via_ocr else "")),
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

    **重跑会重建全部行。** 已经生成过凭证的行不能这么干 —— 那些行带着
    ``matched_voucher_id``，重建就没了，账上那几张凭证就变成了「来路不明」。
    所以有行已经入账时，这里直接拒绝并点名。
    """
    statement = _statement(db, book_id, statement_id)
    posted = db.scalar(select(func.count()).select_from(BankStatementRow).where(
        BankStatementRow.statement_id == statement.id,
        BankStatementRow.status != STATUS_OPEN,
    )) or 0
    if posted:
        raise ValueError(
            f"这份对账单已经有 {posted} 行生成过凭证或被忽略，不能重新解析 —— "
            "重建行会丢掉与凭证的关联。请先删除这份对账单再重新上传。")
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
    statement.parse_message = "；".join(parsed.diagnosis)
    statement.diagnosis = _diagnosis_of(parsed)
    rules = list_rules(db, book_id)
    _store_rows(db, statement, parsed, rules)
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


def _rule_subject(rules: Iterable[BankStatementRule],
                  row: semantics.StatementRow) -> tuple[Optional[int], str]:
    """这一行该记到哪个对方科目。

    命中规则 → ``(科目 id, RULE)``；没命中 → ``(None, PENDING)``。

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
        created_by=getattr(user, "username", "") or "",
    )
    db.add(rule)
    db.flush()
    _replan_rules(db, book_id)
    return serialize_rule(db, rule)


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
    """
    rules = list_rules(db, book_id)
    rows = db.scalars(select(BankStatementRow).where(
        BankStatementRow.book_id == book_id,
        BankStatementRow.plan_status != PLAN_MANUAL,
    )).all()
    for row in rows:
        pseudo = semantics.StatementRow(
            seq=row.seq,
            summary=row.summary,
            purpose=row.purpose,
            counterparty_name=row.counterparty_name,
        )
        subject_id, plan_status = _rule_subject(rules, pseudo)
        row.plan_subject_id = subject_id
        if row.direction == DIRECTION_UNKNOWN:
            # 方向都没定，「对方科目待确认」这句话是错的 —— 它等的是方向。
            plan_status = PLAN_NONE
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
    }


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
    subjects = _subjects_of(db, book_id, [
        row.plan_subject_id for row in rows if row.plan_subject_id])
    return [serialize_row(db, row, subjects) for row in rows]


def serialize_row(db: Session, row: BankStatementRow,
                  subjects: dict[int, Subject] | None = None) -> dict[str, Any]:
    subjects = subjects or {}
    subject = subjects.get(row.plan_subject_id) if row.plan_subject_id else None
    if subject is None and row.plan_subject_id:
        subject = db.get(Subject, row.plan_subject_id)
    return {
        "id": row.id,
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
        "plan_subject_id": row.plan_subject_id,
        "plan_subject_code": subject.code if subject else "",
        "plan_subject_name": subject.name if subject else "",
        "plan_status": row.plan_status,
        "problems": row.problems or [],
        "voucher_state": _voucher_state(row),
    }


def _voucher_state(row: BankStatementRow) -> str:
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
    if row.plan_status in (PLAN_RULE, PLAN_MANUAL) and row.plan_subject_id:
        return "READY"
    return "NONE"


# ----------------------------------------------------------------------
# 逐行操作
# ----------------------------------------------------------------------

def update_row(db: Session, book_id: int, row_id: int,
               payload: dict[str, Any]) -> dict[str, Any]:
    """改一行：指定对方科目，或忽略 / 恢复这一行。"""
    row = db.get(BankStatementRow, row_id)
    if row is None or row.book_id != book_id:
        raise ValueError("流水行不存在")
    if "plan_subject_id" in payload:
        subject_id = payload.get("plan_subject_id")
        if subject_id in (None, "", 0):
            row.plan_subject_id = None
            # 方向还没定的行**不是**「待确认科目」，是「待确认方向」——
            # 两件事在界面上是不同的提示，别混成一个。
            row.plan_status = (PLAN_PENDING if row.direction != DIRECTION_UNKNOWN
                               else PLAN_NONE)
        else:
            subject = db.get(Subject, int(subject_id))
            if subject is None or subject.book_id != book_id:
                raise ValueError("选择的科目不属于本账套")
            if not subject.is_leaf:
                raise ValueError(f"「{subject.code} {subject.name}」不是末级科目")
            if not subject.active:
                raise ValueError(f"「{subject.code} {subject.name}」已停用")
            row.plan_subject_id = subject.id
            row.plan_status = PLAN_MANUAL
    if "status" in payload and payload["status"]:
        status = payload["status"]
        if status not in (STATUS_OPEN, STATUS_IGNORED):
            raise ValueError("状态只能是 OPEN 或 IGNORED")
        if row.matched_voucher_id:
            raise ValueError("这一行已经生成了凭证，不能忽略")
        row.status = status
    db.flush()
    return serialize_row(db, row)


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
    rules = list_rules(db, book_id)
    subjects = _subjects_of(db, book_id, [row.plan_subject_id for row in rows])

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        if row.status == STATUS_IGNORED:
            skipped.append({**serialize_row(db, row, subjects), "reason": "已忽略"})
            continue
        # ---- 去重（已有凭证）----
        matched = _voucher_matches(
            db, book_id, statement.account_subject_id,
            row.amount or Decimal("0"), row.direction, row.tx_date, window_days)
        if matched:
            voucher_id, voucher_date = matched[0]
            row.status = STATUS_POSTED
            row.matched_voucher_id = voucher_id
        elif row.status == STATUS_POSTED and not row.matched_voucher_id:
            # 之前标过已入账，现在查不到那张凭证了（多半是被删了）——
            # 收回标记，这一行重新成为候选。
            row.status = STATUS_OPEN
        elif row.status == STATUS_POSTED:
            # 标记还在，但匹配到的凭证已经不是那张了（比如金额改了）。
            # 重新判一次。
            row.status = STATUS_OPEN
            row.matched_voucher_id = None
        if row.status == STATUS_POSTED:
            skipped.append({
                **serialize_row(db, row, subjects),
                "reason": f"已入账（凭证 #{row.matched_voucher_id}）",
            })
            continue
        if row.matched_invoice_record_id:
            skipped.append({**serialize_row(db, row, subjects),
                            "reason": "已被发票占用"})
            continue
        if row.direction == DIRECTION_UNKNOWN:
            skipped.append({**serialize_row(db, row, subjects), "reason": "方向未知"})
            continue
        if row.amount is None or row.amount <= 0:
            skipped.append({**serialize_row(db, row, subjects), "reason": "金额为 0"})
            continue
        if row.plan_status == PLAN_PENDING or not row.plan_subject_id:
            skipped.append({**serialize_row(db, row, subjects), "reason": "对方科目待确认"})
            continue
        candidates.append(serialize_row(db, row, subjects))
    db.flush()
    return {
        "statement_id": statement.id,
        "account_subject_id": statement.account_subject_id,
        "candidates": candidates,
        "skipped": skipped,
        "candidate_count": len(candidates),
        "pending_count": len([s for s in skipped if s["reason"] == "对方科目待确认"]),
        "window_days": window_days,
    }


def _summary_for(row: BankStatementRow) -> str:
    if row.summary:
        return row.summary
    if row.purpose:
        return row.purpose
    who = row.counterparty_name or "对方"
    return f"银行{'收' if row.direction == DIRECTION_IN else '付'} {who}"


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

    for item in plan["candidates"]:
        row = db.get(BankStatementRow, item["id"])
        if row is None or row.status != STATUS_OPEN:
            continue
        savepoint = db.begin_nested()
        try:
            voucher = _make_voucher_for_row(db, book_id, statement, row, user)
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
                          row: BankStatementRow, user: SysUser):
    """一条流水 → 一张凭证。"""
    if row.tx_date is None:
        raise ValueError("这一行没有日期，定不了凭证日期")
    if not row.plan_subject_id:
        raise ValueError("对方科目还没确定")
    period = find_period(db, book_id, row.tx_date)
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能新增凭证")

    mapping, missing = _remap_subjects(
        db, book_id, period.year,
        [statement.account_subject_id, row.plan_subject_id])
    if missing:
        raise ValueError(
            f"这些科目在 {period.year} 年度的科目表里不存在：{'、'.join(missing)}。"
            "请在流水科目规则里改成本年度的科目")
    cash_id = mapping.get(statement.account_subject_id, statement.account_subject_id)
    other_id = mapping.get(row.plan_subject_id, row.plan_subject_id)

    amount = money(row.amount)
    summary = _summary_for(row)
    if row.direction == DIRECTION_IN:
        # 收钱：银行存款增加记**借方**（会计口径），对方记贷方。
        entries = [
            {"subject_id": cash_id, "direction": "DEBIT", "amount": amount, "summary": summary},
            {"subject_id": other_id, "direction": "CREDIT", "amount": amount, "summary": summary},
        ]
    else:
        entries = [
            {"subject_id": other_id, "direction": "DEBIT", "amount": amount, "summary": summary},
            {"subject_id": cash_id, "direction": "CREDIT", "amount": amount, "summary": summary},
        ]
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

    对账单没有余额列时**不报红** —— 「对账单未提供余额列，无法核对」
    是一条事实陈述，不是错误。
    """
    statement = db.get(BankStatement, statement_id)
    if statement is None or statement.book_id != book_id:
        return {"available": False, "reason": "对账单不存在"}
    if statement.closing_balance is None:
        return {
            "available": False,
            "reason": "对账单未提供余额列，无法核对",
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
