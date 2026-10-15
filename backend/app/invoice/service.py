"""发票批次解析、规则匹配与自动凭证预览/提交。"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.accounting import vouchers as voucher_service
from app.accounting.audit_helper import log
from app.accounting.util import money
from app.core.config import settings
from app.core.system_settings import ocr_config
from app.invoice import storage
from app.invoice.parser import invoice_dict, parse_pdf_with_fallback
from app.models import (
    InvoiceBatch,
    InvoiceConfig,
    InvoiceRecord,
    InvoiceRule,
    Subject,
    SysUser,
)


# 账套配置里可指定科目的那几个字段。**刻意不带默认值。**
#
# 原来这里按科目编码预填 2221 / 1001，省掉一次选择。但预填意味着「用户没想过，
# 系统替他想了」：一家公司可能把预付款挂在别的科目、把税额记在 2221 的另一个
# 明细上，而界面上一个绿框似的已选项不会提醒任何人去核对 —— 生成出来的凭证
# 科目错了，得等总账对不上才发现。空着就是「还没配」，预览时明确报
# 「未配置税额科目」，比猜一个像样的强。
CONFIG_SUBJECT_KEYS = (
    "output_tax_subject_id",
    "input_credit_subject_id",
    "output_debit_subject_id",
)


def _money(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    try:
        return money(value)
    except ValueError:
        raise ValueError(f"金额 {value!r} 无法解析")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _meta(record: InvoiceRecord) -> dict[str, Any]:
    return (record.payload or {}).get("meta") or {}


def _lines(record: InvoiceRecord) -> list[dict[str, Any]]:
    return list((record.payload or {}).get("lines") or [])


def _counterparty(meta: dict[str, Any], invoice_type: str) -> str:
    return _text(meta.get("seller_name") if invoice_type == "INPUT" else meta.get("buyer_name"))


def _ensure_leaf_subjects(db: Session, book_id: int, ids: list[int | None], label: str) -> dict[int, Subject]:
    wanted = {int(value) for value in ids if value is not None}
    if not wanted:
        return {}
    rows = db.scalars(select(Subject).where(Subject.book_id == book_id, Subject.id.in_(wanted))).all()
    by_id = {row.id: row for row in rows}
    missing = wanted - set(by_id)
    if missing:
        raise ValueError(f"{label}包含不属于本账套的科目：{sorted(missing)}")
    inactive = [f"{row.code} {row.name}" for row in rows if not row.active]
    if inactive:
        raise ValueError(f"{label}包含已停用科目：{'、'.join(inactive)}")
    non_leaf = [f"{row.code} {row.name}" for row in rows if not row.is_leaf]
    if non_leaf:
        raise ValueError(f"{label}必须选择末级科目：{'、'.join(non_leaf)}")
    return by_id


def _default_config(db: Session, book_id: int) -> dict[str, Any]:
    """尚未配置时的形态：**科目一律为空**，只带上身份信息。

    以前会按科目编码去查 2221 / 1001 并预填。现在不预填（见 CONFIG_SUBJECT_KEYS），
    这里也就不必再查库。
    """
    return {
        "id": None,
        "book_id": book_id,
        "taxpayer_id": "",
        **{key: None for key in CONFIG_SUBJECT_KEYS},
    }


def get_config(db: Session, book_id: int) -> dict[str, Any]:
    config = db.scalar(select(InvoiceConfig).where(InvoiceConfig.book_id == book_id))
    if config is None:
        return _default_config(db, book_id)
    return {
        "id": config.id,
        "book_id": config.book_id,
        "taxpayer_id": config.taxpayer_id,
        "output_tax_subject_id": config.output_tax_subject_id,
        "input_credit_subject_id": config.input_credit_subject_id,
        "output_debit_subject_id": config.output_debit_subject_id,
    }


def save_config(db: Session, book_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    # 存的就是界面上选的，**不回填任何默认值**。留空就是留空 —— 回填会让
    # 「我把这一项清掉了」变成「怎么又变回 1001 了」。
    values = {key: payload.get(key) for key in CONFIG_SUBJECT_KEYS}
    _ensure_leaf_subjects(db, book_id, list(values.values()), "发票默认科目")
    config = db.scalar(select(InvoiceConfig).where(InvoiceConfig.book_id == book_id))
    if config is None:
        config = InvoiceConfig(book_id=book_id)
        db.add(config)
    config.taxpayer_id = _text(payload.get("taxpayer_id"))
    for key, value in values.items():
        setattr(config, key, value)
    db.flush()
    return get_config(db, book_id)


def serialize_rule(rule: InvoiceRule, subject: Subject | None = None) -> dict[str, Any]:
    return {
        "id": rule.id,
        "match_type": rule.match_type,
        "invoice_type": rule.invoice_type,
        "keyword": rule.keyword,
        "subject_id": rule.subject_id,
        "subject_code": subject.code if subject else "",
        "subject_name": subject.name if subject else "",
        "priority": rule.priority,
        "active": rule.active,
    }


def list_rules(db: Session, book_id: int) -> list[dict[str, Any]]:
    rows = db.scalars(select(InvoiceRule).where(InvoiceRule.book_id == book_id).order_by(InvoiceRule.priority.desc(), InvoiceRule.id)).all()
    subjects = _ensure_leaf_subjects(db, book_id, [row.subject_id for row in rows], "发票规则")
    return [serialize_rule(row, subjects.get(row.subject_id)) for row in rows]


def create_rule(db: Session, book_id: int, user: SysUser, payload: dict[str, Any]) -> dict[str, Any]:
    subject = _ensure_leaf_subjects(db, book_id, [payload.get("subject_id")], "发票规则科目")
    keyword = _text(payload.get("keyword"))
    if not keyword:
        raise ValueError("规则关键词不能为空")
    rule = InvoiceRule(
        book_id=book_id,
        match_type=payload["match_type"],
        invoice_type=payload.get("invoice_type", "BOTH"),
        keyword=keyword,
        subject_id=int(payload["subject_id"]),
        priority=int(payload.get("priority", 0)),
        active=bool(payload.get("active", True)),
        created_by=user.username,
    )
    db.add(rule)
    db.flush()
    log(db, action="CREATE_INVOICE_RULE", entity="invoice_rule", entity_id=rule.id,
        user=user, book_id=book_id, detail=f"新增发票科目规则：{keyword}",
        after=serialize_rule(rule, subject.get(rule.subject_id)))
    db.commit()
    return serialize_rule(rule, subject.get(rule.subject_id))


def update_rule(db: Session, book_id: int, rule_id: int, user: SysUser, payload: dict[str, Any]) -> dict[str, Any]:
    rule = db.get(InvoiceRule, rule_id)
    if rule is None or rule.book_id != book_id:
        raise ValueError("发票规则不存在")
    changes = {key: value for key, value in payload.items() if value is not None}
    if "keyword" in changes:
        changes["keyword"] = _text(changes["keyword"])
        if not changes["keyword"]:
            raise ValueError("规则关键词不能为空")
    if "subject_id" in changes:
        _ensure_leaf_subjects(db, book_id, [changes["subject_id"]], "发票规则科目")
    before = serialize_rule(rule)
    for key, value in changes.items():
        setattr(rule, key, value)
    db.flush()
    subjects = _ensure_leaf_subjects(db, book_id, [rule.subject_id], "发票规则科目")
    after = serialize_rule(rule, subjects.get(rule.subject_id))
    log(db, action="UPDATE_INVOICE_RULE", entity="invoice_rule", entity_id=rule.id,
        user=user, book_id=book_id, detail=f"修改发票科目规则：{rule.keyword}", before=before, after=after)
    db.commit()
    return after


def delete_rule(db: Session, book_id: int, rule_id: int, user: SysUser) -> None:
    rule = db.get(InvoiceRule, rule_id)
    if rule is None or rule.book_id != book_id:
        raise ValueError("发票规则不存在")
    db.delete(rule)
    log(db, action="DELETE_INVOICE_RULE", entity="invoice_rule", entity_id=rule.id,
        user=user, book_id=book_id, detail=f"删除发票科目规则：{rule.keyword}")
    db.commit()


def _rule_matches(rule: InvoiceRule, invoice_type: str, counterparty: str, item: str) -> bool:
    if not rule.active or rule.invoice_type not in ("BOTH", invoice_type):
        return False
    keyword = rule.keyword.casefold()
    if rule.match_type == "SELLER":
        return bool(counterparty) and keyword in counterparty.casefold()
    return bool(item) and keyword in item.casefold()


def _resolve_rule(rules: list[InvoiceRule], invoice_type: str, counterparty: str, item: str) -> tuple[int | None, str | None]:
    matched = [r for r in rules if _rule_matches(r, invoice_type, counterparty, item)]
    if not matched:
        return None, "没有命中科目规则"
    matched.sort(key=lambda rule: (rule.priority, len(rule.keyword)), reverse=True)
    top = matched[0]
    tied = [rule for rule in matched if (rule.priority, len(rule.keyword)) == (top.priority, len(top.keyword))]
    subject_ids = {rule.subject_id for rule in tied}
    if len(subject_ids) > 1:
        return None, "多条同优先级规则命中不同科目：" + "、".join(rule.keyword for rule in tied)
    return top.subject_id, None


def _override(record: InvoiceRecord, overrides: dict[str, dict]) -> dict[str, Any]:
    return dict(overrides.get(str(record.id)) or {})


def _type_from_taxpayer(meta: dict[str, Any], taxpayer_id: str) -> str:
    if not taxpayer_id:
        return "UNKNOWN"
    buyer = re.sub(r"\s+", "", _text(meta.get("buyer_tax_id"))).casefold()
    seller = re.sub(r"\s+", "", _text(meta.get("seller_tax_id"))).casefold()
    own = re.sub(r"\s+", "", taxpayer_id).casefold()
    if buyer == own:
        return "INPUT"
    if seller == own:
        return "OUTPUT"
    return "UNKNOWN"


def _decimal_string(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _build_preview(
    db: Session,
    batch: InvoiceBatch,
    rules: list[InvoiceRule],
    config: dict[str, Any],
    overrides: dict[str, dict],
) -> tuple[list[dict[str, Any]], int, int]:
    record_rows = sorted(batch.invoices, key=lambda row: row.page_index)
    default_ids = [config.get(key) for key in CONFIG_SUBJECT_KEYS]
    subjects = _ensure_leaf_subjects(db, batch.book_id, default_ids, "发票默认科目")
    for rule in rules:
        subjects.update(_ensure_leaf_subjects(db, batch.book_id, [rule.subject_id], "发票规则科目"))
    # 人工覆盖的科目不一定出现在默认配置或规则里，预览前统一加载并校验，
    # 这样前端选择合法末级科目后不会被误报为「无效科目」。
    override_subject_ids: list[int | None] = []
    for override in overrides.values():
        override_subject_ids.extend([
            override.get("tax_subject_id"),
            override.get("settlement_subject_id"),
        ])
        override_subject_ids.extend((override.get("line_subjects") or {}).values())
    subjects.update(_ensure_leaf_subjects(db, batch.book_id, override_subject_ids, "人工覆盖科目"))

    previews: list[dict[str, Any]] = []
    ready = 0
    errors = 0
    for record in record_rows:
        meta = _meta(record)
        lines = _lines(record)
        override = _override(record, overrides)
        invoice_type = str(override.get("invoice_type") or record.invoice_type or "UNKNOWN")
        if invoice_type == "UNKNOWN":
            invoice_type = _type_from_taxpayer(meta, config.get("taxpayer_id", ""))
        problems: list[str] = []
        issue_date = _date(meta.get("issue_date"))
        invoice_no = _text(meta.get("invoice_no"))
        if not invoice_no:
            problems.append("缺少发票号码")
        if issue_date is None:
            problems.append("缺少或无法识别开票日期")
        if invoice_type not in ("INPUT", "OUTPUT"):
            problems.append("无法根据账套税号判断进项/销项，请在预览中选择")
        if record.parse_warning:
            problems.append(record.parse_warning)

        counterparty = _counterparty(meta, invoice_type)
        line_subjects: dict[int, int] = {}
        line_resolution: list[dict[str, Any]] = []
        excluded = {int(index) for index in override.get("exclude_lines", []) if str(index).isdigit()}
        for index, line in enumerate(lines):
            if index in excluded:
                line_resolution.append({"line_index": index, "excluded": True, "subject_id": None, "reason": "人工排除"})
                continue
            forced = (override.get("line_subjects") or {}).get(str(index))
            if forced is not None:
                subject_id = int(forced)
                if subject_id not in subjects:
                    problems.append(f"第 {index + 1} 行指定了无效科目")
                    continue
                reason = "人工指定"
            else:
                subject_id, reason = _resolve_rule(rules, invoice_type, counterparty, _text(line.get("name")))
            if subject_id is None:
                problems.append(f"第 {index + 1} 行「{_text(line.get('name')) or '未命名项目'}」{reason or '没有科目'}")
            else:
                line_subjects[index] = subject_id
            line_resolution.append({
                "line_index": index,
                "excluded": False,
                "subject_id": subject_id,
                "reason": reason or "规则命中",
            })

        base = _money(meta.get("total_excl_tax"))
        tax = _money(meta.get("total_tax"))
        total = _money(meta.get("total_incl_tax"))
        if base == 0 and lines:
            base = sum((_money(line.get("amount")) for index, line in enumerate(lines) if index not in excluded), Decimal("0"))
        if tax == 0 and lines:
            tax = sum((_money(line.get("tax")) for index, line in enumerate(lines) if index not in excluded), Decimal("0"))
        if total == 0:
            total = base + tax
        if total != base + tax:
            problems.append(f"金额校验不一致：不含税 {base} + 税额 {tax} != 价税合计 {total}")
        settlement_subject_id = override.get("settlement_subject_id") or config.get(
            "input_credit_subject_id" if invoice_type == "INPUT" else "output_debit_subject_id"
        )
        # 只有销项需要税额科目。小微企业不做进项抵扣（用户约定），进项的税额
        # 直接计入成本费用，不落到任何应交税费明细科目上。
        tax_subject_id = None
        if invoice_type == "OUTPUT":
            tax_subject_id = override.get("tax_subject_id") or config.get("output_tax_subject_id")
            if not tax_subject_id:
                problems.append("未配置税额科目")
        if not settlement_subject_id:
            problems.append("未配置结算科目")
        if tax_subject_id and int(tax_subject_id) not in subjects:
            subjects.update(_ensure_leaf_subjects(db, batch.book_id, [int(tax_subject_id)], "税额科目"))
        if settlement_subject_id and int(settlement_subject_id) not in subjects:
            subjects.update(_ensure_leaf_subjects(db, batch.book_id, [int(settlement_subject_id)], "结算科目"))

        by_subject: dict[int, Decimal] = {}
        for index, subject_id in line_subjects.items():
            by_subject[subject_id] = by_subject.get(subject_id, Decimal("0")) + _money(lines[index].get("amount"))
        entries: list[dict[str, Any]] = []
        summary = f"发票 {invoice_no or '未识别'}"
        if invoice_type == "INPUT":
            entries = _input_entries(by_subject, line_subjects, tax,
                                     int(settlement_subject_id), total, summary)
        elif invoice_type == "OUTPUT":
            entries = _output_entries(by_subject, tax, int(tax_subject_id),
                                      int(settlement_subject_id), total, summary)

        if not by_subject:
            problems.append("没有可入账的发票明细")
        if problems:
            record.status = "REJECTED"
            errors += 1
        else:
            record.status = "READY"
            ready += 1
        record.invoice_type = invoice_type
        record.invoice_no = invoice_no or None
        record.issue_date = issue_date
        record.errors = problems
        record.payload = {
            **(record.payload or {}),
            "resolution": {
                "invoice_type": invoice_type,
                "line_resolution": line_resolution,
                "counterparty": counterparty,
                "base": _decimal_string(base),
                "tax": _decimal_string(tax),
                "total": _decimal_string(total),
                "entries": entries,
                "overrides": override,
            },
        }
        previews.append(serialize_record(record, subjects))
    batch.ready_count = ready
    batch.error_count = errors
    return previews, ready, errors


def _input_entries(
    by_subject: dict[int, Decimal],
    line_subjects: dict[int, int],
    tax: Decimal,
    settlement_subject_id: int,
    total: Decimal,
    summary: str,
) -> list[dict[str, Any]]:
    """进项发票的分录：明细借记不含税额，结算贷记价税合计。

    **进项税额不单列。** 用户是小微企业，不做进项抵扣，所以发票上的税额没有
    对应的资产 —— 按用户约定整笔并入**第一条明细行**，即价税合计直接进成本费用。
    落在哪一行是要看出来的，所以摘要标「含税」：复核时一眼能看出这笔费用为什么
    比发票明细那行大，而不是以为录错了。

    两条容易写错的：

    1. 「第一条」按 ``line_subjects`` 的最小行号取，不按 ``by_subject`` 的第一个
       键。多行撞到同一个科目时 ``by_subject`` 会把它们合并，键的插入顺序就与
       原始行序脱钩了 —— 那样税额会落错行，而且只在「恰好同科目」时才错。
    2. 税额只加一次。上面按科目聚合后，同科目只留一行，所以「第一条的科目」在
       ``by_subject`` 里唯一，``==`` 判断不会重复计。

    借贷恒等：借方合计 = Σ明细 + 税额 = 价税合计 = 贷方，全程 Decimal 不经过
    float，不会出现差一分。
    """
    first_subject_id = line_subjects[min(line_subjects)] if line_subjects else None
    entries: list[dict[str, Any]] = []
    for subject_id, amount in by_subject.items():
        takes_tax = bool(tax) and subject_id == first_subject_id
        entries.append({
            "subject_id": subject_id,
            "direction": "DEBIT",
            "amount": _decimal_string(amount + tax if takes_tax else amount),
            "summary": summary + ("（含税）" if takes_tax else ""),
        })
    entries.append({
        "subject_id": settlement_subject_id,
        "direction": "CREDIT",
        "amount": _decimal_string(total),
        "summary": summary,
    })
    return entries


def _output_entries(
    by_subject: dict[int, Decimal],
    tax: Decimal,
    tax_subject_id: int,
    settlement_subject_id: int,
    total: Decimal,
    summary: str,
) -> list[dict[str, Any]]:
    """销项发票的分录：结算借记价税合计，明细与税额贷记。

    销项税额**照旧单列** —— 那是向对方收的钱，要交出去，和进项不是一回事，
    不能跟着一起并进收入。
    """
    entries: list[dict[str, Any]] = [{
        "subject_id": settlement_subject_id,
        "direction": "DEBIT",
        "amount": _decimal_string(total),
        "summary": summary,
    }]
    for subject_id, amount in by_subject.items():
        entries.append({
            "subject_id": subject_id,
            "direction": "CREDIT",
            "amount": _decimal_string(amount),
            "summary": summary,
        })
    if tax:
        entries.append({
            "subject_id": tax_subject_id,
            "direction": "CREDIT",
            "amount": _decimal_string(tax),
            "summary": summary + "（销项税额）",
        })
    return entries


def serialize_record(record: InvoiceRecord, subjects: dict[int, Subject] | None = None) -> dict[str, Any]:
    payload = record.payload or {}
    meta = payload.get("meta") or {}
    lines = payload.get("lines") or []
    resolution = payload.get("resolution") or {}
    subjects = subjects or {}
    entries = []
    for entry in resolution.get("entries") or []:
        subject = subjects.get(entry.get("subject_id"))
        entries.append({**entry, "subject_code": subject.code if subject else "", "subject_name": subject.name if subject else ""})
    return {
        "id": record.id,
        "page_index": record.page_index,
        "invoice_no": record.invoice_no or _text(meta.get("invoice_no")),
        "issue_date": record.issue_date.isoformat() if record.issue_date else _text(meta.get("issue_date")),
        "invoice_type": record.invoice_type,
        "status": record.status,
        "parse_warning": record.parse_warning,
        "errors": list(record.errors or []),
        "meta": meta,
        "lines": lines,
        "resolution": {**resolution, "entries": entries},
        "voucher_id": record.voucher_id,
    }


def create_batch(db: Session, book_id: int, user: SysUser, filename: str, data: bytes) -> InvoiceBatch:
    batch = InvoiceBatch(book_id=book_id, filename=filename, created_by=user.username)
    db.add(batch)
    db.flush()
    saved_path = ""
    try:
        saved_path = storage.save_pdf(batch.id, data)
        batch.file_path = saved_path
        # 凭据从系统配置解析（库里优先、.env 兜底），解析层只负责执行。
        ocr = ocr_config(db)
        invoices, ocr_used = parse_pdf_with_fallback(str(storage.path_for(saved_path)), ocr)
        config = get_config(db, book_id)
        rules = db.scalars(select(InvoiceRule).where(InvoiceRule.book_id == book_id, InvoiceRule.active.is_(True))).all()

        invoice_numbers = [
            _text((invoice_dict(invoice).get("meta") or {}).get("invoice_no"))
            for invoice in invoices
        ]
        duplicate_in_file = sorted({number for number in invoice_numbers if number and invoice_numbers.count(number) > 1})
        if duplicate_in_file:
            raise ValueError(f"同一文件内发票号码重复，无法导入：{'、'.join(duplicate_in_file)}")
        existing_numbers = {
            number for number in db.scalars(
                select(InvoiceRecord.invoice_no).where(
                    InvoiceRecord.book_id == book_id,
                    InvoiceRecord.invoice_no.in_({number for number in invoice_numbers if number}),
                )
            ).all() if number
        }
        if existing_numbers:
            raise ValueError(f"发票已存在，不能重复导入：{'、'.join(sorted(existing_numbers))}")

        records: list[InvoiceRecord] = []
        for invoice, invoice_no in zip(invoices, invoice_numbers):
            payload = invoice_dict(invoice)
            meta = payload.get("meta") or {}
            record = InvoiceRecord(
                batch=batch,
                book_id=book_id,
                page_index=invoice.page_index,
                invoice_no=invoice_no or None,
                issue_date=_date(meta.get("issue_date")),
                invoice_type=_type_from_taxpayer(meta, config.get("taxpayer_id", "")),
                parse_warning=payload.get("parse_warning", ""),
                payload=payload,
            )
            db.add(record)
            records.append(record)
        db.flush()
        batch.total = len(records) if records else len(invoices)
        batch.ocr_used = ocr_used
        # 解析时只写暂存记录；预览接口再按最新规则计算，允许先维护规则再回来看批次。
        batch.status = "PENDING"
        db.commit()
    except Exception:
        if saved_path:
            storage.delete(saved_path)
        db.rollback()
        raise
    db.refresh(batch)
    return batch


def preview_batch(db: Session, batch: InvoiceBatch, overrides: dict[str, dict] | None = None) -> dict[str, Any]:
    if batch.status not in ("PENDING", "REJECTED"):
        raise ValueError(f"该批次已是「{batch.status}」状态，不能重新预览")
    config = get_config(db, batch.book_id)
    rules = db.scalars(select(InvoiceRule).where(InvoiceRule.book_id == batch.book_id, InvoiceRule.active.is_(True))).all()
    _, ready, errors = _build_preview(db, batch, rules, config, overrides or {})
    db.commit()
    records = db.scalars(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id).order_by(InvoiceRecord.page_index)).all()
    return {
        "batch_id": batch.id,
        "filename": batch.filename,
        "status": batch.status,
        "total": batch.total,
        "ready_count": ready,
        "error_count": errors,
        "ocr_used": batch.ocr_used,
        "invoices": [serialize_record(record) for record in records],
    }


def commit_batch(db: Session, batch: InvoiceBatch, user: SysUser) -> dict[str, Any]:
    if batch.status != "PENDING":
        raise ValueError(f"该批次已是「{batch.status}」状态，不能确认")
    records = db.scalars(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id).order_by(InvoiceRecord.page_index)).all()
    if not records:
        raise ValueError("批次没有发票")
    if any(record.status != "READY" for record in records):
        raise ValueError("仍有发票未通过校验，请处理红色异常后再确认")
    existing = {
        value for value in db.scalars(select(InvoiceRecord.invoice_no).where(
            InvoiceRecord.book_id == batch.book_id,
            InvoiceRecord.invoice_no.is_not(None),
            InvoiceRecord.status == "COMMITTED",
        )).all() if value
    }
    numbers = [record.invoice_no for record in records if record.invoice_no]
    duplicate = sorted(set(numbers) & existing)
    if duplicate:
        raise ValueError(f"发票已存在，不能重复生成凭证：{'、'.join(duplicate)}")
    created = 0
    try:
        for record in records:
            resolution = (record.payload or {}).get("resolution") or {}
            voucher = voucher_service.create_voucher(
                db,
                book_id=batch.book_id,
                user=user,
                voucher_date=record.issue_date,
                entries=resolution.get("entries") or [],
                remark=f"发票 {record.invoice_no}",
                attachments=1,
                source="INVOICE",
                commit=False,
            )
            record.voucher_id = voucher.id
            record.status = "COMMITTED"
            created += 1
    except (ValueError, ArithmeticError, IntegrityError) as exc:
        db.rollback()
        raise ValueError(f"自动生成凭证失败，已整体回滚：{exc}") from exc
    batch.status = "COMMITTED"
    batch.committed_count = created
    batch.committed_at = datetime.now(timezone.utc)
    batch.committed_by = user.username
    batch.message = f"成功生成 {created} 张凭证"
    log(db, action="IMPORT_INVOICES", entity="invoice_batch", entity_id=batch.id,
        user=user, book_id=batch.book_id, detail=f"发票导入生成凭证 {created} 张（{batch.filename}）",
        after={"batch_id": batch.id, "created": created})
    db.commit()
    return {"ok": True, "created": created, "message": batch.message}


def discard_batch(db: Session, batch: InvoiceBatch, user: SysUser) -> None:
    if batch.status != "PENDING":
        raise ValueError(f"该批次已是「{batch.status}」状态，不能作废")
    batch.status = "DISCARDED"
    batch.message = "已作废"
    for record in batch.invoices:
        record.status = "DISCARDED"
    log(db, action="DISCARD_INVOICES", entity="invoice_batch", entity_id=batch.id,
        user=user, book_id=batch.book_id, detail=f"作废发票批次（{batch.filename}）")
    db.commit()


def batch_summary(batch: InvoiceBatch) -> dict[str, Any]:
    return {
        "id": batch.id,
        "filename": batch.filename,
        "status": batch.status,
        "total": batch.total,
        "ready_count": batch.ready_count,
        "error_count": batch.error_count,
        "committed_count": batch.committed_count,
        "ocr_used": batch.ocr_used,
        "created_by": batch.created_by,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "message": batch.message,
    }
