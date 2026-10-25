"""插件后端代码**在独立进程里**执行（§7 第 6 步第 48 项 · 阶段 1）。

**这一轮钉的是什么**
--------------------
阶段 1 不改变任何对外行为（业务请求仍由进程内那份代码处理）。
它做的是：让同一份插件后端代码**在另一个进程里也跑一遍**，然后逐条比对
两边声明了什么。

所以这里的四条验收各自证明**一件不同的事**：

1. **worker 的 PID ≠ 宿主的 PID** —— 证明插件代码真的**不在**宿主进程里。
2. **worker 没有加载 ``app.core.db``** —— 证明**进程边界**挡住了数据库层。
   这一条与「插件导不进来」是**两件事**：插件导不进来是**沙箱**的功劳，
   而 worker 自己没加载它是**进程边界**的功劳 —— 后者才是隔离模型的意义。
3. **插件在 worker 里 ``import app.core.db`` 失败** —— 沙箱那一层仍在
   （第 49 项没被删掉）。
4. **shadow 差异为空** —— 同一份插件代码，两种执行方式，声明**逐条相同**。
   这是阶段 2 敢切过去的前提。

**为什么第 4 条要盯签名**：阶段 2 宿主要按声明**重建** FastAPI 路由，
而 FastAPI 靠 ``__signature__`` 解析请求体。序列化这一趟最容易丢它，
丢了的症状是「路由 422」，而且只在切过去之后才出现。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugins.ipc import shadow
from app.plugins.ipc.declarations import describe_ctx_surface
from app.plugins.loader import Registrar

PLUGIN_ID = "com.test.worker"
DIGEST = "a" * 64


def _manifest(*, permissions=None, hooks=None, tasks=None) -> dict:
    return {
        "id": PLUGIN_ID,
        "plugin_digest": DIGEST,
        "permissions": permissions or [],
        "hooks": hooks or [],
        "tasks": tasks or [],
        "tables": [],
    }


def _write_plugin(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "backend_main.py"
    path.write_text(source, encoding="utf-8")
    return path


#: 一个只声明、不碰宿主的插件。
SIMPLE_PLUGIN = '''
def register(registrar, ctx):
    def chart(book_id: int):
        return {"book_id": book_id}

    def save(book_id: int, payload: dict):
        return {"saved": True}

    registrar.get("/chart", chart)
    registrar.post("/save", save)
'''


# ---------------------------------------------------------------------------
# 一、插件代码真的在另一个进程里
# ---------------------------------------------------------------------------


def test_worker的进程号与宿主不同(tmp_path):
    entry = _write_plugin(tmp_path, SIMPLE_PLUGIN)
    run = shadow.run_worker(entry=entry, manifest=_manifest(), ctx=SimpleNamespace())
    assert run.error is None, run.error or run.stderr
    assert run.pid, "worker 没有报出自己的进程号"
    assert run.pid != os.getpid()


def test_worker只加载了插件侧的模块_没有拖进数据库层(tmp_path):
    """**进程边界**的证据：worker 自己就没有 ``app.core.db``。

    与「插件导不进来」是**两件事** —— 后者是沙箱的功劳。
    """
    entry = _write_plugin(tmp_path, SIMPLE_PLUGIN)
    run = shadow.run_worker(entry=entry, manifest=_manifest(), ctx=SimpleNamespace())
    assert run.error is None, run.error or run.stderr
    assert run.modules, "worker 没有报出自己加载了哪些模块"
    assert "app.core.db" not in run.modules
    assert "app.core.config" not in run.modules
    # 插件侧的模块该在（沙箱、协议、代理）
    assert "app.plugins.sandbox" in run.modules


# ---------------------------------------------------------------------------
# 二、沙箱那一层仍然在
# ---------------------------------------------------------------------------


def test_插件在worker里导不到宿主的数据库层(tmp_path):
    """沙箱那一层仍然在（第 49 项没被删掉）。

    探针在 **register 期间**就跑，把结果交给宿主侧的 ``ctx.record`` ——
    这样结果真的回到测试手里，而不是「插件被加载了」这种间接证据。

    探针里**不能**出现双下划线形态（``type(exc).__name__`` 这种）——
    装载期的反射复核会直接拒装整个模块（这是它该做的）。
    所以这里用 ``str(exc)`` 而不是 ``__name__``。
    """
    recorded: list = []
    entry = _write_plugin(tmp_path, '''
def register(registrar, ctx):
    def probe():
        try:
            import app.core.db
            return "imported"
        except ImportError:
            return "blocked"
        except Exception as exc:
            return "blocked: " + str(exc)

    def probe_os():
        try:
            import os
            return "imported"
        except ImportError:
            return "blocked"
        except Exception as exc:
            return "blocked: " + str(exc)

    ctx.record(probe())
    ctx.record(probe_os())
    registrar.get("/probe", probe)
    registrar.get("/probe_os", probe_os)
''')
    ctx = SimpleNamespace(record=lambda value: recorded.append(value))
    run = shadow.run_worker(entry=entry, manifest=_manifest(), ctx=ctx)
    assert run.error is None, run.error or run.stderr
    assert recorded == ["blocked", "blocked"], f"插件在 worker 里导到了宿主能力：{recorded}"
    handlers = {item["handler"] for item in run.declarations}
    assert {"probe", "probe_os"} <= handlers


def test_宿主进程不受worker里的失败影响(tmp_path):
    """worker 里的插件炸了，宿主**照常**。

    这是隔离模型最实在的一条：插件的崩溃/失控发生在**另一个进程**里，
    宿主的服务不受影响（进程边界挡住了「一起死」）。
    """
    entry = _write_plugin(tmp_path, '''
def register(registrar, ctx):
    raise RuntimeError("插件自己炸了")
''')
    run = shadow.run_worker(entry=entry, manifest=_manifest(), ctx=SimpleNamespace())
    # worker 启动失败，宿主这边拿到的是「没拿到声明」而不是宿主自己崩了。
    assert run.declarations == []
    assert run.returncode is not None
    # 宿主进程还活着，而且还能干活。
    assert os.getpid() == os.getpid()
    from app.plugins.ipc.protocol import encode_frame, decode_frame

    assert decode_frame(encode_frame({"type": "ping"})) == {"type": "ping"}


# ---------------------------------------------------------------------------
# 三、shadow 比对：两种执行方式，声明逐条相同
# ---------------------------------------------------------------------------


def test_shadow比对_进程内与worker的声明完全一致(tmp_path):
    entry = _write_plugin(tmp_path, SIMPLE_PLUGIN)
    manifest = _manifest()
    ctx = SimpleNamespace()

    registrar = Registrar(manifest)
    from app.plugins.ipc.proxy_registrar import ProxyRegistrar

    proxy = ProxyRegistrar(PLUGIN_ID)
    proxy.bind_manifest(manifest)
    # 同一份插件代码在两侧各注册一次（用同一个源）。
    namespace: dict = {}
    exec(compile(SIMPLE_PLUGIN, "plugin", "exec"), namespace)  # noqa: S102 —— 测试里直接跑源码
    namespace["register"](registrar, ctx)
    namespace["register"](proxy, ctx)

    in_process = registrar.declarations()
    run = shadow.run_worker(entry=entry, manifest=manifest, ctx=ctx)
    assert run.error is None, run.error or run.stderr

    # **先确认两边都有东西** —— 否则「两边同时丢信息」也会让比对通过，
    # 而那恰恰是最该被发现的失败模式（比对只能证「一致」，证不了「正确」）。
    assert {item["path"] for item in in_process} == {"/chart", "/save"}
    assert {item["path"] for item in run.declarations} == {"/chart", "/save"}

    differences = shadow.compare(in_process, run.declarations)
    assert differences == [], f"shadow 比对出现差异：{differences}"


def test_shadow比对_签名也被带过去了(tmp_path):
    """签名是阶段 2 的命门：宿主要靠它重建 FastAPI 路由。

    **不只是「两边一样」，还要「内容是对的」** —— 只断言一致的话，
    两边同时把签名丢掉也会通过。
    """
    entry = _write_plugin(tmp_path, SIMPLE_PLUGIN)
    manifest = _manifest()
    registrar = Registrar(manifest)
    namespace: dict = {}
    exec(compile(SIMPLE_PLUGIN, "plugin", "exec"), namespace)  # noqa: S102
    namespace["register"](registrar, SimpleNamespace())

    run = shadow.run_worker(entry=entry, manifest=manifest, ctx=SimpleNamespace())
    assert run.error is None, run.error or run.stderr

    in_process = {item["path"]: item for item in registrar.declarations()}
    for item in run.declarations:
        assert item["path"] in in_process
        assert item["signature"] == in_process[item["path"]]["signature"]
        assert item["handler"] == in_process[item["path"]]["handler"]

    # 签名**确实有内容**，而且是插件原本写的那份。
    chart = in_process["/chart"]
    assert [p["name"] for p in chart["signature"]] == ["book_id"]
    assert chart["signature"][0]["annotation"]["name"] == "int"
    assert chart["signature"][0]["annotation"]["rebuildable"] is True
    save = in_process["/save"]
    assert [p["name"] for p in save["signature"]] == ["book_id", "payload"]
    assert save["signature"][1]["annotation"]["name"] == "dict"


# ---------------------------------------------------------------------------
# 四、ctx 代理：worker → 宿主 → worker 的往返
# ---------------------------------------------------------------------------


def test_ctx代理能把调用送到宿主并拿回结果(tmp_path):
    """worker 里的插件调 ctx，宿主真的执行了，结果真的回来了。

    调用必须发生在 **register 期间** —— 阶段 1 还不派活，
    路由处理函数只有「被声明」，没被调用过。
    """
    recorded: list = []
    calls: list = []

    def answer():
        calls.append("called")
        return {"ok": True, "n": 7}

    entry = _write_plugin(tmp_path, '''
def register(registrar, ctx):
    def probe():
        return {"declared": True}

    ctx.record(ctx.answer())
    registrar.get("/probe", probe)
''')
    ctx = SimpleNamespace(answer=answer, record=lambda value: recorded.append(value))
    run = shadow.run_worker(entry=entry, manifest=_manifest(), ctx=ctx)
    assert run.error is None, run.error or run.stderr
    assert calls == ["called"], "宿主侧的能力没有被调用"
    assert recorded == [{"ok": True, "n": 7}], f"结果没有回到 worker：{recorded}"
    assert run.calls and run.calls[0]["method"] == "answer"


def test_ctx表面说明是从真实ctx推导的():
    """表面说明**不是手写的** —— 手写的迟早和真实 ctx 分叉。"""
    ctx = SimpleNamespace(
        book_list=lambda: [],
        data=SimpleNamespace(read=lambda name: [], count=lambda name: 0),
    )
    surface = describe_ctx_surface(ctx)
    methods = {item["method"]: item["params"] for item in surface}
    assert methods["book_list"] == []
    assert methods["data.read"] == ["name"]
    assert methods["data.count"] == ["name"]


def test_ctx代理的形状和真实ctx一样():
    """代理必须**长得一样** —— 插件的代码是照着真实 ctx 写的。"""
    from app.plugins.ipc.proxy_ctx import build_proxy_ctx

    class _Transport:
        def __init__(self):
            self.seen = []

        def call(self, method, params):
            self.seen.append((method, params))
            return [method, params]

    ctx = SimpleNamespace(
        book_list=lambda: [],
        data=SimpleNamespace(read=lambda name: [], count=lambda name: 0),
    )
    transport = _Transport()
    proxy = build_proxy_ctx(describe_ctx_surface(ctx), transport)
    assert proxy.book_list() == ["book_list", {}]
    assert proxy.data.read("t") == ["data.read", {"name": "t"}]
    assert transport.seen[0] == ("book_list", {})


def test_ctx代理不接受不存在的参数():
    from app.plugins.ipc.proxy_ctx import build_proxy_ctx

    class _Transport:
        def call(self, method, params):
            return None

    ctx = SimpleNamespace(book_list=lambda: [])
    proxy = build_proxy_ctx(describe_ctx_surface(ctx), _Transport())
    with pytest.raises(TypeError):
        proxy.book_list(nope=1)
    with pytest.raises(TypeError):
        proxy.book_list(1, 2)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(0)
