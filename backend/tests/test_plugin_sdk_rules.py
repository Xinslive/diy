"""插件 ctx 暴露面的**序列化登记表**，以及支撑它的那几道闸。

**为什么要有这张表**（``app.plugins.public_api.CTX_METHOD_SERIALIZATION``）：
§7 第 6 步第 48 项要把插件后端代码搬进独立进程，插件调宿主的一切都要经
IPC —— 那条路上**返回值必须能序列化**。现在就把「哪个方法的返回值带二进制」
登记下来，阶段 2 才知道要写多少分块代码；比到时候一边写一边撞上
「这个方法返回 bytes」便宜得多。

**这张表的可信度靠三件事撑着**，本文件逐条钉住：

1. **覆盖性** —— 登记表恰好等于插件面上每一个可调用叶子路径（无缺无多）。
   少登记一个，阶段 2 才会在「插件调了它、宿主回不去」的地方撞上，
   而那正是线上才会暴露的路径。
2. **二进制证据** —— 标 ``"bytes"`` 的两项都有**行为证据**（真调用一次，
   看返回值里是不是真的 bytes），不是源码里的一句注释。
3. **闸门** —— 标 ``"json"`` 的项之所以能被保证，靠的是插件自有表不许有
   二进制列（:func:`app.plugins.migrations.validate_tables`）。没有那道闸，
   ``data.read`` 的返回值就可能是 bytes，登记表写的 "json" 就是一句空话。
"""
import inspect
import io
import re

import pytest
from sqlalchemy import LargeBinary, inspect as sa_inspect

from app.core.db import engine
from app.models.plugin_import import PluginImportFile
from app.plugins import imports as imports_module
from app.plugins.errors import PluginError
from app.plugins.manifest import PERMISSIONS, table_prefix
from app.plugins.migrations import migrate
from app.plugins.public_api import CTX_METHOD_SERIALIZATION, context_for
from app.plugins.security import verify_archive
from app.plugins.uploads import read_file
from tests.plugin_fixtures import plugin_env
from tests.plugin_helpers import package


def _full_manifest():
    """一份**所有能力都开**的 manifest。

    暴露面开到什么程度完全取决于权限，所以覆盖性测试必须用**最大暴露面**：
    用小权限测，「少登记」会被权限过滤掩盖成「本来就没有那个方法」。
    """
    _, manifest, _, _ = package(tables=True)
    manifest["permissions"] = sorted(PERMISSIONS)
    return manifest


def _leaf_paths(ctx):
    """ctx 上每一个可调用的叶子路径（``data.read`` 这种二级也算）。

    递归而不是写死两层：将来多一层命名空间，这里照样数得全，
    登记表漏登记也会照样暴露。
    """
    found = set()

    def walk(node, prefix):
        for name, value in vars(node).items():
            if not prefix and name == "plugin_id":
                continue  # 字符串属性，不是方法
            path = prefix + name
            if callable(value):
                found.add(path)
            elif hasattr(value, "__dict__"):
                walk(value, path + ".")

    walk(ctx, "")
    return found


# ---------------------------------------------------------------------------
# 1. 覆盖性：登记表不多不少
# ---------------------------------------------------------------------------


def test_序列化登记表恰好覆盖插件面上的每一个方法():
    ctx = context_for(_full_manifest(), plugin=True)
    assert _leaf_paths(ctx) == set(CTX_METHOD_SERIALIZATION)


def test_宿主面比插件面只多一个确认能力_它不进ipc登记表():
    """确认是**宿主路由**的能力（「用户点了确认」必须是一次独立请求），
    它不经 IPC —— 插件在后端进程里根本拿不到它，所以不进登记表。

    这条也顺带钉住「插件面不含 confirm」这个安全语义：两者的差集
    **恰好**是确认这一项，多一项少一项都说明哪一面被改错了。
    """
    manifest = _full_manifest()
    host = _leaf_paths(context_for(manifest, plugin=False))
    plug = _leaf_paths(context_for(manifest, plugin=True))
    assert host - plug == {"imports.confirm"}
    assert host - set(CTX_METHOD_SERIALIZATION) == {"imports.confirm"}


