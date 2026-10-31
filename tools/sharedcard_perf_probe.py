"""共享卡对账插件的取数耗时探针（**只读**）。

在生产机上直接装载**已安装的那一份插件代码**，按宿主真实的能力面
（``context_for(manifest, plugin=True)``）跑一遍它的路由，然后把每个接口的
耗时与热点打出来。

**为什么要在进程内跑**：
浏览器里点一下只能看到「慢」，看不到「慢在哪一步」；而这个插件慢的原因
不在单个 SQL 上，而在**调用次数 × 每次的固定开销**。要区分「一次查询慢」
和「同一个查询调了几百次」，必须能看到每次调用。

**只读，不写任何业务数据**：全程只调 GET 语义的取数函数；
``ctx.data`` 只被用于读。

用法（生产机）::

    python3 sharedcard_perf_probe.py <插件digest目录> <card_id> <user_id>

环境变量：

``CWQ_PROBE_PRINCIPAL=cache``
    做一次 A/B：把宿主那条「每次能力调用都重新解析调用者」的路径换成
    **每个请求只解析一次**（这是「修好之后」的上界）。用来量出
    **授权重复解析**到底吃掉了多少时间 —— 不是猜测，是实测差额。
"""
from __future__ import annotations

import collections
import cProfile
import io
import json
import os
import pstats
import sys
import time

#: 宿主的应用根（生产机上的后端代码目录）。
BACKEND_ROOT = os.environ.get("CWQ_BACKEND_ROOT", "/www/wwwroot/acct.xins.live/backend")

#: **必须 cd 到应用根再 import**：``app/core/config.py`` 的
#: ``SettingsConfigDict(env_file=".env")`` 是**相对 cwd** 的 ——
#: 站在 /tmp 里跑会读不到现网的 ``.env``，于是退回源码里写死的开发默认值
#: ``localhost:15432/cwq``（生产机上是 5432），报「连接被拒」。
os.chdir(BACKEND_ROOT)
sys.path.insert(0, BACKEND_ROOT)

#: 每次能力调用都会触发一次「解析调用者」。探针要数清楚**调用了多少次**。
CALL_COUNTS: collections.Counter = collections.Counter()

#: 宿主原始的调用者解析函数（A/B 要能换回去）。
_ORIGINAL_REQUEST_USER = None


class _StubRegistrar:
    """只收集插件声明的路由，不碰宿主自己的路由表。"""

    def __init__(self):
        self.routes: dict[str, object] = {}

    def add(self, path, endpoint, methods):
        for method in methods:
            self.routes[f"{method.upper()} {path}"] = endpoint

    def get(self, path, endpoint):
        self.add(path, endpoint, ["GET"])

    def post(self, path, endpoint):
        self.add(path, endpoint, ["POST"])

    def delete(self, path, endpoint):
        self.add(path, endpoint, ["DELETE"])

    def route(self, router, *, namespace=None):
        for route in router.routes:
            self.add(route.path, route.endpoint, route.methods)

    def hook(self, name, handler):
        pass

    def task(self, name, handler):
        pass


def _load_plugin(directory: str):
    """按宿主的方式装载插件后端模块。

    宿主用 ``exec_plugin_module``（带运行期内建裁剪），这里用普通的
    ``exec_module`` —— **只读测量**不需要那层裁剪，而且裁剪过的沙箱会把
    ``cProfile`` 之类的反射原语一起禁掉，反而量不准。
    """
    import importlib.util

    manifest = json.load(open(os.path.join(directory, "plugin.json"), encoding="utf-8"))
    entry = os.path.join(directory, manifest["contributes"]["backend_entry"])
    spec = importlib.util.spec_from_file_location("cwq_probe_plugin_main", entry)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cwq_probe_plugin_main"] = module
    spec.loader.exec_module(module)
    return manifest, module


def _instrument(ctx):
    """给 ctx 上的能力方法套一层计数器（只数，不改行为）。"""
    for name in list(vars(ctx)):
        attr = getattr(ctx, name)
        if not callable(attr):
            continue

        def make(name=name, attr=attr):
            def wrapper(*args, **kwargs):
                CALL_COUNTS[name] += 1
                return attr(*args, **kwargs)
            return wrapper

        try:
            setattr(ctx, name, make())
        except (AttributeError, TypeError):
            pass


def _install_cached_principal():
    """把「每次能力调用都重新解析调用者」换成「每个请求只解析一次」。

    **这是 A/B 的「修好之后」那一侧**：宿主网关本来就在每个请求入口做过
    一次 ``require_plugin_access``；请求内部再逐次解析，是**重复**。
    这里用 ``current_request_id`` 当缓存键 —— 同一个请求里调用者不可能变。
    """
    import app.plugins.public_api as public_api

    global _ORIGINAL_REQUEST_USER
    _ORIGINAL_REQUEST_USER = public_api.request_user
    original = _ORIGINAL_REQUEST_USER
    cache: dict[str, object] = {}

    def cached_request_user(db, pid):
        key = public_api.current_request_id.get() or "probe-no-request-id"
        hit = cache.get(key)
        if hit is None:
            hit = original(db, pid)
            cache[key] = hit
        return hit

    public_api.request_user = cached_request_user


def _restore_principal():
    """把调用者解析换回宿主原样（第三遍热基线要用）。"""
    import app.plugins.public_api as public_api

    if _ORIGINAL_REQUEST_USER is not None:
        public_api.request_user = _ORIGINAL_REQUEST_USER


