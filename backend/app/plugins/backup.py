"""备份 v2 插件段与存量库一致性；不往 Base.metadata 动态注册业务表。"""
from __future__ import annotations

import re
from sqlalchemy import MetaData, Table, select, inspect
from app.plugins.errors import PluginError
from app.plugins.migrations import read_version, validate_tables
from app.plugins.models import PluginRegistry


class Catalog(tuple):
    """``(entries, tables)`` 二元组，附带 ``warnings``。

    保持二元组是为了不改 ``app.backup.export`` 的调用点（``plugin_metadata,
    plugin_tables = catalog(conn)``）；``warnings`` 供调用方看到被跳过的插件。
    """

    def __new__(cls, entries, tables, warnings):
        self = super().__new__(cls, (entries, tables))
        self.warnings = warnings
        return self


def catalog(conn, *, strict: bool = False) -> Catalog:
    """插件数据表目录。

    **分域隔离**：一个插件的表漂移（人工改过、被外部清理、跨版本恢复失败）
    过去会让 ``catalog`` 直接抛错，而 ``export_scope`` 每个账套和系统快照
    都要调它 —— 于是**一个**插件的漂移会让全部账套与系统备份一起失败。

    现在默认是**降级**模式：漂移的插件被跳过，并在归档头里留下一条带
    ``drifted`` 标记的记录（数据不会被静默省略，运维在归档里能看见少了谁），
    其余插件与宿主数据照常备份。``strict=True`` 用于恢复路径：那里的正确
    行为是拒绝，而不是降级。
    """
    entries, tables, warnings = [], [], []
    reflected = MetaData()
    for p in conn.execute(select(PluginRegistry.__table__).order_by(PluginRegistry.plugin_id)).mappings():
        if p["error_code"] == "TABLES_CLEANED":
            continue
        manifest = p["manifest_metadata"]
        try:
            metadata = validate_tables(conn, manifest, expected=p["tables"])
            if read_version(conn, p["plugin_id"]) != p["migration_head"]:
                raise PluginError("MIGRATION_PENDING", "插件库版本与登记不符，拒绝备份/恢复")
        except PluginError as exc:
            detail = f"插件 {p['plugin_id']} 的表结构核验失败：{exc.code}"
            if strict:
                raise PluginError(exc.code, detail) from exc
            warnings.append({"id": p["plugin_id"], "code": exc.code, "message": detail,
                             "tables": [t["name"] for t in p["tables"]]})
            # 归档头里保留这条记录：数据没有被静默省略，恢复时会被拒绝。
            entries.append({"id": p["plugin_id"], "version": p["version"], "digest": p["plugin_digest"],
                            "migration_head": p["migration_head"], "tables": [], "drifted": True,
                            "error_code": exc.code})
            continue
        entries.append({"id": p["plugin_id"], "version": p["version"], "digest": p["plugin_digest"],
                        "migration_head": p["migration_head"], "tables": metadata})
        for t in metadata:
            Table(t["name"], reflected, autoload_with=conn, schema="public")
    by_name = {t["name"]: t for p in entries for t in p["tables"]}
    actual = {n for n in inspect(conn).get_table_names(schema="public") if re.fullmatch(r"plugin_[a-f0-9]{12}_.+", n)}
    if actual != set(by_name):
        message = "存在未登记插件表，拒绝静默遗漏数据；请核对 registry 并恢复对应包"
        if strict:
            raise PluginError("UNREGISTERED_PLUGIN_TABLE", message)
        warnings.append({"id": "", "code": "UNREGISTERED_PLUGIN_TABLE", "message": message,
                         "tables": sorted(actual - set(by_name))})
    table_list = [t for t in reflected.sorted_tables if t.name in by_name]
    return Catalog(entries, table_list, warnings)


def validate_restore(conn, data, scope, *, require_installed=True):
    from app.backup.export import ExportRefused, SCOPE_BOOK
    try:
        # 恢复路径用严格模式：结构漂移时**拒绝**恢复，并指出是哪个插件。
        current, all_tables = catalog(conn, strict=True)
    except PluginError as exc:
        raise ExportRefused(f"插件结构核验失败：{exc.code}（{exc.message}）") from exc
    supplied = data["header"].get("plugins", [])
    if not isinstance(supplied, list):
        raise ExportRefused("插件 metadata 格式错误")
    known = {p["id"]: p for p in current}
    seen, expected_names = set(), set()
    for item in supplied:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen or item["id"] not in known:
            raise ExportRefused("归档包含未知或重复插件；必须先安装完全匹配的插件包")
        if require_installed:
            from app.core.config import settings
            from app.plugins.security import verify_directory
            from pathlib import Path
            registered = conn.execute(select(PluginRegistry.__table__).where(
                PluginRegistry.plugin_id == item["id"])).mappings().one()
            if registered["deleted_at"] is not None:
                raise ExportRefused("归档插件已卸载；请先恢复完全匹配的插件安装，再恢复数据")
            try:
                verify_directory(Path(settings.plugin_root).absolute() / item["id"] / registered["plugin_digest"],
                                 registered["plugin_digest"])
            except PluginError as exc:
                raise ExportRefused(f"归档插件代码缺失或校验失败：{exc.code}；请先恢复完全匹配的包") from exc
        seen.add(item["id"])
        if item != known[item["id"]]:
            raise ExportRefused("插件 version/digest/migration head/表 metadata 不兼容；写入前拒绝恢复")
        for meta in item["tables"]:
            name = meta["name"]
            expected_names.add(name)
            payload = data["tables"].get(name)
            if not isinstance(payload, dict) or payload.get("columns") != [c["name"] for c in meta["columns"]]:
                raise ExportRefused("归档缺少插件表或列 metadata 不一致")
            for row in payload.get("rows", []):
                if not isinstance(row, dict) or set(row) != set(payload["columns"]):
                    raise ExportRefused("插件表归档行字段不一致")
                if meta["scope"] == "book" and (type(row.get("book_id")) is not int or row["book_id"] != data["header"].get("book_id")):
                    raise ExportRefused("插件表数据超出归档账套 scope")
    from app.models.base import Base
    # plugin_* 也包含宿主自己管理的用户授权、源文件和导入批次。
    # 只排除明确注册在宿主 metadata 中的表，未知插件表仍拒绝恢复。
    injected = {name for name in data["tables"] if name.startswith("plugin_") and name not in Base.metadata.tables}
    if injected != expected_names:
        raise ExportRefused("归档插件表与 metadata 集合不一致")
    # 同插件外键闭包必须在本归档中，不能让 dry-run 漏掉丢失的父行。
    for item in supplied:
        for meta in item["tables"]:
            payload = data["tables"].get(meta["name"])
            if payload is None:
                continue
            for fk in meta["foreign_keys"]:
                target = data["tables"].get(fk["table"])
                if target is None:
                    if fk["table"] in expected_names:
                        raise ExportRefused("归档插件 FK 闭包不完整")
                    continue
                target_keys = {tuple(r.get(c) for c in fk["target_columns"]) for r in target["rows"]}
                for row in payload["rows"]:
                    value = tuple(row.get(c) for c in fk["columns"])
                    if all(v is not None for v in value) and value not in target_keys:
                        raise ExportRefused("归档插件外键缺少父行")
    return [t for t in all_tables if t.name in expected_names]
