#!/usr/bin/env python3
"""验证桌面壳「真的把浏览器拉起来」—— 真 spawn，不是 dry-run。

**为什么要有这个。**
桌面壳唯一对外的副作用是 ``spawn()``（``launcher.py`` 里的
``subprocess.Popen``）。它坏掉的症状极其安静：双击什么也不发生，
没有报错、没有弹窗、没有非零退出码 —— 用户只会觉得「这软件是坏的」。
而 ``--dry-run`` 在 ``main()`` 里是**提前返回**的，压根走不到 ``spawn()``，
所以 CI 的 dry-run 全绿，并不等于「用户双击能开窗」。

**这个脚本验的是什么。**
它把三件事串成一条真链路，一次跑完：

1. **发现**：壳在**真实的 Edge 安装位置**上找到了浏览器
   （``find_browser()`` 的 ``expand_env`` + ``is_file`` 那段逻辑，
   只有根目录被换成了沙箱）；
2. **拉起**：壳真的 ``Popen`` 了这个浏览器，进程真的起来了
   （替身浏览器把收到的命令行写进了文件）；
3. **参数正确**：替身收到的参数 == 期望的参数，一个字不差
   （含 ``--app=`` / ``--user-data-dir=`` / ``--window-size=`` /
   ``--no-first-run`` / ``--no-default-browser-check``）。

**替身浏览器必须是真 exe、必须装在真实安装位置、必须不带参数运行。**
少一条，验到的就是另一件事：
- 装在临时目录、用 ``--browser`` 显式指定 → 验到的是「显式路径能跑」，
  不是「发现逻辑能跑」；
- 用 ``.bat`` 改名 → Windows 拒绝执行（错误 193），
  验到的是「spawn 抛异常」；
- 带着参数运行 → 绕过了「壳自己决定传什么参数」这件事。

**为什么用环境变量沙箱，而不是真的往 Program Files 里塞替身。**
CI runner 上真的装着 Edge。往那儿塞替身会破坏 runner 自己的浏览器，
还可能触发杀软。这里的做法是把 ``ProgramFiles(x86)`` / ``ProgramFiles`` /
``LOCALAPPDATA`` 三个环境变量指到临时目录：发现逻辑走的是**同一段代码**，
只是根目录换了 —— runner 的真 Edge 一根汗毛都不会掉，
用户的 ``%LOCALAPPDATA%`` 也不会被写脏。

用法：

    python tools/desktop-shell/launch_path_probe.py --exe <CWQ.exe> --stub <替身.exe>

    # 替身要先打成真 exe：
    python -m PyInstaller --noconfirm --clean --onefile --name msedge \
        --distpath <目录>/dist --workpath <目录>/build --specpath <目录> \
        tools/desktop-shell/stub_browser.py

**必须在 Windows 上跑**（真 Windows 或 Wine 里的 Windows Python）——
它要直接 ``Popen`` 一个 PE 文件。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping

# UTF-8 输出加固在 launcher.py 里（一份实现，脚本都从那儿取）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from launcher import force_utf8_output  # noqa: E402
from smoke_test import RC_OK, SmokeFailure, check, report, run_exe  # noqa: E402

# **期望值在这里独立写死，不 import 产品代码里的常量。**
#
# 理由：期望值如果复用产品自己的常量，那它永远不可能独立地失败 ——
# 产品改了值，期望值跟着一起变，闸门就永远开着。
# 这里写死的每一个字都是「这个壳**应该**做到什么」的外部约定：
# 目标地址、窗口尺寸、应用名（也就是 profile 目录名）。
EXPECTED_URL = "https://acct.xins.live"
EXPECTED_WINDOW_SIZE = "1360,880"
EXPECTED_APP_NAME = "财务记账系统"

# 替身浏览器的文件名必须与 launcher.py 的 BROWSER_CANDIDATES 里写的一模一样 ——
# 否则壳在那个位置根本找不到它，验到的会是「没找到浏览器」那条降级路径。
STUB_FILENAME = "msedge.exe"

# 替身在真实 Windows 上的安装位置（相对 Program Files (x86)）。
EDGE_SUBDIR = ("Microsoft", "Edge", "Application")

# 替身把命令行写到这个环境变量指定的文件里。
RECORD_ENV = "CWQ_STUB_RECORD_FILE"

# 壳日志里「启动 mode=... browser=...」那一行的关键字。
LOG_MARKER = "启动"


# ---------------------------------------------------------------------------
# 沙箱布局
# ---------------------------------------------------------------------------

def build_layout(root: Path) -> dict[str, Path]:
    """算出沙箱里各目录的位置。

    **为什么目录名要跟真 Windows 一模一样**（``Program Files (x86)``）：
    目录名本身不参与判定（判定看的是环境变量的值），但保持一致能让
    失败时的日志一眼看懂 —— 看到 ``Program Files (x86)`` 就知道是沙箱，
    不用去猜是不是改到了真目录。
    """
    pf86 = root / "Program Files (x86)"
    pf64 = root / "Program Files"
    local = root / "AppData" / "Local"
    return {
        "root": root,
        "pf86": pf86,
        "pf64": pf64,
        "local": local,
        # 替身要放在**真实的 Edge 安装位置**，否则验不到发现逻辑。
        "stub": pf86.joinpath(*EDGE_SUBDIR) / STUB_FILENAME,
        # 壳算出来的 profile 目录（app_data_dir / "browser-profile"）。
        "profile": local / EXPECTED_APP_NAME / "browser-profile",
        # 壳自己的日志。
        "log": local / EXPECTED_APP_NAME / "logs" / "shell.log",
    }


def set_env(env: dict[str, str], key: str, value: str) -> None:
    """设置环境变量，并**先删掉所有大小写不同的同名键**。

    **为什么必须这么写。**
    Windows 的环境变量名是大小写不敏感的，而 CPython 构造子进程环境块时
    是把 dict 的键**原样**写进去的 —— 同名不同大小写的两条会同时进环境块，
    子进程读到哪一条取决于排序，不受控。

    这不是理论问题：CI runner 的环境里本来就有 ``ProgramFiles(x86)``，
    而我们要覆盖的正是它。如果新旧两条同时存在，壳可能读到**真 Edge 的
    目录**，然后真的去启动 runner 上那个 Edge —— 沙箱就白做了，
    而且会在 runner 上弹出真的浏览器窗口。

    所以这里统一：先按大小写不敏感删干净，再写入唯一的键。
    """
    upper = key.upper()
    for existing in [k for k in env if k.upper() == upper]:
        del env[existing]
    env[key] = value


def build_child_env(
    base: Mapping[str, str],
    layout: Mapping[str, Path],
    record: Path,
) -> dict[str, str]:
    """构造传给壳子进程的环境。

    每一项都有理由：

    - ``ProgramFiles(x86)`` / ``ProgramFiles`` → 指向沙箱，让发现逻辑
      落在替身上，而**不是** runner 上真的 Edge；
    - ``LOCALAPPDATA`` → 指向沙箱，壳的 profile 与日志都不会写脏
      runner（也不会写脏开发者自己的机器）；
    - ``CI`` / ``CWQ_SHELL_NONINTERACTIVE`` → 让 ``is_interactive()``
      判定为「无人值守」，于是**任何错误提示都只写日志、不弹模态框**。
      模态框在无人环境里会**永久阻塞进程**（CI 上真卡过 11 分钟）；
    - ``CWQ_STUB_RECORD_FILE`` → 告诉替身把命令行记到哪儿去。
    """
    env = dict(base)
    set_env(env, "ProgramFiles(x86)", str(layout["pf86"]))
    set_env(env, "ProgramFiles", str(layout["pf64"]))
    set_env(env, "LOCALAPPDATA", str(layout["local"]))
    set_env(env, "CI", "1")
    set_env(env, "CWQ_SHELL_NONINTERACTIVE", "1")
    set_env(env, RECORD_ENV, str(record))
    return env


def expected_browser_args(layout: Mapping[str, Path]) -> list[str]:
    """壳应该传给浏览器的参数（顺序敏感）。

    抄的是 ``launcher.build_app_args()`` 的形状 —— 这里**故意不 import
    那个函数**：探针要验的是「产物真的这么传」，而不是「函数这么返回」。
    复用同一个函数会让两者一起错、错得一模一样，闸门就白设了。
    """
    return [
        f"--app={EXPECTED_URL}",
        f"--user-data-dir={layout['profile']}",
        f"--window-size={EXPECTED_WINDOW_SIZE}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


# ---------------------------------------------------------------------------
# 替身浏览器的安放与观测
# ---------------------------------------------------------------------------

def check_pe(path: Path, label: str) -> None:
    """确认它是个真的 Windows PE（``MZ`` 头）。

    **这道闸防的是「拿 .bat 改名当替身」**：Windows 会拒绝执行它，
    症状是 spawn 抛异常 —— 看起来像「壳坏了」，其实是替身不合格。
    """
    with path.open("rb") as handle:
        header = handle.read(2)
    check(
        header == b"MZ",
        f"{label} 不是 Windows PE（头是 {header!r}，应该是 b'MZ'）。\n"
        "替身必须是 PyInstaller 打出来的真 exe —— .bat 改名不行，"
        "Windows 会拒绝执行（错误 193）。",
    )


def install_stub(stub: Path, dest: Path) -> None:
    """把替身装到**真实的 Edge 安装位置**。

    为什么用复制而不是直接指过去：发现逻辑找的是
    ``%ProgramFiles(x86)%\\Microsoft\\Edge\\Application\\msedge.exe``
    这个**固定路径**，替身必须真的在那儿。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stub, dest)


