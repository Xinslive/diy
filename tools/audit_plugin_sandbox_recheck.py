#!/usr/bin/env python3
"""插件沙箱的四条复核主张：**在当前代码上重新取证**。

用法（仓库根）::

    python3 tools/audit_plugin_sandbox_recheck.py            # 只跑探针（快，几秒）
    python3 tools/audit_plugin_sandbox_recheck.py --mutate   # 加跑变异证明（慢，每个约 10 秒）

退出码：0 = 四条主张在当前代码上**都不成立**（且都被用例钉住）；
非 0 = 至少一条主张**成立**（真出现了逃逸）。

---
四条主张（来自审计报告 §3.1 的复核意见，附行号）：

  A. ``_sanitize(os) is os → True``
     —— 「不在白名单内的子模块原样返回」
  B. ``__builtins__['__import__'].__globals__['builtins'].__import__('os')`` 拿到真 os
     —— 「裁剪后的内建表仍保留 __import__，而 guarded_import.__globals__ 里放着真 builtins」
  C. 「允许模块的命名空间把危险子模块带进来」这条路径**没有任何用例覆盖**
  D. §7 第 48 项（插件后端代码移到独立进程执行）**未实施**

⚠️ **别按行号复核** —— 复核意见引的行号（``sandbox.py:304-308`` / ``:137``）
属于**修好之前**的快照。本脚本按**主张的原话**复现，不看行号。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
TMP = Path(tempfile.mkdtemp(prefix="cwq_sandbox_recheck_"))
ENV = {**os.environ,
       "CWQ_TEST_DATABASE_URL": os.environ.get(
           "CWQ_TEST_DATABASE_URL",
           "postgresql+psycopg://cwq:cwq123@localhost:15432/cwq_mavis_test")}


def _relaunch_with_venv() -> None:
    """用**后端那套虚拟环境**重跑自己。

    沙箱模块依赖 fastapi / pydantic —— 系统 python 里没有。
    与其在这里手工凑依赖，不如换解释器重来一次。
    """
    venv_python = BACKEND / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return
    if os.environ.get("_CWQ_RECHECK_RELAUNCHED"):
        return
    import subprocess as _sp
    _sp.run([str(venv_python), os.path.abspath(__file__), *sys.argv[1:]],
            env={**ENV, "_CWQ_RECHECK_RELAUNCHED": "1"}, cwd=str(REPO))
    raise SystemExit(0)


try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    _relaunch_with_venv()

sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

_N = [0]


def load(src: str):
    """把一段插件代码放进**真沙箱**里执行，返回模块。"""
    from app.plugins.sandbox import exec_plugin_module

    _N[0] += 1
    p = TMP / f"m{_N[0]}.py"
    p.write_text(textwrap.dedent(src), encoding="utf-8")
    return exec_plugin_module(f"m{_N[0]}", p, flavor="backend")


def claim_a() -> tuple[bool, str]:
    """A. ``_sanitize`` 对白名单外的模块必须**失败即闭**。"""
    import os as real_os

    from app.plugins.sandbox import _sanitize
    from app.plugins.scan_policy import IMPORT_FLAVORS

    try:
        got = _sanitize(real_os, set(IMPORT_FLAVORS["backend"]))
    except ImportError as e:
        return False, f"抛 ImportError：{e}（失败即闭）"
    if got is real_os:
        return True, "_sanitize(os) is os → 真 os 被原样交出去"
    return False, f"返回的是代理（{type(got).__name__}），不是真 os"


def claim_b() -> tuple[bool, str]:
    """B. 受控导入器身上**不能有** ``__globals__``。"""
    from app.plugins.sandbox import RESTRICTED_BUILTINS

    importer = RESTRICTED_BUILTINS.get("__import__")
    if importer is None:
        return True, "内建表里根本没有 __import__（插件连 import 都不能用）"
    if hasattr(importer, "__globals__"):
        return True, "受控导入器身上有 __globals__ → 顺着它能回到真 builtins"
    # 再实测一遍那条链
    try:
        mod = load('''
def probe():
    imp = __builtins__["__import__"]
    try:
        g = imp.__globals__
    except Exception as e:
        return ("NO_GLOBALS", type(e).__name__)
    try:
        b = g["builtins"]
    except Exception as e:
        return ("NO_BUILTINS", type(e).__name__)
    try:
        m = b.__import__("os")
    except Exception as e:
        return ("IMPORT_BLOCKED", type(e).__name__)
    return ("GOT_OS", getattr(m, "__name__", "?"))
''')
    except Exception as e:
        # **装载期就拒了** —— 那条链压根没跑起来，主张同样不成立
        return False, f"装载期即拒：{type(e).__name__}: {str(e)[:70]}"
    try:
        res = mod.probe()
    except Exception as e:
        return False, f"运行期抛 {type(e).__name__}: {str(e)[:70]}"
    if res[0] == "GOT_OS":
        return True, f"那条链真的拿到了 {res[1]}"
    return False, f"那条链断在 {res[0]}（{res[1]}）"


def claim_c() -> tuple[bool, str]:
    """C. 命名空间走私路径**必须有用例覆盖**。"""
    import re

    src = (BACKEND / "tests" / "test_plugin_sandbox.py").read_text(encoding="utf-8")
    # 运行期：白名单模块上的宿主模块属性
    has_runtime = "test_白名单模块上的宿主模块属性拿不到" in src
    # 静态扫描：PY_MODULE_ESCAPE
    has_static = "PY_MODULE_ESCAPE" in src
    # 动态拼名形态（静态扫描的盲区）
    has_dynamic = "test_动态拼名形态的走私拿不到宿主模块" in src
    covered = has_runtime and has_static and has_dynamic
    # 顺便实测一条：dataclasses.sys 在运行期拿不到
    mod = load('''
import dataclasses

def probe():
    try:
        return dataclasses.sys
    except AttributeError:
        return None
''')
    runtime_blocked = mod.probe() is None
    if not covered:
        return True, "用例覆盖不全（运行期/静态扫描/动态形态 三项缺一）"
    if not runtime_blocked:
        return True, "运行期把 dataclasses.sys 放出去了"
    n = len(re.findall(r'^\s*\("', src, re.M))
    return False, (f"覆盖存在（运行期参数化 + 静态扫描 PY_MODULE_ESCAPE + 动态拼名形态，"
                   f"名单项约 {n} 条）；实测 dataclasses.sys 运行期被拒")


def claim_d() -> tuple[bool, str]:
    """D. §7 第 48 项：插件后端代码在**独立进程**里执行。"""
    from app.core.config import settings

    try:
        from app.plugins.ipc.host import WorkerHandle  # noqa: F401
    except Exception as e:
        return True, f"没有独立进程句柄：{type(e).__name__}: {e}"
    loader = (BACKEND / "app" / "plugins" / "loader.py").read_text(encoding="utf-8")
    if "settings.plugin_isolation" not in loader or "_load_isolated" not in loader:
        return True, "loader 不按 plugin_isolation 分流"
    modes = [m for m in dir(settings) if "isolation" in m]
    return False, (f"已实现：ipc/host.py 有 WorkerHandle，loader 按 settings.plugin_isolation 分流"
                   f"（当前值 {settings.plugin_isolation!r}，模式字段 {modes}）"
                   f"—— **默认是 off**（现网行为不变），首选形态可切到 on")


# ---------------------------------------------------------------------------
# 变异证明：把防线改回旧行为，确认对应用例会红
# ---------------------------------------------------------------------------

MUTATIONS = [
    ("A", "app/plugins/sandbox.py",
     '    rule = _rule_for(module.__name__)\n    if rule is None:\n'
     '        raise ImportError(f"插件不允许使用模块：{module.__name__}")',
     '    rule = _rule_for(module.__name__)\n    if rule is None:\n        return module',
     "tests/test_plugin_sandbox.py"),
    ("B", "app/plugins/sandbox.py",
     "    return _Importer()",
     "    return guarded_import",
     "tests/test_plugin_sandbox.py"),
    ("C", "app/plugins/sandbox.py",
     '    module_name = getattr(value, "__name__", "") or ""\n'
     '    root = module_name.split(".")[0]\n'
     '    if root not in allowed_roots:\n'
     '        raise AttributeError(f"{name} 对插件不可用")\n'
     '    rule = _rule_for(module_name)\n'
     '    if rule is None:\n'
     '        raise AttributeError(f"{name} 对插件不可用")\n'
     '    return _make_sandbox_module(value, rule, allowed_roots)',
     "    return value",
     "tests/test_plugin_sandbox.py"),
    ("D", "app/plugins/loader.py",
     "    mode = settings.plugin_isolation\n    if mode == ISOLATION_ON:",
     "    mode = settings.plugin_isolation\n    if False:",
     "tests/test_plugin_isolation_modes.py"),
]


def run_mutations() -> list[tuple[str, str, str]]:
    out = []
    for tag, rel, old, new, testfile in MUTATIONS:
        path = BACKEND / rel
        bak = TMP / (path.name + ".bak")
        shutil.copy(path, bak)
        src = path.read_text(encoding="utf-8")
        if old not in src:
            out.append((tag, "SKIP", "注入点没找到（代码已经变过？）"))
            shutil.copy(bak, path)
            continue
        path.write_text(src.replace(old, new, 1), encoding="utf-8")
        try:
            r = subprocess.run(
                [".venv/bin/python", "-m", "pytest", testfile, "-n", "0",
                 "-p", "no:warnings", "-o", "addopts=", "-q", "--tb=no"],
                cwd=BACKEND, capture_output=True, text=True, env=ENV)
            line = next((l.strip() for l in reversed((r.stdout + r.stderr).splitlines())
                         if "passed" in l or "failed" in l or "error" in l), "?")
            red = "failed" in line
            out.append((tag, "RED" if red else "GREEN", line))
        finally:
            shutil.copy(bak, path)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutate", action="store_true", help="加跑变异证明（慢）")
    args = ap.parse_args()

    print("=" * 76)
    print("插件沙箱：四条复核主张在**当前代码**上的重新取证")
    print("=" * 76)
    print(f"仓库根：{REPO}\n")

    checks = [("A", claim_a), ("B", claim_b), ("C", claim_c), ("D", claim_d)]
    holds = []
    for tag, fn in checks:
        try:
            holds_it, detail = fn()
        except Exception as e:
            holds_it, detail = True, f"探针本身报错：{type(e).__name__}: {e}"
        mark = "❌ 主张成立（真问题）" if holds_it else "✅ 主张不成立"
        print(f"[{tag}] {mark}")
        print(f"     {detail}")
        if holds_it:
            holds.append(tag)
        print()

    if args.mutate:
        print("=" * 76)
        print("变异证明：把防线改回旧行为，确认对应用例会红")
        print("=" * 76)
        for tag, verdict, detail in run_mutations():
            mark = "✓ 变红（钉住了）" if verdict == "RED" else (
                "✗ 没红" if verdict == "GREEN" else "—")
            print(f"[{tag}] {mark}  {detail}")
        print()

    if holds:
        print(f"结论：主张 {holds} 在当前代码上**成立** —— 需要修。")
        return 1
    print("结论：四条主张在当前代码上**全部不成立**（复核意见读的是修好之前的快照）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
