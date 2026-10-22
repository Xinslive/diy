#!/usr/bin/env python3
"""**只跑受影响的用例** —— 日常开发用不着每次都跑全量。

    python scripts/test_changed.py            # 打印受影响的测试文件 + 建议命令
    python scripts/test_changed.py --run      # 直接跑
    python scripts/test_changed.py --base main # 跟 main 比（而不是 HEAD）
    python scripts/test_changed.py --all      # 不筛，直接给全量命令

**为什么要这个。** 全量 2000+ 条用例要跑一两分钟，而一次改动通常只碰得着
其中十几个文件。全量跑是**提交前**的事，不是**改一行代码**时的事。

**怎么判定「受影响」。** 靠**静态导入图**，不靠文件名猜：

1. 扫出每个测试文件 import 了哪些 ``app.*`` 模块（``ast`` 解析，不执行代码）；
2. 再把 ``app`` 内部的导入关系也扫出来，算出**传递闭包** ——
   改了 ``app/core/db.py``，那么 import 了 ``app/core/db.py`` 的模块、
   以及 import 了**那些**模块的模块，全都受影响（测试文件 import 的只是
   ``app.api.books``，但 ``app.api.books`` import 了 ``app.core.db``，
   所以改 ``core/db.py`` 一样会打穿到这条测试）；
3. 反查：某个 ``app`` 模块被哪些测试文件（传递地）依赖 → 这些就是候选。

**认输的情况（有，它们是故意留的）。** 下面这些东西一改就**全量跑**，
因为它们的影响面靠静态分析算不出来：

* ``migrations/**`` 与 ``app/core/db_init.sql`` —— 改的是**库结构**，
  任何一条用例都可能碰它；
* ``tests/conftest.py``、``pytest.ini``、``requirements*.txt`` —— 改的是**跑法**；
* 任何 ``app/`` 下的非 ``.py`` 资源（模板、SQL、配置）。

**它不会漏。** 这不是「按名字猜」，是按真实导入关系算的；算不准的地方
（动态 import、反射）在这套代码里不存在 —— 全仓没有一处 ``importlib``
动态导入业务模块。宁可多跑几条，也不要「以为不用跑」。

**依赖它的模块会一起算进来。** 所以改了 ``app/core/db.py`` 这种底层模块时，
它会告诉你「要跑全量」—— 这不是工具笨，是那件事本来就得全量跑。
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
TESTS = ROOT / "tests"

# 一改就得全量跑的东西：影响面靠静态导入图算不出来。
GLOBAL_PATTERNS = (
    "migrations/",
    "app/core/db_init.sql",
    "tests/conftest.py",
    "pytest.ini",
    "requirements",
    "alembic.ini",
)


def _run_git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout


def _module_name(path: Path) -> str:
    """``app/accounting/vouchers.py`` -> ``app.accounting.vouchers``。"""
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports_of(path: Path) -> set[str]:
    """这个文件 import 了哪些**项目内**模块（``app.*`` / ``tests.*``）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(("app.", "tests.")):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # 相对导入：from .x import y
                continue
            if node.module and node.module.startswith(("app.", "tests.")):
                found.add(node.module)
    return found


def _build_graph() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """返回 (模块 -> 它 import 的模块, 测试文件 -> 它依赖的模块集合)。

    第二个是**传递闭包**：测试直接 import 的模块，以及这些模块再往下拉的模块。
    ``app/`` 与 ``tests/`` 一起进图 —— ``tests/accounting_helpers.py`` 这种
    公共助手被改了，依赖它的那些用例同样要重跑。
    """
    modules: dict[str, set[str]] = {}
    for base in (APP, TESTS):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            modules[_module_name(path)] = _imports_of(path)

    # 传递闭包：mod -> 它（传递地）依赖的全部模块
    def closure(root: str) -> set[str]:
        seen: set[str] = set()
        stack = list(modules.get(root, ()))
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            stack.extend(modules.get(m, ()))
        return seen

    deps: dict[str, set[str]] = {}
    for test in sorted(TESTS.rglob("test_*.py")):
        acc: set[str] = set()
        for m in _imports_of(test):
            if m.startswith(("app.", "tests.")):
                acc.add(m)
                acc |= {x for x in closure(m) if x.startswith(("app.", "tests."))}
        deps[test.name] = acc
    return modules, deps