def wait_for_record(
    path: Path,
    timeout: float,
    interval: float = 0.2,
    log_path: Path | None = None,
) -> list[str]:
    """等替身把命令行写出来。

    **为什么要等，而不是读一次就完。**
    壳 ``Popen`` 完就退出了 —— 它**不等**浏览器启动完（这是对的行为：
    用户双击后应该立刻能继续用电脑）。所以壳的进程已经结束了，
    替身可能还在解包、还在启动。等文件出现是唯一可靠的办法。

    轮询而不是死等：替身是先写 ``.part`` 再原子替换的，正常情况下
    第一次读到就是完整的；万一撞上写入中间，``.part`` 还没被替换，
    文件根本不存在，不会读到半个 JSON。

    :param log_path: 壳自己的日志。等不到记录时把它**贴进报错** ——
        那行日志直接写着「壳选中了哪个浏览器」，是定位这个问题最快的线索。
    """
    deadline = time.monotonic() + timeout
    while True:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = None
            if isinstance(data, list):
                return [str(item) for item in data]
        if time.monotonic() >= deadline:
            detail = ""
            if log_path is not None and log_path.is_file():
                try:
                    tail = log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                except OSError:
                    tail = []
                if tail:
                    detail = "\n壳的日志（最后几行）：\n  " + "\n  ".join(tail[-3:])
            raise SmokeFailure(
                f"等了 {timeout:.0f} 秒，替身浏览器没有记录到命令行参数。\n"
                f"记录文件：{path}\n"
                "如果这个文件根本不存在，说明替身**压根没被拉起来** —— "
                "问题在壳这一侧（发现、spawn、或路径里的中文被破坏）。"
                + detail
            )
        time.sleep(interval)


