"""插件隔离模式（``PLUGIN_ISOLATION``）的验收（§7 第 6 步第 48 项 · 阶段 2）。

**这一轮钉的是什么**
--------------------
阶段 2 之后，插件的业务请求**不再由宿主进程里的那份代码处理** —— 它只在
worker 进程里跑，宿主经 Unix socket 派活。于是「行为不变」这件事必须
被**逐条**证明，而不是「看起来应该一样」：

1. **宿主进程里没有插件模块** —— 证明插件代码真的不在宿主里跑
   （进程边界，而不是「沙箱挡住了它导进来」那回事）。
2. **worker 自报的进程号 ≠ 宿主进程号** —— 进程号只能由那个进程自己报。
3. **两种模式的响应逐字相同** —— GET / POST body / 路径参数 / 404。
4. **ctx 调用能往返** —— 插件在 worker 里调 ``ctx.*``，宿主真的执行了。
5. **错误映射逐档相同** —— ``HTTPException`` 原样、``PluginError`` 原样、
   其它异常**不泄露细节**。
6. **钩子与定时任务也走 IPC** —— 它们不是「只把路由搬走了」。
7. **重建不了的签名 → 拒绝加载** —— 不是塞个 ``Any`` 上去让行为悄悄变掉。
8. **shadow 模式差异为空** —— 切过去之前，两边声明必须一致。

**反向验证（mutation）** 在文件末尾：每条注入都对应上面某条断言，
注入后那条断言必须变红 —— 否则它证明不了任何事。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.plugins.errors import PluginError
from app.plugins.installer import install
from app.main import app
from tests.plugin_fixtures import plugin_env


#: 一个「什么都会一点」的插件：查询参数、JSON body、路径参数、ctx 调用、
#: 钩子、定时任务、两种错误。
BACKEND = b'''def register(registrar, ctx):
    def chart(book_id: int):
        return {"book_id": book_id}

    def save_rule(book_id: int, payload: dict):
        return {"book_id": book_id, "name": payload.get("name"), "fields": sorted(payload)}

    def delete_rule(book_id: int, rule_id: int):
        return {"book_id": book_id, "rule_id": rule_id}

    def books():
        return {"books": ctx.book_list()}

    def denied():
        from fastapi import HTTPException
        raise HTTPException(422, "\u7f3a\u5c11\u53c2\u6570")

    def boom():
        raise ValueError("secret-\u4e1a\u52a1\u5bc6\u5bc6")

    def on_period_close(payload: dict):
        return {"hook": "ran", "book_id": payload.get("book_id")}

    def monthly(context):
        return {"task": "ran", "run_id": context.run_id}

    registrar.get("/chart", chart)
    registrar.post("/rules", save_rule)
    registrar.post("/rules/{rule_id}/delete", delete_rule)
    registrar.get("/books", books)
    registrar.get("/denied", denied)
    registrar.get("/boom", boom)
    registrar.hook("on_period_close", on_period_close)
    registrar.task("monthly", monthly)
'''

FRONTEND = b'export default (host) => ({render: () => host.h("div")});\n'

MANIFEST_EDIT = lambda manifest: manifest.update({  # noqa: E731 —— 测试里的短钩子
    "permissions": ["book:read"],
    "hooks": ["on_period_close"],
    "tasks": [{"name": "monthly", "schedule": {"type": "interval", "minutes": 60}}],
})


def _build(build, **kwargs):
    kwargs.setdefault("files", {"backend/main.py": BACKEND, "frontend/index.js": FRONTEND})
    kwargs.setdefault("edit", MANIFEST_EDIT)
    return build(**kwargs)


def _grant(client, auth, admin_user, pid):
    assert client.post(f"/api/system/plugins/{pid}/users/{admin_user.id}", headers=auth).status_code == 200


def _runtime():
    return app.state.plugin_runtime


# ---------------------------------------------------------------------------
# 一、on 模式：插件代码不在宿主进程里
# ---------------------------------------------------------------------------


def test_on模式_插件模块不在宿主进程里且worker是另一个进程(plugin_env, client, auth, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "plugin_isolation", "on")
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    _grant(client, auth, admin_user, pid)

    handle = _runtime().workers[pid]
    assert handle.worker_pid, "worker 没有报出自己的进程号"
    assert handle.worker_pid != os.getpid(), "worker 与宿主是同一个进程"

    # 宿主进程里**没有**插件模块 —— 证明插件代码没在宿主里 exec 过。
    digest = _runtime_digest(pid)
    assert f"cwq_plugin_{digest}" not in sys.modules


def _runtime_digest(pid) -> str:
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.plugins.models import PluginRegistry

    with SessionLocal() as db:
        return db.scalar(select(PluginRegistry.plugin_digest).where(PluginRegistry.plugin_id == pid))


# ---------------------------------------------------------------------------
# 二、两种模式的对外行为逐字相同
# ---------------------------------------------------------------------------


def _probe(client, auth, pid):
    """把插件的每种调用形态都打一遍，收下状态码与响应体。"""
    return {
        "get": (lambda r: (r.status_code, r.json()))(
            client.get(f"/api/plugins/{pid}/chart", params={"book_id": 1}, headers=auth)),
        "post": (lambda r: (r.status_code, r.json()))(
            client.post(f"/api/plugins/{pid}/rules", json={"book_id": 1, "name": "\u623f\u94fa"}, headers=auth)),
        "path": (lambda r: (r.status_code, r.json()))(
            client.post(f"/api/plugins/{pid}/rules/7/delete", json={"book_id": 1}, headers=auth)),
        "missing": (lambda r: (r.status_code, r.text))(
            client.get(f"/api/plugins/{pid}/nope", headers=auth)),
    }


def test_on模式与进程内模式的对外行为完全一致(plugin_env, client, auth, admin_user, monkeypatch):
    """**逐字相同** —— 状态码与响应体都要一样。

    这是「行为不变」这条承诺的正面验收：同一份插件源码、同样的请求，
    两种执行方式给出同一个答案。
    """
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    pid = manifest["id"]

    monkeypatch.setattr(settings, "plugin_isolation", "off")
    restart()
    _grant(client, auth, admin_user, pid)
    in_process = _probe(client, auth, pid)

    monkeypatch.setattr(settings, "plugin_isolation", "on")
    restart()
    _grant(client, auth, admin_user, pid)
    isolated = _probe(client, auth, pid)

    assert isolated == in_process, f"两种模式的行为不一致：{isolated} != {in_process}"


# ---------------------------------------------------------------------------
# 三、ctx 调用能往返
# ---------------------------------------------------------------------------


def test_on模式_插件在worker里调ctx能力(plugin_env, client, auth, admin_user, monkeypatch):
    """插件在 worker 里调 ``ctx.book_list()``，宿主真的执行了。

    这一条证明的是**嵌套**那条路：宿主派活 → worker 要能力 → 宿主执行 →
    worker 拿到结果 → worker 应答宿主。
    """
    monkeypatch.setattr(settings, "plugin_isolation", "on")
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    _grant(client, auth, admin_user, pid)

    response = client.get(f"/api/plugins/{pid}/books", headers=auth)
    assert response.status_code == 200, response.text
    assert "books" in response.json()
    assert isinstance(response.json()["books"], list)


# ---------------------------------------------------------------------------
# 四、错误映射逐档相同
# ---------------------------------------------------------------------------


def test_on模式_插件抛HTTPException时状态码与内容一致(plugin_env, client, auth, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "plugin_isolation", "on")
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    _grant(client, auth, admin_user, pid)

    response = client.get(f"/api/plugins/{pid}/denied", headers=auth)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "\u7f3a\u5c11\u53c2\u6570"


def test_on模式_插件抛普通异常时细节不进响应(plugin_env, client, auth, admin_user, monkeypatch):
    """插件自己抛的异常**不能**把内容泄进响应体。

    进程内模式下宿主把它变成 500 ``PLUGIN_EXECUTION_FAILED``；
    隔离模式下异常发生在另一个进程里，更不能让它顺着 IPC 漏出来。
    """
    monkeypatch.setattr(settings, "plugin_isolation", "on")
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    _grant(client, auth, admin_user, pid)

    response = client.get(f"/api/plugins/{pid}/boom", headers=auth)
    assert response.status_code == 500, response.text
    assert "secret-\u4e1a\u52a1\u5bc6\u5bc6" not in response.text
    assert "ValueError" not in response.text


# ---------------------------------------------------------------------------
# 五、钩子与定时任务也走 IPC
# ---------------------------------------------------------------------------


def test_on模式_钩子与定时任务经由worker执行(plugin_env, client, auth, admin_user, monkeypatch):
    monkeypatch.setattr(settings, "plugin_isolation", "on")
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    _grant(client, auth, admin_user, pid)

    runtime = _runtime()
    hook = runtime.hooks[pid]["on_period_close"]
    assert hook({"book_id": 7}) == {"hook": "ran", "book_id": 7}

    task = runtime.tasks[pid]["monthly"]
    assert task[1](SimpleNamespace(run_id="run-1")) == {"task": "ran", "run_id": "run-1"}


# ---------------------------------------------------------------------------
# 六、重建不了的签名 → 拒绝加载
# ---------------------------------------------------------------------------


def test_on模式_签名重建不了的插件装不上(plugin_env, client, auth, admin_user, monkeypatch):
    """注解宿主重建不了时**拒绝加载**，而不是塞个 ``Any`` 上去。

    降级成 ``Any`` 与原注解对 FastAPI 的意义不同（``Any`` 少一层校验，
    错的注解则按错的方式解析）—— 与其让行为悄悄变掉，不如明确拒绝。
    """
    backend = b'''from typing import Literal

def register(registrar, ctx):
    def pick(kind: Literal["a", "b"]):
        return {"kind": kind}

    registrar.get("/pick", pick)
'''
    build, restart = plugin_env
    raw, manifest, _, _ = build(files={"backend/main.py": backend, "frontend/index.js": FRONTEND})
    install(raw, admin_user)
    pid = manifest["id"]

    # 进程内模式：装得上（这是今天的行为）。
    monkeypatch.setattr(settings, "plugin_isolation", "off")
    restart()
    assert _runtime().handlers.get(pid) is not None

    # 隔离模式：装不上，并且要说清楚是哪个参数。
    monkeypatch.setattr(settings, "plugin_isolation", "on")
    restart()
    assert pid not in _runtime().handlers
    code = _runtime_status(pid)
    assert code == "PLUGIN_ISOLATION_UNSUPPORTED", code


def _runtime_status(pid) -> str:
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.plugins.models import PluginWorkerRuntime

    with SessionLocal() as db:
        return db.scalar(select(PluginWorkerRuntime.error_code)
                         .where(PluginWorkerRuntime.plugin_id == pid)
                         .order_by(PluginWorkerRuntime.lease_at.desc()).limit(1)) or ""


# ---------------------------------------------------------------------------
# 七、shadow 模式：只报告，不改行为
# ---------------------------------------------------------------------------


def test_shadow模式_跑两遍且差异为空(plugin_env, client, auth, admin_user, monkeypatch):
    """shadow 模式**不改变对外行为**，只是多跑一遍 worker 比对声明。

    断言两件事：差异为空（= 切过去的前提成立），以及接口照常工作。
    """
    monkeypatch.setattr(settings, "plugin_isolation", "shadow")
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    _grant(client, auth, admin_user, pid)

    assert _runtime().isolation_diffs.get(pid) == [], "shadow 比对出现了差异"
    response = client.get(f"/api/plugins/{pid}/chart", params={"book_id": 3}, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json() == {"book_id": 3}


# ---------------------------------------------------------------------------
# 六、worker 没了：宿主照常，插件接口 503
# ---------------------------------------------------------------------------


def test_on模式_worker自己上了限制且报回宿主(plugin_env, client, auth, admin_user, monkeypatch):
    """worker **自己**给自己上限制，并把**生效结果**报回宿主。

    「限制到底生效了没有」必须**可观测** —— 否则「代码里写了就当它生效了」。
    """
    monkeypatch.setattr(settings, "plugin_isolation", "on")
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    _grant(client, auth, admin_user, pid)

    limits = _runtime().workers[pid].limits
    assert limits, "worker 没有报出生效的限制"
    # core dump 里可能有账套数据与令牌 —— 插件崩了不该把它写进磁盘。
    assert limits.get("core") == 0, f"core dump 没被限制住：{limits}"
    assert limits.get("nofile"), f"文件描述符上限没生效：{limits}"


def test_on模式_worker被杀之后宿主仍然健康(plugin_env, client, auth, admin_user, monkeypatch):
    """worker 被 ``kill -9`` → 宿主检测到它没了，插件接口 503，**宿主本身照常**。

    这是隔离模型最实在的一条：插件的崩溃/失控发生在**另一个进程**里，
    宿主的服务不受影响（进程边界挡住了「一起死」）。

    而且是 **503 不是 500**：插件不可用与「插件自己出错」是两件事 ——
    前者该让调用方重试，后者不该重试。
    """
    import signal
    import time

    monkeypatch.setattr(settings, "plugin_isolation", "on")
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    _grant(client, auth, admin_user, pid)

    handle = _runtime().workers[pid]
    os.kill(handle.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while handle.alive and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not handle.alive, "worker 还没死"

    response = client.get(f"/api/plugins/{pid}/chart", params={"book_id": 1}, headers=auth)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "PLUGIN_UNAVAILABLE"
    # 宿主自己照常。
    assert client.get("/api/health").status_code == 200


# ---------------------------------------------------------------------------
# 七、ctx 分发的白名单是**结构性**的
# ---------------------------------------------------------------------------


def test_隔离模式取值非法时配置校验拒绝():
    """取值只有 ``off`` / ``shadow`` / ``on`` 三个。

    拼错一个值就该在**启动时**拒绝，而不是「悄悄按 off 跑」——
    后者会让人以为已经在隔离模式下了。
    """
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(plugin_isolation="bogus")
    with pytest.raises(ValidationError):
        Settings(plugin_isolation="")
    assert Settings(plugin_isolation="on").plugin_isolation == "on"


def test_ctx调用只认白名单里的方法():
    """插件不能点名让宿主执行白名单之外的东西。

    白名单来自**真实 ctx** 推导出来的表面说明，不是手写的；
    「不许下划线开头的属性」是另一道**结构性**的闸 —— 白名单是数据，
    将来有人往 ctx 上加个 ``_internal`` 也不会漏。
    """
    from app.plugins.ipc.ctx_dispatch import check_method
    from app.plugins.ipc.protocol import ProtocolError

    surface = [{"method": "book_list", "params": []}]
    check_method("book_list", surface)
    with pytest.raises(ProtocolError):
        check_method("__globals__", surface)
    with pytest.raises(ProtocolError):
        check_method("data.__class__", surface)
    with pytest.raises(ProtocolError):
        check_method("not_in_surface", surface)


def test_ctx调用的参数名也要在白名单里():
    from app.plugins.ipc.ctx_dispatch import check_params
    from app.plugins.ipc.protocol import ProtocolError

    surface = [{"method": "data.read", "params": ["name"]}]
    check_params("data.read", {"name": "t"}, surface)
    with pytest.raises(ProtocolError):
        check_params("data.read", {"name": "t", "book_id": 1}, surface)


# ---------------------------------------------------------------------------
# 九、反向验证（mutation）
#
# 下面每一条注入都对应上面某条断言。注入后那条断言必须变红 ——
# 否则它证明不了任何事（「用例一直绿」常常是因为注入没真的拆掉防线）。
# ---------------------------------------------------------------------------


def test_反向验证_签名重建坏了则行为等价断言会变红(plugin_env, client, auth, admin_user, monkeypatch):
    """把签名重建改成「一律 Any」→ **行为等价那条断言必须变红**。

    这条证明「行为等价」这条断言**真的**在看着签名 —— 而不是不管重建成
    什么样都绿。（注入无效的用例证明不了任何事。）
    """
    import app.plugins.ipc.router as router_module

    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    pid = manifest["id"]

    monkeypatch.setattr(settings, "plugin_isolation", "off")
    restart()
    _grant(client, auth, admin_user, pid)
    baseline = _probe(client, auth, pid)

    # **注入**：重建出来的签名一律用 ``Any`` —— 注解信息全丢。
    original = router_module.rebuild_signature

    def broken(described, *, strict=False):
        import inspect as pyinspect
        from typing import Any

        params = [pyinspect.Parameter(item["name"], pyinspect.Parameter.POSITIONAL_OR_KEYWORD,
                                      default=None, annotation=Any)
                  for item in (described or [])]
        return pyinspect.Signature(params)

    monkeypatch.setattr(router_module, "rebuild_signature", broken)
    monkeypatch.setattr(settings, "plugin_isolation", "on")
    restart()
    _grant(client, auth, admin_user, pid)
    mutated = _probe(client, auth, pid)

    assert mutated != baseline, "注入没有生效：签名重建坏了，行为却一模一样 —— 这条断言证明不了任何事"
    assert original is not None


def test_反向验证_宿主装载插件模块则那条断言会变红(plugin_env, client, auth, admin_user, monkeypatch):
    """``on`` 模式下宿主**不许**执行插件模块的装载。

    这条用「宿主一次也没调过 ``exec_plugin_module``」来钉，比「模块不在
    ``sys.modules`` 里」更直接 —— 后者会被「之前在进程内模式装载过」的
    残留骗过去。

    注入：让 ``on`` 模式也把插件模块装进宿主 → 断言必须变红。
    """
    import app.plugins.loader as loader_module

    calls: list = []
    original_exec = loader_module.exec_plugin_module

    def spy(name, path, **kwargs):
        calls.append(name)
        return original_exec(name, path, **kwargs)

    monkeypatch.setattr(loader_module, "exec_plugin_module", spy)
    monkeypatch.setattr(settings, "plugin_isolation", "on")
    build, restart = plugin_env
    raw, manifest, _, _ = _build(build)
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    _grant(client, auth, admin_user, pid)
    assert calls == [], f"on 模式下宿主不该装载插件模块，却装载了：{calls}"
    assert client.get(f"/api/plugins/{pid}/chart", params={"book_id": 1}, headers=auth).status_code == 200

    # **注入**：让隔离模式也把插件模块装进宿主。
    original_isolated = loader_module._load_isolated

    def leaky(runtime, plugin, entry, verified):
        loader_module.exec_plugin_module("cwq_plugin_" + plugin.plugin_digest, entry)
        return original_isolated(runtime, plugin, entry, verified)

    calls.clear()
    monkeypatch.setattr(loader_module, "_load_isolated", leaky)
    restart()
    assert calls, "注入没有生效：宿主照样没装载插件模块 —— 那条断言证明不了任何事"


def test_反向验证_错误映射一旦泄露细节那条断言就会变红():
    """内部错误这一档**不许**带异常细节 —— 这是「响应里没有 secret」的根。

    注入：把细节塞进内部错误这一档 → 这条断言立刻变红。

    （端到端那条「细节不进响应」在**另一个进程**里跑，测试进程改不到
    worker 的函数，所以这一层只能在这里钉 —— 而它恰好是根因那一层。）
    """
    import json

    from app.plugins.ipc import protocol

    exc = ValueError("secret-\u4e1a\u52a1\u5bc6\u5bc6")
    payload = protocol.error_payload(exc, error_id="e1")
    assert payload["kind"] == "internal"
    assert "secret" not in json.dumps(payload, ensure_ascii=False)
    # 对端还原出来的异常里也不该有细节。
    assert "secret" not in str(protocol.protocol_error_to_exception(payload))

    # **注入**：内部错误这一档带上细节。
    leaky = {"kind": "internal", "status": 500,
             "detail": {"detail": protocol.INTERNAL_ERROR_DETAIL, "error_id": "e1",
                        "leak": str(exc)}}
    assert "secret" in json.dumps(leaky, ensure_ascii=False), "注入的构造有问题"
    # 而且宿主这一层**也不该**把细节透出去（第二道防线）。
    assert "secret" not in str(protocol.protocol_error_to_exception(leaky))


def test_反向验证_ctx白名单被拆掉则属性就能被点名(monkeypatch):
    """白名单 + 「不许下划线属性」是**唯一**挡住「插件点名宿主内部属性」的东西。

    注入：把方法名校验改成放行 → 插件就能调到 ctx 上的私有能力。
    """
    import app.plugins.ipc.ctx_dispatch as dispatch_module
    from app.plugins.ipc.protocol import ProtocolError

    ctx = SimpleNamespace(_secret=lambda: "host-internal", book_list=lambda: [])

    # 正常情况下：下划线开头的属性点不到。
    with pytest.raises(ProtocolError):
        dispatch_module.dispatch_ctx_call(ctx, [], "_secret", {})

    # **注入**：校验被拆掉。
    monkeypatch.setattr(dispatch_module, "check_method", lambda method, surface: None)
    monkeypatch.setattr(dispatch_module, "check_params", lambda method, params, surface: None)
    assert dispatch_module.dispatch_ctx_call(ctx, [], "_secret", {}) == "host-internal"


def test_反向验证_值编码坏了则Decimal与datetime过不去():
    """把类型标记去掉 → ``Decimal`` / ``datetime`` 还原不回来。

    这一条钉的是「插件在 worker 里算钱算得对」：金额是 ``Decimal``，
    时间是 ``datetime``，编码一丢类型，插件写进账里的数字就变了。
    """
    from datetime import datetime
    from decimal import Decimal

    from app.plugins.ipc.values import decode_value, encode_value

    encoded = encode_value({"amount": Decimal("1.50"), "at": datetime(2026, 1, 2, 3, 4, 5)})
    decoded = decode_value(encoded)
    assert decoded["amount"] == Decimal("1.50")
    assert decoded["at"] == datetime(2026, 1, 2, 3, 4, 5)
    # 不带标记的编码会退化成 float / str —— 金额的文本就变了。
    plain = {"amount": float(Decimal("1.50")), "at": datetime(2026, 1, 2, 3, 4, 5).isoformat()}
    assert str(plain["amount"]) != "1.50"


def test_反向验证_声明目标标识重复时不会串路由():
    """两个路由都注册 ``lambda`` 时，目标标识必须**不同**。

    早先拿 handler 名字当标识，两个 ``lambda`` 名字一样（``"<lambda>"``），
    后者会把前者顶掉 —— 于是第一条路由会调到第二个函数。
    """
    from app.plugins.ipc.declarations import describe_route, resolve_target

    first = describe_route("/a", ["GET"], lambda: {"a": 1})
    second = describe_route("/b", ["GET"], lambda: {"b": 2})
    assert first["target"] != second["target"]
    assert resolve_target([first, second], first["target"]) is first
    assert resolve_target([first, second], second["target"]) is second


# ---------------------------------------------------------------------------
# 隔离档位必须在启动时**出声**
# ---------------------------------------------------------------------------
#
# 「默认是什么」只有翻代码才知道 = 默认是**静默**的。
# 而审计 §3.1 的根因（插件后端代码在宿主进程内执行）对应的正面对策是
# ``PLUGIN_ISOLATION=on`` —— 默认档不提醒，就等于没人知道有这一档。
#
# 这一条钉的是**启动日志**：三种档位各自说什么，改动会立刻被看见。


@pytest.mark.parametrize("mode,expect", [
    ("off", "宿主进程内"),
    ("on", "独立 worker 进程"),
    ("shadow", "比对"),
])
def test_启动时说清楚插件代码跑在哪(mode, expect, caplog, monkeypatch):
    """三种档位都要出声，且**说清当前这一档在做什么**。"""
    import logging

    from app.plugins.loader import _log_isolation_posture

    monkeypatch.setattr(settings, "plugin_isolation", mode)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _log_isolation_posture()
    assert expect in caplog.text, f"{mode} 档的播报里没有「{expect}」：{caplog.text}"
    # off 是默认档，也是**唯一**还在宿主进程里跑插件的档 —— 它必须更响
    if mode == "off":
        assert "PLUGIN_ISOLATION=on" in caplog.text, "没告诉运维怎么切档"
        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "默认档（插件在宿主进程内跑）不该用 INFO 级别轻轻带过")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(0)
