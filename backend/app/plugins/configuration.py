"""标准 book-scoped 配置契约，数据保存在插件自己声明的表里。"""
import hashlib
import json
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import Integer, JSON, MetaData, Table, UniqueConstraint, select, text
from app.accounting.audit_helper import log
from app.core.db import SessionLocal
from app.core.config import settings
from app.core.deps import get_my_book, require_book_write


def digest(value, *, limit=65536):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HTTPException(422, "配置必须是合法 JSON") from exc
    if len(raw) > limit:
        raise HTTPException(413, f"插件配置内容超过 {limit} 字节上限")
    return hashlib.sha256(raw).hexdigest()


def config_table(db, manifest, name):
    if "plugin_data:rw" not in manifest["permissions"] or not any(t["name"] == name and t["scope"] == "book" for t in manifest.get("tables", [])):
        raise HTTPException(403, "配置表必须是当前插件声明的 book-scoped 表")
    table = Table(name, MetaData(), autoload_with=db.connection(), schema="public")
    needed = {"id", "book_id", "key", "value", "version"}
    unique = any(isinstance(c, UniqueConstraint) and {x.name for x in c.columns} == {"book_id", "key"} for c in table.constraints)
    unique = unique or any(i.unique and {x.name for x in i.columns} == {"book_id", "key"} for i in table.indexes)
    if not needed <= set(table.c.keys()) or not unique or not isinstance(table.c.value.type, JSON) or not isinstance(table.c.version.type, Integer):
        raise HTTPException(400, "配置表需要 id/book_id/key/value(JSONB)/version(Integer)，以及 (book_id,key) 唯一约束")
    return table


def snapshot(db, manifest, name, key, book_id):
    if not isinstance(key, str) or not 1 <= len(key) <= 100:
        raise HTTPException(400, "配置 key 长度需要为 1 至 100")
    table = config_table(db, manifest, name)
    row = db.execute(select(table).where(table.c.book_id == book_id, table.c.key == key)).mappings().first()
    value = row["value"] if row else {}
    return {"table": name, "key": key, "version": row["version"] if row else 0,
            "value": value, "sha256": digest(value)}


def validate_snapshot(db, manifest, book_id, configuration):
    """校验解析时读取的配置版本，防止读旧配置、按新配置确认的竞态。"""
    if not isinstance(configuration, dict):
        raise HTTPException(422, "配置快照需要是 JSON 对象")
    digest(configuration, limit=settings.import_max_bytes)
    refs = configuration.get("refs", [])
    if not isinstance(refs, list) or len(refs) > 32:
        raise HTTPException(422, "配置快照 refs 最多包含 32 个配置引用")
    if refs:
        # 持有到外层批次事务提交；与配置写入共用账套锁。
        db.execute(text("SELECT pg_advisory_xact_lock(18740, :book_id)"), {"book_id": book_id})
    for ref in refs:
        if not isinstance(ref, dict) or not {"table", "key", "version", "sha256"} <= set(ref) or not isinstance(ref["table"], str) or type(ref["version"]) is not int or ref["version"] < 0 or not isinstance(ref["sha256"], str):
            raise HTTPException(422, "配置引用需要 table/key/version/sha256")
        current = snapshot(db, manifest, ref["table"], ref["key"], book_id)
        if current["version"] != ref["version"] or current["sha256"] != ref["sha256"]:
            raise HTTPException(409, {"code": "IMPORT_CONFIGURATION_CHANGED", "message": "账套配置已修改，请重新解析、预览并确认"})
        if "value" in ref and digest(ref["value"]) != current["sha256"]:
            raise HTTPException(409, "配置快照内容与摘要不一致")
    return configuration


def service(manifest, principal):
    def get(name, key, *, book_id):
        with SessionLocal() as db:
            user = principal(db)
            get_my_book(book_id, db, user)
            return snapshot(db, manifest, name, key, book_id)

    def put(name, key, value, *, book_id, expected_version):
        if type(expected_version) is not int or expected_version < 0 or not isinstance(value, dict):
            raise HTTPException(422, "配置需要对象 value 和非负整数 expected_version")
        sha = digest(value)
        with SessionLocal.begin() as db:
            user = principal(db)
            get_my_book(book_id, db, user)
            require_book_write(book_id, db, user)
            table = config_table(db, manifest, name)
            # 与首次插入并发也互斥；不能只锁一条尚不存在的配置行。
            db.execute(text("SELECT pg_advisory_xact_lock(18740, :book_id)"), {"book_id": book_id})
            previous = snapshot(db, manifest, name, key, book_id)
            if previous["version"] != expected_version:
                raise HTTPException(409, {"code": "CONFIG_REVISION_CONFLICT", "message": "配置已被修改，请重新读取", "version": previous["version"]})
            version = expected_version + 1
            if expected_version:
                db.execute(table.update().where(table.c.book_id == book_id, table.c.key == key).values(value=value, version=version))
            else:
                db.execute(table.insert().values(book_id=book_id, key=key, value=value, version=version))
            log(db, action="plugin.config.updated", entity="plugin_config", entity_id=hashlib.sha256((name + ":" + key).encode()).hexdigest(),
                user=user, book_id=book_id, before={"sha256": previous["sha256"], "version": expected_version},
                after={"plugin_id": manifest["id"], "table": name, "key": key, "sha256": sha, "version": version})
            return {"table": name, "key": key, "version": version, "value": value, "sha256": sha}

    return SimpleNamespace(get=get, put=put)
