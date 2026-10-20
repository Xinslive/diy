"""窄 SDK：只返回序列化结果，复用宿主账套/角色/凭证/LedgerEngine 护栏。"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import MetaData, Table, func, select
from app.core.db import SessionLocal
from app.core.deps import WRITE_ROLES, get_my_book, require_book_write
from app.models.book import Book
from app.models.system import SysUser, SystemSetting
from app.plugins import audit
from app.plugins.access import require_plugin_access

# 只由宿主 ASGI 门禁设置，插件不能选择调用者。
current_principal = ContextVar("plugin_principal", default=None)
current_request_id = ContextVar("plugin_request_id", default="")


def request_user(db, pid):
    value = current_principal.get()
    if value is None or value[0] != pid:
        raise HTTPException(403, "插件能力只能在已鉴权请求内调用")
    user = db.get(SysUser, value[1])
    if not user or not user.active:
        raise HTTPException(401, "用户已停用")
    require_plugin_access(db, pid, user, generation=value[2])
    return user


#: 受限过滤 DSL 支持的操作符。刻意没有「原始表达式」这一类入口。
FILTER_OPERATORS = ("eq", "ne", "lt", "lte", "gt", "gte", "in", "like", "is_null")
#: 过滤值允许的标量类型：插件只能按值过滤，不能拼 SQL。
FILTER_SCALARS = (str, int, float, bool, Decimal, datetime, date)


def _filter_scalar(value):
    if value is None or isinstance(value, FILTER_SCALARS):
        return value
    raise HTTPException(400, "插件过滤值只能是标量")


def _filter_clauses(table, where):
    """把 ``where`` 翻译成 SQLAlchemy 条件。

    字段必须是本表真实列，值只能是标量；账套范围由调用方另行附加，
    过滤条件**改不掉**它。
    """
    if where is None:
        return []
    if not isinstance(where, dict):
        raise HTTPException(400, "插件过滤条件必须是对象")
    clauses = []
    for field, condition in where.items():
        if not isinstance(field, str) or field not in table.c:
            raise HTTPException(400, "插件过滤字段不存在")
        column = table.c[field]
        if isinstance(condition, dict):
            if not condition:
                raise HTTPException(400, "插件过滤条件不能为空")
            for operator, operand in condition.items():
                if operator not in FILTER_OPERATORS:
                    raise HTTPException(400, "插件过滤操作符不受支持")
                if operator == "in":
                    if not isinstance(operand, (list, tuple)) or not operand or len(operand) > 200:
                        raise HTTPException(400, "插件过滤集合无效")
                    clauses.append(column.in_([_filter_scalar(item) for item in operand]))
                elif operator == "like":
                    if not isinstance(operand, str) or len(operand) > 200:
                        raise HTTPException(400, "插件模糊匹配条件无效")
                    clauses.append(column.like(operand))
                elif operator == "is_null":
                    if not isinstance(operand, bool):
                        raise HTTPException(400, "插件空值判断条件无效")
                    clauses.append(column.is_(None) if operand else column.isnot(None))
                elif operator == "ne":
                    clauses.append(column != _filter_scalar(operand))
                elif operator == "lt":
                    clauses.append(column < _filter_scalar(operand))
                elif operator == "lte":
                    clauses.append(column <= _filter_scalar(operand))
                elif operator == "gt":
                    clauses.append(column > _filter_scalar(operand))
                elif operator == "gte":
                    clauses.append(column >= _filter_scalar(operand))
                else:
                    clauses.append(column == _filter_scalar(operand))
        else:
            clauses.append(column == _filter_scalar(condition))
    return clauses


def _order_clauses(table, order_by):
    if order_by is None:
        return list(table.primary_key.columns)
    if isinstance(order_by, str):
        order_by = [order_by]
    if not isinstance(order_by, (list, tuple)) or not order_by:
        raise HTTPException(400, "插件排序字段无效")
    columns = []
    for item in order_by:
        if not isinstance(item, str):
            raise HTTPException(400, "插件排序字段无效")
        field, _, direction = item.strip().partition(" ")
        if field not in table.c:
            raise HTTPException(400, "插件排序字段不存在")
        direction = (direction or "asc").strip().lower()
        if direction not in {"asc", "desc"}:
            raise HTTPException(400, "插件排序方向无效")
        columns.append(table.c[field].desc() if direction == "desc" else table.c[field].asc())
    return columns



def context_for(manifest):
    pid = manifest["id"]
    permissions = set(manifest["permissions"])
    declarations = {t["name"]: t["scope"] for t in manifest.get("tables", [])}

    def principal(db):
        return request_user(db, pid)

    def book_read(book_id: int):
        with SessionLocal() as db:
            user = principal(db)
            book = get_my_book(book_id, db, user)
            return {"id": book.id, "name": book.name}

    def book_list():
        """返回当前用户可访问的账套，附带能否写入的明确结果。

        插件是用户级授权，不绑定某一本账。文件导入插件需要在自己的页面
        让用户选择目标账套，因此不能只依赖宿主顶部当前账套。列表只来自
        当前用户的 ``UserBook`` 授权，``can_write`` 仅是提示；提交时仍由
        ``book_write`` 或凭证写入助手重新做实时校验。
        """
        from app.models.system import UserBook
        with SessionLocal() as db:
            user = principal(db)
            rows = db.execute(
                select(Book, UserBook.role)
                .join(UserBook, UserBook.book_id == Book.id)
                .where(UserBook.user_id == user.id)
                .order_by(UserBook.sort_order.asc().nulls_last(), Book.company_name, Book.name)
            ).all()
            return [
                {"id": book.id, "name": book.name, "company_name": book.company_name,
                 "status": book.status, "role": role,
                 "can_write": (role or "").strip().upper() in WRITE_ROLES}
                for book, role in rows
            ]

    def book_write(book_id: int):
        with SessionLocal() as db:
            user = principal(db)
            book = get_my_book(book_id, db, user)
            require_book_write(book_id, db, user)
            return {"id": book.id, "name": book.name}

    def upload_read(file, *, limit=None):
        """受宿主容量闸保护地读取插件路由收到的 UploadFile。

        插件可以决定更小的单次上限，但不能放宽宿主 ``import_max_bytes``。
        原始字节只在当前请求内返回给插件；持久化、去重和批次关联属于后续
        导入批次能力，不由这个低层读取助手偷偷完成。
        """
        from app.plugins.uploads import read_file

        with SessionLocal() as db:
            principal(db)
        return read_file(file, limit=limit)

    def ledger_read(book_id: int, period_id: int):
        from app.api.ledger import balance
        with SessionLocal() as db:
            user = principal(db)
            book = get_my_book(book_id, db, user)
            return balance(book_id=book_id, period_id=period_id, from_period_id=None,
                           book=book, db=db, include_unposted=False)

    def subject_read(book_id: int):
        """当前账套的科目表。插件用它把文件里的科目**编码**换成凭证要的
        ``subject_id``。

        返回**全部**科目（末级与非末级、启用与停用都给），而不是像
        ``/subjects/flat`` 那样默认只给末级且启用的：插件要区分「这个编码根本
        不存在」「这个科目已停用」「这是个上级科目，不能记账」三种情况才能
        给人报出有用的错。只给可用集合的话，插件只能说「找不到」，用户拿到
        一句没有指向的错。筛选是插件自己的事，宿主不替它做这个判断。
        """
        from app.models.accounting import Subject
        with SessionLocal() as db:
            user = principal(db)
            book = get_my_book(book_id, db, user)
            rows = db.scalars(
                select(Subject)
                .where(Subject.book_id == book.id)
                .order_by(Subject.code)
            ).all()
            return [{"id": s.id, "code": s.code, "name": s.name, "level": s.level,
                     "category": s.category, "direction": s.direction,
                     "is_leaf": s.is_leaf, "active": s.active} for s in rows]

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

    def voucher_batch_create(book_id: int, payloads: list[dict]):
        """一次建多张凭证，**整批同一个事务**。

        单张 ``voucher_create`` 每次自己开一个 session 并提交，所以拿它循环
        500 行：一行失败时前 499 张**已经落库**了。「按文件批量生成凭证」正是
        这个形状 —— 失败留下半批凭证，重跑又因为没有水位而重复，用户要自己去
        账上把半批一张张删掉。这里对齐宿主导入那条路（``importer.commit``）：
        逐张 ``commit=False``，退出时才统一提交，任一张抛错整个事务回滚。

        错误信息带上**第几张**，否则插件只能回一句「失败了」，用户面对 500 行
        不知道该改哪一行。
        """
        from app.accounting import vouchers as svc
        from app.api.schemas import VoucherIn
        from app.api.vouchers import _book_subjects, _serialize
        from app.core.config import settings

        if not isinstance(payloads, list) or not payloads:
            raise HTTPException(400, "批量创建至少需要一张凭证")
        limit = settings.import_max_vouchers
        if len(payloads) > limit:
            raise HTTPException(400, f"一次最多创建 {limit} 张凭证，本次传了 {len(payloads)} 张。"
                                     "请拆成多批，每批成功后再提交下一批")
        with SessionLocal.begin() as db:
            user = principal(db)
            book = get_my_book(book_id, db, user)
            require_book_write(book_id, db, user)
            made = []
            for i, raw in enumerate(payloads, 1):
                try:
                    data = VoucherIn.model_validate(raw)
                except ValidationError as exc:
                    raise HTTPException(422, f"第 {i} 张凭证输入不符合宿主校验规则：{exc.errors()[0]['msg']}") from exc
                try:
                    made.append(svc.create_voucher(
                        db, book_id=book.id, user=user, voucher_date=data.voucher_date,
                        entries=[e.model_dump() for e in data.entries], remark=data.remark,
                        attachments=data.attachments, source=data.source,
                        voucher_no=data.voucher_no, insert_after_id=data.insert_after,
                        commit=False))
                except (ValueError, ArithmeticError) as e:
                    raise HTTPException(400, f"第 {i} 张凭证没建成，整批已回滚：{e}") from e
            # 科目一次取回给所有凭证复用（见 _book_subjects 的 docstring）。
            subs = _book_subjects(db, book.id)
            return [_serialize(db, v, subs) for v in made]

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

    def data_read(name: str, *, book_id=None, limit=100, offset=0, where=None, order_by=None):
        """读插件自有表的一页。

        旧契约只有「按主键顺序 + 最多 500 行」，长期运行的插件只能把整表读进
        内存自己过滤。这里补上**受限过滤**、**分页**和**计数**：

        - ``where``：``{"字段": 值}`` 或 ``{"字段": {"gte": 值}}``。字段必须是
          本表真实列，值只能是标量 —— 没有任何拼 SQL 的入口。
        - ``offset``/``limit``：分页；``limit`` 仍然夹在 1..500。
        - ``order_by``：``"字段"`` 或 ``"字段 desc"``，默认按主键。

        账套范围（book scope）永远由宿主附加，过滤条件改不掉它。
        返回值仍是行列表 —— 与旧契约一致，新增能力都是可选参数。
        """
        with SessionLocal() as db:
            user = principal(db)
            table = table_access(db, user, name, book_id)
            stmt = select(table)
            if declarations[name] == "book":
                stmt = stmt.where(table.c.book_id == book_id)
            stmt = stmt.where(*_filter_clauses(table, where))
            stmt = stmt.order_by(*_order_clauses(table, order_by))
            stmt = stmt.offset(max(0, int(offset))).limit(max(1, min(int(limit), 500)))
            return [dict(row) for row in db.execute(stmt).mappings()]

    def data_count(name: str, *, book_id=None, where=None):
        """符合条件的行数。

        与 :func:`data_read` 用同一套受限过滤条件；单独提供而不是把
        ``data.read`` 的返回值改成字典，是为了不破坏既有插件的调用契约。
        """
        with SessionLocal() as db:
            user = principal(db)
            table = table_access(db, user, name, book_id)
            stmt = select(func.count()).select_from(table)
            if declarations[name] == "book":
                stmt = stmt.where(table.c.book_id == book_id)
            stmt = stmt.where(*_filter_clauses(table, where))
            return int(db.scalar(stmt) or 0)

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
    for permission, key, fn in [("book:read", "book_read", book_read), ("book:read", "book_list", book_list),
        ("book:write", "book_write", book_write),
        ("upload:read", "upload_read", upload_read),
        ("ledger:read", "ledger_read", ledger_read), ("subject:read", "subject_read", subject_read),
        ("voucher:write", "voucher_create", voucher_create),
        ("audit:write", "audit_write", audit_write)]:
        if permission in permissions:
            exposed[key] = fn
    # 批量与单张同权限：批量只是把同一套校验放进一个事务，不额外给权。
    if "voucher:write" in permissions:
        exposed["voucher_batch_create"] = voucher_batch_create
    if "plugin_data:rw" in permissions:
        from app.plugins.configuration import service as configuration_service
        exposed["config"] = configuration_service(manifest, principal)
        exposed["data"] = SimpleNamespace(read=data_read, count=data_count, insert=data_insert,
                                          update=data_update, delete=data_delete)
    if "settings:rw" in permissions:
        exposed["settings"] = SimpleNamespace(get=setting_get, put=setting_put)
    if "import:rw" in permissions:
        from app.plugins.imports import service
        exposed["imports"] = service(manifest, principal)
    return SimpleNamespace(**exposed)
