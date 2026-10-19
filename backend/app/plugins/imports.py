"""宿主导入批次：源文件 -> 持久预览 -> 明确确认 -> 原子提交。

调用者身份始终来自 ASGI 上下文。每批只写一本账；SELECT FOR UPDATE 串行化
修订和提交。SUBMITTING 只存在于提交事务内，进程中断会回滚到 CONFIRMED。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import exists, func, or_, select, text, update
from sqlalchemy.orm import defer
from app.accounting import vouchers
from app.accounting.audit_helper import log
from app.api.schemas import VoucherIn
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.deps import get_my_book, require_book_write
from app.models.plugin import PluginRegistry
from app.models.plugin_import import PluginImportBatch as Batch, PluginImportFile as Source, PluginVoucherOrigin as Origin
from app.plugins.uploads import read_file
from app.plugins.configuration import validate_snapshot

FINAL = {"SUBMITTED", "CANCELLED"}
EDITABLE = {"UPLOADED", "PREVIEW_READY", "EDITING", "CONFIRMED", "FAILED"}


def now():
    return datetime.now(timezone.utc)


def canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HTTPException(422, "预览或配置必须是合法 JSON") from exc
    if len(raw) > settings.import_max_bytes:
        raise HTTPException(413, "预览或配置超过导入内容上限")
    return hashlib.sha256(raw).hexdigest()


def iso(value):
    return value.isoformat() if value else None


def serialize(db, batch, *, detail=True):
    files = db.scalars(select(Source).where(Source.batch_id == batch.id).order_by(Source.id)).all()
    value = {"id": batch.id, "plugin_id": batch.plugin_id, "book_id": batch.book_id,
             "user_id": batch.user_id, "username": batch.username,
             "plugin_version": batch.plugin_version, "plugin_digest": batch.plugin_digest,
             "fingerprint": batch.fingerprint, "status": batch.status, "revision": batch.revision,
             "preview_digest": batch.preview_digest, "configuration_digest": batch.configuration_digest,
             "confirmed_at": iso(batch.confirmed_at), "submitted_at": iso(batch.submitted_at),
             "created_at": iso(batch.created_at), "expires_at": iso(batch.expires_at),
             "validation_errors": batch.validation_errors,
             "request_id": batch.request_id,
             "files": [{"id": f.id, "filename": f.filename, "content_type": f.content_type,
                        "size": f.size, "sha256": f.sha256, "expires_at": iso(f.expires_at)} for f in files]}
    if detail:
        value.update(preview=batch.preview, original_preview=batch.original_preview,
                     configuration=batch.configuration, result=batch.result)
    return value


def check_revision(batch, revision):
    if type(revision) is not int or revision != batch.revision:
        raise HTTPException(409, {"code": "IMPORT_REVISION_CONFLICT", "message": "批次已变化，请重新读取预览", "revision": batch.revision})


def check_current_version(db, batch):
    plugin = db.get(PluginRegistry, batch.plugin_id)
    if plugin.plugin_digest != batch.plugin_digest:
        raise HTTPException(409, {"code": "IMPORT_PLUGIN_CHANGED", "message": "插件已升级，请用当前版本重新解析并预览"})


def validate_payloads(db, book_id, payloads):
    if not isinstance(payloads, list) or not payloads:
        raise HTTPException(422, "预览至少需要一张凭证")
    if len(payloads) > settings.import_max_vouchers:
        raise HTTPException(413, f"每批最多 {settings.import_max_vouchers} 张凭证")
    if sum(len(p.get("entries", [])) for p in payloads if isinstance(p, dict) and isinstance(p.get("entries", []), list)) > settings.import_max_rows:
        raise HTTPException(413, f"每批最多 {settings.import_max_rows} 条分录")
    canonical(payloads)
    normalized, errors = [], []
    # 保存原始错误行，允许用户在插件自己的页面里修正，而不是丢掉整份预览。
    for position, raw in enumerate(payloads, 1):
        try:
            data = VoucherIn.model_validate(raw)
            if data.attachments < 0:
                raise ValueError("附单据数不能为负数")
            if data.voucher_no is not None or data.insert_after is not None:
                raise ValueError("插件批次使用宿主自动编号，不能指定凭证号或插入位置")
            entries = [entry.model_dump() for entry in data.entries]
            vouchers.validate_new_voucher(db, book_id=book_id, voucher_date=data.voucher_date, entries=entries)
            normalized.append(data.model_copy(update={"source": "IMPORT"}).model_dump(mode="json"))
        except ValidationError as exc:
            for problem in exc.errors()[:20]:
                loc = list(problem["loc"])
                errors.append({"voucher": position, "line": loc[1] + 1 if len(loc) > 1 and loc[0] == "entries" and isinstance(loc[1], int) else None,
                               "field": ".".join(map(str, loc)), "message": problem["msg"]})
            normalized.append(raw)
        except (ValueError, ArithmeticError) as exc:
            errors.append({"voucher": position, "line": None, "field": "", "message": str(exc)})
            normalized.append(raw)
        if len(errors) >= 100:
            break
    # 不因错误数量上限而截断持久预览。
    normalized.extend(payloads[len(normalized):])
    return normalized, errors


def evidence(batch):
    return {"plugin_id": batch.plugin_id, "plugin_version": batch.plugin_version,
            "plugin_digest": batch.plugin_digest, "fingerprint": batch.fingerprint,
            "configuration_digest": batch.configuration_digest, "preview_digest": batch.preview_digest,
            "revision": batch.revision, "status": batch.status, "request_id": batch.request_id}


def service(manifest, principal):
    pid = manifest["id"]
    permissions = set(manifest["permissions"])

    def access(db, ident, *, lock=False, write=False):
        user = principal(db)
        stmt = select(Batch).where(Batch.id == ident, Batch.plugin_id == pid, Batch.user_id == user.id)
        batch = db.scalar(stmt.with_for_update() if lock else stmt)
        if batch is None:
            raise HTTPException(404, "导入批次不存在")
        get_my_book(batch.book_id, db, user)
        if write:
            require_book_write(batch.book_id, db, user)
        if batch.status not in FINAL and batch.expires_at <= now():
            raise HTTPException(410, "导入预览已过期，请取消后重新上传")
        return user, batch

    def record(db, user, batch, action, **extra):
        log(db, action="plugin.import." + action, entity="plugin_import_batch", entity_id=batch.id,
            user=user, book_id=batch.book_id, after={**evidence(batch), **extra})

    def create(book_id, files):
        # 先鉴权和授权，再读取任何上传内容；随后持有账套级配额锁检查去重与总量。
        with SessionLocal.begin() as db:
            user = principal(db)
            get_my_book(book_id, db, user)
            require_book_write(book_id, db, user)
            if not isinstance(files, list) or not 1 <= len(files) <= settings.plugin_import_max_files:
                raise HTTPException(400, f"每批需要 1 至 {settings.plugin_import_max_files} 个文件")
            sources, size = [], 0
            for file in files:
                item = read_file(file, limit=settings.import_max_bytes - size) if size < settings.import_max_bytes else None
                if item is None:
                    raise HTTPException(413, "整批文件合计超过上传上限")
                size += item["size"]
                sources.append(item)
            digests = sorted(item["sha256"] for item in sources)
            if len(set(digests)) != len(digests):
                raise HTTPException(400, "同一批中包含内容相同的文件，请移除重复文件")
            fingerprint = hashlib.sha256("\n".join(digests).encode()).hexdigest()
            # 配额按账套、跨插件计算；并发上传不能穿透检查。
            db.execute(text("SELECT pg_advisory_xact_lock(18740, :book_id)"), {"book_id": book_id})
            existing = db.scalar(select(Batch).where(Batch.plugin_id == pid, Batch.book_id == book_id,
                                                       Batch.fingerprint == fingerprint, Batch.status != "CANCELLED"))
            if existing:
                if existing.user_id != user.id:
                    raise HTTPException(409, {"code": "IMPORT_DUPLICATE", "message": "该账套已存在相同文件的导入批次，请联系原操作者处理"})
                return serialize(db, existing)
            overlap = db.scalar(select(Source.id).join(Batch, Batch.id == Source.batch_id).where(
                Batch.plugin_id == pid, Batch.book_id == book_id, Batch.status != "CANCELLED",
                Source.sha256.in_(digests)).limit(1))
            if overlap:
                raise HTTPException(409, {"code": "IMPORT_FILE_OVERLAP", "message": "本批部分文件已存在于该账套的其他导入批次，请移除重复文件"})
            pending = db.scalar(select(func.count()).select_from(Batch).where(Batch.user_id == user.id, Batch.book_id == book_id, Batch.status.notin_(FINAL)))
            if pending >= settings.plugin_import_max_pending:
                raise HTTPException(409, "待处理导入批次已达上限，请先完成或取消旧批次")
            stored = db.scalar(select(func.coalesce(func.sum(Source.size), 0)).join(Batch, Batch.id == Source.batch_id).where(
                Batch.book_id == book_id, Source.content.is_not(None)))
            if stored + size > settings.plugin_import_storage_max_bytes:
                raise HTTPException(413, "该账套的插件原文件留存容量已满，请管理员执行到期清理")
            plugin = db.get(PluginRegistry, pid)
            batch = Batch(id=uuid.uuid4().hex, plugin_id=pid, book_id=book_id, user_id=user.id, username=user.username,
                          plugin_version=plugin.version, plugin_digest=plugin.plugin_digest, fingerprint=fingerprint,
                          expires_at=now() + timedelta(days=settings.import_batch_ttl_days))
            db.add(batch)
            db.flush()
            for item in sources:
                db.add(Source(id=uuid.uuid4().hex, batch_id=batch.id, expires_at=batch.expires_at, **item))
            db.flush()
            record(db, user, batch, "uploaded", files=[{k: f[k] for k in ("filename", "size", "sha256")} for f in sources])
            return serialize(db, batch)

    def read(ident):
        with SessionLocal() as db:
            _, batch = access(db, ident)
            return serialize(db, batch)

    def list_batches(book_id, *, before=None, limit=50):
        with SessionLocal() as db:
            user = principal(db)
            get_my_book(book_id, db, user)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise HTTPException(400, "批次分页数量需要为 1 至 100")
            stmt = select(Batch).options(defer(Batch.preview), defer(Batch.original_preview), defer(Batch.configuration), defer(Batch.result)).where(Batch.plugin_id == pid, Batch.book_id == book_id, Batch.user_id == user.id)
            if before is not None:
                anchor = db.scalar(select(Batch).where(Batch.id == before, Batch.plugin_id == pid, Batch.book_id == book_id, Batch.user_id == user.id))
                if anchor is None:
                    raise HTTPException(400, "批次分页游标无效")
                from sqlalchemy import tuple_
                stmt = stmt.where(tuple_(Batch.created_at, Batch.id) < (anchor.created_at, anchor.id))
            rows = db.scalars(stmt.order_by(Batch.created_at.desc(), Batch.id.desc()).limit(limit + 1)).all()
            return {"items": [serialize(db, b, detail=False) for b in rows[:limit]],
                    "next": rows[limit - 1].id if len(rows) > limit else None}

    def source(ident, file_id):
        with SessionLocal() as db:
            access(db, ident)
            file = db.scalar(select(Source).where(Source.id == file_id, Source.batch_id == ident))
            if file is None:
                raise HTTPException(404, "源文件不存在")
            if file.expires_at <= now() or file.content is None:
                raise HTTPException(410, "源文件留存期已结束，摘要仍保留")
            return {"id": file.id, "filename": file.filename, "content_type": file.content_type,
                    "size": file.size, "sha256": file.sha256, "content": file.content}

    def save_preview(ident, payloads, *, revision, configuration=None, parsed):
        with SessionLocal.begin() as db:
            user, batch = access(db, ident, lock=True, write=True)
            check_revision(batch, revision)
            if batch.status not in EDITABLE:
                raise HTTPException(409, "该批次不能修改预览")
            if configuration is not None and not isinstance(configuration, dict):
                raise HTTPException(422, "配置快照需要是 JSON 对象")
            if not parsed:
                if batch.preview is None:
                    raise HTTPException(409, "请先调用插件解析接口生成预览")
                check_current_version(db, batch)
            normalized, errors = validate_payloads(db, batch.book_id, payloads)
            first = batch.original_preview is None
            if first:
                batch.original_preview = normalized
            batch.preview = normalized
            batch.preview_digest = canonical(normalized)
            if configuration is not None:
                batch.configuration = configuration
            validate_snapshot(db, manifest, batch.book_id, batch.configuration)
            batch.configuration_digest = canonical(batch.configuration)
            plugin = db.get(PluginRegistry, pid)
            if parsed:
                batch.plugin_version, batch.plugin_digest = plugin.version, plugin.plugin_digest
            batch.status = "PREVIEW_READY" if first and not errors else "EDITING"
            batch.revision += 1
            batch.validation_errors, batch.confirmed_at = errors, None
            record(db, user, batch, "preview", error_count=len(errors))
            return serialize(db, batch)

    def preview(ident, payloads, *, revision, configuration=None):
        return save_preview(ident, payloads, revision=revision, configuration=configuration, parsed=True)

    def edit(ident, payloads, *, revision):
        return save_preview(ident, payloads, revision=revision, parsed=False)

    def confirm(ident, *, revision):
        with SessionLocal.begin() as db:
            user, batch = access(db, ident, lock=True, write=True)
            check_revision(batch, revision)
            check_current_version(db, batch)
            validate_snapshot(db, manifest, batch.book_id, batch.configuration)
            if batch.status not in {"PREVIEW_READY", "EDITING", "CONFIRMED"}:
                raise HTTPException(409, "请先生成并检查预览")
            _, errors = validate_payloads(db, batch.book_id, batch.preview)
            if errors:
                raise HTTPException(422, {"code": "IMPORT_INVALID_PREVIEW", "errors": errors})
            if batch.status != "CONFIRMED":
                batch.status, batch.confirmed_at = "CONFIRMED", now()
                batch.revision += 1
                record(db, user, batch, "confirmed")
            return serialize(db, batch)

    def submit(ident, *, revision, request_id=""):
        if "voucher:write" not in permissions:
            raise HTTPException(403, "插件未声明 voucher:write 能力")
        error = None
        with SessionLocal.begin() as db:
            user, batch = access(db, ident, lock=True, write=True)
            if batch.status == "SUBMITTED":
                return serialize(db, batch)
            check_revision(batch, revision)
            check_current_version(db, batch)
            validate_snapshot(db, manifest, batch.book_id, batch.configuration)
            if batch.status != "CONFIRMED" or not batch.confirmed_at:
                raise HTTPException(409, "必须先由用户确认当前预览")
            if canonical(batch.preview) != batch.preview_digest:
                raise HTTPException(409, "预览摘要不一致，请重新生成并确认")
            batch.request_id = str(request_id or "")[:128]
            batch.status = "SUBMITTING"
            db.flush()
            try:
                # SAVEPOINT 使账务失败可回滚整批，外层事务仍能保存 FAILED 和错误。
                with db.begin_nested():
                    made = []
                    for position, raw in enumerate(batch.preview, 1):
                        data = VoucherIn.model_validate(raw)
                        voucher = vouchers.create_voucher(db, book_id=batch.book_id, user=user,
                            voucher_date=data.voucher_date, entries=[e.model_dump() for e in data.entries],
                            remark=data.remark, attachments=data.attachments, source="IMPORT", commit=False)
                        db.add(Origin(batch_id=batch.id, book_id=batch.book_id, voucher_id=voucher.id,
                                      original_voucher_id=voucher.id, voucher_no=voucher.voucher_no, position=position))
                        made.append({"id": voucher.id, "voucher_no": voucher.voucher_no,
                                     "period_id": voucher.period_id, "voucher_date": voucher.voucher_date.isoformat(), "position": position})
                    db.flush()
            except (ValueError, ArithmeticError) as exc:
                error = {"code": "IMPORT_SUBMIT_FAILED", "voucher": position, "message": str(exc), "request_id": batch.request_id}
                batch.status, batch.validation_errors = "FAILED", [error]
                record(db, user, batch, "failed", error=error)
            else:
                batch.result, batch.status, batch.submitted_at = made, "SUBMITTED", now()
                batch.expires_at = now() + timedelta(days=settings.plugin_import_retention_days)
                db.execute(update(Source).where(Source.batch_id == batch.id).values(expires_at=batch.expires_at))
                batch.validation_errors = []
                record(db, user, batch, "submitted", vouchers=made)
            batch.revision += 1
            db.flush()
            result = serialize(db, batch)
        if error:
            raise HTTPException(422, error)
        return result

    def cancel(ident, *, revision):
        with SessionLocal.begin() as db:
            # 过期批次也允许取消和释放去重身份。
            user = principal(db)
            batch = db.scalar(select(Batch).where(Batch.id == ident, Batch.plugin_id == pid, Batch.user_id == user.id).with_for_update())
            if batch is None:
                raise HTTPException(404, "导入批次不存在")
            get_my_book(batch.book_id, db, user)
            require_book_write(batch.book_id, db, user)
            check_revision(batch, revision)
            if batch.status == "SUBMITTED":
                raise HTTPException(409, "已提交批次不能取消；请通过宿主凭证流程处理")
            batch.status, batch.preview, batch.original_preview = "CANCELLED", None, None
            batch.configuration, batch.validation_errors = {}, []
            batch.revision += 1
            db.execute(update(Source).where(Source.batch_id == ident).values(content=None, expires_at=now()))
            record(db, user, batch, "cancelled")
            return serialize(db, batch)

    def origin(voucher_id):
        with SessionLocal() as db:
            user = principal(db)
            row = db.scalar(select(Origin).join(Batch, Batch.id == Origin.batch_id).where(
                Origin.original_voucher_id == voucher_id, Batch.plugin_id == pid, Batch.user_id == user.id))
            if row is None:
                raise HTTPException(404, "凭证来源不存在")
            get_my_book(row.book_id, db, user)
            batch = db.get(Batch, row.batch_id)
            return {"batch": serialize(db, batch, detail=False), "position": row.position,
                    "voucher_id": row.voucher_id, "original_voucher_id": row.original_voucher_id}

    def limits():
        with SessionLocal() as db:
            principal(db)
        return {"max_files": settings.plugin_import_max_files, "max_bytes": settings.import_max_bytes,
                "max_rows": settings.import_max_rows, "max_vouchers": settings.import_max_vouchers,
                "draft_days": settings.import_batch_ttl_days, "retention_days": settings.plugin_import_retention_days}

    return SimpleNamespace(create=create, read=read, list=list_batches, source=source, limits=limits, origin=origin,
                           preview=preview, edit=edit, confirm=confirm, submit=submit, cancel=cancel)


def purge_expired(db, *, at=None, limit=500):
    """每次最多清理 limit 批；跳过正在编辑/提交的行，保留摘要与结果。"""
    at = at or now()
    retained_source = exists(select(Source.id).where(Source.batch_id == Batch.id, Source.content.is_not(None)))
    batches = db.scalars(select(Batch).options(defer(Batch.preview), defer(Batch.original_preview), defer(Batch.configuration)).where(
        Batch.expires_at <= at, or_(Batch.status.notin_(FINAL), Batch.preview.is_not(None),
            Batch.original_preview.is_not(None), retained_source)).order_by(Batch.expires_at, Batch.id)
        .limit(limit).with_for_update(skip_locked=True)).all()
    count = 0
    for batch in batches:
        if batch.status not in FINAL:
            batch.status = "CANCELLED"
            batch.revision += 1
        batch.preview = batch.original_preview = None
        batch.configuration = {}
        count += db.execute(update(Source).where(Source.batch_id == batch.id, Source.content.is_not(None)).values(content=None)).rowcount
        log(db, action="plugin.import.expired", entity="plugin_import_batch", entity_id=batch.id,
            book_id=batch.book_id, user=None, after=evidence(batch))
    return count