def read_log_line(layout: Mapping[str, Path]) -> str:
    """取壳日志里最后一条「启动 ...」那一行。取不到就返回空串。"""
    path = layout["log"]
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if LOG_MARKER in line:
            return line
    return ""


# ---------------------------------------------------------------------------
# 两个场景
# ---------------------------------------------------------------------------

def run_scenario(
    exe: Path,
    args: tuple[str, ...],
    record: Path,
    base_env: Mapping[str, str],
    layout: Mapping[str, Path],
    timeout: float,
) -> list[str]:
    """跑一次壳，把替身记下的命令行取回来。

    :param args: 传给壳的命令行参数。**发现场景必须是空的** ——
        空参数才是「用户双击」那一下，带了 ``--browser`` 就绕过了发现逻辑。
    :param record: 替身记录文件的路径。
    :param base_env: 基础环境（通常是 ``os.environ``）。

    **记录路径在这里一次性决定，不许外面再传一份。**
    环境变量里告诉替身「记到哪儿去」的那个路径，和探针轮询的那个路径，
    必须是**同一个**。第一次在 Wine 下跑探针时这两条不一致 ——
    环境变量指向 ``argv.json``，探针在等 ``argv-discovery.json`` ——
    结果替身明明把文件写对了，探针却在等另一个文件，报「没记录到参数」。
    症状极具误导性：看起来像「壳没把浏览器拉起来」，其实壳完全正常。

    所以环境在这里**现造**，record 只从这一个入口进来。
    """
    env = build_child_env(base_env, layout, record)
    rc, stdout, stderr = run_exe(exe, *args, timeout=timeout, env=env)
    check(
        rc == RC_OK,
        f"壳的退出码是 {rc}，应该是 {RC_OK}。\n"
        f"参数：{list(args)}\nstdout: {stdout}\nstderr: {stderr}\n"
        "退出码 2 = URL 被拒；4 = 拉起浏览器失败；5 = 没有可用浏览器。",
    )
    return wait_for_record(record, timeout=timeout, log_path=layout["log"])


