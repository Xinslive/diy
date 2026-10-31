"""共享卡工作台请求的 **SQL 普查**（只读）。

把一次 ``GET /cards/{id}/workspace`` 里发出的每一条 SQL 抓下来，按「这条
SQL 到底在查什么」分类 —— 于是「一次请求 1041 条 SQL」这种数字才能拆成
**真正的业务查询** 与 **纯授权样板** 两堆。

**为什么要分类**：读同一个接口慢，可能是「一条查询慢」，也可能是
「同一件事查了 500 遍」。前者要加索引，后者要改结构 —— 处置方式完全不同，
不分类就只能靠猜。

用法（生产机）::

    python3 sharedcard_sql_census.py <插件digest目录> <card_id> <user_id> [接口]

``接口`` 可以是 ``workspace``（默认）、``statements``、``queue``、``cards``。
"""
from __future__ import annotations

import collections
import json
import os
import re
import sys
import time

BACKEND_ROOT = os.environ.get("CWQ_BACKEND_ROOT", "/www/wwwroot/acct.xins.live/backend")
os.chdir(BACKEND_ROOT)
sys.path.insert(0, BACKEND_ROOT)

import importlib.util  # noqa: E402

from app.core.db import engine  # noqa: E402
from app.plugins import public_api  # noqa: E402
from app.plugins.public_api import context_for, current_principal  # noqa: E402

#: SQL 分类。**顺序有意义**：先匹配到的先算。
CATEGORIES: list[tuple[str, re.Pattern]] = [
    ("授权样板（插件授权/worker 状态）", re.compile(
        r"\b(plugin_registry|plugin_user_access|plugin_cohort|plugin_worker_runtime|sys_user)\b", re.I)),
    ("账套授权（用户与账套的关系）", re.compile(
        r"\b(user_book|sys_user)\b", re.I)),
    ("插件自有表", re.compile(
        r"\bplugin_275b391c351a_(card|member|rule|stmt|row|alloc|recon)\b", re.I)),
    ("宿主业务表（凭证/分录/科目/期间）", re.compile(
        r"\b(entry|voucher|subject|period|book)\b", re.I)),
]


def _classify(sql: str) -> str:
    for name, pattern in CATEGORIES:
        if pattern.search(sql):
            return name
    return "其它"


def main() -> int:
    directory = sys.argv[1]
    card_id = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    user_id = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    target = sys.argv[4] if len(sys.argv) > 4 else "workspace"

    manifest = json.load(open(os.path.join(directory, "plugin.json"), encoding="utf-8"))
    entry = os.path.join(directory, manifest["contributes"]["backend_entry"])
    spec = importlib.util.spec_from_file_location("cwq_census_plugin_main", entry)
    plugin = importlib.util.module_from_spec(spec)
    sys.modules["cwq_census_plugin_main"] = plugin
    # **必须关掉字节码写入**：`exec_module` 默认会在源文件旁边写
    # ``__pycache__/*.pyc``，而那是**线上插件包目录** —— 宿主验签要求
    # 目录里的文件集合与签名清单完全一致，多一个 ``.pyc`` 就会让插件页面
    # 的前端资源全部 404。详见 sharedcard_perf_probe.py 里同一处的说明。
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(plugin)
    finally:
        sys.dont_write_bytecode = previous

    current_principal.set((manifest["id"], user_id, None))
    ctx = context_for(manifest, plugin=True)

    class _Registrar:
        """只收集路由 —— 插件的取数函数是 ``register()`` 里的闭包，
        拿不到模块级函数，就按声明的路由去调。"""

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
            pass

        def hook(self, name, handler):
            pass

        def task(self, name, handler):
            pass

    registrar = _Registrar()
    plugin.register(registrar, ctx)

    #: 每条要普查的线。**key 是给人看的名字**；路由表的 key 是**路径模板**，
    #: 用替换过的具体路径去查会「没测到」而不报错 —— 最容易骗过自己。
    TARGETS = {
        "workspace": ("GET /cards/{card_id}/workspace",
                      lambda: registrar.routes["GET /cards/{card_id}/workspace"](card_id)),
        "statements": ("GET /cards/{card_id}/statements",
                       lambda: registrar.routes["GET /cards/{card_id}/statements"](card_id)),
        "queue": ("GET /queue", lambda: registrar.routes["GET /queue"]()),
        "cards": ("GET /cards", lambda: registrar.routes["GET /cards"]()),
    }
    label, action = TARGETS[target]

    counts: collections.Counter = collections.Counter()
    by_statement: collections.Counter = collections.Counter()
    total_time = 0.0

    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info["_census_started"] = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        nonlocal total_time
        started = conn.info.pop("_census_started", None)
        if started is not None:
            total_time += time.perf_counter() - started
        category = _classify(statement)
        counts[category] += 1
        by_statement[(category, " ".join(statement.split())[:110])] += 1

    started = time.perf_counter()
    # **必须包在 ``principal_scope()`` 里**：宿主网关在每个请求入口就是
    # 这么包着的。不开这个区间，普查量到的就是「每次能力调用都重新解析
    # 调用者」那条旧路径 —— 会把 SQL 条数算高一倍多。
    with public_api.principal_scope():
        action()
    elapsed = (time.perf_counter() - started) * 1000

    print(f"{label}  共 {sum(counts.values())} 条 SQL，"
          f"墙上 {elapsed:.0f} ms，SQL 执行 {total_time * 1000:.0f} ms\n")

    print("===== 按用途分类 =====")
    for name, count in counts.most_common():
        print(f"  {count:6d} 条  {name}")

    print("\n===== 重复最多的 12 条 SQL =====")
    for (category, statement), count in by_statement.most_common(12):
        print(f"  ×{count:<5d} [{category}]\n            {statement}")

    print("\n===== 出现次数最多的表（从 SQL 文本里抓） =====")
    tables: collections.Counter = collections.Counter()
    for (category, statement), count in by_statement.items():
        for match in re.findall(r"\b(?:plugin_275b391c351a_\w+|sys_user|user_book|entry|voucher|subject|period|book|plugin_\w+)\b",
                                statement, re.I):
            tables[match.lower()] += count
    for name, count in tables.most_common(14):
        print(f"  {count:6d} 次  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
