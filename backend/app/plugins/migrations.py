"""独立事务迁移与存量库 metadata 核对；启动只使用 read_head/read_version。"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from sqlalchemy import MetaData, Table, inspect, text
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from app.plugins.errors import PluginError
from app.plugins.manifest import table_prefix, version_table


def read_head(directory: Path) -> str:
    """读取 revision 常量，不 import/执行迁移文件。首版只支持线性单 head。"""
    revisions, parents = {}, set()
    for path in sorted((directory / "migrations" / "versions").glob("*.py")):
        values = {}
        try:
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                            values[target.id] = ast.literal_eval(node.value)
            rev, parent = values["revision"], values.get("down_revision")
            if not isinstance(rev, str) or not 1 <= len(rev) <= 64 or rev in revisions or (parent is not None and not isinstance(parent, str)):
                raise ValueError
            revisions[rev] = parent
            if parent:
                parents.add(parent)
        except (KeyError, TypeError, ValueError, SyntaxError) as exc:
            raise PluginError("INVALID_MIGRATION", "插件迁移必须使用唯一、线性的 revision 常量") from exc
    if not revisions:
        return ""
    heads = set(revisions) - parents
    if len(heads) != 1 or not parents <= set(revisions) or sum(p is None for p in revisions.values()) != 1:
        raise PluginError("INVALID_MIGRATION", "插件迁移必须具有单一完整 revision 链")
    head = next(iter(heads))
    visited, current = set(), head
    while current is not None:
        if current in visited:
            raise PluginError("INVALID_MIGRATION", "插件迁移存在循环")
        visited.add(current)
        current = revisions[current]
    if visited != set(revisions):
        raise PluginError("INVALID_MIGRATION", "插件迁移存在不可达 revision")
    return head


def read_version(conn, plugin_id: str) -> str:
    name = version_table(plugin_id)
    if not inspect(conn).has_table(name, schema="public"):
        return ""
    rows = conn.execute(text(f'SELECT version_num FROM public."{name}"')).scalars().all()
    if len(rows) != 1:
        raise PluginError("MIGRATION_PENDING", "插件迁移版本未就绪", 503)
    return rows[0]


def _json(value):
    return json.loads(json.dumps(value, default=str))


def describe_table(conn, name: str, scope: str) -> dict:
    i = inspect(conn)
    columns = [{"name": c["name"], "type": str(c["type"].compile(dialect=conn.dialect)),
                "nullable": c["nullable"], "default": c.get("default"),
                "identity": c.get("identity"), "computed": c.get("computed")}
               for c in i.get_columns(name, schema="public")]
    fks = [{"name": f["name"], "columns": f["constrained_columns"],
            "schema": f.get("referred_schema") or "public", "table": f["referred_table"],
            "target_columns": f["referred_columns"], "options": f.get("options", {})}
           for f in i.get_foreign_keys(name, schema="public")]
    triggers = conn.execute(text("""SELECT t.tgname AS name, pg_get_triggerdef(t.oid) AS definition,
        pg_get_functiondef(t.tgfoid) AS function, pn.nspname AS function_schema,
        p.proname AS function_name, p.prosecdef AS security_definer, p.prosrc AS source FROM pg_trigger t
        JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace
        JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=:name AND NOT t.tgisinternal ORDER BY t.tgname"""), {"name": name}).mappings().all()
    return _json({"schema": "public", "name": name, "scope": scope, "columns": columns,
                  "primary_key": i.get_pk_constraint(name, schema="public"),
                  "foreign_keys": sorted(fks, key=lambda f: f["name"] or ""),
                  "checks": sorted(i.get_check_constraints(name, schema="public"), key=lambda c: c["name"] or ""),
                  "unique": sorted(i.get_unique_constraints(name, schema="public"), key=lambda c: c["name"] or ""),
                  "indexes": sorted(i.get_indexes(name, schema="public"), key=lambda c: c["name"] or ""),
                  "triggers": [dict(t) for t in triggers]})


def validate_tables(conn, manifest: dict, *, expected=None) -> list[dict]:
    pid = manifest["id"]
    declarations = {t["name"]: t for t in manifest.get("tables", [])}
    names = set(inspect(conn).get_table_names(schema="public"))
    actual = {n for n in names if n.startswith(table_prefix(pid))}
    if actual != set(declarations):
        raise PluginError("TABLE_METADATA_MISMATCH", "插件实际表集合与声明不一致")
    result = []
    for name, decl in sorted(declarations.items()):
        meta = describe_table(conn, name, decl["scope"])
        if not meta["primary_key"]["constrained_columns"]:
            raise PluginError("INVALID_TABLE_SCOPE", "插件表必须具有主键")
        for trigger in meta["triggers"]:
            if trigger["function_schema"] != "public" or not trigger["function_name"].startswith(table_prefix(pid)) or trigger["security_definer"]:
                raise PluginError("INVALID_PLUGIN_TRIGGER", "触发器函数必须为 public 下的插件私有 SECURITY INVOKER 函数")
            if re.search(r"\b(?:insert|update|delete|select|execute|perform|create|alter|drop|truncate|copy|call)\b", trigger["source"], re.I):
                raise PluginError("INVALID_PLUGIN_TRIGGER", "首版触发器仅允许修改 NEW 行，不能执行 SQL 或调用其他过程")
        cols = {c["name"]: c for c in meta["columns"]}
        if decl["scope"] == "book" and ("book_id" not in cols or cols["book_id"]["nullable"] or cols["book_id"]["type"] not in {"INTEGER", "BIGINT"}):
            raise PluginError("INVALID_TABLE_SCOPE", "账套插件表必须包含非空整数 book_id")
        if decl["scope"] == "global" and "book_id" in cols:
            raise PluginError("INVALID_TABLE_SCOPE", "全局配置表不得含账套业务数据")
        has_book_fk = False
        for fk in meta["foreign_keys"]:
            if fk["schema"] != "public":
                raise PluginError("INVALID_TABLE_FK", "插件外键不得跨 schema")
            if fk["table"] == "book" and decl["scope"] == "book" and fk["columns"] == ["book_id"] and fk["target_columns"] == ["id"]:
                has_book_fk = True
            elif fk["table"] in declarations:
                target_scope = declarations[fk["table"]]["scope"]
                if decl["scope"] == "global" and target_scope != "global":
                    raise PluginError("INVALID_TABLE_FK", "全局表不得引用账套表")
                if decl["scope"] == target_scope == "book" and ("book_id" not in fk["columns"] or "book_id" not in fk["target_columns"] or fk["columns"].index("book_id") != fk["target_columns"].index("book_id")):
                    raise PluginError("INVALID_TABLE_FK", "账套表之间外键必须包含相同 book_id")
            else:
                raise PluginError("INVALID_TABLE_FK", "插件不得引用其他插件或宿主业务表")
        if decl["scope"] == "book" and not has_book_fk:
            raise PluginError("INVALID_TABLE_FK", "book_id 必须引用宿主 book.id")
        result.append(meta)
    function_names = set(conn.execute(text("""SELECT p.proname FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND starts_with(p.proname, :prefix)"""), {"prefix": table_prefix(pid)}).scalars())
    if function_names != {t["function_name"] for meta in result for t in meta["triggers"]}:
        raise PluginError("INVALID_PLUGIN_TRIGGER", "插件函数必须绑定到声明表的触发器")
    if expected is not None and result != expected:
        raise PluginError("TABLE_METADATA_MISMATCH", "插件存量库结构与登记 metadata 漂移")
    return result


def _non_table_objects(conn, plugin_id):
    """DDL 范围检查的补充：不开放自定义函数/类型/schema/view；序列须为本插件。"""
    prefix = table_prefix(plugin_id)
    schemas = tuple(conn.execute(text("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' ORDER BY nspname")).scalars())
    relations = tuple(conn.execute(text("""SELECT n.nspname,c.relname,c.relkind FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
        AND (n.nspname <> 'public' OR c.relkind IN ('v','m','f','S')) ORDER BY 1,2""")).all())
    relations = tuple(tuple(row) for row in relations if not (row[0] == 'public' and row[2] == 'S' and row[1].startswith(prefix)))
    functions = tuple(tuple(row) for row in conn.execute(text("""SELECT n.nspname,p.proname,pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' AND p.prokind IN ('f','p','w') ORDER BY p.oid""")))
    types = tuple(tuple(row) for row in conn.execute(text("""SELECT n.nspname,t.typname,t.typtype,t.typbasetype
        FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' AND t.typtype IN ('e','d','r') ORDER BY t.oid""")))
    functions = tuple(row for row in functions if not (row[0] == "public" and row[1].startswith(prefix)))
    return schemas, relations, functions, types


