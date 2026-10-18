import json
import re
import time
import uuid
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, inspect, text
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.main import app
from app.plugins.models import PluginRegistry, PluginUserAccess, PluginInstallAttempt, PluginWorkerRuntime, PluginCohort
from tests.plugin_helpers import package


@pytest.fixture
def plugin_env(tmp_path, monkeypatch, db):
    # ``db`` 是这里必须显式声明的一个依赖，原因见 cleanup() 里的注释：
    # 夹具按创建顺序的**逆序**销毁，所以声明之后 db 会先于 plugin_env 收尾，
    # 插件表的 DROP 不会撞上还开着事务的 db 会话。
    private = Ed25519PrivateKey.generate()
    _, _, keys, _ = package(private=private)
    monkeypatch.setattr(settings, "plugin_enabled", True)
    monkeypatch.setattr(settings, "plugin_root", str(tmp_path / "plugins"))
    monkeypatch.setattr(settings, "plugin_trusted_keys", json.dumps(keys))
    monkeypatch.setenv("CWQ_PLUGIN_GENERATION", uuid.uuid4().hex)
    monkeypatch.setenv("CWQ_PLUGIN_STARTED_AT", str(time.time_ns()))
    monkeypatch.setenv("CWQ_PLUGIN_EXPECTED_WORKERS", "1")
    def cleanup():
        from app.plugins.hooks import set_runtime
        set_runtime(None)
        runtime = getattr(app.state, "plugin_runtime", None)
        if runtime:
            runtime.close()
            app.state.plugin_runtime = None
        with engine.begin() as conn:
            # 插件表带 book_id 外键，DROP ... CASCADE 需要 book 上的锁。测试里
            # 常有请求会话或服务层的 SessionLocal 留在 idle in transaction ——
            # 那种事务攥着锁不放，这里会**永久等待**，在 CI 上表现为整门后端
            # 测试挂着不结束，而不是一条失败。所以先给锁超时：宁可失败也别挂死。
            conn.execute(text("SET LOCAL lock_timeout = '10s'"))
            for name in inspect(conn).get_table_names(schema="public"):
                if re.fullmatch(r"plugin_(?:[a-f0-9]{12}_.+|schema_version_[a-f0-9]{12})", name):
                    conn.execute(text('DROP TABLE public."' + name + '" CASCADE'))
            conn.execute(delete(PluginUserAccess))
            conn.execute(delete(PluginRegistry))
            conn.execute(delete(PluginInstallAttempt))
            conn.execute(delete(PluginWorkerRuntime))
            conn.execute(delete(PluginCohort))
    cleanup()
    def build(**kwargs):
        return package(private=private, **kwargs)
    def restart():
        runtime = getattr(app.state, "plugin_runtime", None)
        if runtime:
            runtime.close()
        monkeypatch.setenv("CWQ_PLUGIN_GENERATION", uuid.uuid4().hex)
        monkeypatch.setenv("CWQ_PLUGIN_STARTED_AT", str(time.time_ns()))
        from app.plugins.loader import startup
        app.state.plugin_runtime = startup()
        return app.state.plugin_runtime
    yield build, restart
    cleanup()
