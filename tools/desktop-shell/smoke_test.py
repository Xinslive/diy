#!/usr/bin/env python3
"""对构建出来的 exe 跑一遍冒烟验证。

**为什么要有这个**：PyInstaller 的打包失败方式很安静 —— 它不报错，
它产出一个「双击没反应」的 exe。静态检查（比如「spec 文件语法对不对」）
完全测不出来：那些错误只在**跑起来的时候**才暴露。

所以这个脚本做的是**真跑一次 exe**，然后断言它「打算怎么做」：
``--dry-run`` 把决策写成 JSON 出来，这里逐条核对。

验的是什么：

1. exe 是个真的 Windows PE（``MZ`` 头）—— 排除「构建产物根本不是 exe」
2. ``--dry-run`` 退出码 0，且产出的 JSON 可解析
3. 目标地址是 acct.xins.live
4. 找到浏览器时是「应用模式」，且参数里带 ``--app=``
5. ``--no-app-mode`` 时退回默认浏览器，且参数为空
6. ``--browser <显式路径>`` 时走「自定义浏览器」分支
7. profile 目录是**绝对路径**（相对路径会被解析到随机的工作目录）
8. 非 http/https 的地址被拒（退出码 2）

用法：
    python3 tools/desktop-shell/smoke_test.py <exe 路径>
    python3 tools/desktop-shell/smoke_test.py tools/desktop-shell/dist/CWQ.exe

    # 仅供本机跨平台验证脚本逻辑时用（macOS/Linux 上没有 Windows exe）。
    # **CI 里绝不能传这个开关** —— 它会跳过 PE 格式那道闸，而那正是
    # 「这东西到底是不是一个 Windows 可执行文件」的唯一判据。
    python3 tools/desktop-shell/smoke_test.py --allow-non-pe <可执行文件>
"""
from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

# UTF-8 输出加固在 launcher.py 里（一份实现，脚本都从那儿取）。
# Windows 控制台默认 cp936/cp1252，print("✅") 会抛 UnicodeEncodeError。
#
# **这个脚本尤其不能崩在 print 上**：它是 CI 的主闸。它自己因为编码问题
# 挂掉，等于闸门自己塌了 —— 那比「没这道闸」还糟，因为日志上会显示
# 「跑过了」，只是没跑成。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from launcher import force_utf8_output  # noqa: E402

EXPECTED_URL = "https://acct.xins.live"


def report(text: str) -> None:
    """打印一条结果，且**绝不因编码问题抛异常**。

    兜底 reason：``force_utf8_output()`` 已经把编码修好了，理论上不会抛。
    但 stdout 在某些环境（管道被关闭、被重定向到 None）下不可写 ——
    一道校验闸因为「打印不出结果」而中途退出，比报个假阴性更糟。

    （公开而不带下划线：``launch_path_probe.py`` 复用同一套「断言 + 汇报」。）
    """
    try:
        print(text)
    except Exception:
        pass