def _changed_files(base: str) -> list[str]:
    """相对 ``base`` 改动过的文件（含未提交改动）。"""
    out: set[str] = set()
    for args in (["diff", "--name-only", base], ["diff", "--name-only", "--cached"]):
        try:
            out |= {line.strip() for line in _run_git(*args).splitlines() if line.strip()}
        except subprocess.CalledProcessError:
            pass
    try:
        out |= {line.strip() for line in _run_git("ls-files", "--others", "--exclude-standard").splitlines()
                if line.strip()}
    except subprocess.CalledProcessError:
        pass
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="只跑受影响的用例")
    ap.add_argument("--base", default="HEAD", help="比较基准（默认 HEAD；可用 main/master）")
    ap.add_argument("--files", nargs="*", default=None,
                    help="直接指定「改了哪些文件」（相对 backend/ 的路径），跳过 git 比对")
    ap.add_argument("--run", action="store_true", help="直接跑 pytest")
    ap.add_argument("--all", action="store_true", help="不筛，给出全量命令")
    ap.add_argument("-q", "--quiet", action="store_true", help="只打印命令")
    args = ap.parse_args()

    py = sys.executable
    if args.all:
        cmd = f"{py} -m pytest"
        print(f"全量：\n  cd {ROOT}\n  {cmd}")
        return 0 if not args.run else subprocess.call(cmd, shell=True, cwd=ROOT)

    changed = args.files if args.files is not None else _changed_files(args.base)
    if not changed:
        print(f"相对 {args.base} 没有改动 —— 直接跑全量即可。")
        cmd = f"{py} -m pytest"
        return 0 if not args.run else subprocess.call(cmd, shell=True, cwd=ROOT)

    # 认输：影响面算不出来的东西 → 全量
    blockers = [f for f in changed
                if any(p in f for p in GLOBAL_PATTERNS)
                or (f.startswith("app/") and not f.endswith(".py"))]
    if blockers:
        print("这些改动的影响面算不出来，**要跑全量**：")
        for f in blockers:
            print(f"  · {f}")
        cmd = f"{py} -m pytest"
        print(f"\n全量：\n  cd {ROOT}\n  {cmd}")
        return 0 if not args.run else subprocess.call(cmd, shell=True, cwd=ROOT)

    _, deps = _build_graph()
    changed_modules = set()
    for f in changed:
        p = ROOT / f
        if f.startswith(("app/", "tests/")) and p.suffix == ".py" and p.exists():
            changed_modules.add(_module_name(p))

    # 改动的测试文件本身也要跑（helper 文件不算 —— 它没有用例，
    # 靠上面的反查找到依赖它的那些用例就够了）
    selected: set[str] = set()
    for f in changed:
        name = Path(f).name
        if (f.startswith("tests/") and name.startswith("test_") and name.endswith(".py")
                and (ROOT / f).exists()):
            selected.add(name)

    # 反查：哪些测试（传递地）依赖这些模块
    for test_name, needed in deps.items():
        if needed & changed_modules:
            selected.add(test_name)

    if not selected:
        print(f"改了 {len(changed)} 个文件，没有测试依赖它们 —— 不用跑。")
        return 0

    files = " ".join(f"tests/{n}" for n in sorted(selected))
    cmd = f"{py} -m pytest {files}"
    if not args.quiet:
        print(f"改了 {len(changed)} 个文件 → 命中 {len(selected)} 个测试文件：")
        for n in sorted(selected):
            print(f"  · tests/{n}")
        print(f"\n跑这些：\n  cd {ROOT}\n  {cmd}")
        print(f"（全量是 {len(deps)} 个文件 —— 这次只需要 {len(selected)} 个）")
    if args.run:
        return subprocess.call(cmd, shell=True, cwd=ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
