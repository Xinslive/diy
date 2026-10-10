"""凭证 Excel 导入。

采用「解析暂存 → 预览 → 确认入库」三段式，绝不边读边写：

    POST /vouchers/import            上传文件，解析并暂存，返回逐张凭证的校验结果
    POST /vouchers/import/{id}/commit 财务确认后真正写入
    POST /vouchers/import/{id}/discard 作废暂存

列名容错
--------
不同财务导出的表头写法差别很大（日期 / 制单日期 / voucher_date …），
因此每个字段都配了一组别名，全部小写去空格后匹配。

断行规则
--------
日期或凭证号为空时视为「接上一张凭证」，这正好对应
Excel 里一张凭证连着几行分录的常见排版。
"""
from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.accounting.util import money
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
}

def _norm(value: Any) -> str:
    return "".join(str(value).split()).lower() if value is not None else ""


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


def _as_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    if isinstance(value, (int, float, Decimal)):
        return money(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    text = text.replace("¥", "").replace("￥", "").strip()
    if text.endswith("%"):
        return Decimal("0")
    try:
        return money(text)
    except Exception:
        return Decimal("0")


def _subject_index(db: Session, book_id: int) -> tuple[dict[str, Subject], dict[str, Subject]]:
    """返回 (编码索引, 名称索引)。只索引叶子科目。"""
    rows = db.scalars(
        select(Subject).where(Subject.book_id == book_id, Subject.active.is_(True))
    ).all()
    by_code = {s.code.strip(): s for s in rows}
    by_name: dict[str, Subject] = {}
    for s in rows:
        by_name.setdefault(s.name.strip(), s)
    return by_code, by_name


# --------------------------------------------------------------------- 解析


def parse_workbook(data: bytes, book_id: int, db: Session) -> dict:
    """解析 xlsx 字节流，返回可 JSON 化的暂存包。不写库。"""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无法读取 Excel 文件：{exc}") from exc

    ws = wb.active
    if ws is None:
        raise ValueError("Excel 里没有工作表")

    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = [c for c in next(rows_iter)]
    except StopIteration as exc:
        raise ValueError("Excel 是空文件") from exc

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

    by_code, by_name = _subject_index(db, book_id)
    unknown_subjects: dict[str, str] = {}

    def col(key: str, row: list[Any]) -> Any:
        idx = mapping.get(key)
        return row[idx] if idx is not None and idx < len(row) else None

    vouchers: list[dict] = []
    errors: list[dict] = []
    current: dict | None = None
    line_no = 1

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
        elif dr <= 0:
            current["errors"].append("金额必须大于 0")
        current["ok"] = not current["errors"]
        vouchers.append(current)
        current = None

    for row in rows_iter:
        line_no += 1
        if row is None or all(c in (None, "") for c in row):
            continue

        raw_date = col("voucher_date", row)
        raw_no = col("voucher_no", row)
        if raw_date not in (None, "") or raw_no not in (None, ""):
            close_current()
            current = {
                "line": line_no,
                "voucher_date": _as_date(raw_date).isoformat() if _as_date(raw_date) else None,
                "voucher_date_raw": str(raw_date or ""),
                "voucher_no": str(raw_no).strip() if raw_no not in (None, "") else None,
                "remark": str(col("remark", row) or ""),
                "attachments": int(_as_decimal(col("attachments", row)) or 0),
                "entries": [],
                "errors": [],
            }
            if current["voucher_date"] is None:
                current["errors"].append(f"日期无法识别：{raw_date!r}")

        if current is None:
            continue  # 表头之后的空行 / 无日期无凭证号的散行

        subject_raw = col("subject", row)
        if subject_raw in (None, ""):
            subject_raw = col("subject_name", row)
        subject = None
        if subject_raw not in (None, ""):
            key = str(subject_raw).strip()
            subject = by_code.get(key) or by_name.get(key)
            if subject is None:
                unknown_subjects[key] = "科目编码或名称在本账套不存在"
                current["errors"].append(f"第 {line_no} 行：找不到科目「{key}」")

        dr = _as_decimal(col("debit", row)) if "debit" in mapping else Decimal("0")
        cr = _as_decimal(col("credit", row)) if "credit" in mapping else Decimal("0")
        if not dr and not cr and "amount" in mapping:
            amt = _as_decimal(col("amount", row))
            direction = str(col("direction", row) or "").strip()
            if direction in ("借", "DEBIT", "D"):
                dr = amt
            else:
                cr = amt

        if subject is None or (dr <= 0 and cr <= 0):
            if subject is None and subject_raw in (None, "") and (dr or cr):
                current["errors"].append(f"第 {line_no} 行：有金额但没填科目")
            continue

        direction = "DEBIT" if dr > 0 else "CREDIT"
        current["entries"].append(
            {
                "subject_id": subject.id,
                "subject_code": subject.code,
                "subject_name": subject.name,
                "direction": direction,
                "amount": str(dr if direction == "DEBIT" else cr),
                "summary": str(col("summary", row) or ""),
            }
        )

    close_current()
    wb.close()

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
    }


# --------------------------------------------------------------------- 暂存 / 入库


def stage(db: Session, book_id: int, user: SysUser, filename: str, data: bytes) -> ImportBatch:
    parsed = parse_workbook(data, book_id, db)
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
        except ValueError as exc:
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
    batch.committed_at = datetime.now()
    batch.committed_by = user.username
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
    db.commit()