# exe 的合法退出码（与 launcher.py 的 main() 返回值对应）
RC_OK = 0
RC_BAD_URL = 2


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def run_exe(
    exe: Path,
    *args: str,
    timeout: int = 45,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """跑一次 exe，返回 ``(退出码, stdout, stderr)``。

    :param env: 子进程的环境变量。``None`` 表示继承当前进程的环境。
        ``launch_path_probe.py`` 用它把 ``ProgramFiles(x86)`` 等
        指向沙箱 —— 传 ``None`` 就会继承真实环境，沙箱就失效了。

    **这里刻意不用 ``capture_output=True``（管道捕获）。**

    原因：exe 是用 ``console=False`` 打的 —— Windows 上它是 **GUI 子系统**
    程序。这类程序与普通控制台程序在**管道**上的行为不同，父进程用管道
    读它的输出时可能**永远等不到 EOF**，于是 ``subprocess.run`` 挂死。

    这不是理论：CI 第一次跑冒烟验证，5 条用例**每条都耗满了 120 秒超时**
    （5 × 120s ≈ 10 分钟，整整卡了 11 分钟我才取消掉）。

    所以这里把 stdout/stderr **重定向到临时文件**，不建管道；输出内容
    从文件读。要断言的数据本来就走 ``--plan-out`` 那个文件，与管道无关。

    超时时**杀整个进程树**：exe 若自己拉起子进程（浏览器），只杀父进程
    会留下孤儿继续占着管道。

    **超时预算：45 秒。** PyInstaller 一文件模式每次启动要解包，实测约
    3~4 秒，所以 45 秒已经很宽松。而超时值是**乘以用例数**的 —— 原来
    用 120 秒，4 条用例一次失败就是 8 分钟起步，CI 上看着像「卡住了」；
    现在一次失败最多 3 分钟，定位问题的人不用干等。

    （顺带一句：第一次跑这条 workflow 时，Windows 上这一步卡了整整
    11 分钟 —— 每条用例都耗满了 120 秒超时。那次的根因是 exe 每次调用
    都不返回；改成文件重定向 + 显式 stdin=DEVNULL 之后不再复现。）

    **临时目录用 ``ignore_cleanup_errors=True``：**
    exe 拉起的子进程（浏览器）会**继承** stdout/stderr 句柄，而壳
    ``Popen`` 完就退出了 —— 父进程返回时，那个子进程往往还开着这两个
    文件。于是删临时目录会撞上「共享冲突」（WinError 32）。

    这不是理论问题：``launch_path_probe.py`` 第一次在 Wine 下跑就撞上了，
    报的是 ``PermissionError: [WinError 32]``。而它其实是**正常现象**
    （子进程还活着），不该让整道闸失败。清理失败最多留下两个空文件，
    比起把一次成功的验证报成失败，划算得多。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        out_path = Path(tmp) / "stdout.txt"
        err_path = Path(tmp) / "stderr.txt"
        cmd = [str(exe), *args]
        with out_path.open("wb") as out_fh, err_path.open("wb") as err_fh:
            process = subprocess.Popen(
                cmd,
                stdout=out_fh,
                stderr=err_fh,
                stdin=subprocess.DEVNULL,
                env=env,
            )
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(process)
                raise
        return (
            returncode,
            out_path.read_text(encoding="utf-8", errors="replace"),
            err_path.read_text(encoding="utf-8", errors="replace"),
        )


def kill_tree(process: subprocess.Popen) -> None:
    """杀掉进程及其所有子进程。

    Windows 上用 ``taskkill /T /F``（杀进程树）；其它平台先杀进程组。
    杀不干净也要保证不抛 —— 这条路径本身就在处理一个已经出问题的进程。
    """
    try:
        if platform.system() == "Windows":
            # taskkill 自己的输出用 DEVNULL 丢掉 —— 它是清理路径上的
            # 最后一道，输出没有意义，而建管道会引入新的挂死可能。
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
        process.wait(timeout=30)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def run_dry_run(exe: Path, *extra: str) -> tuple[int, dict]:
    """跑一次 ``--dry-run``，返回 ``(退出码, 计划 JSON)``。"""
    with tempfile.TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / "plan.json"
        try:
            returncode, stdout, stderr = run_exe(
                exe, "--dry-run", "--plan-out", str(plan_path), *extra
            )
        except subprocess.TimeoutExpired:
            raise SmokeFailure(
                f"exe 跑了 45 秒还没退出（超时）。\n"
                "最可能的原因：它在等人点一个模态对话框 —— "
                "dry-run 模式不应该弹框，见 launcher.py 的 show_message(suppress=...)。"
            ) from None
        check(
            plan_path.is_file(),
            f"--dry-run 没有产出 plan 文件（退出码 {returncode}）。\n"
            f"stdout: {stdout}\nstderr: {stderr}",
        )
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        return returncode, plan


def check_exe_format(exe: Path, allow_non_pe: bool = False) -> None:
    """确认它是个真的 Windows PE。"""
    check(exe.is_file(), f"exe 不存在：{exe}")
    with exe.open("rb") as handle:
        header = handle.read(2)
    if allow_non_pe:
        return
    check(
        header == b"MZ",
        f"{exe} 不是 Windows PE（头是 {header!r}，应该是 b'MZ'）。"
        "可能是 Linux/macOS 上交叉打包的产物。",
    )


def check_default_plan(exe: Path) -> None:
    rc, plan = run_dry_run(exe, "--url", EXPECTED_URL)
    check(rc == RC_OK, f"默认 dry-run 退出码是 {rc}，应该是 {RC_OK}")
    check(
        plan["target_url"] == EXPECTED_URL,
        f"目标地址是 {plan['target_url']!r}，应该是 {EXPECTED_URL!r}",
    )
    check(
        plan["mode"] in ("app-window", "default-browser"),
        f"未知的 mode：{plan['mode']!r}",
    )

    if plan["mode"] == "app-window":
        args = plan["args"]
        check(
            any(a.startswith("--app=") for a in args),
            f"应用模式但参数里没有 --app=：{args}",
        )
        check(
            any(a.startswith("--user-data-dir=") for a in args),
            f"应用模式但参数里没有 --user-data-dir=：{args}",
        )
        check(
            plan["browser"] is not None,
            "应用模式但 browser 是 None",
        )

    profile = Path(plan["profile_dir"])
    check(
        profile.is_absolute(),
        f"profile 目录不是绝对路径：{plan['profile_dir']!r}。"
        "相对路径会被解析到当前工作目录，而双击 exe 时那个目录是随机的。",
    )


def check_no_app_mode(exe: Path) -> None:
    rc, plan = run_dry_run(exe, "--url", EXPECTED_URL, "--no-app-mode")
    check(rc == RC_OK, f"--no-app-mode 退出码是 {rc}，应该是 {RC_OK}")
    check(
        plan["mode"] == "default-browser",
        f"--no-app-mode 时 mode 是 {plan['mode']!r}，应该是 'default-browser'",
    )
    check(plan["args"] == [], f"--no-app-mode 时不该有浏览器参数：{plan['args']}")


def check_explicit_browser(exe: Path) -> None:
    """``--browser`` 指向一个确实存在的文件时，必须走「自定义浏览器」分支。

    用**壳自己**当那个「浏览器」：这里只验决策，不真拉起它，
    所以只要它是个存在的可执行文件就行 —— 而 exe 自己一定满足。
    """
    rc, plan = run_dry_run(exe, "--url", EXPECTED_URL, "--browser", str(exe))
    check(rc == RC_OK, f"--browser 退出码是 {rc}，应该是 {RC_OK}")
    check(
        plan["mode"] == "app-window",
        f"显式指定浏览器时 mode 是 {plan['mode']!r}，应该是 'app-window'",
    )
    check(
        plan["browser"] is not None and plan["browser"]["name"] == "custom",
        f"显式指定浏览器时 browser 不是 custom：{plan['browser']}",
    )


def run_raw(exe: Path, *extra: str) -> tuple[int, str, str]:
    """跑一次 exe，**不**要求它产出 plan 文件。

    用于验「被拒绝」的路径：URL 非法时 launcher 是在生成计划**之前**就
    返回 2 的，所以那一路根本没有 plan.json 可读 —— 用 run_dry_run 去验它
    会得到「没有产出 plan 文件」这种把原因带偏的报错。

    不用管道捕获的理由见 :func:`run_exe`。
    """
    try:
        return run_exe(exe, *extra)
    except subprocess.TimeoutExpired:
        raise SmokeFailure(
            "exe 跑了 45 秒还没退出（超时）。\n"
            "最可能的原因：它在等人点一个模态对话框 —— "
            "dry-run 模式不应该弹框，见 launcher.py 的 show_message(suppress=...)。"
        ) from None


def check_rejects_bad_url(exe: Path) -> None:
    returncode, stdout, stderr = run_raw(
        exe, "--dry-run", "--url", "file:///C:/Windows/System32/cmd.exe"
    )
    check(
        returncode == RC_BAD_URL,
        f"非 http/https 地址的退出码是 {returncode}，"
        f"应该是 {RC_BAD_URL}（被拒绝）。\n"
        f"stdout: {stdout}\nstderr: {stderr}",
    )


def main(argv: list[str]) -> int:
    force_utf8_output()
    allow_non_pe = False
    args = list(argv[1:])
    if args and args[0] == "--allow-non-pe":
        allow_non_pe = True
        args = args[1:]

    if not args:
        report("用法：python3 smoke_test.py <exe 路径>")
        return 1

    exe = Path(args[0]).resolve()
    checks = [
        ("exe 是 Windows PE", lambda: check_exe_format(exe, allow_non_pe)),
        ("默认计划", lambda: check_default_plan(exe)),
        ("--no-app-mode 退回默认浏览器", lambda: check_no_app_mode(exe)),
        ("--browser 显式指定", lambda: check_explicit_browser(exe)),
        ("拒绝非 http/https 地址", lambda: check_rejects_bad_url(exe)),
    ]

    failures = 0
    for name, fn in checks:
        try:
            fn()
        except SmokeFailure as exc:
            failures += 1
            report(f"❌ {name}\n   {exc}")
        except Exception as exc:  # noqa: BLE001 —— 冒烟脚本要报出任何异常
            failures += 1
            report(f"❌ {name}\n   意外异常：{exc!r}")
        else:
            report(f"✅ {name}")

    if failures:
        report(f"\n❌ {failures}/{len(checks)} 项没过")
        return 1
    report(f"\n✅ {len(checks)}/{len(checks)} 项全过：{exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
