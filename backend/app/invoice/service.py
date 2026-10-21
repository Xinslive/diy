"""发票批次解析、规则匹配与自动凭证预览/提交。"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.accounting import vouchers as voucher_service
from app.accounting.audit_helper import log
from app.accounting.util import constraint_name, money
from app.core.config import settings
from app.core.system_settings import ocr_config
from app.invoice import storage
from app.invoice.parser import invoice_dict, parse_pdf_with_fallback
from app.invoice.text_parser import page_count
from app.models import (
    InvoiceBatch,
    InvoiceConfig,
    InvoiceRecord,
    InvoiceRule,
    Subject,
    SysUser,
)


class BatchConflict(ValueError):
    """批次状态冲突：同一个批次的第二次「确认生成」，或发票已被别的批次认领。

    单独一个类型是为了让接口层能翻成 **409**（冲突）而不是 400
    （请求不合法）—— 同一个批次的第二次确认是**正常的并发行为**
    （用户双击了按钮、网络重试），不是用户的请求有问题。
    """


class _DuplicateInvoice(Exception):
    """内部信号：同一张发票被并发传了两遍（撞了唯一约束）。

    它**不**直接给用户看 —— 用户该看到的是「发票已存在，不能重复导入：
    号码（来自哪个文件）」，而那要等事务回滚之后才能查得出来。
    """


#: 发票号码在账套内唯一的那个约束。并发上传同一份发票时撞的是它。
#:
#: 约束名**只用来判断**「这是不是重复上传」，**绝不进响应** ——
#: 约束名属于库结构，回给客户端等于抄送内部数据模型。
INVOICE_NO_UNIQUE_CONSTRAINT = "uq_invoice_record_book_no"


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


def _year_of(db: Session, book_id: int, when: date | None) -> int:
    """某个日期落进哪个会计年度。

    找不到期间（那一天所在的年度还没开）时退回**账套当前在记的那一年** ——
    生成凭证那一步会自己报「会计期间不存在」，这里只管把科目搬到
    哪张表上去，不越权替它判对错。
    """
    from app.accounting.periods import current_accounting_year
    from app.accounting.util import find_period
    from app.models import Book

    if when is not None:
        period = find_period(db, book_id, when)
        if period is not None:
            return period.year
    book = db.get(Book, book_id)
    return current_accounting_year(db, book) if book is not None else 0


def _remap_to_year(db: Session, book_id: int, year: int,
                   subject_ids: Iterable[int]) -> tuple[dict[int, int], list[str]]:
    """把科目按**编码**搬到目标年度的科目表上。

    返回 (旧 id -> 新 id 的映射, 搬不过去的科目描述)。

    ---- 为什么发票要按编码搬 --------------------------------------------
    发票配置与规则是**账套级**的，里面存的是一个科目 id；而科目表
    **每个年度各有一份**（迁移 0032），2026 的 2221 和 2027 的 2221
    是两行不同的记录。于是「配好的科目」与「这张发票要记的那一年」
    天生可能对不上。

    对不上就按**编码**搬 —— 编码是同一个会计科目在两张科目表之间
    唯一稳定的对应关系（id 跨年根本不是一回事）。搬不动（新年度的
    科目表里没有这个编码）就把科目名报出来，让用户去把配置改掉；
    **绝不静默把账记到别的年度的科目上**。

    已经是目标年度的科目原样返回，映射是恒等的。
    """
    ids = {int(s) for s in subject_ids if s is not None}
    if not ids:
        return {}, []
    src = db.scalars(select(Subject).where(
        Subject.book_id == book_id, Subject.id.in_(ids))).all()
    mapping: dict[int, int] = {s.id: s.id for s in src}
    need = [s for s in src if s.year != year]
    if not need:
        return mapping, []
    dst = db.scalars(select(Subject).where(
        Subject.book_id == book_id, Subject.year == year)).all()
    by_code = {s.code: s for s in dst}
    missing: list[str] = []
    for s in need:
        hit = by_code.get(s.code)
        if hit is None:
            missing.append(f"{s.code} {s.name}")
        else:
            mapping[s.id] = hit.id
    return mapping, missing


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
    # 必须 commit，不能只 flush。
    #
    # get_db() 只是 close 掉会话，不会替我们提交（app/core/db.py）。只 flush 的
    # 后果是：保存接口当场回读，界面显示「已保存」，**但事务从未落库** ——
    # 刷新一次（或换一个请求）就回到没配过的样子。get_config 在同一会话里读
    # 得到自己刚 flush 的内容，所以当场看不出任何异常。
    #
    # 这个 bug 一直存在，只是被默认预填盖住了：以前每次打开界面都会补上
    # 2221/1001，「像是有值」，没人发现那行数据其实从没写进去。
    db.commit()
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
        if override.get("ignored"):
            # 用户点名「这张不算发票」：合并 PDF 里的封面、装订页、重复扫描页。
            # 不给它编造科目，也不把它算进可生成/有问题 —— 两头都不该拦下整批。
            record.status = "IGNORED"
            record.errors = []
            record.payload = {
                **(record.payload or {}),
                "resolution": {**((record.payload or {}).get("resolution") or {}), "ignored": True},
            }
            previews.append(serialize_record(record, subjects))
            continue
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

        # 科目表每个年度各有一份 —— 配置里存的科目 id 可能属于别的年度，
        # 而这张发票记在哪一年由**开票日期**决定。所以按编码把科目搬到
        # 目标年度的科目表上；搬不过去的当场报出来。
        #
        # 这一步放在这里而不是等生成凭证时：预览页要显示的就是最终会
        # 记到哪个科目上，两边不一致的话用户核对的是一张不存在的凭证。
        if entries:
            target_year = _year_of(db, batch.book_id, issue_date)
            mapping, missing = _remap_to_year(
                db, batch.book_id, target_year, [int(e["subject_id"]) for e in entries])
            if missing:
                problems.append(
                    f"这些科目在 {target_year} 年度的科目表里不存在，"
                    f"无法入账：{'、'.join(missing)}。"
                    f"请在发票设置里改成本年度的科目")
            else:
                entries = [
                    {**e, "subject_id": mapping.get(int(e["subject_id"]), int(e["subject_id"]))}
                    for e in entries
                ]

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
        "source_file": _text(payload.get("source_file")),
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


def is_pdf(name: str) -> bool:
    return str(name or "").lower().endswith(".pdf")


def skip_note(skipped: list[str]) -> str:
    """被忽略的非 PDF 要留下名字，不能无声消失。"""
    if not skipped:
        return ""
    head = "、".join(skipped[:5])
    more = f" 等 {len(skipped)} 个" if len(skipped) > 5 else ""
    return f"已忽略 {len(skipped)} 个非 PDF 文件：{head}{more}"


def batch_label(files: list[tuple[str, bytes]]) -> str:
    """批次标题：一份就写原名，多份写成「首个文件名 等 N 个文件」。

    历史列表与确认页都用它当标题，所以要短、要能认出是哪批。全量文件名
    只在提醒里出现（几十个名字铺满一列反而认不出哪批）。
    """
    names = [name for name, _ in files]
    if len(names) == 1:
        return names[0]
    return f"{names[0]} 等 {len(names)} 个文件"


def _page_sources(files: list[tuple[str, bytes]], relatives: list[str]) -> list[tuple[str, int, int]]:
    """算出每个源文件在合并后 PDF 里的页码区间 ``(文件名, 起页, 止页)``。

    合并之后原件字节流已经不按文件分开了，但「这张发票来自哪个文件」在核对
    时必须能说出来（哪个文件有问题、重传哪一份）。所以合并前先把每份的页数
    读出来，按选择顺序累加成区间。
    """
    spans: list[tuple[str, int, int]] = []
    cursor = 0
    for (name, _), relative in zip(files, relatives):
        count = page_count(str(storage.path_for(relative)))
        spans.append((name, cursor, cursor + count))
        cursor += count
    return spans


def _source_of_page(spans: list[tuple[str, int, int]], page_index: int) -> str:
    for name, start, end in spans:
        if start <= page_index < end:
            return name
    return spans[-1][0] if spans else ""


def _duplicate_detail(spans: list[tuple[str, int, int]], pages: list[int],
                      numbers: list[str], wanted: list[str]) -> str:
    """判重报错里带上「号码（来自哪几个文件）」。

    批量上传时重复可能跨文件，只给号码的话用户要在几十个文件里自己找是哪
    两张。点名的是**刚传上来的**这份清单里的文件 —— 用户手里只有这批。
    """
    detail = []
    for number in wanted:
        sources = {_source_of_page(spans, index) for index, no in zip(pages, numbers) if no == number}
        detail.append(f"{number}（{'、'.join(sorted(sources))}）")
    return "、".join(detail)


def _existing_invoice_numbers(db: Session, book_id: int, numbers: set[str]) -> set[str]:
    """这个账套里已经存在的发票号码（已作废的批次不算）。

    只认「还会变成凭证」的那些：已确认的（PENDING 还没确认，COMMITTED 已生成）
    都要挡住重复上传，已**作废**的批次不算 —— 用户点「作废批次」的意思就是
    「这批不要」，转头上传同一份 PDF 却一直被拦，等于这个功能没法用
    （commit_batch 那边的判重同样只看 COMMITTED，两边口径必须一致）。
    """
    if not numbers:
        return set()
    return {
        number for number in db.scalars(
            select(InvoiceRecord.invoice_no)
            .join(InvoiceBatch, InvoiceBatch.id == InvoiceRecord.batch_id)
            .where(
                InvoiceRecord.book_id == book_id,
                InvoiceRecord.invoice_no.in_(numbers),
                InvoiceBatch.status != "DISCARDED",
            )
        ).all() if number
    }


def create_batch(db: Session, book_id: int, user: SysUser, files: list[tuple[str, bytes]]) -> InvoiceBatch:
    """一次上传 1..N 个文件，落成一个批次；非 PDF 在这里被忽略。

    单份时就是原来的行为：原文件原样落盘，不合并、不重写。多份时先各自暂存，
    再按选择顺序合并成一份 —— 解析器本来就是按页切发票的，所以合并之后
    「一份批次 N 张发票」的老链路一个字不用改，页码也天然连续。

    **非 PDF 被忽略，PDF 却解析不了就不放行**：整个文件夹里混着 jpg/xlsx 是
    常态，让它们把整批判掉等于这个功能不可用；而该成功的文件失败了还静默
    跳过，等于用户以为传进去了。忽略掉的名字写进批次备注，不无声消失。
    """
    pdfs = [(name, data) for name, data in files if is_pdf(name)]
    skipped = [name for name, _ in files if not is_pdf(name)]
    if not pdfs:
        detail = f"，本次 {len(skipped)} 个文件都不是 PDF：{'、'.join(skipped[:5])}" if skipped else ""
        raise ValueError(f"没有可解析的 PDF 文件{detail}")
    message = skip_note(skipped)
    files = pdfs
    filename = batch_label(files)
    batch = InvoiceBatch(book_id=book_id, filename=filename, created_by=user.username)
    db.add(batch)
    db.flush()
    saved_path = ""
    staged: list[str] = []
    try:
        if len(files) == 1:
            saved_path = storage.save_pdf(batch.id, files[0][1])
        else:
            staged = [storage.stage_pdf(batch.id, index, data) for index, (_, data) in enumerate(files, start=1)]
            saved_path = storage.merge_pdfs(list(zip(staged, [name for name, _ in files])), batch.id)
        batch.file_path = saved_path
        spans = _page_sources(files, staged or [saved_path])
        for relative in staged:
            storage.delete(relative)
        staged = []
        # 凭据从系统配置解析（库里优先、.env 兜底），解析层只负责执行。
        ocr = ocr_config(db)
        invoices, ocr_used = parse_pdf_with_fallback(str(storage.path_for(saved_path)), ocr)
        config = get_config(db, book_id)
        rules = db.scalars(select(InvoiceRule).where(InvoiceRule.book_id == book_id, InvoiceRule.active.is_(True))).all()

        invoice_numbers = [
            _text((invoice_dict(invoice).get("meta") or {}).get("invoice_no"))
            for invoice in invoices
        ]
        # 每张发票对应哪一页，判重报错要靠它点名来源文件
        page_of_invoice = [invoice.page_index for invoice in invoices]
        duplicate_in_file = sorted({number for number in invoice_numbers if number and invoice_numbers.count(number) > 1})
        if duplicate_in_file:
            raise ValueError("本次上传中有重复发票号码，无法导入："
                             + _duplicate_detail(spans, page_of_invoice, invoice_numbers, duplicate_in_file))
        # 判重（口径见 _existing_invoice_numbers）。这是**无锁的先读后写**，
        # 并发下必然有洞 —— 洞被踩到时靠下面那条唯一约束兜住，
        # 并把数据库异常翻成同一句人话（审计 P1-3）。
        existing_numbers = _existing_invoice_numbers(
            db, book_id, {number for number in invoice_numbers if number})
        if existing_numbers:
            raise ValueError("发票已存在，不能重复导入："
                             + _duplicate_detail(spans, page_of_invoice, invoice_numbers,
                                                 sorted(existing_numbers)))

        records: list[InvoiceRecord] = []
        for invoice, invoice_no in zip(invoices, invoice_numbers):
            payload = {**invoice_dict(invoice),
                       "source_file": _source_of_page(spans, invoice.page_index)}
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
        try:
            db.flush()
        except IntegrityError as exc:
            # 并发上传同一份发票：上面那次判重是**无锁的先读后写**，两个请求
            # 都可能通过判重，第二个在唯一约束上撞了（审计 P1-3）。
            #
            # 这里**不把数据库异常带进响应**（它含整条 SQL、绑定参数与约束名），
            # 只记下「是这张发票重复了」；回滚之后再查一次，把到底是哪一张、
            # 来自哪个文件说清楚 —— 事务已经失败的时候是查不了的。
            if constraint_name(exc) != INVOICE_NO_UNIQUE_CONSTRAINT:
                raise
            raise _DuplicateInvoice() from exc
        batch.total = len(records) if records else len(invoices)
        batch.ocr_used = ocr_used
        batch.message = message
        # 解析时只写暂存记录；预览接口再按最新规则计算，允许先维护规则再回来看批次。
        batch.status = "PENDING"
        db.commit()
    except _DuplicateInvoice as dup:
        for relative in staged:
            storage.delete(relative)
        if saved_path:
            storage.delete(saved_path)
        db.rollback()
        # 回滚之后再查：并发的另一个请求这时已经提交了，于是能点出到底是
        # 哪张发票重复、来自哪个文件 —— 和非并发路径上那句人话一模一样。
        try:
            existing = _existing_invoice_numbers(
                db, book_id, {number for number in invoice_numbers if number})
        except Exception:  # noqa: BLE001 - 查不到就退回一句不带细节的提示
            existing = set()
        if existing:
            raise ValueError("发票已存在，不能重复导入："
                             + _duplicate_detail(spans, page_of_invoice, invoice_numbers,
                                                 sorted(existing))) from dup
        raise ValueError("发票已存在，不能重复导入（这份文件与已上传的发票重复）") from dup
    except Exception:
        for relative in staged:
            storage.delete(relative)
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
    sources = sorted({_text((record.payload or {}).get("source_file")) for record in records} - {""})
    return {
        # 键是 ``id``，和 batch_summary / GET /batches/{id} 一致。
        #
        # 原来这里叫 ``batch_id``，前端拿到上传结果后却按 ``batch.id`` 去
        # commit/recalculate/discard —— 于是「上传完直接点确认生成」必然
        # POST /batches/undefined，422。上传是主路径，这条路却从来没通过。
        "id": batch.id,
        "filename": batch.filename,
        "status": batch.status,
        "message": batch.message,
        "total": batch.total,
        "ready_count": ready,
        "error_count": errors,
        "ignored_count": sum(1 for record in records if record.status == "IGNORED"),
        "ocr_used": batch.ocr_used,
        "sources": sources,
        "invoices": [serialize_record(record) for record in records],
    }


def commit_batch(db: Session, batch: InvoiceBatch, user: SysUser) -> dict[str, Any]:
    """把批次里通过校验的发票生成凭证。

    **锁顺序：批次锁 → 记录锁。**

    批次锁由接口层取得（``api/invoices.py::_batch`` 的 ``SELECT … FOR UPDATE``），
    **一直持有到本函数末尾的 ``db.commit()``**；失败路径上的 ``db.rollback()``
    会随事务回滚把锁释放掉。锁提前释放（提交之前）或释放得太晚都不对。

    顺序是**写死的**：先锁批次行，再锁记录行（下面那条条件写 UPDATE 会取到
    记录行的行锁）。将来若有人在别的路径上按**相反**顺序加锁
    （先记录后批次），两个事务各持一把锁、互相等对方 —— 死锁。
    要在这条路上加锁的，请照这个顺序来。

    为什么需要它（审计 P0-1）：原来这里三道检查全是「先读后写」的**无锁读**
    （``batch.status``、``existing`` 判重、``create_voucher`` 取号），两个并发的
    「确认生成」各自读到「还没人提交过」，于是双双建了一套凭证 —— 同一张
    发票的费用/收入被**重复入账**，而批次摘要还少报一张、发票侧只追溯得到
    第 2 张，三处都看不出异常。
    """
    # 这一条读的是**锁内**的状态：后到的那个请求会阻塞到前一个提交完，
    # 锁释放时重新读到 COMMITTED，于是确定性地失败（而不是各建一套）。
    if batch.status != "PENDING":
        raise BatchConflict(f"该批次已是「{batch.status}」状态，不能确认")
    records = db.scalars(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id).order_by(InvoiceRecord.page_index)).all()
    if not records:
        raise ValueError("批次没有发票")
    # IGNORED 是用户点名不要的（封面页之类），既不该被拦下，也不该生成凭证。
    if any(record.status not in ("READY", "IGNORED") for record in records):
        raise ValueError("仍有发票未通过校验，请处理红色异常后再确认")
    targets = [record for record in records if record.status == "READY"]
    # 判重**在锁内**重读一次（第二道兜底）。第一道是批次锁本身：
    # 同一张发票若已被**别的**批次确认过，这里会挡住。
    existing = {
        value for value in db.scalars(select(InvoiceRecord.invoice_no).where(
            InvoiceRecord.book_id == batch.book_id,
            InvoiceRecord.invoice_no.is_not(None),
            InvoiceRecord.status == "COMMITTED",
        )).all() if value
    }
    numbers = [record.invoice_no for record in targets if record.invoice_no]
    duplicate = sorted(set(numbers) & existing)
    if duplicate:
        raise ValueError(f"发票已存在，不能重复生成凭证：{'、'.join(duplicate)}")
    created = 0
    try:
        for record in targets:
            # **条件写**：只有 READY 的记录能被认领。
            #
            # 这是第二道兜底（第一道是批次锁）：万一将来有别的路径绕过批次锁
            # 直接改这张记录，``rowcount != 1`` 就说明它已经不是 READY 了
            # （被别的批次确认过，或已被作废），这里必须停下 ——
            # 否则同一张发票会生成第二张凭证。
            claimed = db.execute(
                update(InvoiceRecord)
                .where(InvoiceRecord.id == record.id, InvoiceRecord.status == "READY")
                .values(status="COMMITTED")
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise BatchConflict("该发票已被其它批次确认，不能重复生成凭证")
            # 上面那条 UPDATE 已经把库里的状态改掉了；ORM 这边还拿着旧值，
            # 手动同步一份，免得随后的 flush 又按旧值写回去。
            record.status = "COMMITTED"
            resolution = (record.payload or {}).get("resolution") or {}
            entries = resolution.get("entries") or []
            # **再搬一次**：科目表每个年度各有一份，而这张凭证要记在
            # 开票日期所在的那一年。预览时已经搬过一次，这里是权威的一遍 ——
            # 预览到确认之间可能隔着一次年度结转，那之后科目表就不是同一张了。
            if entries:
                target_year = _year_of(db, batch.book_id, record.issue_date)
                mapping, missing = _remap_to_year(
                    db, batch.book_id, target_year, [int(e["subject_id"]) for e in entries])
                if missing:
                    raise ValueError(
                        f"这些科目在 {target_year} 年度的科目表里不存在，"
                        f"无法生成凭证：{'、'.join(missing)}。"
                        f"请在发票设置里改成本年度的科目")
                entries = [
                    {**e, "subject_id": mapping.get(int(e["subject_id"]), int(e["subject_id"]))}
                    for e in entries
                ]
            voucher = voucher_service.create_voucher(
                db,
                book_id=batch.book_id,
                user=user,
                voucher_date=record.issue_date,
                entries=entries,
                remark=f"发票 {record.invoice_no}",
                attachments=1,
                source="INVOICE",
                commit=False,
            )
            record.voucher_id = voucher.id
            created += 1
    except BatchConflict:
        db.rollback()
        raise
    except (ValueError, ArithmeticError) as exc:
        # 这里是**业务**异常（ValueError / Decimal 异常族）：文案是应用自己
        # 写的（「凭证号已经有一张了」之类），可以给人看。数据库异常走下面
        # 单独一条原样抛出去，不会混进响应。
        db.rollback()
        raise ValueError(f"自动生成凭证失败，已整体回滚：{exc}") from exc
    except IntegrityError:
        # 数据库异常**原样抛出去**：接口层会把它翻成脱敏的 500（带请求号）。
        # 它的 str() 里有整条 SQL 与绑定参数（科目、金额、摘要、发票号码），
        # 拼进给人看的文案等于把账务数据抄送给调用方（审计 P1-2）。
        db.rollback()
        raise
    batch.status = "COMMITTED"
    batch.committed_count = created
    batch.committed_at = datetime.now(timezone.utc)
    batch.committed_by = user.username
    ignored = len(records) - len(targets)
    batch.message = (f"成功生成 {created} 张凭证" + (f"，{ignored} 张已忽略" if ignored else ""))
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
        # 作废 = 这批不要了，发票号码要让出来给人重传。
        #
        # 号码并没有丢：payload 里原样留着，历史里照常看得到（serialize_record
        # 会回落到 meta）。但那一列必须置空 —— ``uq_invoice_record_book_no``
        # 是 book_id + invoice_no 上的唯一约束，不置空的话「作废完再传同一份
        # PDF」会被数据库直接拒掉，用户从此再也传不进这张发票。批量导入里
        # 「先传错、作废、再重传」是正常动作，这条约束不该把它堵死。
        record.invoice_no = None
    log(db, action="DISCARD_INVOICES", entity="invoice_batch", entity_id=batch.id,
        user=user, book_id=batch.book_id, detail=f"作废发票批次（{batch.filename}）")
    db.commit()


def ignored_counts(db: Session, batch_ids: list[int]) -> dict[int, int]:
    """一批批次的「已忽略」张数，按批次 id 归拢。

    忽略状态记在 invoice_record.status 上，不回写批次表，所以重开一个
    批次（GET /batches/{id}、历史列表）时必须现算。少了这一格，用户看到的
    是「共 3 张发票：2 张可生成」—— 第 3 张既不在可生成里也不在异常里，
    像是凭空多出来的一页。
    """
    if not batch_ids:
        return {}
    rows = db.execute(
        select(InvoiceRecord.batch_id, func.count(InvoiceRecord.id))
        .where(InvoiceRecord.batch_id.in_(batch_ids), InvoiceRecord.status == "IGNORED")
        .group_by(InvoiceRecord.batch_id)
    ).all()
    return {batch_id: count for batch_id, count in rows}


def batch_summary(batch: InvoiceBatch, ignored_count: int = 0) -> dict[str, Any]:
    return {
        "id": batch.id,
        "filename": batch.filename,
        "status": batch.status,
        "total": batch.total,
        "ready_count": batch.ready_count,
        "error_count": batch.error_count,
        "ignored_count": ignored_count,
        "committed_count": batch.committed_count,
        "ocr_used": batch.ocr_used,
        "created_by": batch.created_by,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "message": batch.message,
    }
