#!/usr/bin/env python3
"""acct.xins.live 桌面壳 —— 把线上站点开成一个独立窗口。

**它是什么。** 一个 exe。双击之后弹出一个没有地址栏、没有标签页的窗口，
里面就是 https://acct.xins.live。后端、数据库、部署全都在服务器上，
这个 exe 只负责「把网站用像本地程序一样的方式打开」。

**它不是什么。** 它不是桌面版，不带离线数据，不改任何业务逻辑。
线上站点更新了，壳里的内容跟着更新 —— 这是壳的最大优点，也是它与
L1/L2（本地实例）方案的根本区别。

--------------------------------------------------------------------------
为什么不做成内嵌 WebView 壳（pywebview / Electron / Tauri）
--------------------------------------------------------------------------
看起来「内嵌自己的窗口」更像一个正经桌面程序，而且不依赖用户装浏览器。
但**本项目的凭证打印走的是** ``window.open()`` + ``w.print()``
（``frontend/src/api/print.js`` 的 ``openPrintWindow`` / ``printWindow``），
而 **WebView2 里 ``window.print()`` 不弹打印对话框**（浏览器闪一下就结束），
``window.open`` 也默认被拦截、必须自己接 ``NewWindowRequested``。
换句话说，换成 WebView 壳，**凭证打印这个核心功能直接废掉**。

要把它救回来，就得改成「前端调 IPC、主进程调 ``ShowPrintUI``」——
那已经不是封装，是重做。所以这里选择**用真正的浏览器内核**：
``--app=`` 模式下跑的是完整的 Edge/Chrome，打印、下载、多标签一切照旧。

（完整分析见 ``docs/Windows打包exe可行性研究.md``。）

--------------------------------------------------------------------------
为什么只依赖标准库
--------------------------------------------------------------------------
这个壳的全部价值是「不拖累」：多一个第三方依赖就多一份供应链面、
多一个升级负担，而它要干的事（找浏览器、拼参数、拉起进程）标准库全都干得了。
图标生成、打包脚本也都是零依赖。

--------------------------------------------------------------------------
设计约束：逻辑与平台分离
--------------------------------------------------------------------------
``find_browser`` / ``build_app_args`` / ``expand_env`` 这些是**纯函数**，
输入一个假的 env 与一个假的文件探针就能测。这样 CI 在 Linux 上也能真跑
这套逻辑的单元测试，不必为了验证一行路径拼接先装一台 Windows。

（Windows 上另有一条更硬的闸：CI 里在 ``windows-latest`` 上真构建 exe，
再用 ``--dry-run`` 把「它打算怎么做」写成 JSON 出来断言。
见 ``.github/workflows/desktop-shell.yml``。）
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

APP_NAME = "财务记账系统"
APP_SLUG = "cwq"
DEFAULT_URL = "https://acct.xins.live"
CONFIG_FILENAME = "shell.json"
VERSION = "0.1.0"

# 应用模式窗口的默认尺寸。Chromium 的 ``--app`` 窗口默认偏小（约 1024x768），
# 而这个系统的主界面是三栏布局（科目树 + 凭证 + 金额），太窄会挤成一团。
# Chromium 会把超出屏幕的部分夹回可视区，所以给一个大值是安全的。
DEFAULT_WINDOW_SIZE = "1360,880"


# ---------------------------------------------------------------------------
# 浏览器候选
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrowserCandidate:
    """一个浏览器：名字 + 可能的安装路径模板（Windows 风格 ``%VAR%``）。

    顺序即优先级。**Edge 排第一**：微软已说明 WebView2/Edge 运行时
    在所有 Windows 11 上预装、所有符合条件的 Windows 10 上也已投放，
    所以在一台干净的机器上它是最可能存在的那个 —— 壳不该要求用户
    先去装浏览器。
    """

    name: str
    paths: tuple[str, ...]


BROWSER_CANDIDATES: tuple[BrowserCandidate, ...] = (
    BrowserCandidate(
        "edge",
        (
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
            r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    BrowserCandidate(
        "chrome",
        (
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    BrowserCandidate(
        "brave",
        (
            r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        ),
    ),
)


@dataclass(frozen=True)
class Browser:
    """找到了一个可用的浏览器。"""

    name: str
    path: str


# ---------------------------------------------------------------------------
# 纯函数区（可跨平台单测）
# ---------------------------------------------------------------------------

def expand_env(template: str, env: Mapping[str, str]) -> str:
    r"""展开 Windows 风格的 ``%VAR%`` 模板。

    **不用** ``os.path.expandvars``：它在 POSIX 上只认 ``$VAR`` / ``${VAR}``，
    不认 ``%VAR%`` —— 而浏览器的安装路径在 Windows 上恰恰是
    ``%ProgramFiles(x86)%`` 这种写法。自己实现，CI 才能在 Linux 上
    用一份假的 env 测同一段逻辑。

    未定义的环境变量**原样保留**（不替换成空串）：留着 ``%LOCALAPPDATA%\...``
    这样的字面量，路径一定不存在，自然会被跳过；换成空串反而会拼出
    一个 ``\Microsoft\Edge\...`` 这种看着像真的、其实会指向当前盘根目录的路径。

    （这个 docstring 是 raw string：里面的 ``\...`` 在普通字符串里是非法转义，
    Python 3.12 起会告警、将来会直接变成语法错误。）
    """
    def _replace(match: re.Match[str]) -> str:
        value = env.get(match.group(1))
        return match.group(0) if value is None else value

    return re.sub(r"%([^%]+)%", _replace, template)


def find_browser(
    env: Mapping[str, str] | None = None,
    probe: Callable[[str], bool] | None = None,
) -> Browser | None:
    """按优先级找一个支持 ``--app=`` 的浏览器。

    :param env: 环境变量映射，默认取 ``os.environ``。测试传假的。
    :param probe: 判断「这个路径存在吗」的探针，默认 ``Path.is_file``。
        测试传假的 —— 因为 Linux 上 ``C:\\...\\msedge.exe`` 永远不存在。
    :return: 找到的第一个浏览器；一个都没有时返回 ``None``（不是抛异常：
        「机器上没装浏览器」是一个要降级处理的状态，不是错误）。
    """
    env = os.environ if env is None else env
    check = (lambda p: Path(p).is_file()) if probe is None else probe

    for candidate in BROWSER_CANDIDATES:
        for template in candidate.paths:
            path = expand_env(template, env)
            if path and check(path):
                return Browser(name=candidate.name, path=path)
    return None


def build_app_args(
    url: str,
    profile_dir: str,
    window_size: str = DEFAULT_WINDOW_SIZE,
    extra: Sequence[str] = (),
) -> list[str]:
    """拼 ``--app=`` 模式下的浏览器参数。

    :param url: 目标地址（必须是 http/https，由 :func:`validate_url` 保证）。
    :param profile_dir: 独立用户数据目录，见 :func:`profile_dir` 的说明。
    :param window_size: 窗口尺寸 ``宽,高``。
    :param extra: 额外参数，追加在最后（用户自定义配置用）。
    """
    return [
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        f"--window-size={window_size}",
        # 独立 profile 是全新的，Chromium 会弹「首次使用」向导。
        # 对一个记账系统来说，那个向导只会让人以为装错了东西。
        "--no-first-run",
        "--no-default-browser-check",
        *extra,
    ]


def validate_url(url: str) -> str:
    """校验目标地址，只放行 http/https。

    为什么必须校验：URL 可能来自命令行参数或 exe 旁边的 ``shell.json``。
    一份被改过的配置文件若塞进 ``file://`` 或 ``ms-msdt:`` 之类，
    壳就会替用户执行它 —— 壳是「替人点开一个网址」，不该是「替人执行任意协议」。
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"只允许 http/https 地址，收到的是 {url!r}")
    if not parsed.netloc:
        raise ValueError(f"地址缺少主机名：{url!r}")
    return url.strip()


