"""worker 侧的 registrar 代理：只记录声明，不发调用。

**它做什么**
------------
插件的 ``register(registrar, ctx)`` 照旧跑，只是 ``registrar`` 换成了这一份。
它把插件声明的路由 / 钩子 / 任务**记下来**，最后由 worker 统一发回宿主。

**它不做什么**
--------------
它**不注册任何东西**。真正的 FastAPI 路由要等阶段 2 由宿主按声明重建
（宿主那边拿不到插件的函数对象）。所以这一份里没有 router，
只有一张声明表。

**校验照做**：全部走 :mod:`app.plugins.sdk_rules` —— 与宿主**同一份**实现。
校验留在 worker 侧不是为了「给插件好脸色」，而是**为了让两边判同一件事**：
如果 worker 侧不校验，宿主侧校验时看到的就已经是被加工过的声明，
shadow 比对就变成了「拿加工后的东西比」，失去意义。
"""
from __future__ import annotations

from app.plugins.errors import PluginError
from app.plugins.ipc.declarations import describe_hook, describe_route, describe_task
from app.plugins.sdk_rules import (
    validate_endpoint_signature,
    validate_hook_handler,
    validate_route_methods,
    validate_route_path,
    validate_task_handler,
)


class ProxyRegistrar:
    """worker 侧的 registrar 代理。"""

    def __init__(self, plugin_id: str):
        self._plugin_id = plugin_id
        self._routes: list[dict] = []
        self._hooks: list[dict] = []
        self._tasks: list[dict] = []
        # 目标标识 → 真的函数。**worker 侧要留着它**（宿主派活过来时
        # 要在 worker 里真的调它）；声明本身只是给宿主看的描述。
        #
        # **键是目标标识，不是 handler 名字** —— 两个路由都注册了 ``lambda``
        # 时它们的名字一样（``"<lambda>"``），按名字存后者会把前者顶掉，
        # 于是第一条路由会调到第二个函数。
        self._endpoints: dict[str, object] = {}
        # 插件自己声明了哪些钩子与任务 —— 与宿主侧一样，**只认 manifest 声明过的**。
        self._declared_hooks: set[str] = set()
        self._declared_tasks: set[str] = set()

    # -- 声明来自 manifest ------------------------------------------------

    def bind_manifest(self, manifest: dict) -> None:
        """把 manifest 里声明的钩子与任务名字绑进来。"""
        self._declared_hooks = set(manifest.get("hooks", []))
        self._declared_tasks = {task["name"] for task in manifest.get("tasks", [])}

    # -- 路由 --------------------------------------------------------------

    def add(self, path, endpoint, methods):
        validate_route_path(path, self._plugin_id)
        validate_route_methods(methods)
        validate_endpoint_signature(endpoint)
        for route in self._routes:
            if route["path"] == path and set(route["methods"]) & {str(m).upper() for m in methods}:
                raise PluginError("PLUGIN_ROUTE_CONFLICT", "插件路由重复")
        declaration = describe_route(path, methods, endpoint)
        self._routes.append(declaration)
        self._remember(declaration["target"], endpoint)

    def get(self, path, endpoint):
        self.add(path, endpoint, ["GET"])

    def post(self, path, endpoint):
        self.add(path, endpoint, ["POST"])

    def put(self, path, endpoint):
        self.add(path, endpoint, ["PUT"])

    def patch(self, path, endpoint):
        self.add(path, endpoint, ["PATCH"])

    def delete(self, path, endpoint):
        self.add(path, endpoint, ["DELETE"])

    def route(self, router, *, namespace=None):
        """把插件自己组装好的 router 记下来。

        宿主的 :meth:`app.plugins.loader.Registrar.route` 还会检查 router
        的形状（无生命周期钩子、无依赖）。这里做同样的检查 ——
        **同一份判据**，所以行为不会因为「跑在 worker 里」而变松。
        """
        from fastapi import APIRouter
        from fastapi.routing import APIRoute

        expected = self._plugin_id if namespace is None else namespace
        if expected != self._plugin_id or not isinstance(router, APIRouter) \
                or router.on_startup or router.on_shutdown or router.dependencies:
            raise PluginError("INVALID_PLUGIN_ROUTER", "插件只能提交无生命周期/依赖的命名空间 router")
        for route in router.routes:
            if not isinstance(route, APIRoute) or route.dependencies or route.callbacks:
                raise PluginError("INVALID_PLUGIN_ROUTER", "插件只允许 HTTP 业务路由")
            self.add(route.path, route.endpoint, route.methods)

    # -- 钩子与任务 --------------------------------------------------------

    def hook(self, name, handler):
        if name not in self._declared_hooks or any(h["name"] == name for h in self._hooks):
            raise PluginError("INVALID_PLUGIN_HOOK", "插件 hook 未声明、重复或不在白名单中")
        validate_hook_handler(handler)
        declaration = describe_hook(name, handler)
        self._hooks.append(declaration)
        self._remember(declaration["target"], handler)

    def task(self, name, handler):
        if name not in self._declared_tasks or any(t["name"] == name for t in self._tasks):
            raise PluginError("INVALID_PLUGIN_TASK", "插件定时任务未声明、重复或不能是异步函数")
        validate_task_handler(handler)
        declaration = describe_task(name, handler)
        self._tasks.append(declaration)
        self._remember(declaration["target"], handler)

    # -- 结果 --------------------------------------------------------------

    def declarations(self) -> list[dict]:
        """全部声明。"""
        return list(self._routes) + list(self._hooks) + list(self._tasks)

    # -- 派活用 ------------------------------------------------------------

    def endpoint(self, target: str):
        """按目标标识取处理函数（宿主派活过来时用）。"""
        return self._endpoints.get(target)

    def _remember(self, target: str, handler) -> None:
        self._endpoints[target] = handler