def test_登记表取值只能是json或bytes():
    assert set(CTX_METHOD_SERIALIZATION.values()) <= {"json", "bytes"}


def test_登记为bytes的正好是两个真正带二进制的方法():
    assert {k for k, v in CTX_METHOD_SERIALIZATION.items() if v == "bytes"} == {
        "upload_read", "imports.source"}


# ---------------------------------------------------------------------------
# 2. 二进制证据：行为级，不是注释级
# ---------------------------------------------------------------------------


def test_上传读取返回的确实是二进制内容():
    """真调用一次：返回值里的 ``content`` 必须是 bytes。"""

    class _Upload:
        def __init__(self, data):
            self.file = io.BytesIO(data)
            self.filename = "报表.xlsx"
            self.content_type = "application/vnd.ms-excel"

    raw = b"PK\x03\x04not-really-a-zip"
    out = read_file(_Upload(raw), limit=4096)
    assert isinstance(out["content"], bytes)
    assert out["content"] == raw
    assert out["size"] == len(raw)


def test_导入源文件返回的确实是二进制内容():
    """证据有两截：列类型是 BYTEA（psycopg 取回来就是 bytes），
    暴露面把这一列**原样**交给插件。"""
    assert isinstance(PluginImportFile.__table__.c.content.type, LargeBinary)
    ctx = context_for(_full_manifest(), plugin=True)
    assert '"content": file.content' in inspect.getsource(ctx.imports.source)


def test_批次序列化不把原始二进制带出来():
    """``imports.*`` 里除 ``source`` 外的返回值都走批次序列化这个出口；
    出口不许碰原始字节列 —— 这条是「它们都是 json」的结构性依据。

    词边界很重要：``content_type`` 是元数据（可以出现），``content``
    才是原始字节（不许出现）。
    """
    funnel = inspect.getsource(imports_module.serialize)
    assert re.search(r"\bcontent\b", funnel) is None
    ctx = context_for(_full_manifest(), plugin=True)
    for name in ("create", "read", "list", "preview", "edit", "submit", "cancel", "origin"):
        source = inspect.getsource(getattr(ctx.imports, name))
        assert "serialize(" in source or "save_preview(" in source, name


# ---------------------------------------------------------------------------
# 3. 闸门：插件自有表不许有二进制列
# ---------------------------------------------------------------------------


def test_插件自有表不得声明二进制列(tmp_path, plugin_env):
    """这是「``data.*`` 是 json」的那道闸。

    没有它，插件只要在自己的表里放一个 BYTEA 列，``ctx.data.read`` 的
    返回值就带二进制 —— 而登记表写的是 "json"，那就是一句空话
    （实测过：改之前这种表会被照常接受，登记到的列类型里就有 BYTEA）。
    """
    raw, manifest, keys, _ = package(tables=True)
    verified = verify_archive(raw, keys=keys)
    script = "migrations/versions/0001_items.py"
    verified.files[script] = verified.files[script].replace(
        b'sa.Column("label", sa.String(120), nullable=False)',
        b'sa.Column("label", sa.String(120), nullable=False),\n'
        b'        sa.Column("payload", sa.LargeBinary)')
    path = tmp_path / "binary_column"
    verified.extract(path)
    with pytest.raises(PluginError, match="二进制列"):
        migrate(engine, path, manifest)
    # 整批回滚：这张表不能留下来。
    with engine.connect() as conn:
        assert not sa_inspect(conn).has_table(table_prefix(manifest["id"]) + "items")


def test_没有二进制列的插件表照常装得上(tmp_path, plugin_env):
    """反向对照：闸门只挡二进制列，不是把插件表全挡了。"""
    raw, manifest, keys, _ = package(tables=True)
    path = tmp_path / "normal"
    verify_archive(raw, keys=keys).extract(path)
    head, metadata = migrate(engine, path, manifest)
    assert head == "0001_items"
    assert all("BYTEA" not in column["type"] for column in metadata[0]["columns"])
