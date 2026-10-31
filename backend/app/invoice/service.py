"""发票批次解析、规则匹配与自动凭证预览/提交。"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.accounting import vouchers as voucher_service
from app.accounting.audit_helper import log
from app.accounting.util import constraint_name, money, period_label
from app.core.config import settings
from app.core.system_settings import ocr_config
from app.invoice import duplicates, storage
from app.invoice.parser import invoice_dict, parse_pdf_with_fallback
from app.invoice.text_parser import (
    extract_pages_text, merge_invoice_pages, page_count, parse_page,
)
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
    # 兜底科目也在这张表里：它同样要校验「属于本账套、末级、启用」，
    # 校验漏了就会把发票记到一个停用科目上。
    "input_fallback_subject_id",
    "output_fallback_subject_id",
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


def subjects_by_id(db: Session, book_id: int, records: Iterable[InvoiceRecord]) -> dict[int, Subject]:
    """把一批记录里用到的科目 id 一次性查出来。

    为什么要有：界面上「凭证预览」要显示「借 6602 管理费用 12.00」这样
    的话，光有 ``subject_id`` 不够 —— 得把编码和名称一起带出去。

    以前只有 ``_build_preview`` 内部那条路径带着科目表，批次详情接口
    (``GET /batches/{id}``) 和预览接口的返回没带，于是界面上那串科目名
    一直是空的，看着像「这笔钱没记到科目上」。

    一次查完，不按记录逐个查 —— 一个批次几百张发票，逐个查就是几百次
    数据库往返。
    """
    wanted: set[int] = set()
    for record in records:
        resolution = (record.payload or {}).get("resolution") or {}
        for item in list(resolution.get("entries") or []) + list(resolution.get("line_resolution") or []):
            value = item.get("subject_id")
            if value is None:
                continue
            try:
                wanted.add(int(value))
            except (TypeError, ValueError):
                continue
    if not wanted:
        return {}
    rows = db.scalars(select(Subject).where(Subject.book_id == book_id, Subject.id.in_(wanted))).all()
    return {row.id: row for row in rows}


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
        "input_fallback_subject_id": config.input_fallback_subject_id,
        "output_fallback_subject_id": config.output_fallback_subject_id,
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


def _resolve_rule(rules: list[InvoiceRule], invoice_type: str, counterparty: str, item: str) -> tuple[int | None, str | None, str]:
    """按规则给一个发票项目找科目。

    返回 ``(科目 id, 原因, 命中类型)``，命中类型只有三种：

    * ``matched`` —— 命中了规则，科目就是规则里配的那个；
    * ``unmatched`` —— 一条规则都没命中（**可以走兜底科目**）；
    * ``conflict`` —— 命中了多条同优先级规则，且它们指向不同科目。

    「没匹配上」与「冲突」必须分开。没匹配上是**用户还没配**，可以兜底；
    冲突是**规则配重了**（同一个关键词两个科目），兜底会把配错悄悄盖住，
    用户永远不知道自己写重了。所以冲突仍然报错。
    """
    matched = [r for r in rules if _rule_matches(r, invoice_type, counterparty, item)]
    if not matched:
        return None, "没有命中科目规则", "unmatched"
    matched.sort(key=lambda rule: (rule.priority, len(rule.keyword)), reverse=True)
    top = matched[0]
    tied = [rule for rule in matched if (rule.priority, len(rule.keyword)) == (top.priority, len(top.keyword))]
    subject_ids = {rule.subject_id for rule in tied}
    if len(subject_ids) > 1:
        return None, "多条同优先级规则命中不同科目：" + "、".join(rule.keyword for rule in tied), "conflict"
    return top.subject_id, None, "matched"


def _persisted_overrides(record: InvoiceRecord) -> dict[str, Any]:
    """这张发票**上次落库**的人工覆盖项。

    存在 ``payload.resolution.overrides`` 里 —— 预览算完时一起写进去的，
    所以它是「用户上次做好的决定」的存根。
    """
    resolution = (record.payload or {}).get("resolution") or {}
    saved = resolution.get("overrides")
    return dict(saved) if isinstance(saved, dict) else {}


def _subject_ids_in(override: dict[str, Any]) -> list[int | None]:
    """一份覆盖项里用到的**科目 id**。

    三个位置：税额科目、结算科目、逐项科目（``line_subjects`` 的每个值）。
    """
    ids: list[int | None] = [
        override.get("tax_subject_id"),
        override.get("settlement_subject_id"),
    ]
    ids.extend((override.get("line_subjects") or {}).values())
    return ids


def _usable_subject_ids(db: Session, book_id: int, ids: Iterable[int | None]) -> set[int]:
    """这些 id 里，哪些是**现在还能用**的科目。

    「能用」= 属于本账套 + 启用 + 末级 —— 与 ``_ensure_leaf_subjects`` 的
    判据逐字一致，区别只在于：那边遇到不能用的会**抛错**，这里把不能用的
    **剔掉**。两处判据必须一致，否则会出现「这里放过去了、那边又炸了」。
    """
    wanted = {int(v) for v in ids if v is not None}
    if not wanted:
        return set()
    rows = db.scalars(
        select(Subject).where(Subject.book_id == book_id, Subject.id.in_(wanted))
    ).all()
    return {row.id for row in rows if row.active and row.is_leaf}


def _sanitize_saved_override(override: dict[str, Any], usable: set[int]) -> dict[str, Any]:
    """把「落库的覆盖项」里指向**已不可用科目**的部分剔掉。

    为什么要剔：落库的覆盖项是**历史**。用户上次挑的科目，今天可能已经
    停用或不再是末级了。照用的话，预览会直接报错
    （``_ensure_leaf_subjects`` 见到停用科目会抛「人工覆盖科目包含已停用科目」），
    **整个批次都打不开** —— 一张票的历史决定拖垮了另外几十张。

    剔掉之后，那一项回落规则/配置科目；界面上「结算科目来源」会从
    「人工指定」变回「配置科目」，用户看得见，不是悄悄换的。
    """
    out = dict(override)
    for key in ("tax_subject_id", "settlement_subject_id"):
        value = out.get(key)
        if value is not None and int(value) not in usable:
            out.pop(key)
    lines = out.get("line_subjects")
    if isinstance(lines, dict):
        kept = {k: v for k, v in lines.items() if v is None or int(v) in usable}
        if kept:
            out["line_subjects"] = kept
        else:
            out.pop("line_subjects")
    return out


def _saved_overrides_by_record(db: Session, book_id: int,
                               records: list[InvoiceRecord]) -> dict[int, dict[str, Any]]:
    """每张发票**落库的**人工覆盖项（已剔掉指向不可用科目的那些）。

    一次查完所有科目，别按记录逐个查 —— 一个批次几百张发票，逐个查就是
    几百次数据库往返。
    """
    saved: dict[int, dict[str, Any]] = {}
    wanted: list[int | None] = []
    for record in records:
        item = _persisted_overrides(record)
        if not item:
            continue
        saved[record.id] = item
        wanted.extend(_subject_ids_in(item))
    usable = _usable_subject_ids(db, book_id, wanted)
    return {rid: _sanitize_saved_override(item, usable) for rid, item in saved.items()}


def _override(record: InvoiceRecord, overrides: dict[str, dict],
              saved: dict[str, Any] | None = None) -> dict[str, Any]:
    """这一张的人工覆盖项：**落库的**与**这次传进来的**合并。

    落库的垫底（那是用户上次做好的决定），这次传进来的盖在上面
    （那是用户刚做的决定，更近）。

    **为什么不能只用传进来的。**

    前端只在用户**动过**某一行时才有那一行的覆盖项 —— 它不知道用户
    在上一次会话里改过什么。于是刷新页面、或从历史里重开一个批次时，
    一次「重新计算预览」就会把用户人工改过的科目、发票类型**全部冲掉**，
    悄悄按规则重算一遍。页面上看着像「改过」，账上记的却是另一个科目。

    这跟「忽略是结论、不是待算的草稿」是同一条道理
    （见 :func:`_is_ignored`）：人工改过的地方是**结论**，
    重算是拿草稿去盖结论。

    ``line_subjects`` 是**逐项**的字典，要合而不是整个换 ——
    否则新传进来的 ``{1: X}`` 会把落库的 ``{0: Y}`` 顶掉。
    """
    persisted = saved if saved is not None else _persisted_overrides(record)
    incoming = overrides.get(str(record.id)) or {}
    merged = {**persisted, **incoming}
    lines = {
        **(persisted.get("line_subjects") or {}),
        **(incoming.get("line_subjects") or {}),
    }
    if lines:
        merged["line_subjects"] = lines
    return merged


def _is_ignored(record: InvoiceRecord, override: dict[str, Any]) -> bool:
    """这一张现在算不算「已被点名忽略」。

    判定顺序和前端的 ``isIgnoredRow`` 一致：

    1. 覆盖项里**明确**写了 ignored —— 照它走（True 是点名忽略，
       False 是取消忽略）；
    2. 覆盖项没提 —— 回落到**已经落库的状态**。

    第 2 条是「重新计算预览不该抹掉已忽略」的关键。以前这里只认覆盖项：
    从历史里重开一个批次、点一次「重新计算预览」，那些早就点名忽略的
    发票就被重新算成「可生成」—— 用户已经做好的决定，被一次重算悄悄
    抹掉。忽略是**结论**，不是待算的草稿，草稿才会被下一次重算盖掉。

    注意覆盖项优先于落库状态：用户点「取消忽略」时前端会明确发
    ``ignored=False``，那一条必须能盖过库里记着的「已忽略」。
    """
    flag = override.get("ignored")
    if flag is True:
        return True
    if flag is False:
        return False
    if record.status == "IGNORED":
        return True
    resolution = (record.payload or {}).get("resolution") or {}
    return bool(resolution.get("ignored"))


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


# ----------------------------------------------------------------------
# 结算科目：对账单识别
# ----------------------------------------------------------------------


def _release_statement_claims(db: Session, batch: InvoiceBatch,
                               record_rows: list[InvoiceRecord],
                               only_ids: set[str] | None) -> None:
    """重算预览之前，把「本批次里还没提交的发票」占用的流水放掉。

    只重算一张（``only_ids``）时只放那一张的 —— 别的发票占着的流水不能动，
    那是真的有主了。

    **已提交的发票不放**：它们的凭证已经在账上，放掉会让同一条流水
    再被一张新发票认领一次。

    **例外是「孤儿」**：状态还停在「已生成」、可凭证已经被删了的那种 ——
    它的凭证不在账上，放掉它占着的流水是放回一张本来就空着的位置。
    """
    from app.bankstatement import matching

    targets = [
        row for row in record_rows
        if not (row.status == "COMMITTED" and row.voucher_id is not None)
        and (only_ids is None or str(row.id) in only_ids)
    ]
    if not targets:
        return
    matching.release_claims(db, [row.id for row in targets])


def _claim_statement_row(db: Session, row_id: int, record_id: int) -> bool:
    from app.bankstatement import matching

    return matching.claim(db, row_id, record_id)


def _statement_settlement(db: Session, book_id: int, total: Decimal,
                           invoice_type: str, claimed_rows: set[int]
                           ) -> tuple[dict[str, Any] | None, str]:
    """按「金额 + 收支方向」去对账单里找这条发票的结算账户。

    返回 ``(命中的流水, 提示语)``。命中不了时提示语说明为什么 ——
    「多账户同额」与「没命中」在界面上要显示成不同的两句话，因为用户
    要做的动作不一样（一个要去指定账户，一个去确认钱还没到账）。

    **命中不到就回落配置科目，绝不猜。** 猜出来的结算科目会让凭证上的
    银行账户是错的，而那种错要等到对不上账时才发现。
    """
    from app.bankstatement import matching

    direction = matching.direction_for_invoice(invoice_type)
    if not direction or total == 0:
        return None, ""
    candidates = [
        item for item in matching.candidates_for(db, book_id, total, direction)
        if item["row_id"] not in claimed_rows
    ]
    if not candidates:
        return None, ""
    hit, hint = matching.pick(candidates)
    if hit is None:
        return None, hint
    # 命中的那个科目必须是**本账套的末级启用科目** —— 对账单可以是别的
    # 账套传进来的（跨账套共享一个网银账户的情况），那种科目不能拿来记账。
    subject = db.get(Subject, hit["account_subject_id"])
    if subject is None or subject.book_id != book_id:
        return None, "命中的账户科目不属于本账套，需人工指定"
    if not subject.is_leaf:
        return None, "命中的账户科目不是末级科目，需人工指定"
    if not subject.active:
        return None, "命中的账户科目已停用，需人工指定"
    return hit, ""


def _build_preview(
    db: Session,
    batch: InvoiceBatch,
    rules: list[InvoiceRule],
    config: dict[str, Any],
    overrides: dict[str, dict],
    only_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """重算预览。

    ``only_ids`` 给定时**只重算这几张**：其余记录的落库状态原样保留。
    「忽略这一张」走的就是这条路 —— 别的记录上还没点「重新计算预览」的
    改动不跟着落库（点一下忽略封面页，不该顺手把用户还没确认的科目
    改动一起存了）。
    """
    record_rows = sorted(batch.invoices, key=lambda row: row.page_index)
    # **重算之前先放掉「本批次里还没提交的发票」占用的流水。**
    #
    # 用户在预览里把一张发票的金额改了之后，原来占着的那条流水应当重新
    # 变成「没人用」—— 否则改完之后：那条流水永远显示「已被发票占用」，
    # 而真正该占它的发票（金额改过的那张）反而匹配不上，回落到配置科目。
    #
    # **已提交的发票不放**：它们的凭证已经在账上，放掉会让同一条流水
    # 再被一张新发票认领一次。
    _release_statement_claims(db, batch, record_rows, only_ids)
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
    # **落库的**人工覆盖项：先剔掉指向已不可用科目的那些，再垫在这次
    # 传进来的覆盖项底下（见 :func:`_override` 的说明）。
    #
    # 这一步与上面那句严格校验是**两件事**：上面校验的是用户**刚点**的
    # （选错了要当场报错），这里处理的是**历史**（科目可能已经停用，
    # 历史不该拖垮整个批次）。
    saved_overrides = _saved_overrides_by_record(db, batch.book_id, record_rows)

    previews: list[dict[str, Any]] = []
    ready = 0
    errors = 0
    # 本次重算里**已经被占用的流水**。同一批次内一行流水只被一张发票占用，
    # 后来的发票只能回落配置科目。
    claimed_rows: set[int] = set()
    for record in record_rows:
        if only_ids is not None and str(record.id) not in only_ids:
            continue
        if record.status == "COMMITTED" and record.voucher_id is not None:
            # **已入账的不重算。**
            #
            # 重新预览是为了改**还没入账**的那些发票。把已入账的也算一遍，
            # 它的状态会被改回「可生成」，下一次确认就**又生成一张凭证** ——
            # 同一张发票入两次账，账上凭空多一笔，还查不出是哪张干的。
            #
            # 所以这里只把它原样带出去（界面上还要显示它已经生成了），
            # 不重算、不改状态、不动它的 payload。
            previews.append(serialize_record(record, subjects))
            continue
        # **孤儿记录**（状态还停在「已生成」、可凭证已经被删了）落到下面
        # 按正常记录重算：科目重新解析、状态回到「可生成」，用户就能重新
        # 生成一张凭证。
        #
        # 上面那条「已入账的不重算」对它**不成立** —— 它的凭证已经不在账上了。
        # 不当孤儿处理，它就永远变不回「可生成」：服务端跳过重算，
        # 「确认生成」也只认「可生成」的那几张。
        meta = _meta(record)
        lines = _lines(record)
        override = _override(record, overrides, saved_overrides.get(record.id))
        if _is_ignored(record, override):
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
        # 兜底科目分进项/销项：进项兜底记成本费用、销项兜底记收入，
        # 混用必有一边记错。UNKNOWN 不给兜底 —— 那张发票本来就要人工判定
        # 进项/销项，判定完自然会走对应的那一列。
        fallback_subject_id = None
        if invoice_type == "INPUT":
            fallback_subject_id = config.get("input_fallback_subject_id")
        elif invoice_type == "OUTPUT":
            fallback_subject_id = config.get("output_fallback_subject_id")
        line_subjects: dict[int, int] = {}
        line_resolution: list[dict[str, Any]] = []
        excluded = {int(index) for index in override.get("exclude_lines", []) if str(index).isdigit()}
        for index, line in enumerate(lines):
            if index in excluded:
                line_resolution.append({"line_index": index, "excluded": True, "subject_id": None, "reason": "人工排除"})
                continue
            forced = (override.get("line_subjects") or {}).get(str(index))
            fallback = False
            if forced is not None:
                subject_id = int(forced)
                if subject_id not in subjects:
                    problems.append(f"第 {index + 1} 行指定了无效科目")
                    continue
                reason = "人工指定"
            else:
                subject_id, reason, kind = _resolve_rule(
                    rules, invoice_type, counterparty, _text(line.get("name")))
                if subject_id is None and kind == "unmatched" and fallback_subject_id:
                    # 配不出规则的项目落到兜底科目，不再报错。
                    #
                    # 以前这里是**整张发票作废**：一个没见过的怪项目就把这批
                    # 发票全堵住，而用户除了「给每个项目名都配一条规则」之外
                    # 没有第二条路。兜底让它们先记着 —— 但要在预览里标出来，
                    # 这些行是「等人回头核对」，不是「已经对了」。
                    subject_id = int(fallback_subject_id)
                    reason = "兜底科目"
                    fallback = True
            if subject_id is None:
                problems.append(f"第 {index + 1} 行「{_text(line.get('name')) or '未命名项目'}」{reason or '没有科目'}")
            else:
                line_subjects[index] = subject_id
            line_resolution.append({
                "line_index": index,
                "excluded": False,
                "subject_id": subject_id,
                "reason": reason or "规则命中",
                "fallback": fallback,
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
        # **结算科目的取值序：人工指定 > 对账单识别 > 自动制证配置。**
        #
        # 为什么要有对账单这一层：发票上不写「从哪个账户付的钱」，
        # 而对账单知道。用户在「流水对账」里把网银导出的对账单绑到
        # 具体的 1002 子科目上之后，这张发票的结算科目就该自动跟过去，
        # 不用每张发票手工选一次 —— 尤其是一笔发票对应一个专用账户时。
        #
        # 命中不唯一（多账户同额）或者压根没命中时，**一律回落配置科目**，
        # 并在 ``settlement_source``/``settlement_note`` 里说清为什么 ——
        # 绝不猜。
        settlement_source = ""
        settlement_note = ""
        settlement_subject_id = override.get("settlement_subject_id")
        if settlement_subject_id:
            settlement_source = "MANUAL"
        else:
            matched, hint = _statement_settlement(
                db, batch.book_id, total, invoice_type, claimed_rows)
            if matched is not None:
                settlement_subject_id = matched["account_subject_id"]
                settlement_source = "STATEMENT"
                settlement_note = (
                    f"命中流水：{matched['filename']} 第 {matched['seq']} 行"
                    f"（{matched['tx_date']}，{matched['account_subject_code']}"
                    f" {matched['account_name']}）")
                if _claim_statement_row(db, matched["row_id"], record.id):
                    claimed_rows.add(matched["row_id"])
                else:
                    # 候选查询与条件更新之间可能已经有人制证/认领；
                    # 这张发票必须回落配置，不能把过期候选写进凭证快照。
                    matched = None
                    settlement_subject_id = None
                    settlement_note = "流水已被其他操作占用或已入账，需人工指定"
            if matched is None:
                settlement_subject_id = config.get(
                    "input_credit_subject_id" if invoice_type == "INPUT"
                    else "output_debit_subject_id")
                settlement_source = "CONFIG"
                if not settlement_note:
                    settlement_note = hint
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

        # 一张发票一张凭证，**按项目列支**：发票上每个项目一行借方（销项是
        # 贷方），不再按科目合并。
        #
        # 原来同一科目的几行会并成一条借方。合并之后凭证上看不见这张发票
        # 买了哪几样东西，摘要也只能写「发票 XXXXX」—— 摘要里没有项目名，
        # 翻凭证时认不出是哪笔。逐项列支的代价是凭证长几行，换来的是
        # 「凭证上逐项对得上发票」。
        items: list[dict[str, Any]] = []
        item_index: list[int] = []
        for index, subject_id in line_subjects.items():
            line = lines[index]
            items.append({
                "subject_id": subject_id,
                "amount": _money(line.get("amount")),
                # 摘要 = 发票上印的项目名。``display_name`` 是干净的那个
                # （规格型号被 PDF 文字层粘在项目名后面，见 text_parser），
                # 没有就退回原名。
                "summary": _text(line.get("display_name")) or _text(line.get("name")) or "未命名项目",
            })
            item_index.append(index)

        # **各项目的金额必须加总成发票的金额。**
        #
        # 凭证的借方是逐项列的、贷方是发票的价税合计，两边必须相等 ——
        # 不相等的话这张凭证**借贷不平**，而用户在预览页看不出来，只在点
        # 「确认生成」时才炸（那时整批回滚，白忙一场）。
        #
        # 差的**几分钱**（发票自己就这么印的）落到最后一项上：金额以发票
        # 的合计为准，明细是它的拆解。差得多（超过 5 分）就不是舍入，
        # 是**少解析了一项** —— 那时不能替用户编一个金额出来，让它当
        # 异常报出去，那张发票会被标成红字等人核对。
        rounding = Decimal("0")
        if items:
            line_sum = sum((item["amount"] for item in items), Decimal("0"))
            diff = (total - tax) - line_sum
            if diff != 0:
                if abs(diff) <= Decimal("0.05"):
                    # 落到**最后一个非零**明细行上。
                    #
                    # 原来是无条件落 ``items[-1]`` —— 但最后一行可能是净额
                    # 抵成 0 的那种（整额折扣，见 ``_net_items``）。那行最后
                    # 不生成分录，差的几分钱就跟着它一起蒸发了：借方少了几
                    # 分、贷方没少，这张票照样过不去 —— 而且错得没头没脑。
                    #
                    # 同样不能选「加上差值正好抵成 0」的行：那行本来有数，
                    # 抵成 0 之后它也被剔掉，几分钱还是没了。
                    target = next(
                        (item for item in reversed(items)
                         if item["amount"] != 0 and item["amount"] + diff != 0),
                        None,
                    )
                    if target is None:
                        problems.append(
                            f"各项目金额合计 {line_sum} 与发票金额 {total - tax} 不一致"
                            f"（差 {diff}），请核对发票明细")
                    else:
                        target["amount"] += diff
                        rounding = diff
                else:
                    problems.append(
                        f"各项目金额合计 {line_sum} 与发票金额 {total - tax} 不一致"
                        f"（差 {diff}），请核对发票明细")
        entries: list[dict[str, Any]] = []
        summary = f"发票 {invoice_no or '未识别'}"
        # **科目没配齐就不生成分录。**
        #
        # 少了结算科目（或销项少了税额科目），这里以前是 ``int(None)`` ——
        # 直接抛 TypeError，接口层翻成 500，用户看到的是「服务器错误」，
        # 而真正的原因（「未配置结算科目」）就写在上面那行 ``problems`` 里，
        # 永远轮不到显示。改成：科目不齐就**不生成分录**，让上面那句
        # 「未配置结算科目」变成界面上看得见的红字。
        if invoice_type == "INPUT" and settlement_subject_id:
            entries = _input_entries(items, tax, int(settlement_subject_id), total, summary)
        elif invoice_type == "OUTPUT" and settlement_subject_id and tax_subject_id:
            entries = _output_entries(items, tax, int(tax_subject_id),
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

        # **借贷必须平**，而且要在这里平，不是等到「确认生成」才平。
        #
        # 平不了的情况是有的：整张票的明细净额全被折扣抵成 0（借方一条都
        # 不成行），或者票面金额是 0。这类票在预览页就该标红等人看，
        # 留到「确认生成」才炸的话，那一下是**整批回滚** —— 几十张本来
        # 没问题的凭证陪着一张有问题的票一起白干。
        if entries:
            debit = sum((_money(e["amount"]) for e in entries
                         if e["direction"] == "DEBIT"), Decimal("0"))
            credit = sum((_money(e["amount"]) for e in entries
                          if e["direction"] == "CREDIT"), Decimal("0"))
            if debit != credit:
                problems.append(f"分录借贷不平：借 {debit} / 贷 {credit}")
        if total == 0:
            problems.append("发票金额为 0，无法生成凭证")
        if not items:
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
                # 结算科目**从哪来**：MANUAL 人工指定 / STATEMENT 对账单识别 /
                # CONFIG 自动制证配置。界面上要给用户看这三个标签。
                "settlement_source": settlement_source,
                "settlement_note": settlement_note,
                "line_resolution": line_resolution,
                "counterparty": counterparty,
                "base": _decimal_string(base),
                "tax": _decimal_string(tax),
                "total": _decimal_string(total),
                # 各行**实际入账**的金额。与 payload 里的 lines（发票上印的
                # 原数）可能差那几分钱 —— 前端要显示的是这个，不是原数。
                "line_amounts": {
                    str(index): _decimal_string(item["amount"])
                    for index, item in zip(item_index, items)
                },
                # 末项为对齐发票合计而调整的金额（0 表示没调整）。
                "rounding": _decimal_string(rounding) if rounding else "",
                # 净额抵成 0 的明细行（整额折扣）。这些行**不生成分录** ——
                # 前端要标出来，不然用户看见「凭证预览」里没有这一项，
                # 会以为漏记了。
                "zero_lines": [
                    index for index, item in zip(item_index, items)
                    if item["amount"] == 0
                ],
                "entries": entries,
                "overrides": override,
            },
        }
        previews.append(serialize_record(record, subjects))
    if only_ids is None:
        # 只重算了几张的时候，ready/errors 只是那几张的张数 —— 回写到批次表上
        # 会把整批的快照写坏。这种情况下批次表那两列由调用方按现算结果更新。
        batch.ready_count = ready
        batch.error_count = errors
    return previews, ready, errors


def _net_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把**净额抵成 0** 的明细行剔掉。

    发票上常见「整额折扣」：一行商品原价 2.04，紧跟着一行 -2.04 的折扣行，
    净额正好抵成 0（超市的买一送一、满减就是这么印的）。解析器把折扣行并进
    商品行，于是这行的金额是 0。

    这一行**不该**变成一条 0 金额的分录：凭证上凭空多一行 0、报表上多一行
    没有金额的格子，数据库的 ``ck_entry_amount_nonzero`` 也会拦下来 ——
    拦下来的后果是「确认生成」时整批回滚，61 张凭证一张都生成不出来。

    剔掉它**不影响恒等式**：0 加进任何和里都不变，Σ明细 仍等于 Σ可入账明细，
    借方合计仍等于价税合计。发票上这一项照样列在预览的明细表里（金额 0.00），
    用户看得见它被抵掉了，只是不进凭证。
    """
    return [item for item in items if item["amount"] != 0]