def check_discovery_scenario(
    exe: Path,
    layout: dict[str, Path],
    base_env: Mapping[str, str],
    timeout: float,
) -> list[str]:
    """场景一：**不带任何参数**运行壳（= 用户双击那一下）。

    验的是「发现 → 拉起 → 参数」整条链路。
    """
    record = layout["root"] / "argv-discovery.json"
    argv = run_scenario(exe, (), record, base_env, layout, timeout)

    check(
        len(argv) == 6,
        f"替身收到的参数个数是 {len(argv)}，应该是 6（自己 1 个 + 浏览器参数 5 个）：\n"
        f"{argv}",
    )
    check(
        os.path.basename(argv[0]).lower() == STUB_FILENAME,
        f"被拉起的不是沙箱里的替身，而是 {argv[0]!r}。",
    )

    args = argv[1:]
    expected = expected_browser_args(layout)
    check(
        args == expected,
        "替身收到的参数与期望不一致。\n"
        f"实际：{args}\n期望：{expected}",
    )
    check(
        EXPECTED_APP_NAME in args[1],
        f"profile 路径里没有应用名 {EXPECTED_APP_NAME!r}：{args[1]!r}\n"
        "中文路径被 ANSI 编码破坏的话，Windows 上会指向一个不存在的目录，"
        "浏览器的登录态就存不下来。",
    )

    # 日志是「走的是发现这条路径」的直接证据：它记下了**实际选中的**
    # 浏览器路径。参数对了但路径是别人，就说明发现逻辑没被验到。
    line = read_log_line(layout)
    check(
        line != "",
        f"壳的日志里没有「{LOG_MARKER}」那一行：{layout['log']}\n"
        "壳连启动都没记 —— 它多半在弹框（无人环境下会被永久阻塞）。",
    )
    check(
        "mode=app-window" in line,
        f"壳不是以应用模式启动的：{line!r}\n"
        "mode=default-browser 说明它没找到浏览器，走了降级路径。",
    )
    check(
        str(layout["stub"]) in line,
        f"壳选中的浏览器不是沙箱里的替身：{line!r}\n"
        f"期望包含：{layout['stub']}\n"
        "选中了别的路径 = 这次验的是「显式指定」，不是「发现」。",
    )
    return args


