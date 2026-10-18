"""窄 SDK：只返回序列化结果，复用宿主账套/角色/凭证/LedgerEngine 护栏。"""
from __future__ import annotations

from contextvars import ContextVar
from types import SimpleNamespace
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import MetaData, Table, select
from app.core.db import SessionLocal
from app.core.deps import get_my_book, require_book_write
from app.models.system import SysUser, SystemSetting
from app.plugins import audit
from app.plugins.access import require_plugin_access

# 只由宿主 ASGI 门禁设置，插件不能选择调用者。
current_principal = ContextVar("plugin_principal", default=None)


def context_for(manifest):
    pid = manifest["id"]
    permissions = set(manifest["permissions"])
    declarations = {t["name"]: t["scope"] for t in manifest.get("tables", [])}

    def principal(db):
        value = current_principal.get()
        if value is None or value[0] != pid:
            raise HTTPException(403, "插件能力只能在已鉴权请求内调用")
        user = db.get(SysUser, value[1])
        if not user or not user.active:
            raise HTTPException(401, "用户已停用")
        require_plugin_access(db, pid, user, generation=value[2])
        return user

    def book_read(book_id: int):
        with SessionLocal() as db:
            user = principal(db)
            book = get_my_book(book_id, db, user)
            return {"id": book.id, "name": book.name}

    def book_write(book_id: int):
        with SessionLocal() as db:
            user = principal(db)
            book = get_my_book(book_id, db, user)
            require_book_write(book_id, db, user)
            return {"id": book.id, "name": book.name}

    def ledger_read(book_id: int, period_id: int):
        from app.api.ledger import balance
        with SessionLocal() as db:
            user = principal(db)
            book = get_my_book(book_id, db, user)
            return balance(book_id=book_id, period_id=period_id, from_period_id=None,
                           book=book, db=db, include_unposted=False)

    def voucher_create(book_id: int, payload: dict):
        from app.api.schemas import VoucherIn
        from app.api.vouchers import create
        with SessionLocal() as db:
            user = principal(db)
            book = get_my_book(book_id, db, user)
            link = require_book_write(book_id, db, user)
            try:
                data = VoucherIn.model_validate(payload)
            except ValidationError as exc:
                raise HTTPException(422, "插件凭证输入不符合宿主校验规则") from exc
            return create(payload=data, book=book, db=db, _link=link, user=user)

    def table_access(db, user, name, book_id, write=False):
        if name not in declarations:
            raise HTTPException(403, "只能访问当前插件声明的数据表")
        scope = declarations[name]
        if scope == "book":
            if type(book_id) is not int:
                raise HTTPException(400, "账套表需要 book_id")
            get_my_book(book_id, db, user)
            if write:
                require_book_write(book_id, db, user)
        elif book_id is not None:
            raise HTTPException(400, "全局表不能指定 book_id")
        elif write and not user.is_admin:
            raise HTTPException(403, "全局配置写入需要管理员")
        table = Table(name, MetaData(), autoload_with=db.connection(), schema="public")
        return table

    def data_read(name: str, *, book_id=None, limit=100):
        with SessionLocal() as db:
            user = principal(db)
            table = table_access(db, user, name, book_id)
            stmt = select(table)
            if declarations[name] == "book":
                stmt = stmt.where(table.c.book_id == book_id)
            stmt = stmt.order_by(*table.primary_key.columns).limit(max(1, min(int(limit), 500)))
            return [dict(row) for row in db.execute(stmt).mappings()]

    def data_insert(name: str, values: dict, *, book_id=None):
        with SessionLocal.begin() as db:
            user = principal(db)
            table = table_access(db, user, name, book_id, True)
            if not isinstance(values, dict) or set(values) - set(table.c.keys()) or ("book_id" in values and values["book_id"] != book_id):
                raise HTTPException(400, "插件数据字段或账套不合法")
            record = dict(values)
            if declarations[name] == "book":
                record["book_id"] = book_id
            return dict(db.execute(table.insert().values(**record).returning(*table.c)).mappings().one())

    def data_update(name: str, ident: int, values: dict, *, book_id=None):
        with SessionLocal.begin() as db:
            user = principal(db)
            table = table_access(db, user, name, book_id, True)
            if "id" not in table.c or not isinstance(values, dict) or not values or set(values) - set(table.c.keys()) or set(values) & {"id", "book_id"}:
                raise HTTPException(400, "插件更新字段不合法，不能修改主键或账套")
            stmt = table.update().where(table.c.id == ident)
            if declarations[name] == "book":
                stmt = stmt.where(table.c.book_id == book_id)
            return {"updated": db.execute(stmt.values(**values)).rowcount}

    def data_delete(name: str, ident: int, *, book_id=None):
        with SessionLocal.begin() as db:
            user = principal(db)
            table = table_access(db, user, name, book_id, True)
            if "id" not in table.c:
                raise HTTPException(400, "此助手要求 id 主键")
            stmt = table.delete().where(table.c.id == ident)
            if declarations[name] == "book":
                stmt = stmt.where(table.c.book_id == book_id)
            return {"deleted": db.execute(stmt).rowcount}

    def audit_write(action: str):
        if not isinstance(action, str) or not action or len(action) > 32:
            raise HTTPException(400, "审计事件标识无效")
        with SessionLocal.begin() as db:
            user = principal(db)
            audit.event(db, "event", pid, user=user, code=action)

    def settings_key(key):
        from app.plugins.manifest import table_prefix
        full = f"plugin.{table_prefix(pid)[7:19]}.{key}"
        if not isinstance(key, str) or not key.isascii() or not key.replace("_", "").isalnum() or len(full) > 64:
            raise HTTPException(400, "插件设置 key 无效或过长")
        return full

    def setting_get(key: str):
        with SessionLocal() as db:
            principal(db)
            setting = db.get(SystemSetting, settings_key(key))
            return setting.value if setting else None

    def setting_put(key: str, value: str):
        with SessionLocal.begin() as db:
            user = principal(db)
            if not user.is_admin:
                raise HTTPException(403, "全局配置写入需要管理员")
            if not isinstance(value, str) or len(value) > 4096:
                raise HTTPException(400, "插件设置值过长")
            ident = settings_key(key)
            item = db.get(SystemSetting, ident)
            if item:
                item.value = value
            else:
                db.add(SystemSetting(key=ident, value=value))

    exposed = {"plugin_id": pid}
    for permission, key, fn in [("book:read", "book_read", book_read), ("book:write", "book_write", book_write),
        ("ledger:read", "ledger_read", ledger_read), ("voucher:write", "voucher_create", voucher_create),
        ("audit:write", "audit_write", audit_write)]:
        if permission in permissions:
            exposed[key] = fn
    if "plugin_data:rw" in permissions:
        exposed["data"] = SimpleNamespace(read=data_read, insert=data_insert, update=data_update, delete=data_delete)
    if "settings:rw" in permissions:
        exposed["settings"] = SimpleNamespace(get=setting_get, put=setting_put)
    return SimpleNamespace(**exposed)