def migrate(engine, directory: Path, manifest: dict, *, on_commit=None) -> tuple[str, list]:
    pid, head = manifest["id"], read_head(directory)
    if manifest.get("tables") and not head:
        raise PluginError("INVALID_MIGRATION", "声明数据表的插件必须提供迁移")
    # Alembic 的脚本 import 不能向 immutable 包写入 __pycache__。
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with engine.begin() as conn:
            objects_before = _non_table_objects(conn, pid)
            main_before = conn.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars().all()
            before_names = set(inspect(conn).get_table_names(schema="public"))
            protected = {n: describe_table(conn, n, "host") for n in before_names
                         if not n.startswith(table_prefix(pid)) and n != version_table(pid)}
            if head:
                script = ScriptDirectory(str(directory / "migrations"))
                config = Config()
                # EnvironmentContext 使用宿主连接与 version table，不运行包内 env.py。
                with EnvironmentContext(config, script, fn=lambda rev, ctx: script._upgrade_revs("head", rev)) as env:
                    env.configure(connection=conn, version_table=version_table(pid), version_table_schema="public", transactional_ddl=True)
                    env.run_migrations()
            if _non_table_objects(conn, pid) != objects_before:
                raise PluginError("MIGRATION_SCOPE_VIOLATION", "迁移修改了未开放的 schema/函数/类型/view/外部序列")
            result = validate_tables(conn, manifest)
            after_names = set(inspect(conn).get_table_names(schema="public"))
            allowed_new = {t["name"] for t in manifest.get("tables", [])} | {version_table(pid)}
            if after_names - before_names - allowed_new or before_names - after_names:
                raise PluginError("MIGRATION_SCOPE_VIOLATION", "迁移修改了声明范围外的表")
            if any(describe_table(conn, n, "host") != meta for n, meta in protected.items()):
                raise PluginError("MIGRATION_SCOPE_VIOLATION", "迁移修改了宿主或其他插件结构")
            if conn.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars().all() != main_before:
                raise PluginError("MAIN_SCHEMA_CHANGED", "插件不得修改主迁移版本")
            if read_version(conn, pid) != head:
                raise PluginError("MIGRATION_PENDING", "插件迁移未到 head")
            if on_commit:
                # 与 DDL 一起提交，消除迁移已提交但 journal 仍 PREPARED 的窗口。
                on_commit(conn, head, result)
        return head, result
    finally:
        sys.dont_write_bytecode = previous
