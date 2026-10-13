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

容量边界（审计 P2-02）
---------------------
导入是这套系统里唯一「一个请求能把整个进程的内存吃光」的入口：
解析器把每一张凭证、每一条分录、每一条错误都累积进列表，
最后整包写进 ``import_batch.payload``。因此设了三道闸：

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

import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.accounting.util import money
from app.core.config import settings
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


def _parse_amount(value: Any) -> tuple[Decimal, str | None]:
    """把单元格解析成金额，返回 (金额, 错误说明)。

    原来这里是 ``except: return Decimal("0")`` —— 解析不了就当 0，
    不报错、不留痕。审计实测：一条含「非法金额行」的导入文件，
    5 条分录只入库 2 条，批次仍报 ok=true，错误列表**是空的**。
    用户以为凭证齐了，实际少记了金额，而且没有任何告警 ——
    这是财务软件里最容易造成实际损失的一类缺陷。

    所以规则改成：**解析不了必须说出来**。金额按 0 参与后续计算只是为了让
    流程继续走完，真正的处置权在调用方 —— 它必须把这条错误挂到凭证上。
    """
    if value in (None, ""):
        return Decimal("0"), None
    if isinstance(value, (int, float, Decimal)):
        try:
            return money(value), None
        except Exception as exc:
            return Decimal("0"), f"{value!r} 不是合法金额（{exc}）"
    text = str(value).strip().replace(",", "").replace("，", "")
    text = text.replace("¥", "").replace("￥", "").strip()
    if text.endswith("%"):
        # 原来直接返回 0。金额列写百分比，99% 的可能是从别处粘串了整列。
        return Decimal("0"), f"{text!r} 是百分比，金额列不该出现百分号"
    try:
        return money(text), None
    except Exception:
        return Decimal("0"), f"{text!r} 无法解析为金额"


def _as_decimal(value: Any) -> Decimal:
    """宽松版：只用于「有就用、没有就 0」的场合（如附单据数）。

    **不要**用它解析借贷方金额 —— 那样会把解析失败悄悄吞掉。
    金额一律走 :func:`_parse_amount`。
    """
    amount, _err = _parse_amount(value)
    return amount


def _subject_index(db: Session, book_id: int) -> tuple[dict[str, Subject], dict[str, Subject]]:
    """返回 (编码索引, 名称索引)。末级和上级都收进来：
    匹配到上级科目时才能说清「不是明细科目」，而不是含糊地报一句「找不到科目」——
    「1002 银行存款」在这个账套里明明存在，只是不能往它身上记。"""
    rows = db.scalars(
        select(Subject).where(Subject.book_id == book_id, Subject.active.is_(True))
    ).all()
    by_code = {s.code.strip(): s for s in rows}
    by_name: dict[str, Subject] = {}
    for s in rows:
        by_name.setdefault(s.name.strip(), s)
    return by_code, by_name


# --------------------------------------------------------------------- 容量边界


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}B"


def check_archive_size(data: bytes) -> None:
    """按 zip 目录里**声明的**解压大小预检，不真正解压（审计 P2-02）。

    xlsx 是 zip。一份几百 KB 的文件完全可以解压出几百 MB 的 XML ——
    「上传大小没超限」在这条路上不构成任何保护。这里读 zip 的中央目录
    （``ZipFile`` 只解析目录，不读文件内容）把各成员的 file_size 加起来，
    超了就拒。

    只能当**第一道**闸：中央目录里的 size 是可以随便写的。真正的兜底是
    解析时逐行计数（:func:`parse_workbook` 里的行数与凭证数上限）。

    不是 zip 就直接放行 —— 解析器稍后会自己报「无法读取 Excel 文件」。
    """
    limit = settings.import_max_unzipped_bytes
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = sum(info.file_size for info in zf.infolist())
    except (zipfile.BadZipFile, OSError):
        return  # 不是 zip（或已经坏到读不了目录）：交给解析器报错
    if total > limit:
        raise ValueError(
            f"Excel 解压后约 {_human(total)}，超过上限 {_human(limit)}。"
            "请拆成多份分别导入，或删除与本表无关的工作表后重试")


# --------------------------------------------------------------------- 解析


def parse_workbook(data: bytes, book_id: int, db: Session) -> dict:
    """解析 xlsx 字节流，返回可 JSON 化的暂存包。不写库。"""
    from openpyxl import load_workbook

    check_archive_size(data)
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
        elif dr == 0 and cr == 0:
            # 只挡「全零」，不挡负数：红字凭证（-300 / -300）是冲销，
            # 照样是平的、照样该存 —— 冲掉一张记错的凭证正是记账的日常。
            current["errors"].append("借贷金额相抵为 0，这张凭证什么也没记")
        current["ok"] = not current["errors"]
        vouchers.append(current)
        current = None
        # 凭证数上限：行数上限挡不住「一张凭证几十分录」的放大 ——
        # 行数合规但凭证条数失控时，累积的列表依然能吃掉大量内存。
        if len(vouchers) > settings.import_max_vouchers:
            raise ValueError(
                f"这份表解析出超过 {settings.import_max_vouchers} 张凭证，超过单次导入上限。"
                "请拆成多份分别导入")

    for row in rows_iter:
        line_no += 1
        # 行数与凭证数的上限在**遍历过程中**判，而不是先 count 一遍：
        # 先 count 照样会把整个工作表读进内存，那正是要防的事。
        # 触发上限时立刻中止解析并抛错，调用方回滚，暂存表里不留残骸。
        if line_no > settings.import_max_rows:
            raise ValueError(
                f"这份表超过 {settings.import_max_rows} 行，超过单次导入上限。"
                "请按期间或按凭证类别拆成多份分别导入")
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
            elif not subject.is_leaf:
                # 表里写的是上级科目。它存在，只是不能往它身上记：
                # 说清楚是哪一行、哪个科目，人改表还是改科目决定权都在自己手上
                unknown_subjects[key] = "不是明细科目，凭证要记在明细级"
                current["errors"].append(
                    f"第 {line_no} 行：{subject.code} {subject.name} 不是明细科目，凭证要记在明细级")
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
            # 单金额 + 方向列的老表格式
            raw_amt = col("amount", row)
            amt, amt_err = _parse_amount(raw_amt)
            if amt_err:
                current["errors"].append(f"第 {line_no} 行：{amt_err}")
            d = str(col("direction", row) or "").strip()
            if amt == 0:
                pass                      # 金额 0：不生成行（错误若已记过就够了）
            elif d in ("借", "DEBIT", "D"):
                direction, amount, amount_raw = "DEBIT", amt, str(raw_amt)
            elif d in ("贷", "CREDIT", "C"):
                direction, amount, amount_raw = "CREDIT", amt, str(raw_amt)
            else:
                current["errors"].append(
                    f"第 {line_no} 行：金额 {raw_amt!r} 无法判断方向，"
                    f"方向列填「{d or '空'}」，只认 借/贷 或 DEBIT/CREDIT")

        if direction is None:
            # 这一行没有金额：表头、空行、说明文字都走这条路，不该报错。
            # 真正的非法金额（乱码、百分比）已经在上面记进 errors 了。
            continue
        if subject is None:
            # 找不到科目 / 不是明细科目：原因上面已经记过，这里只补「压根没填科目」
            if subject_raw in (None, ""):
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
    }
    if dropped:
        summary["errors_truncated"] = dropped
    return summary


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