def _input_entries(
    items: list[dict[str, Any]],
    tax: Decimal,
    settlement_subject_id: int,
    total: Decimal,
    summary: str,
) -> list[dict[str, Any]]:
    """进项发票的分录：**每个项目一行借方**，结算贷记价税合计。

    ``items`` 是发票明细逐项摊开的入账行，每项一个科目、一个金额、一个摘要
    （摘要 = 发票上印的项目名）。**不按科目合并** —— 合并之后凭证上看不见
    这张发票买了哪几样东西，摘要里也没有项目名。

    **进项税额不单列。** 用户是小微企业，不做进项抵扣，所以发票上的税额没有
    对应的资产 —— 按用户约定整笔并入**第一条明细行**，即价税合计直接进成本费用。

    **摘要就是发票上印的项目名，不自动加「（含税）」**（用户口径：2026-10-24）。
    摘要要回答的是「这笔钱买了什么」，往里塞系统自己加的标记，核对发票时
    反而对不上 —— 发票上印的项目名没有这几个字。税额并到了哪一行，从明细表的
    「税额」列和贷方价税合计看得出来。

    三条容易写错的：

    1. 「第一条」是**第一条非零明细行** —— 它按发票明细的行序排，与科目无关。
       合并过科目时第一条的科目可能重复出现，税额就会落错行。
    2. 税额只加一次：只有第 0 项加，后面各项都不加。
    3. 净额抵成 0 的行不进分录（``_net_items``）。所以「第一条」得在剔掉
       净零行**之后**数 —— 否则税额会落在一个根本不会成行的 0 元行上，
       那张票的借方就永远少一笔，凭证直接不成立。

    借贷恒等：借方合计 = Σ明细 + 税额 = 价税合计 = 贷方，全程 Decimal 不经过
    float，不会出现差一分。
    """
    entries: list[dict[str, Any]] = []
    for position, item in enumerate(_net_items(items)):
        # 税额只加在第一条**非零**明细行上（见上面的第 3 条）。
        takes_tax = bool(tax) and position == 0
        amount = item["amount"] + tax if takes_tax else item["amount"]
        entries.append({
            "subject_id": item["subject_id"],
            "direction": "DEBIT",
            "amount": _decimal_string(amount),
            # 摘要原样用发票上的项目名 —— 不追加「（含税）」之类系统标记。
            "summary": item["summary"],
        })
    entries.append({
        "subject_id": settlement_subject_id,
        "direction": "CREDIT",
        "amount": _decimal_string(total),
        "summary": summary,
    })
    return entries


