"""「确认核对」慢在哪一步 —— **只读**探针。

用户报的是「点击确认核对，反应速度很慢」。这一条路径上有**两段**重活：

1. ``POST /cards/{card_id}/reconciliations`` —— 插件的 ``confirm`` 里
   **每次都重算一遍整个工作台**（``_live_workspace``），只为了核对
   「用户看到的快照」和「服务器现在的样子」是不是同一份；
2. 前端写完之后 ``refreshLive()`` 又 ``GET /cards/{card_id}/workspace``
   —— **又一次**全量重算。

所以「点一下确认」= 两次全量工作台重算。这个探针量的是**第一段**里
时间到底花在哪：能力调用（``detail_ledger_read`` / ``entry_match`` /
``subject_read`` / ``period_list``）、插件自有表读取、还是纯 CPU 建模。

**只读**：全程不调用任何写能力；``ctx.data`` 只被用于读。

**不往线上目录写一个字节**：先把插件包目录**复制到临时目录**再装载
（``exec_module`` 会在源文件旁边写 ``__pycache__/*.pyc``，那是线上包目录，
多一个文件就会让宿主验签抛 ``FILE_SET_MISMATCH``，页面资源全 404）。

用法（生产机）::

    python3 sharedcard_confirm_probe.py <插件digest目录> <card_id> <user_id>

环境变量：
``CWQ_PROBE_REPEAT``
    每个测点跑几遍取最小值（默认 5）。生产机上还跑着真实请求，
    单次测量的波动能到 3 倍 —— 最小值才是抗噪的那一档。
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import sys
import tempfile
import time

#: 宿主的应用根（生产机上的后端代码目录）。
BACKEND_ROOT = os.environ.get("CWQ_BACKEND_ROOT", "/www/wwwroot/acct.xins.live/backend")

#: **必须 cd 到应用根再 import**：``app/core/config.py`` 的
#: ``SettingsConfigDict(env_file=".env")`` 是**相对 cwd** 的 ——
#: 站在 /tmp 里跑会退回源码里写死的开发默认值，报「连接被拒」。
os.chdir(BACKEND_ROOT)
sys.path.insert(0, BACKEND_ROOT)

from app.plugins import public_api  # noqa: E402

#: 计时器：名字 → (调用次数, 累计毫秒)
TIMERS: dict[str, list] = collections.defaultdict(lambda: [0, 0.0])


def _timed(label: str, func):
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            slot = TIMERS[label]
            slot[0] += 1
            slot[1] += (time.perf_counter() - started) * 1000
    return wrapper


def _load_plugin_from_copy(directory: str):
    """把插件包目录**复制到临时目录**再装载（线上目录只读）。"""
    import importlib.util

    staging = tempfile.mkdtemp(prefix="cwq-confirm-probe-")
    target = os.path.join(staging, "plugin")
    shutil.copytree(directory, target)
    manifest = json.load(open(os.path.join(target, "plugin.json"), encoding="utf-8"))
    entry = os.path.join(target, manifest["contributes"]["backend_entry"])
    spec = importlib.util.spec_from_file_location("cwq_confirm_probe_main", entry)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cwq_confirm_probe_main"] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return manifest, module, staging


def _instrument_ctx(ctx):
    """给 ctx 的能力方法套上计时器（只计时，不改行为）。"""
    for name in list(vars(ctx)):
        attr = getattr(ctx, name)
        if not callable(attr):
            continue
        try:
            setattr(ctx, name, _timed("ctx." + name, attr))
        except (AttributeError, TypeError):
            pass
    data = getattr(ctx, "data", None)
    if data is not None and hasattr(data, "read"):
        try:
            data.read = _timed("ctx.data.read", data.read)
        except (AttributeError, TypeError):
            pass


def _instrument_module(module):
    """给插件模块里的关键函数套上计时器。

    ``_live_workspace`` 里的调用都是**按模块全局名**查找的
    （``build_model(...)``、``_stmt_rows(...)``），所以改模块属性就能截到。
    """
    for name in ("build_model", "_stmt_rows", "_recon_rows", "_read_all",
                 "_members", "_books", "_card"):
        func = getattr(module, name, None)
        if callable(func):
            setattr(module, name, _timed("mod." + name, func))


def _report(title: str):
    print(f"\n===== {title} =====")
    rows = sorted(TIMERS.items(), key=lambda kv: -kv[1][1])
    for label, (count, total) in rows:
        if total < 0.5 and count == 0:
            continue
        print(f"  {label:<34} ×{count:<5} {total:9.1f} ms")


def main() -> int:
    directory = sys.argv[1]
    card_id = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    user_id = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    repeat = max(1, int(os.environ.get("CWQ_PROBE_REPEAT", "5")))

    manifest, module, staging = _load_plugin_from_copy(directory)
    public_api.current_principal.set((manifest["id"], user_id, None))
    ctx = public_api.context_for(manifest, plugin=True)
    _instrument_ctx(ctx)
    _instrument_module(module)

    print(f"插件 {manifest['id']} v{manifest['version']}（临时副本 {staging}）")
    print(f"卡片 {card_id} · 用户 {user_id} · 每测点取 {repeat} 次最小值\n")

    # ---- 1) 工作台全量重算（confirm 内部就是它） ----
    best = None
    for _ in range(repeat):
        TIMERS.clear()
        started = time.perf_counter()
        with public_api.principal_scope():
            model = module._live_workspace(ctx, card_id)
        elapsed = (time.perf_counter() - started) * 1000
        if best is None or elapsed < best:
            best = elapsed
    print(f"  _live_workspace（工作台全量）{best:9.1f} ms")
    print(f"  模型规模：交易 {len(model.get('transactions', []))} 笔 · "
          f"分录 {len(model.get('entries', []))} 笔 · "
          f"核对组 {len(model.get('groups', []))} 个 · "
          f"账套 {len(model.get('books', []))} 本")
    _report(f"_live_workspace 内部耗时（{repeat} 次里最好的一次）")

    # ---- 2) 单独量一次「纯建模」的 CPU 成本 ----
    TIMERS.clear()
    started = time.perf_counter()
    module.build_model({
        "cards": [], "books": [], "members": [], "statements": [], "rows": [],
        "allocations": [], "entries": [], "ledger_anchors": [],
        "captured_at": "2026-01-01T00:00:00+00:00", "source": "probe",
    }, card_id)
    print(f"\n  build_model（空快照，仅看函数体开销）"
          f" {(time.perf_counter() - started) * 1000:9.1f} ms")

    # ---- 3) 便宜的候选路径：只读「银行侧」所需的数据 ----
    TIMERS.clear()
    started = time.perf_counter()
    books = module._books(ctx)
    books_elapsed = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    statements = []
    for book in books:
        statements.extend(module._read_all(ctx, module.STMT_TABLE, book_id=book["id"],
                                           where={"card_id": int(card_id)}))
    stmt_elapsed = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    rows = []
    for stmt in statements:
        rows.extend(module._stmt_rows(ctx, stmt["id"], stmt["book_id"]))
    rows_elapsed = (time.perf_counter() - started) * 1000
    print(f"\n  银行侧（便宜的那一半）："
          f" 账套清单 {books_elapsed:.1f} ms · "
          f"对账单 {stmt_elapsed:.1f} ms · "
          f"原始行 {rows_elapsed:.1f} ms")
    print(f"    → 合计 {(books_elapsed + stmt_elapsed + rows_elapsed):.1f} ms，"
          f"对账单 {len(statements)} 份 / 原始行 {len(rows)} 行")

    print("\n  结论看两件事：")
    print("   ① confirm 内部那次 _live_workspace 的耗时；")
    print("   ② 其中「银行侧」占多少 —— 剩下的就是账面侧（账簿读取 + 分录匹配）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
