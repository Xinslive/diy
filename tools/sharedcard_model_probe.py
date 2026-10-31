"""工作台模型的**指纹** —— 用来比对「两套读法给出的模型是不是同一份」。

为什么需要它
------------
插件把「跨账套一次读」（宿主 SDK 1.5.0 起）换掉了「按账套读一遍」。
这种改动**看不见**：页面上还是那些数字，但模型里某个列表的**顺序**
变了、某个字段的**空值**变了，界面上就会以「顺序不对」「金额不对」
的样子冒出来 —— 而那时候已经上线了。

所以判据是**指纹**：把整个模型（去掉时间戳）序列化后取 sha256。
生产（旧读法）与本地（新读法）各跑一次，指纹**必须逐字相同**。

只读
----
全程只调用读能力；插件包目录**复制到临时目录**再装载（``exec_module``
会在源文件旁边写 ``__pycache__/*.pyc`` —— 多一个文件，宿主验签就会抛
``FILE_SET_MISMATCH``，页面资源全 404）。

用法::

    python3 sharedcard_model_probe.py <插件包目录> <card_id> <user_id> [输出JSON路径]
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import tempfile

BACKEND_ROOT = os.environ.get("CWQ_BACKEND_ROOT", "/www/wwwroot/acct.xins.live/backend")

#: **必须 cd 到应用根再 import**：``config.py`` 的 ``env_file=".env"`` 是
#: **相对 cwd** 的 —— 站在 /tmp 里跑会退回源码里写死的开发默认值。
os.chdir(BACKEND_ROOT)
sys.path.insert(0, BACKEND_ROOT)

from app.plugins import public_api  # noqa: E402

#: 模型里**每次跑都会变**的字段 —— 比指纹时要剔掉。
VOLATILE = {"captured_at"}


def _load_plugin_from_copy(directory: str):
    import importlib.util

    staging = tempfile.mkdtemp(prefix="cwq-model-probe-")
    target = os.path.join(staging, "plugin")
    shutil.copytree(directory, target)
    manifest = json.load(open(os.path.join(target, "plugin.json"), encoding="utf-8"))
    entry = os.path.join(target, manifest["contributes"]["backend_entry"])
    spec = importlib.util.spec_from_file_location("cwq_model_probe_main", entry)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cwq_model_probe_main"] = module
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return manifest, module, staging


def fingerprint(model: dict) -> str:
    """整个模型的 sha256（剔掉时间戳）。"""
    payload = {k: v for k, v in model.items() if k not in VOLATILE}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def section_hash(rows) -> str:
    text = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: 能力调用计数：名字 → (次数, 累计毫秒)
COUNTERS: dict[str, list] = {}


def _counted(name: str, func):
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            slot = COUNTERS.setdefault(name, [0, 0.0])
            slot[0] += 1
            slot[1] += (time.perf_counter() - started) * 1000
    return wrapper


def instrument(ctx):
    """给 ctx 的能力方法套上计数器（只计数，不改行为）。"""
    for name in list(vars(ctx)):
        attr = getattr(ctx, name)
        if not callable(attr):
            continue
        try:
            setattr(ctx, name, _counted("ctx." + name, attr))
        except (AttributeError, TypeError):
            pass
    data = getattr(ctx, "data", None)
    if data is not None:
        for name in ("read", "count"):
            func = getattr(data, name, None)
            if callable(func):
                try:
                    setattr(data, name, _counted("ctx.data." + name, func))
                except (AttributeError, TypeError):
                    pass


def main() -> int:
    directory = sys.argv[1]
    card_id = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    user_id = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    output = sys.argv[4] if len(sys.argv) > 4 else ""
    repeat = max(1, int(os.environ.get("CWQ_PROBE_REPEAT", "5")))

    manifest, module, staging = _load_plugin_from_copy(directory)
    public_api.current_principal.set((manifest["id"], user_id, None))
    ctx = public_api.context_for(manifest, plugin=True)
    instrument(ctx)

    # **取多次的最小值**：跑着真实流量的机器上，单次测量能差三倍。
    best = None
    model = None
    for _ in range(repeat):
        COUNTERS.clear()
        started = time.perf_counter()
        with public_api.principal_scope():
            model = module._live_workspace(ctx, card_id)
        elapsed = (time.perf_counter() - started) * 1000
        if best is None or elapsed < best:
            best = elapsed
            best_counters = {k: list(v) for k, v in COUNTERS.items()}

    report = {
        "plugin_version": manifest["version"],
        "plugin_digest": manifest.get("signature", {}).get("value", "")[:16],
        "card_id": card_id,
        "workspace_ms_min": round(best, 1),
        "capability_calls": {k: v[0] for k, v in sorted(best_counters.items())},
        "capability_ms": {k: round(v[1], 1) for k, v in sorted(best_counters.items())},
        "model_fingerprint": fingerprint(model),
        "counts": {
            "transactions": len(model.get("transactions") or []),
            "entries": len(model.get("entries") or []),
            "groups": len(model.get("groups") or []),
            "history": len(model.get("history") or []),
            "members": len(model.get("members") or []),
            "books": len(model.get("books") or []),
        },
        "sections": {
            "transactions": section_hash(model.get("transactions") or []),
            "entries": section_hash(model.get("entries") or []),
            "groups": section_hash(model.get("groups") or []),
            "history": section_hash(model.get("history") or []),
            "members": section_hash(model.get("members") or []),
            "ledger": section_hash(model.get("ledger") or {}),
            "summary": section_hash(model.get("summary") or {}),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(model, handle, ensure_ascii=False, sort_keys=True, default=str)
        print(f"\n完整模型已写到 {output}")
    shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