def _output_entries(
    items: list[dict[str, Any]],
    tax: Decimal,
    tax_subject_id: int,
    settlement_subject_id: int,
    total: Decimal,
    summary: str,
) -> list[dict[str, Any]]:
    """销项发票的分录：结算借记价税合计，**每个项目一行贷方**，税额单列。

    销项税额**照旧单列** —— 那是向对方收的钱，要交出去，和进项不是一回事，
    不能跟着一起并进收入。

    明细行的摘要与进项同口径：**发票上印的项目名**，这样一张凭证的贷方
    逐行对得上发票上的每一项。
    """
    entries: list[dict[str, Any]] = [{
        "subject_id": settlement_subject_id,
        "direction": "DEBIT",
        "amount": _decimal_string(total),
        "summary": summary,
    }]
    # 净额抵成 0 的明细行不成行（``_net_items``）—— 0 金额的分录不让存。
    credited_items = _net_items(items)
    for item in credited_items:
        entries.append({
            "subject_id": item["subject_id"],
            "direction": "CREDIT",
            "amount": _decimal_string(item["amount"]),
            "summary": item["summary"],
        })
    if tax:
        # 销项税额也是贷方分录，摘要沿用这张发票的项目名，不重复写发票号。
        # 多项目税额是一笔汇总，按出现顺序列出不重复的项目名。
        tax_summary = "、".join(dict.fromkeys(
            str(item.get("summary") or "").strip()
            for item in credited_items
            if str(item.get("summary") or "").strip()
        )) or summary
        entries.append({
            "subject_id": tax_subject_id,
            "direction": "CREDIT",
            "amount": _decimal_string(tax),
            "summary": tax_summary,
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
    # 逐项的科目也要带上编码和名称：界面上「入账科目」那一列显示的是
    # **最终记到哪个科目**，已入账的发票不再能改，只能显示结果。
    line_resolution = []
    for item in resolution.get("line_resolution") or []:
        subject = subjects.get(item.get("subject_id"))
        line_resolution.append({**item, "subject_code": subject.code if subject else "", "subject_name": subject.name if subject else ""})
    resolved = {**resolution, "entries": entries}
    if "line_resolution" in resolution:
        resolved["line_resolution"] = line_resolution
    return {
        "id": record.id,
        "page_index": record.page_index,
        # 这张发票跨了几页（分页票合成后 >1）。界面上的「页码」列据此显示
        # 成「1-3」—— 只给起始页的话，用户会以为第 2、3 页没被读进来。
        "page_span": int(payload.get("page_span") or 1),
        "source_file": _text(payload.get("source_file")),
        "invoice_no": record.invoice_no or _text(meta.get("invoice_no")),
        "issue_date": record.issue_date.isoformat() if record.issue_date else _text(meta.get("issue_date")),
        "invoice_type": record.invoice_type,
        "status": record.status,
        "parse_warning": record.parse_warning,
        "errors": list(record.errors or []),
        "meta": meta,
        "lines": lines,
        "resolution": resolved,
        "voucher_id": record.voucher_id,
        # 「可能重复记账」的提示。**默认没有** —— 有没有要看账上同月有没有
        # 同额分录，那要查库，由 :func:`serialize_records` 填。
        "duplicate_warning": None,
    }


def duplicate_warnings(db: Session, batch: InvoiceBatch,
                       records: list[InvoiceRecord]) -> dict[str, dict[str, Any]]:
    """每张发票的「可能重复记账」提示（按记录 id 索引）。

    **口径见** :mod:`app.invoice.duplicates` 的模块说明：同月、同额、同向、
    同结算科目。**只提示，不拦** —— 发票号码才是这张票的唯一身份，金额
    相同不代表是同一笔（2026-10-22 的教训：管理费每月同额，两张票不是
    重复）。

    **已经生成过凭证的不提示**：它已经在账上了，再说「可能重复」是废话。
    """
    wanted = [record for record in records if not record.voucher_id]
    if not wanted:
        return {}
    ords = duplicates.period_ords(db, batch.book_id,
                                  [record.issue_date for record in wanted])
    probes: list[tuple[InvoiceRecord, duplicates.Probe]] = []
    for record in wanted:
        resolution = (record.payload or {}).get("resolution") or {}
        total = _money(resolution.get("total"))
        if total <= 0:
            continue
        direction = duplicates.settlement_direction(record.invoice_type)
        if not direction:
            continue
        subject_id = _settlement_subject_id(resolution, direction, total)
        if subject_id is None:
            continue
        if not record.issue_date:
            continue
        period_ord = ords.get((record.issue_date.year, record.issue_date.month))
        if period_ord is None:
            # 那天不在本账套的会计期间里 —— 这张票本来就入不了账，
            # 不该由这里报「可能重复」。
            continue
        probes.append((record, duplicates.Probe(
            period_ord=period_ord, direction=direction,
            amount=total, subject_id=subject_id)))
    if not probes:
        return {}
    # **本批次自己生成的凭证不算**：同一批里两张金额相同的发票是两张票，
    # 拿自己刚生成的凭证去提示自己是噪声。
    own_vouchers = {record.voucher_id for record in records if record.voucher_id}
    hits = duplicates.find_matches(db, batch.book_id,
                                   [probe for _, probe in probes], own_vouchers)
    warnings: dict[str, dict[str, Any]] = {}
    for record, probe in probes:
        matched = hits.get(probe.key) or []
        if not matched:
            continue
        warnings[record.id] = {
            "period": period_label(record.issue_date.year, record.issue_date.month),
            "amount": _decimal_string(probe.amount),
            "direction": probe.direction,
            "matches": matched,
        }
    return warnings


def _settlement_subject_id(resolution: dict[str, Any], direction: str,
                            total: Decimal) -> int | None:
    """这张发票将来那条**结算分录**用的科目 id。

    就是那条「结算方向 + 金额等于价税合计」的分录（进项的贷方结算行、
    销项的借方结算行）。判重要跟**将来真正写进账里的那一条**对齐 ——
    对不上号，比出来的就是另一笔。
    """
    for entry in resolution.get("entries") or []:
        if entry.get("direction") != direction:
            continue
        if _money(entry.get("amount")) != total:
            continue
        subject_id = entry.get("subject_id")
        if subject_id is None:
            continue
        try:
            return int(subject_id)
        except (TypeError, ValueError):
            continue
    return None


def serialize_records(db: Session, batch: InvoiceBatch, records: list[InvoiceRecord],
                       subjects: dict[int, Subject] | None = None) -> list[dict[str, Any]]:
    """序列化一批记录，并带上「可能重复记账」的提示。

    **为什么提示在这里算，而不是预览时算完存进 payload。**

    账上的分录是**会变的** —— 用户在导入这批票之后又手工记了一笔账，
    预览时算出来的提示就过期了。而提示过期比没有提示更糟：用户看着
    「可能重复记账」以为已经确认过其实没有。去查一次账（两次查询，与
    发票张数无关）换来的是**每次打开都是当下那一刻的账**。
    """
    rows = [serialize_record(record, subjects) for record in records]
    warnings = duplicate_warnings(db, batch, records)
    for row in rows:
        row["duplicate_warning"] = warnings.get(row["id"])
    return rows


def is_pdf(name: str) -> bool:
    return str(name or "").lower().endswith(".pdf")


def skip_note(skipped: list[str]) -> str:
    """被忽略的非 PDF 要留下名字，不能无声消失。"""
    if not skipped:
        return ""
    head = "、".join(skipped[:5])
    more = f" 等 {len(skipped)} 个" if len(skipped) > 5 else ""
    return f"已忽略 {len(skipped)} 个非 PDF 文件：{head}{more}"


def batch_label(names: list[str]) -> str:
    """批次标题：一份就写原名，多份写成「首个文件名 等 N 个文件」。

    历史列表与确认页都用它当标题，所以要短、要能认出是哪批。全量文件名
    只在提醒里出现（几十个名字铺满一列反而认不出哪批）。
    """
    if len(names) == 1:
        return names[0]
    return f"{names[0]} 等 {len(names)} 个文件"


def _page_sources(names: list[str], relatives: list[str]) -> list[tuple[str, int, int]]:
    """算出每个源文件在合并后 PDF 里的页码区间 ``(文件名, 起页, 止页)``。

    合并之后原件字节流已经不按文件分开了，但「这张发票来自哪个文件」在核对
    时必须能说出来（哪个文件有问题、重传哪一份）。所以合并前先把每份的页数
    读出来，按选择顺序累加成区间。

    **只要名字**：页数是从磁盘上那份文件读出来的（``storage.path_for``），
    字节流本身在这里没有用处 —— 所以这一层只接名字，不接字节。
    """
    spans: list[tuple[str, int, int]] = []
    cursor = 0
    for name, relative in zip(names, relatives):
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

    **字节入口**（既有契约，测试与内部调用走这里）。
    字节先落成临时源文件，再走 :func:`_create_batch` —— **同一份实现**，
    不写第二遍，免得两条路漂移。

    想让内存里不放大头，走 :func:`create_batch_from_staged`（上传接口用的就是它）。
    """
    staged: list[tuple[str, str]] = []
    skipped: list[str] = []
    for name, data in files:
        if is_pdf(name):
            staged.append((name, storage.stage_temp_bytes(data)))
        else:
            skipped.append(name)
    return _create_batch(db, book_id, user, staged, skipped)


def create_batch_from_staged(db: Session, book_id: int, user: SysUser,
                             staged: list[tuple[str, str]],
                             skipped: list[str]) -> InvoiceBatch:
    """**已落盘**的源文件入口：``staged`` 是 ``(用户文件名, 临时相对路径)``。

    上传接口走这条：上传的字节流本来就在磁盘上
    （FastAPI 的 ``UploadFile.file`` 是 ``SpooledTemporaryFile``，超过 1 MB
    自动溢写），把它 ``read()`` 成 ``bytes`` 等于把已经在磁盘上的东西
    **又抄一份进内存** —— 一批最多 50 MB，全部同时驻留。

    改成边读边写之后，峰值内存 = 一个 64 KB 的块，与上传大小无关。
    """
    return _create_batch(db, book_id, user, staged, skipped)


def _create_batch(db: Session, book_id: int, user: SysUser,
                  staged: list[tuple[str, str]], skipped: list[str]) -> InvoiceBatch:
    """建批次的**唯一一份实现**（源文件已经在磁盘上）。

    ``staged`` 是 ``(用户文件名, 临时相对路径)``；``skipped`` 是被忽略的
    非 PDF 文件名。

    单份时就是原来的行为：原文件原样落盘，不合并、不重写。多份时先各自暂存，
    再按选择顺序合并成一份 —— 解析器本来就是按页切发票的，所以合并之后
    「一份批次 N 张发票」的老链路一个字不用改，页码也天然连续。

    **非 PDF 被忽略，PDF 却解析不了就不放行**：整个文件夹里混着 jpg/xlsx 是
    常态，让它们把整批判掉等于这个功能不可用；而该成功的文件失败了还静默
    跳过，等于用户以为传进去了。忽略掉的名字写进批次备注，不无声消失。
    """
    names = [name for name, _ in staged]
    if not staged:
        detail = f"，本次 {len(skipped)} 个文件都不是 PDF：{'、'.join(skipped[:5])}" if skipped else ""
        raise ValueError(f"没有可解析的 PDF 文件{detail}")
    message = skip_note(skipped)
    filename = batch_label(names)
    batch = InvoiceBatch(book_id=book_id, filename=filename, created_by=user.username)
    db.add(batch)
    db.flush()
    saved_path = ""
    staged_paths: list[str] = []
    temps = [relative for _, relative in staged]
    try:
        if len(staged) == 1:
            # 单份：原文件原样落盘，**不重写**（改名不改内容）。
            saved_path = storage.promote(temps[0], f"batch_{batch.id}.pdf")
        else:
            staged_paths = [
                storage.promote(temp, f"batch_{batch.id}_src{index}.pdf")
                for index, temp in enumerate(temps, start=1)
            ]
            saved_path = storage.merge_pdfs(list(zip(staged_paths, names)), batch.id)
        batch.file_path = saved_path
        spans = _page_sources(names, staged_paths or [saved_path])
        for relative in staged_paths:
            storage.delete(relative)
        staged_paths = []
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
        for relative in staged_paths:
            storage.delete(relative)
        for relative in temps:
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
        for relative in staged_paths:
            storage.delete(relative)
        # 临时源文件也要清掉 —— 它们是被 promote 之前的名字，
        # promote 成功的那几个已经不在这个列表里了（改的是名字，不是复制）。
        for relative in temps:
            storage.delete(relative)
        if saved_path:
            storage.delete(saved_path)
        db.rollback()
        raise
    db.refresh(batch)
    return batch


def batch_sources(db: Session, batch_id: int) -> list[str]:
    """这一批是**几个来源文件**拼起来的。

    界面上「来源文件」那一列只在**多于一个来源文件**时才摆出来
    （一个文件时那列是废话）。而那一列摆不摆得出来，看的是这份名单 ——
    所以凡是返回批次详情的地方都得带上它。

    以前只有预览接口（``_preview_payload``）带了，批次详情接口
    （``GET /batches/{id}``）没带：于是用户打开批次页时那一列是空的，
    得点一次「重新计算预览」才看见 —— 像是这列数据要算出来才有，
    其实它一直都在库里。
    """
    rows = db.scalars(
        select(InvoiceRecord.payload).where(InvoiceRecord.batch_id == batch_id)
    ).all()
    return sorted({_text((row or {}).get("source_file")) for row in rows} - {""})


def preview_batch(db: Session, batch: InvoiceBatch, overrides: dict[str, dict] | None = None) -> dict[str, Any]:
    # **已生成过的批次也允许重新预览。**
    #
    # 现实里很常见：20 张发票，先确认了 10 张，剩下 10 张当时点的是「忽略」。
    # 第二天发现那 10 张其实要入账 —— 得能把它们取消忽略、重新预览、然后
    # 补一张凭证出来。以前这里直接拒掉（"该批次已是 COMMITTED 状态"），
    # 用户就只能把整批重新传一遍，而重传会被查重拦下（号码已经入过账）。
    #
    # 安全性靠的是 ``_build_preview`` 里那条规矩：**已入账的记录不重算**，
    # 所以重新预览不会把它们变回「可生成」，也就不会生成第二张凭证。
    if batch.status not in ("PENDING", "REJECTED", "COMMITTED"):
        raise ValueError(f"该批次已是「{batch.status}」状态，不能重新预览")
    config = get_config(db, batch.book_id)
    rules = db.scalars(select(InvoiceRule).where(InvoiceRule.book_id == batch.book_id, InvoiceRule.active.is_(True))).all()
    _, ready, errors = _build_preview(db, batch, rules, config, overrides or {})
    db.commit()
    return _preview_payload(db, batch, ready=ready, errors=errors)


def _preview_payload(
    db: Session,
    batch: InvoiceBatch,
    ready: int | None = None,
    errors: int | None = None,
) -> dict[str, Any]:
    """预览/忽略接口的返回体。

    ``ready``/``errors`` 是**这一轮重算**数出来的张数；不给就按落库的
    状态现算（``status_counts``）。整批重算时两者是一回事，只重算一张
    时只有现算才对 —— 那时 ``_build_preview`` 的计数器只覆盖那一张。
    """
    records = db.scalars(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id).order_by(InvoiceRecord.page_index)).all()
    sources = batch_sources(db, batch.id)
    subjects = subjects_by_id(db, batch.book_id, records)
    counts = status_counts(db, [batch.id]).get(batch.id, {})
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
        "ready_count": counts.get("READY", 0) if ready is None else ready,
        "error_count": counts.get("REJECTED", 0) if errors is None else errors,
        "ignored_count": counts.get("IGNORED", 0),
        # 已入账的张数。补生成（取消忽略后再确认）之后，界面要能说清
        # 「这一批一共生成了多少张」，而不是让人拿两轮的数字自己加。
        "committed_count": batch.committed_count or 0,
        "ocr_used": batch.ocr_used,
        "sources": sources,
        # 带上「可能重复记账」的提示：这张发票的金额在同月账上是不是已经
        # 有对应的分录（见 ``serialize_records``）。
        "invoices": serialize_records(db, batch, records, subjects),
    }


def set_record_ignored(
    db: Session,
    batch: InvoiceBatch,
    record: InvoiceRecord,
    ignored: bool,
    overrides: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """点名忽略（或取消忽略）一张发票，**立刻落库**。

    为什么不能只在前端记一个本地标记：那样「状态」那一列永远停在
    「可生成」/「有问题」，用户得再点一次「重新计算预览」才看得出改动。
    而忽略是**用户已经做好的决定** —— 合并件里的封面页、装订页、重复
    扫描页，一眼就知道不是发票。不该逼着人再点一次才能看到结果。

    只重算**这一张**（``_build_preview(only_ids=...)``）：别的记录上还没
    点「重新计算预览」的改动不跟着落库。点一下忽略封面页，不该顺手把
    用户还没确认的科目改动一起存了。

    取消忽略时这一张要**重新算一遍**：它从「已忽略」回到正常的科目解析
    与金额校验，该报的问题要重新报出来（当年没配置的结算科目、缺发票号
    这些，得让人重新处理，而不是无声地变成「可生成」）。
    """
    if record.status == "COMMITTED":
        raise ValueError("已生成凭证的发票不能忽略")
    if batch.status not in ("PENDING", "REJECTED", "COMMITTED"):
        raise ValueError(f"该批次已是「{batch.status}」状态，不能改忽略状态")
    # 这一张自己的覆盖项：取消忽略时要把它一起重算，用户在这张上改过
    # 的发票类型和科目要跟着生效。别的记录的覆盖项不参与。
    row_override = {**((overrides or {}).get(str(record.id)) or {})}
    row_override["ignored"] = bool(ignored)
    config = get_config(db, batch.book_id)
    rules = db.scalars(select(InvoiceRule).where(InvoiceRule.book_id == batch.book_id, InvoiceRule.active.is_(True))).all()
    _build_preview(db, batch, rules, config, {str(record.id): row_override},
                   only_ids={str(record.id)})
    # 先把状态刷进库，再按现算的分布回写批次表上那两列快照 —— 那两列是
    # 预览那一刻的快照，只重算一张时不能拿那一张的张数去覆盖整批。
    db.flush()
    counts = status_counts(db, [batch.id]).get(batch.id, {})
    batch.ready_count = counts.get("READY", 0)
    batch.error_count = counts.get("REJECTED", 0)
    db.commit()
    return _preview_payload(db, batch)


def reparse_record(
    db: Session,
    batch: InvoiceBatch,
    record: InvoiceRecord,
    overrides: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """把一张**解析失败的**发票重新解析一次。

    为什么要有这个按钮：解析是在**上传那一刻**跑的，而解析器会认不出一些
    票面写法（PDF 把汉字拆成独立一格、明细表头少一列…）。解析器后来修好了，
    可这批票已经躺在库里了 —— 让用户把整个批次重新传一遍，等于让他白干一遍
    （几百张票重传一次要几分钟，还可能撞上重复上传的拦截）。

    重新解析走的是**原文件**：批次存的那份 PDF 还在（只有「作废批次」才会
    删掉它），于是只重跑那一页的文字提取与解析，别的记录一根汗毛都不动。

    **判重没有被放宽**：重新解析出来的号码照样要跟批次里别的记录、跟账上
    已有的对一遍 —— 撞了就报错，不悄悄写进去。

    重新解析可能把一页认出**多张**发票（比如原来那几页其实是三张票）。
    那就一张一张建出来，而不是只留第一张把另外两张丢掉 —— 丢掉了用户就
    永远不知道自己少了两张票。
    """
    if batch.status not in ("PENDING", "REJECTED"):
        raise ValueError(f"该批次已是「{batch.status}」状态，不能重新解析")
    if not batch.file_path:
        raise ValueError("这个批次没有留下原始文件，无法重新解析")
    pdf_path = storage.path_for(batch.file_path)
    if not pdf_path.exists():
        raise ValueError("这个批次的原始文件已经不在了，无法重新解析")

    texts = extract_pages_text(str(pdf_path))
    start = max(0, int(record.page_index))
    span = max(1, int((record.payload or {}).get("page_span") or 1))
    pages = texts[start:start + span]
    if not pages:
        raise ValueError("原文件里找不到这一页")

    invoices = merge_invoice_pages(
        [parse_page(start + offset, text) for offset, text in enumerate(pages)]
    )
    if not invoices:
        raise ValueError("这一页仍然解析不出发票")

    numbers = [_text((invoice_dict(inv).get("meta") or {}).get("invoice_no")) for inv in invoices]
    wanted = {number for number in numbers if number}
    # 批次里**别的**记录占着的号码
    same_batch = {
        _text(other.invoice_no) for other in batch.invoices
        if other.id != record.id and _text(other.invoice_no)
    }
    # 账上已有的（作废的批次不算，与上传那条路径同一口径）
    existing = _existing_invoice_numbers(db, batch.book_id, wanted)
    own = _text(record.invoice_no)
    clash = sorted((wanted & (same_batch | existing)) - {own})
    if clash:
        raise ValueError("重新解析出来的发票号码与已有的重复：" + "、".join(clash))

    config = get_config(db, batch.book_id)
    # 来源文件沿用旧记录的那一个：重新解析的是**同一页**，它原来是从哪个
    # 文件来的还是哪个文件（批次里的「来源文件」列不能凭空变个名字）。
    source_file = _text((record.payload or {}).get("source_file"))
    # 人工改过的东西（选过的进项/销项、手工挑过的科目）要**跟着搬到新记录上** ——
    # 重新解析换掉的是记录（新的记录新的 id），用户改过的那张发票还是那张，
    # 不能让他的修改跟着旧记录一起消失。
    overrides = dict(overrides or {})
    carried = overrides.get(str(record.id))
    db.delete(record)
    db.flush()
    for invoice, number in zip(invoices, numbers):
        payload = {**invoice_dict(invoice), "source_file": source_file}
        meta = payload.get("meta") or {}
        new_record = InvoiceRecord(
            batch=batch,
            book_id=batch.book_id,
            page_index=invoice.page_index,
            invoice_no=number or None,
            issue_date=_date(meta.get("issue_date")),
            invoice_type=_type_from_taxpayer(meta, config.get("taxpayer_id", "")),
            parse_warning=payload.get("parse_warning", ""),
            payload=payload,
        )
        db.add(new_record)
        db.flush()
        if carried:
            overrides[str(new_record.id)] = dict(carried)
    # 关系集合要重新读一遍：刚删掉的那条还在 batch.invoices 里挂着，
    # 后面 _build_preview 拿到的就会是**旧的**那一份。
    db.expire(batch, ["invoices"])
    return preview_batch(db, batch, overrides)


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
    #
    # **COMMITTED 的批次还能再确认一次** —— 那是「补生成」：上一次确认时
    # 有一批发票被点了「忽略」，后来又取消忽略要入账。那些发票还在批次里，
    # 状态是 READY，照样该能生成凭证。
    #
    # 以前这里一律拒掉，用户看到的提示是「该批次已是 COMMITTED 状态，
    # 不能确认」—— 一个也不说错的状态，却把人堵死了：唯一的出路是把
    # 整批重新传一遍，而重传会被查重拦下（号码已经入过账）。
    #
    # 补生成**不会**重复入账：targets 只取 READY 的记录，已经 COMMITTED
    # 的那些压根不在里面（见下面 targets 那一行）。
    if batch.status not in ("PENDING", "COMMITTED"):
        raise BatchConflict(f"该批次已是「{batch.status}」状态，不能确认")
    records = db.scalars(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id).order_by(InvoiceRecord.page_index)).all()
    if not records:
        raise ValueError("批次没有发票")
    # IGNORED 是用户点名不要的（封面页之类），既不该被拦下，也不该生成凭证。
    # COMMITTED 是已经入账的（补生成时它们还在批次里），也不能再生成一次。
    if any(record.status not in ("READY", "IGNORED", "COMMITTED") for record in records):
        raise ValueError("仍有发票未通过校验，请处理红色异常后再确认")
    # **目标 = 「可生成」的 + 「孤儿」的。**
    #
    # 孤儿是那种状态还写着「已生成」、可凭证已经被删掉的记录：它其实**不在
    # 账上**，那就该能重新生成一张。只按状态取目标的话，它永远进不来，
    # 用户删了凭证之后就再也用这张发票生成不了凭证。
    targets = [
        record for record in records
        if record.status == "READY"
        or (record.status == "COMMITTED" and record.voucher_id is None)
    ]
    # **重复提交**要挡回去。
    #
    # 补生成的前提是「还有没生成的发票」。一件都没有还来确认，那是双击、
    # 并发、重试 —— 不是补生成。那种情况按老规矩给 409（冲突），
    # 而不是回一个「成功生成了 0 张」：后者会让界面弹一句「凭证已生成」，
    # 用户以为生成了，其实什么也没发生。
    #
    # 注意这跟「并发只生成一张凭证」是同一条保障：第二个请求拿到锁之后
    # 读到的记录已经是 COMMITTED，没有 READY，于是这里 409 把它挡回去。
    if batch.status == "COMMITTED" and not targets:
        raise BatchConflict(f"该批次已是「{batch.status}」状态，没有待生成的发票")
    # 判重**在锁内**重读一次（第二道兜底）。第一道是批次锁本身：
    # 同一张发票若已被**别的**批次确认过，这里会挡住。
    #
    # **判据是「有凭证」**：凭证已经被删掉的那几张不算「已入账」——
    # 它们本来就是要重新生成一票的，别自己把自己挡住。
    existing = {
        value for value in db.scalars(select(InvoiceRecord.invoice_no).where(
            InvoiceRecord.book_id == batch.book_id,
            InvoiceRecord.invoice_no.is_not(None),
            InvoiceRecord.status == "COMMITTED",
            InvoiceRecord.voucher_id.is_not(None),
        )).all() if value
    }
    numbers = [record.invoice_no for record in targets if record.invoice_no]
    duplicate = sorted(set(numbers) & existing)
    if duplicate:
        raise ValueError(f"发票已存在，不能重复生成凭证：{'、'.join(duplicate)}")
    created = 0
    try:
        for record in targets:
            # **条件写**：只有「可生成」的（以及「孤儿」）记录能被认领。
            #
            # 这是第二道兜底（第一道是批次锁）：万一将来有别的路径绕过批次锁
            # 直接改这张记录，``rowcount != 1`` 就说明它已经不是「可生成」了
            # （被别的批次确认过，或已被作废），这里必须停下 ——
            # 否则同一张发票会生成第二张凭证。
            #
            # 「孤儿」（状态是「已生成」但凭证已删）也能被认领：它其实不在
            # 账上，重新生成一张是对的。认领时顺手把 voucher_id 清空 ——
            # 新凭证的 id 紧接着就写上去了。
            claimed = db.execute(
                update(InvoiceRecord)
                .where(
                    InvoiceRecord.id == record.id,
                    or_(
                        InvoiceRecord.status == "READY",
                        and_(
                            InvoiceRecord.status == "COMMITTED",
                            InvoiceRecord.voucher_id.is_(None),
                        ),
                    ),
                )
                .values(status="COMMITTED", voucher_id=None)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise BatchConflict("该发票已被其它批次确认，不能重复生成凭证")
            # 上面那条 UPDATE 已经把库里的状态改掉了；ORM 这边还拿着旧值，
            # 手动同步一份，免得随后的 flush 又按旧值写回去。
            record.status = "COMMITTED"
            record.voucher_id = None
            resolution = (record.payload or {}).get("resolution") or {}
            entries = resolution.get("entries") or []
            # **0 金额的分录不让存**（数据库的 ck_entry_amount_nonzero 也会拦）。
            #
            # 净额抵成 0 的明细行（整额折扣）在预览时就已经不生成分录了，
            # 这里再滤一道是给**旧预览**兜底：这个批次可能是上一版代码算
            # 出来的，那一版还会把抵成 0 的行变成一条 0 元的分录，点「确认
            # 生成」就整批回滚 —— 几十张本来没问题的凭证陪着一张有问题的
            # 票一起白干。
            #
            # 滤掉它不改变合计：0 加进任何和里都不变，借贷照样平。
            entries = [e for e in entries if _money(e.get("amount")) != 0]
            if not entries:
                raise ValueError(
                    f"发票 {record.invoice_no or '未识别'} 没有可入账的分录")
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
            # **借贷必须平**，而且报错要**点名是哪张发票**。
            #
            # 不点名的话，用户看到的是「自动生成凭证失败，已整体回滚：借贷
            # 不平衡：借 179.65 / 贷 197.24」—— 一张几十张的批次里挑不出
            # 哪张票有问题，只能把整批重传一遍。
            debit = sum((_money(e["amount"]) for e in entries
                         if e["direction"] == "DEBIT"), Decimal("0"))
            credit = sum((_money(e["amount"]) for e in entries
                          if e["direction"] == "CREDIT"), Decimal("0"))
            if debit != credit:
                raise ValueError(
                    f"发票 {record.invoice_no or '未识别'} 的分录借贷不平："
                    f"借 {debit} / 贷 {credit}")
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
    # 「可生成」的张数要跟着实际状态走：确认完就没有待生成的了。
    #
    # 不改的话，界面上会一直显示「确认生成 21 张凭证」—— 那 21 张早就入账了，
    # 点下去只会得到一句「该批次已是 COMMITTED 状态，没有待生成的发票」；
    # 概要那一行还会自相矛盾：「21 张可生成，21 张已入账」（可 23 张发票）。
    batch.ready_count = 0
    batch.error_count = sum(1 for record in records if record.status == "REJECTED")
    # 补生成时**累加**：第一次确认了 10 张、这次又补 10 张，批次的
    # 「已生成凭证」该是 20 张，不是 10 张。
    batch.committed_count = (batch.committed_count or 0) + created
    batch.committed_at = datetime.now(timezone.utc)
    batch.committed_by = user.username
    # 「已忽略」要**数忽略的那几张**，不能拿「总数减掉这次生成的」——
    # 补生成时那个减法会把已经入账的那些也算成「已忽略」。
    ignored = sum(1 for record in records if record.status == "IGNORED")
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
    # **发票记录与原件文件一起删掉。**
    #
    # 为什么要删：一张几十页的合并 PDF 就有好几 MB，解析出来的 payload 更是
    # 每页一整段原文（JSONB）。用户传错、作废、再传 —— 这些东西没人再要，
    # 却一直占着磁盘和库。作废就是「这批不要」，留着才是浪费。
    #
    # 删掉记录之后，「号码要让出来给人重传」这件事**自动成立**：
    # ``uq_invoice_record_book_no`` 是 book_id + invoice_no 上的唯一约束，
    # 记录都没了，同一个号码自然能再传（原来要专门把 invoice_no 置空）。
    #
    # 批次行本身留着 —— 历史里还看得见「什么时候传过什么、被作废了」，
    # 删掉的是**发票记录**和**原件**。
    for record in list(batch.invoices):
        db.delete(record)
    if batch.file_path:
        # 文件删不掉不该让整批作废失败：记录已经标成作废了，文件留着只是
        # 占点地方。
        try:
            storage.delete(batch.file_path)
        except OSError:
            pass
    log(db, action="DISCARD_INVOICES", entity="invoice_batch", entity_id=batch.id,
        user=user, book_id=batch.book_id,
        detail=f"作废发票批次并删除记录与原件（{batch.filename}）")
    db.commit()


def status_counts(db: Session, batch_ids: list[int]) -> dict[int, dict[str, int]]:
    """一批批次的各状态张数，按批次 id 归拢。

    状态记在 invoice_record.status 上，**不回写批次表**，所以重开一个批次
    （GET /batches/{id}、历史列表）时必须现算。少算一格，用户看到的就是
    「共 3 张发票：2 张可生成」—— 第 3 张既不在可生成里也不在异常里，
    像是凭空多出来的一页。

    「可生成」「有问题」这两格更要现算：批次表里那两列是**预览那一刻**的
    快照，确认之后就没再更新过。不现算的话，界面会显示
    「共 23 张发票，23 张可生成，23 张已入账」—— 一共才 23 张，自相矛盾；
    那个「确认生成 23 张凭证」按钮也是亮的，点下去只会得到一句
    「该批次已是 COMMITTED 状态，没有待生成的发票」。
    """
    if not batch_ids:
        return {}
    # **「已生成」的判据是「有凭证」，不是「状态写着已生成」。**
    #
    # 凭证被删掉之后，数据库的外键 ``ON DELETE SET NULL`` 会把
    # ``voucher_id`` 清空，而 ``status`` 还停在「COMMITTED」——
    # 那条记录其实**已经不在账上了**，却会被算进「已入账」那一格。
    #
    # 后果是界面上「可生成」永远是 0、「确认生成 N 张凭证」那个按钮
    # 永远是灰的：用户明明有一张没入账的票，却再也点不动它。
    #
    # 所以这里按「有没有凭证」来分类：没有凭证的「已生成」一律算「可生成」。
    effective = case(
        (
            and_(InvoiceRecord.status == "COMMITTED", InvoiceRecord.voucher_id.is_(None)),
            "READY",
        ),
        else_=InvoiceRecord.status,
    )
    rows = db.execute(
        select(InvoiceRecord.batch_id, effective.label("status"), func.count(InvoiceRecord.id))
        .where(InvoiceRecord.batch_id.in_(batch_ids))
        .group_by(InvoiceRecord.batch_id, effective)
    ).all()
    out: dict[int, dict[str, int]] = {}
    for batch_id, status, count in rows:
        out.setdefault(batch_id, {})[status] = count
    return out


def batch_summary(batch: InvoiceBatch, counts: dict[str, int] | None = None) -> dict[str, Any]:
    counts = counts or {}
    return {
        "id": batch.id,
        "filename": batch.filename,
        "status": batch.status,
        "total": batch.total,
        # 现算的状态张数；批次表里那两列是预览那一刻的快照，确认后就是旧的。
        "ready_count": counts.get("READY", 0),
        "error_count": counts.get("REJECTED", 0),
        "ignored_count": counts.get("IGNORED", 0),
        "committed_count": batch.committed_count,
        "ocr_used": batch.ocr_used,
        "created_by": batch.created_by,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "message": batch.message,
    }