# ---------------------------------------------------------------------------
# 路径区
# ---------------------------------------------------------------------------

def app_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """用户数据根目录。

    Windows 上用 ``%LOCALAPPDATA%``（**不是** ``%APPDATA%``）：
    前者是「应用私有、不随用户漫游」的那一半，放本地数据（浏览器 profile、
    日志）是对的；``%APPDATA%`` 会随域账号漫游，profile 跟着漫游会出问题。

    其它平台用 XDG 约定，方便本机开发时调试同一个壳。
    """
    env = os.environ if env is None else env
    if platform.system() == "Windows":
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    base = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_SLUG


def profile_dir(env: Mapping[str, str] | None = None) -> Path:
    """浏览器的独立用户数据目录。

    **为什么要独立于用户日常浏览器：**

    1. 登录态。JWT 存在 localStorage 里，而 localStorage 属于 profile。
       独立 profile 里登录一次，之后一直有效。
    2. 不打扰用户。``--app`` 窗口如果挂在用户日常 profile 上，
       浏览器可能顺手弹「恢复上次会话」，或者让这个窗口跟着用户的
       其它标签页一起退出。
    3. 可预期。壳的行为不取决于用户平时用什么浏览器、装了什么扩展。
    """
    return app_data_dir(env) / "browser-profile"