def check_explicit_scenario(
    exe: Path,
    layout: dict[str, Path],
    base_env: Mapping[str, str],
    timeout: float,
    expected_args: list[str],
) -> None:
    """场景二：用 ``--browser`` 显式指定**另一个**替身。

    验的是「显式指定」与「自动发现」给出**完全相同**的参数 ——
    两条路径的参数只要有一处不一致，用户就会遇到「有时能开、有时开成
    普通标签页」这种极难复现的问题。

    故意用**另一个路径**的替身：这样还能顺带证明参数与浏览器的位置无关。
    """
    custom = layout["root"] / "custom-browser" / "browser.exe"
    custom.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(layout["stub"], custom)

    record = layout["root"] / "argv-explicit.json"
    argv = run_scenario(
        exe, ("--browser", str(custom)), record, base_env, layout, timeout
    )

    check(
        os.path.basename(argv[0]).lower() == "browser.exe",
        f"被拉起的不是显式指定的替身，而是 {argv[0]!r}。",
    )
    check(
        argv[1:] == expected_args,
        "显式指定浏览器时，参数与自动发现时不一致。\n"
        f"显式：{argv[1:]}\n发现：{expected_args}",
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    force_utf8_output()

    parser = argparse.ArgumentParser(
        prog="launch_path_probe",
        description="验证桌面壳真的把浏览器拉起来了（真 spawn，不是 dry-run）",
    )
    parser.add_argument("--exe", required=True, help="待验的壳 exe 路径")
    parser.add_argument("--stub", required=True, help="替身浏览器 exe 路径（真 PE）")
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="每一步的超时秒数（默认 180）",
    )
    parser.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="验完不删沙箱目录（排查用）",
    )
    args = parser.parse_args(argv[1:])

    if platform.system() != "Windows":
        report(
            "这个脚本必须在 Windows 上跑（真 Windows 或 Wine 里的 Windows Python）——\n"
            "它要直接 Popen 一个 PE 文件。在 macOS/Linux 上直接跑 exe 是跑不起来的。"
        )
        return 1

    exe = Path(args.exe).resolve()
    stub = Path(args.stub).resolve()

    # ---- 第一步：静态检查。不通过就别建沙箱了。
    static_checks = [
        ("壳 exe 存在", lambda: check(exe.is_file(), f"exe 不存在：{exe}")),
        ("替身存在", lambda: check(stub.is_file(), f"替身不存在：{stub}")),
        ("替身是真 PE", lambda: check_pe(stub, "替身浏览器")),
    ]
    for name, fn in static_checks:
        try:
            fn()
        except SmokeFailure as exc:
            report(f"❌ {name}\n   {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001 —— 探针要报出任何异常
            report(f"❌ {name}\n   意外异常：{exc!r}")
            return 1
        report(f"✅ {name}")

    # ---- 第二步：搭沙箱、跑两个场景。
    root = Path(tempfile.mkdtemp(prefix="cwq-launch-probe-"))
    layout = build_layout(root)
    timeout = float(args.timeout)
    try:
        layout["pf86"].mkdir(parents=True, exist_ok=True)
        layout["pf64"].mkdir(parents=True, exist_ok=True)
        layout["local"].mkdir(parents=True, exist_ok=True)
        install_stub(stub, layout["stub"])

        # 基础环境只留一份；每个场景在 run_scenario 里**现造**自己的环境
        # （记录路径必须在同一处决定，见 run_scenario 的说明）。
        base_env = os.environ
        discovered: dict[str, list[str]] = {}

        def scenario_discovery() -> None:
            discovered["args"] = check_discovery_scenario(
                exe, layout, base_env, timeout
            )

        def scenario_explicit() -> None:
            if "args" not in discovered:
                raise SmokeFailure(
                    "发现场景没过，没有可比的参数 —— 本项无法进行。"
                )
            check_explicit_scenario(
                exe, layout, base_env, timeout, discovered["args"]
            )

        scenario_checks = [
            ("不带参数运行 → 发现 → 拉起 → 参数", scenario_discovery),
            ("--browser 显式指定 → 参数一致", scenario_explicit),
        ]

        failures = 0
        for name, fn in scenario_checks:
            try:
                fn()
            except SmokeFailure as exc:
                failures += 1
                report(f"❌ {name}\n   {exc}")
            except Exception as exc:  # noqa: BLE001 —— 探针要报出任何异常
                failures += 1
                report(f"❌ {name}\n   意外异常：{exc!r}")
            else:
                report(f"✅ {name}")

        if failures:
            report(f"\n❌ {failures}/{len(scenario_checks)} 项没过")
            report(f"沙箱目录：{root}")
            return 1
        report(f"\n✅ {len(scenario_checks)}/{len(scenario_checks)} 项全过：{exe}")
        return 0
    finally:
        if args.keep_sandbox:
            report(f"沙箱保留在：{root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