def _measure(label, call, results):
    CALL_COUNTS.clear()
    started = time.perf_counter()
    try:
        call()
        error = ""
    except Exception as exc:  # 探针本身出错也要报出来，别让它变成「没测到」
        error = f"{type(exc).__name__}: {exc}"[:200]
    elapsed = (time.perf_counter() - started) * 1000
    calls = dict(CALL_COUNTS)
    results.append((label, elapsed, error, calls))
    detail = "  ".join(f"{name}×{count}" for name, count in sorted(calls.items(), key=lambda kv: -kv[1])[:6])
    print(f"  {label:<40} {elapsed:9.1f} ms  {error}")
    if detail:
        print(f"      能力调用：{detail}")
    return elapsed


def main() -> int:
    directory = sys.argv[1]
    card_id = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    user_id = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    from app.plugins.public_api import context_for, current_principal

    manifest, plugin = _load_plugin(directory)

    # 只由宿主 ASGI 门禁设置；这里显式设成「管理员本人」，好让插件的能力面
    # 与真实请求一致（能读哪些账套、能不能写）。
    current_principal.set((manifest["id"], user_id, None))

    ctx = context_for(manifest, plugin=True)
    registrar = _StubRegistrar()
    plugin.register(registrar, ctx)
    print(f"插件 {manifest['id']} v{manifest['version']} 已装载，声明 {len(registrar.routes)} 条路由\n")

    # 找一份对账单，用来量「逐笔明细」那条线。
    statement_id = None
    try:
        statements = registrar.routes["GET /cards/{card_id}/statements"](card_id).get("statements") or []
        if statements:
            statement_id = statements[0]["id"]
    except Exception:
        pass

    def run_all(tag: str) -> list:
        print(f"----- {tag} -----")
        _instrument(ctx)
        results = []
        _measure("GET /catalog", lambda: registrar.routes["GET /catalog"](), results)
        _measure("GET /cards", lambda: registrar.routes["GET /cards"](), results)
        _measure("GET /queue", lambda: registrar.routes["GET /queue"](), results)
        _measure(f"GET /cards/{card_id}", lambda: registrar.routes["GET /cards/{card_id}"](card_id), results)
        _measure(f"GET /cards/{card_id}/statements",
                 lambda: registrar.routes["GET /cards/{card_id}/statements"](card_id), results)
        if statement_id:
            _measure(f"GET /statements/{statement_id}/rows",
                     lambda: registrar.routes["GET /statements/{statement_id}/rows"](statement_id, 0, 500), results)

        # 工作台：全量取数 + 建模，是最重的一条。
        # **路由表里的 key 是路径模板**（``/cards/{card_id}/workspace``），
        # 不是替换过的具体路径 —— 用后者去查会「没测到」而不报错，最容易骗过自己。
        workspace_key = "GET /cards/{card_id}/workspace"
        if workspace_key in registrar.routes:
            endpoint = registrar.routes[workspace_key]
            profiler = cProfile.Profile()
            CALL_COUNTS.clear()
            started = time.perf_counter()
            profiler.enable()
            try:
                endpoint(card_id)
                error = ""
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:200]
            profiler.disable()
            elapsed = (time.perf_counter() - started) * 1000
            results.append((workspace_key, elapsed, error, dict(CALL_COUNTS)))
            print(f"  {workspace_key:<40} {elapsed:9.1f} ms  {error}")
            detail = "  ".join(f"{name}×{count}" for name, count in
                               sorted(CALL_COUNTS.items(), key=lambda kv: -kv[1])[:8])
            print(f"      能力调用：{detail}")
            if tag.startswith("基线"):
                buffer = io.StringIO()
                pstats.Stats(profiler, stream=buffer).sort_stats("cumulative").print_stats(25)
                print(buffer.getvalue())
        return results

    baseline = run_all("基线（宿主现状）")

    if os.environ.get("CWQ_PROBE_PRINCIPAL") == "cache":
        _install_cached_principal()
        print()
        cached = run_all("A/B：每个请求只解析一次调用者")
        print()
        # **第三遍再跑一次基线**：第二遍天然是「热的」（数据库页、Python 导入
        # 都已就位），把缓存那侧的收益和热身效应混在一起就分不清了。
        # 拿**热的基线**跟**热的缓存**比，才是缓存那一项的真实收益。
        _restore_principal()
        print()
        warm_baseline = run_all("基线（再跑一遍，已经热了）")

        print("\n===== A/B 差额 =====")
        print("  基线1 是冷的、缓存侧是热的 —— 直接比会把热身效应算进缓存头上。")
        print("  所以看「热基线 vs 缓存」这一行。\n")
        for (label, base_ms, _, _), (_, cache_ms, _, _), (_, warm_ms, _, _) in zip(
                baseline, cached, warm_baseline):
            delta = warm_ms - cache_ms
            share = (delta / warm_ms * 100) if warm_ms else 0.0
            print(f"  {label:<40} 热基线 {warm_ms:8.1f} → 缓存 {cache_ms:8.1f} ms   "
                  f"省 {delta:8.1f} ms ({share:.0f}%)")
        total_warm = sum(row[1] for row in warm_baseline)
        total_cache = sum(row[1] for row in cached)
        print(f"  {'合计':<40} 热基线 {total_warm:8.1f} → 缓存 {total_cache:8.1f} ms   "
              f"省 {total_warm - total_cache:8.1f} ms "
              f"({(total_warm - total_cache) / total_warm * 100:.0f}%)")
        print(f"\n  参考：冷基线合计 {sum(row[1] for row in baseline):.1f} ms "
              f"（比热基线多 {sum(row[1] for row in baseline) - total_warm:.1f} ms —— 这一段是热身效应）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