def log_path(env: Mapping[str, str] | None = None) -> Path:
    """壳自己的日志文件。"""
    return app_data_dir(env) / "logs" / "shell.log"


def executable_dir() -> Path:
    """exe 所在目录（源码运行时是脚本所在目录）。

    打包后 ``sys.executable`` 是 exe 本身，``__file__`` 指向解包出来的临时目录
    —— 所以这里必须用 ``sys.executable``，不能用 ``__file__``。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_search_paths() -> list[Path]:
    """``shell.json`` 的候选位置，按优先级。

    只有**紧挨着 exe** 的那一个会被读。放进用户目录就等于允许第三方
    改壳要打开哪个站点 —— 改的是「点开哪个网址」，看着无害，但配合
    钓鱼站点就是一条完整的攻击链。要改站点，走 ``--url`` 参数或重新打包。
    """
    return [executable_dir() / CONFIG_FILENAME]


def load_config(paths: Sequence[Path] | None = None) -> tuple[dict, Path | None]:
    """读配置。返回 ``(配置, 实际用到的配置文件路径或 None)``。

    配置格式损坏时**不抛异常**：壳的最坏情况是退回默认地址，
    而不是一个「双击没反应还弹红字」的 exe。

    :param paths: 候选路径。默认取 :func:`config_search_paths`；
        测试传临时目录里的路径。
    """
    for path in (paths if paths is not None else config_search_paths()):
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data, path
        except Exception:
            continue
    return {}, None


# ---------------------------------------------------------------------------
# 副作用区
# ---------------------------------------------------------------------------

def write_log(message: str, env: Mapping[str, str] | None = None) -> None:
    """追加一行日志。

    **日志失败绝不能影响启动** —— 一个只读介质、一个被占用的文件、
    一个没权限的目录，都不该让用户双击 exe 后什么也得不到。所以整个
    写入过程吞掉所有异常。
    """
    try:
        path = log_path(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass


def show_message(title: str, text: str, *, error: bool = False) -> None:
    """弹一个系统消息框。

    打包成 GUI 程序（无控制台）后，``print`` 的内容用户是看不见的 ——
    出错时必须有个能弹出来的东西，否则用户只会觉得「双击了没反应」。
    """
    if platform.system() != "Windows":
        _safe_print(f"{title}: {text}", stream=sys.stderr)
        return
    try:
        # 0x10 = MB_ICONERROR, 0x40 = MB_ICONINFORMATION
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        pass


def _safe_print(text: str, stream=None) -> None:
    """在无控制台（GUI 打包）下也能安全地 print。

    PyInstaller 的 windowed 模式会把 ``sys.stdout`` 换成丢弃输出的空对象，
    而某些运行环境下它干脆是 ``None``。直接 ``print`` 会抛
    ``AttributeError``/``TypeError`` —— 一个「打印日志」的辅助函数
    把整个启动流程带崩，是很难查的那种故障。
    """
    target = stream or sys.stdout
    if target is None:
        return
    try:
        print(text, file=target)
    except Exception:
        pass


def open_default_browser(url: str) -> bool:
    """退回系统默认浏览器。返回是否成功。

    ``webbrowser.open`` 在无桌面会话的机器上（比如 CI runner）会返回
    False —— 那不是错误，只是「这里没人看」，所以返回布尔值而不是抛。
    """
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def spawn(cmd: Sequence[str]) -> None:
    """拉起浏览器进程。

    ``CREATE_NO_WINDOW`` 是关键：GUI 打包的 exe 在某些环境下（比如从
    资源管理器双击但父进程带控制台）仍可能让子进程闪一个黑框。
    这个标志让子进程不带控制台窗口。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(list(cmd), creationflags=flags)


# ---------------------------------------------------------------------------
# 计划（dry-run）与主流程
# ---------------------------------------------------------------------------

def build_plan(
    url: str,
    browser: Browser | None,
    use_app_mode: bool,
    profile: Path,
    config_path: Path | None,
    extra_args: Iterable[str],
    window_size: str,
) -> dict:
    """描述「壳打算怎么做」。``--dry-run`` 输出它，CI 断言它。"""
    if browser is not None and use_app_mode:
        args = build_app_args(url, str(profile), window_size, list(extra_args))
        mode = "app-window"
    else:
        args = []
        mode = "default-browser"

    return {
        "version": VERSION,
        "platform": platform.system(),
        "target_url": url,
        "mode": mode,
        "browser": None if browser is None else {"name": browser.name, "path": browser.path},
        "args": args,
        "profile_dir": str(profile),
        "config_path": None if config_path is None else str(config_path),
        "window_size": window_size,
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cwq",
        description=f"{APP_NAME} 桌面壳 —— 打开 {DEFAULT_URL}",
    )
    parser.add_argument("--url", help=f"要打开的地址（默认 {DEFAULT_URL}）")
    parser.add_argument(
        "--no-app-mode",
        action="store_true",
        help="不用浏览器的应用模式窗口，改用系统默认浏览器打开",
    )
    parser.add_argument(
        "--browser",
        help="指定浏览器可执行文件的完整路径",
    )
    parser.add_argument(
        "--window-size",
        # 默认给 None 而不是 DEFAULT_WINDOW_SIZE：argparse 的默认值一旦非空，
        # 就永远压过配置文件里的同名项 —— 配置文件会看起来「能配但没生效」。
        # 真正的兜底在 main() 里按 命令行 > 配置文件 > 内置默认 的顺序解析。
        default=None,
        help=f"应用窗口尺寸 宽,高（默认 {DEFAULT_WINDOW_SIZE}）",
    )
    parser.add_argument(
        "--browser-arg",
        action="append",
        default=[],
        help="追加给浏览器的原始参数，可重复",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印「打算怎么做」的 JSON，不真的打开浏览器",
    )
    parser.add_argument(
        "--plan-out",
        help="把 dry-run 的 JSON 同时写到这个文件",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    config, config_path = load_config()
    raw_url = args.url or config.get("url") or DEFAULT_URL
    window_size = args.window_size or config.get("window_size") or DEFAULT_WINDOW_SIZE
    extra_args = list(args.browser_arg) + list(config.get("browser_args") or [])

    try:
        url = validate_url(raw_url)
    except ValueError as exc:
        show_message(f"{APP_NAME}", str(exc), error=True)
        write_log(f"URL 无效：{exc}")
        return 2

    # 显式指定浏览器时优先用它；指定了但不存在 → 明确失败，不要静默换成别的。
    browser: Browser | None = None
    if args.browser:
        if Path(args.browser).is_file():
            browser = Browser(name="custom", path=args.browser)
        else:
            show_message(f"{APP_NAME}", f"找不到指定的浏览器：\n{args.browser}", error=True)
            write_log(f"指定的浏览器不存在：{args.browser}")
            return 2
    else:
        browser = find_browser()

    use_app_mode = not args.no_app_mode and browser is not None
    plan = build_plan(
        url=url,
        browser=browser,
        use_app_mode=use_app_mode,
        profile=profile_dir(),
        config_path=config_path,
        extra_args=extra_args,
        window_size=window_size,
    )

    if args.dry_run:
        payload = json.dumps(plan, ensure_ascii=False, indent=2)
        _safe_print(payload)
        if args.plan_out:
            try:
                out = Path(args.plan_out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(payload, encoding="utf-8")
            except Exception as exc:
                write_log(f"写 plan 文件失败：{exc}")
                return 3
        return 0

    write_log(
        f"启动 mode={plan['mode']} url={url} "
        f"browser={(browser.path if browser else '-')}"
    )

    if use_app_mode and browser is not None:
        try:
            spawn([browser.path, *plan["args"]])
            return 0
        except Exception as exc:
            write_log(f"拉起浏览器失败：{exc}")
            show_message(f"{APP_NAME}", f"打开浏览器失败：\n{exc}", error=True)
            return 4

    # 没有浏览器，或者用户主动要求用默认浏览器。
    if open_default_browser(url):
        return 0

    show_message(
        f"{APP_NAME}",
        f"没能打开 {url}。\n\n"
        "这台机器上没找到 Edge / Chrome / Brave，也没有可用的默认浏览器。\n"
        f"请直接在浏览器地址栏打开：{url}",
        error=True,
    )
    write_log("没有可用浏览器，且默认浏览器也打不开")
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
