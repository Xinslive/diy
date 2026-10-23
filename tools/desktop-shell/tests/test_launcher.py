#!/usr/bin/env python3
"""桌面壳的单元测试。

**为什么用 unittest 而不是 pytest**：这个壳是零依赖工具，它的测试也不该
拖上一个 pytest。``python -m unittest`` 就能跑；pytest 同样能收集
``unittest.TestCase``，所以仓库里那套 pytest 也能一并跑它 —— 两种方式
都能跑，不是二选一。

**测的是什么**：全部是**平台无关的纯逻辑** —— 路径展开、浏览器发现、
参数拼装、URL 校验、配置解析、启动计划、进程启动参数。真正依赖 Windows
的部分（浏览器真的弹窗出来、消息框）由 CI 在 ``windows-latest`` 上用
``--dry-run`` 产出的 JSON 来断言，那条闸在
``.github/workflows/desktop-shell.yml``。

跑法：
    python3 -m unittest discover -s tools/desktop-shell/tests -v
    # 或
    python3 -m pytest tools/desktop-shell/tests
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 壳不是包，从仓库任何位置跑都要能找到它
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launcher  # noqa: E402


@contextlib.contextmanager
def quiet():
    """吞掉 dry-run 往 stdout 打的 JSON。

    不是为了好看：那些 JSON 会混进 CI 的测试输出里，让人误以为
    那是某个用例的打印残留。测试要断言的输出一律走 ``--plan-out`` 读文件。
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield buffer


class ExpandEnvTest(unittest.TestCase):
    def test_replaces_known_variable(self):
        env = {"ProgramFiles": r"C:\Program Files"}
        self.assertEqual(
            launcher.expand_env(r"%ProgramFiles%\Edge\msedge.exe", env),
            r"C:\Program Files\Edge\msedge.exe",
        )

    def test_replaces_multiple_variables(self):
        env = {"A": "1", "B": "2"}
        self.assertEqual(launcher.expand_env("%A%-%B%", env), "1-2")

    def test_keeps_unknown_variable_verbatim(self):
        # 未知变量**原样保留**，不替换成空串。
        # 换成空串会拼出 r"\Microsoft\Edge\msedge.exe" 这种看着像真的、
        # 实际指向当前盘根目录的路径。
        out = launcher.expand_env(r"%NOPE%\x.exe", {})
        self.assertEqual(out, r"%NOPE%\x.exe")

    def test_keeps_unknown_variable_when_other_known_one_exists(self):
        env = {"A": "1"}
        out = launcher.expand_env("%A%/%B%", env)
        self.assertEqual(out, "1/%B%")

    def test_no_variables_is_unchanged(self):
        self.assertEqual(launcher.expand_env(r"C:\x\y.exe", {}), r"C:\x\y.exe")

    def test_empty_value_is_used(self):
        # 空串是**已定义**的空值，必须被采用，不能当成「未定义」。
        self.assertEqual(launcher.expand_env("%A%", {"A": ""}), "")


class FindBrowserTest(unittest.TestCase):
    """浏览器发现。

    ``probe`` 是假的文件存在性探针 —— 因为在 Linux 上
    ``C:\\...\\msedge.exe`` 永远不存在，真探测的话这套测试只会在
    Windows 上有意义。
    """

    EDGE_X86 = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    EDGE_X64 = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

    def _env(self, **overrides):
        env = {
            "ProgramFiles(x86)": r"C:\Program Files (x86)",
            "ProgramFiles": r"C:\Program Files",
            "LOCALAPPDATA": r"C:\Users\u\AppData\Local",
        }
        env.update(overrides)
        return env

    def test_finds_edge(self):
        found = launcher.find_browser(self._env(), lambda p: p == self.EDGE_X86)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "edge")
        self.assertEqual(found.path, self.EDGE_X86)

    def test_prefers_edge_over_chrome(self):
        # 两者都在时必须选 Edge：Edge 在所有 Windows 11 与符合条件的
        # Windows 10 上预装，是「干净机器上最可能存在」的那个。
        both = {self.EDGE_X86, self.CHROME}
        found = launcher.find_browser(self._env(), lambda p: p in both)
        self.assertEqual(found.name, "edge")

    def test_falls_back_to_chrome(self):
        found = launcher.find_browser(self._env(), lambda p: p == self.CHROME)
        self.assertEqual(found.name, "chrome")

    def test_falls_back_to_localappdata_edge(self):
        local_edge = r"C:\Users\u\AppData\Local\Microsoft\Edge\Application\msedge.exe"
        found = launcher.find_browser(self._env(), lambda p: p == local_edge)
        self.assertEqual(found.name, "edge")
        self.assertEqual(found.path, local_edge)

    def test_returns_none_when_no_browser(self):
        # 「机器上没装浏览器」是**要降级处理的状态**，不是错误 —— 所以返回
        # None 而不是抛异常。
        self.assertIsNone(launcher.find_browser(self._env(), lambda p: False))

    def test_returns_none_when_env_missing(self):
        # 环境变量全缺时也不能崩，更不能拼出一个假的路径。
        self.assertIsNone(launcher.find_browser({}, lambda p: False))


class BuildAppArgsTest(unittest.TestCase):
    def test_contains_app_mode_flag(self):
        args = launcher.build_app_args("https://acct.xins.live", r"C:\p")
        self.assertIn("--app=https://acct.xins.live", args)

    def test_contains_dedicated_profile(self):
        args = launcher.build_app_args("https://acct.xins.live", r"C:\p")
        self.assertIn(r"--user-data-dir=C:\p", args)

    def test_suppresses_first_run_wizard(self):
        # 独立 profile 是全新的，Chromium 会弹首次使用向导。
        # 对一个记账系统来说，那只会让人以为装错了东西。
        args = launcher.build_app_args("https://acct.xins.live", r"C:\p")
        self.assertIn("--no-first-run", args)
        self.assertIn("--no-default-browser-check", args)

    def test_contains_window_size(self):
        args = launcher.build_app_args("https://acct.xins.live", r"C:\p", "1200,800")
        self.assertIn("--window-size=1200,800", args)

    def test_extra_args_appended_last(self):
        args = launcher.build_app_args(
            "https://acct.xins.live", r"C:\p", "1200,800", ["--lang=zh-CN"]
        )
        self.assertEqual(args[-1], "--lang=zh-CN")

    def test_url_is_not_separate_arg(self):
        # URL 必须写成 ``--app=<url>``：单独传一个裸 URL 会被 Chromium
        # 当成「要打开的页面」而不是「应用模式的地址」。
        args = launcher.build_app_args("https://acct.xins.live", r"C:\p")
        self.assertNotIn("https://acct.xins.live", args)


class ValidateUrlTest(unittest.TestCase):
    def test_accepts_https(self):
        self.assertEqual(
            launcher.validate_url("https://acct.xins.live"),
            "https://acct.xins.live",
        )

    def test_accepts_http(self):
        self.assertEqual(
            launcher.validate_url("http://127.0.0.1:8000"),
            "http://127.0.0.1:8000",
        )

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(
            launcher.validate_url("  https://acct.xins.live  "),
            "https://acct.xins.live",
        )

    def test_rejects_file_scheme(self):
        # 壳是「替人点开一个网址」，不该是「替人执行任意协议」。
        with self.assertRaises(ValueError):
            launcher.validate_url("file:///C:/Windows/System32/cmd.exe")

    def test_rejects_javascript_scheme(self):
        with self.assertRaises(ValueError):
            launcher.validate_url("javascript:alert(1)")

    def test_rejects_missing_host(self):
        with self.assertRaises(ValueError):
            launcher.validate_url("https://")


class LoadConfigTest(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, path = launcher.load_config([Path(tmp) / "shell.json"])
        self.assertEqual(config, {})
        self.assertIsNone(path)

    def test_reads_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shell.json"
            target.write_text(json.dumps({"url": "https://x.example"}), encoding="utf-8")
            config, path = launcher.load_config([target])
        self.assertEqual(config["url"], "https://x.example")
        self.assertIsNotNone(path)

    def test_malformed_file_does_not_raise(self):
        # 配置坏了的**最坏情况是退回默认地址**，而不是一个「双击没反应
        # 还弹红字」的 exe。所以这里必须吞掉异常。
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shell.json"
            target.write_text("{ not json", encoding="utf-8")
            config, path = launcher.load_config([target])
        self.assertEqual(config, {})
        self.assertIsNone(path)

    def test_non_object_json_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shell.json"
            target.write_text("[1, 2, 3]", encoding="utf-8")
            config, _ = launcher.load_config([target])
        self.assertEqual(config, {})

    def test_first_existing_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.json"
            second = Path(tmp) / "b.json"
            first.write_text(json.dumps({"url": "https://first.example"}), encoding="utf-8")
            second.write_text(json.dumps({"url": "https://second.example"}), encoding="utf-8")
            config, path = launcher.load_config([first, second])
        self.assertEqual(config["url"], "https://first.example")
        self.assertEqual(path, first)


class BuildPlanTest(unittest.TestCase):
    def test_app_window_mode_when_browser_found(self):
        browser = launcher.Browser(name="edge", path=r"C:\edge.exe")
        plan = launcher.build_plan(
            url="https://acct.xins.live",
            browser=browser,
            use_app_mode=True,
            profile=Path(r"C:\profile"),
            config_path=None,
            extra_args=[],
            window_size="1360,880",
        )
        self.assertEqual(plan["mode"], "app-window")
        self.assertEqual(plan["browser"]["name"], "edge")
        self.assertIn("--app=https://acct.xins.live", plan["args"])

    def test_default_browser_mode_when_no_browser(self):
        plan = launcher.build_plan(
            url="https://acct.xins.live",
            browser=None,
            use_app_mode=False,
            profile=Path(r"C:\profile"),
            config_path=None,
            extra_args=[],
            window_size="1360,880",
        )
        self.assertEqual(plan["mode"], "default-browser")
        self.assertIsNone(plan["browser"])
        self.assertEqual(plan["args"], [])

    def test_plan_has_stable_keys(self):
        # CI 按这些键做断言，键名变了那条闸就该红 —— 所以这里钉住。
        plan = launcher.build_plan(
            url="https://acct.xins.live",
            browser=None,
            use_app_mode=False,
            profile=Path("/tmp/p"),
            config_path=None,
            extra_args=[],
            window_size="1360,880",
        )
        for key in (
            "version", "platform", "target_url", "mode", "browser",
            "args", "profile_dir", "config_path", "window_size",
        ):
            self.assertIn(key, plan)


class MainTest(unittest.TestCase):
    """main() 的行为。

    全部走 ``--dry-run``，所以**不会真的拉起浏览器**。
    """

    def test_dry_run_returns_zero(self):
        with quiet():
            rc = launcher.main(["--dry-run", "--url", "https://acct.xins.live"])
        self.assertEqual(rc, 0)

    def test_dry_run_writes_plan_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            with quiet():
                rc = launcher.main([
                    "--dry-run", "--plan-out", str(out),
                    "--url", "https://acct.xins.live",
                ])
            self.assertEqual(rc, 0)
            plan = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(plan["target_url"], "https://acct.xins.live")
        self.assertIn(plan["mode"], ("app-window", "default-browser"))

    def test_rejects_non_http_url(self):
        with quiet():
            rc = launcher.main(["--dry-run", "--url", "file:///etc/passwd"])
        self.assertEqual(rc, 2)

    def test_rejects_missing_browser_path(self):
        with quiet():
            rc = launcher.main(["--dry-run", "--browser", "/definitely/not/here/chrome"])
        self.assertEqual(rc, 2)

    def test_no_app_mode_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            with quiet():
                launcher.main([
                    "--dry-run", "--plan-out", str(out),
                    "--no-app-mode", "--url", "https://acct.xins.live",
                ])
            plan = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(plan["mode"], "default-browser")

    def test_profile_dir_is_absolute(self):
        # 独立 profile 必须是绝对路径：相对路径会被解析到「当前工作目录」，
        # 而双击 exe 时那个目录是随机的。
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "plan.json"
            with quiet():
                launcher.main(["--dry-run", "--plan-out", str(out)])
            plan = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(Path(plan["profile_dir"]).is_absolute())


class SpawnTest(unittest.TestCase):
    """``spawn()`` —— 壳里唯一真正创建进程的函数。

    **为什么它值得单独测**：它是整个壳唯一的「副作用出口」。前面所有函数
    （找浏览器、拼参数、生成计划）都只碰字符串，只有这一行会真的起进程。
    它坏掉的症状是「双击 exe 什么也不发生」—— 没有报错、没有弹窗、
    窗口一闪就没，比崩溃还难排查。

    「浏览器确实起来了」那部分要真 Windows 才能验；CI 在
    ``windows-latest`` 上跑的是 ``--dry-run``，并不覆盖它。
    Wine 下的替身浏览器实验（方法与结论见 README 4.6）验到了
    「参数确实被交出去了」，但那仍不等于真实 Windows 内核。
    这里钉死的是「参数怎么交给 Popen」—— 那是出问题最难查的一层。
    """

    def test_passes_command_as_list_not_string(self):
        # 传字符串会让 Windows 走 cmd.exe 解析：路径里的空格会被拆开、
        # 中文 profile 路径可能被编码破坏，而且等于把参数交给了 shell。
        with mock.patch.object(launcher.subprocess, "Popen") as popen:
            launcher.spawn(["C:\\Program Files\\msedge.exe", "--app=https://a"])
        cmd = popen.call_args.args[0]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], "C:\\Program Files\\msedge.exe")

    def test_does_not_enable_shell(self):
        with mock.patch.object(launcher.subprocess, "Popen") as popen:
            launcher.spawn(["msedge.exe"])
        self.assertFalse(popen.call_args.kwargs.get("shell", False))

    def test_creationflags_matches_platform_flag(self):
        # Windows 上是 CREATE_NO_WINDOW（不让子进程闪黑框）；
        # 其它平台这个常量不存在，取值必须是 0 —— 非零的 creationflags
        # 在 POSIX 上会被 subprocess 直接拒掉（ValueError）。
        expected = getattr(launcher.subprocess, "CREATE_NO_WINDOW", 0)
        with mock.patch.object(launcher.subprocess, "Popen") as popen:
            launcher.spawn(["msedge.exe"])
        self.assertEqual(popen.call_args.kwargs.get("creationflags"), expected)


class MainLaunchTest(unittest.TestCase):
    """``main()`` 真正启动浏览器的那条路径（不是 --dry-run）。

    **为什么不能只测 dry-run**：``--dry-run`` 在 ``main()`` 里是**提前返回**
    的，它压根走不到 ``spawn()``。也就是说 dry-run 全绿并不代表「双击能开窗」，
    中间那一段是没有闸的。这组用例把那段补上：只 mock 掉 ``Popen``
    （起真进程），其余全部走真代码。
    """

    def _run(self, argv):
        with mock.patch.object(launcher, "write_log"), \
                mock.patch.object(launcher.subprocess, "Popen") as popen:
            rc = launcher.main(argv)
        return rc, popen

    def test_actually_spawns_the_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fakebrowser.exe"
            fake.write_bytes(b"MZ")
            rc, popen = self._run(["--browser", str(fake)])
        self.assertEqual(rc, 0)
        popen.assert_called_once()
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], str(fake))
        self.assertIn(f"--app={launcher.DEFAULT_URL}", cmd)

    def test_spawned_args_match_the_plan(self):
        # 实际传给浏览器的参数必须和 dry-run 输出的计划**完全一致** ——
        # 否则「CI 断言的计划」和「用户实际看到的窗口」就是两回事。
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fakebrowser.exe"
            fake.write_bytes(b"MZ")
            plan_out = Path(tmp) / "plan.json"

            # 先跑一次 dry-run 拿到「计划说什么」
            with quiet():
                launcher.main([
                    "--dry-run", "--plan-out", str(plan_out),
                    "--browser", str(fake),
                ])
            plan = json.loads(plan_out.read_text(encoding="utf-8"))

            # 再跑一次真的启动，拿到「实际传了什么」
            _, popen = self._run(["--browser", str(fake)])
            spawned = list(popen.call_args.args[0])

        self.assertEqual(spawned, [str(fake), *plan["args"]])


class ProfileDirTest(unittest.TestCase):
    def test_under_localappdata_on_windows(self):
        # LOCALAPPDATA 而不是 APPDATA：前者不随域账号漫游。
        env = {"LOCALAPPDATA": r"C:\Users\u\AppData\Local", "APPDATA": r"C:\Users\u\AppData\Roaming"}
        original = launcher.platform.system
        launcher.platform.system = lambda: "Windows"
        try:
            path = launcher.profile_dir(env)
        finally:
            launcher.platform.system = original
        self.assertEqual(
            path, Path(r"C:\Users\u\AppData\Local") / launcher.APP_NAME / "browser-profile"
        )

    def test_never_uses_roaming_appdata(self):
        env = {"LOCALAPPDATA": r"C:\Users\u\AppData\Local", "APPDATA": r"C:\Users\u\AppData\Roaming"}
        original = launcher.platform.system
        launcher.platform.system = lambda: "Windows"
        try:
            path = launcher.profile_dir(env)
        finally:
            launcher.platform.system = original
        self.assertNotIn("Roaming", str(path))


class ForceUtf8OutputTest(unittest.TestCase):
    """``force_utf8_output()`` —— Windows 控制台编码加固。

    **这条测试是被 CI 逼出来的。** 第一次跑 Desktop Shell workflow 时，
    windows-latest 上 ``make_icon.py`` 打印「✅」抛了
    ``UnicodeEncodeError: 'charmap' codec can't encode character '\\u2705'``，
    整条 workflow 在「生成图标」那一步红了。

    修完之后必须有测试守着，否则下一次有人改 print 的写法、或换了台
    编码不同的机器，同样的坑会再踩一遍。
    """

    def _cp1252_stream(self):
        """一个用 cp1252 编码的文本流 —— 模拟 Windows 控制台。"""
        import io

        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        self.addCleanup(stream.detach)
        return stream, raw

    def test_reconfigures_to_utf8(self):
        stream, _ = self._cp1252_stream()
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stream, stream
        try:
            launcher.force_utf8_output()
            encoding = stream.encoding
        finally:
            sys.stdout, sys.stderr = original_out, original_err
        self.assertEqual(encoding.lower(), "utf-8")

    def test_emoji_printable_after_forcing(self):
        # 真正的断言：加固之后 print("✅") 不再抛。
        # 修复前这一行会抛 UnicodeEncodeError。
        stream, _ = self._cp1252_stream()
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stream, stream
        try:
            launcher.force_utf8_output()
            print("✅ 中文与 emoji 都能写出去")
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_tolerates_missing_stream(self):
        # GUI 打包（windowed）下 sys.stdout 可能是 None。
        # 那时不能抛 —— 壳的启动流程不能因为「没有控制台」而崩掉。
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = None, None
        try:
            launcher.force_utf8_output()  # 不抛即通过
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_tolerates_stream_without_reconfigure(self):
        # 没有 reconfigure 方法的流（比如被换成 io.StringIO）也不能抛。
        import io

        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            launcher.force_utf8_output()
        finally:
            sys.stdout, sys.stderr = original_out, original_err


class VersionInfoFileTest(unittest.TestCase):
    """version_info.txt 必须能被 ``eval()`` 加载成一个 ``VSVersionInfo``。

    **为什么这条测试存在。** PyInstaller 用 ``eval(text)`` 加载 version 文件
    （``PyInstaller/utils/win32/versioninfo.py`` 的
    ``load_version_info_from_text_file``），**不是** ``exec``。
    eval 只接受**一个表达式**，所以顶层赋值语句（``version = '0.1.0'``）
    会直接抛 ``SyntaxError``，整个打包随之失败。

    CI 第一次跑就撞上了：windows-latest 上报 ``line 21: version = '0.1.0'
    SyntaxError: invalid syntax``，然后被 PyInstaller 包成
    ``ValueError: Failed to deserialize VSVersionInfo from text-based
    representation!`` —— 报错离病因隔了两层，很难一眼看出是那行赋值。

    所以这里用 eval 真加载一次，把这条约束钉住。
    """

    VERSION_FILE = Path(__file__).resolve().parents[1] / "version_info.txt"

    def _load(self):
        """按 PyInstaller 的方式加载：解码 → 替换 Tuple → eval。"""
        text = self.VERSION_FILE.read_text(encoding="utf-8")
        # PyInstaller 源码里就是这一步（把老式的 Tuple 写法换成 tuple）
        text = text.replace("Tuple", "tuple")
        return eval(text, self._vs_namespace())

    def _vs_namespace(self):
        """version 文件里那些名字的最小实现。

        真 PyInstaller 用的是 win32 versioninfo 的真结构；这里只需要
        能把文件 eval 出来、并能被下面几条断言检查。
        """
        class _Node:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        def _kids_list(kids):
            return kids

        def StringFileInfo(table):
            return _Node(table=table)

        def StringTable(lang_id, strings):
            return _Node(lang_id=lang_id, strings=strings)

        def StringStruct(key, value):
            return _Node(key=key, value=value)

        def VarFileInfo(items):
            return _Node(items=items)

        def VarStruct(key, value):
            return _Node(key=key, value=value)

        def FixedFileInfo(**kw):
            return _Node(**kw)

        def VSVersionInfo(**kw):
            return _Node(**kw)

        return {
            "VSVersionInfo": VSVersionInfo,
            "FixedFileInfo": FixedFileInfo,
            "StringFileInfo": StringFileInfo,
            "StringTable": StringTable,
            "StringStruct": StringStruct,
            "VarFileInfo": VarFileInfo,
            "VarStruct": VarStruct,
        }

    def test_file_exists(self):
        self.assertTrue(self.VERSION_FILE.is_file(), f"缺 version 文件：{self.VERSION_FILE}")

    def test_loads_via_eval(self):
        # 核心断言：文件必须整体是一个表达式。
        # 顶层赋值（version = '...'）会让这一行抛 SyntaxError。
        info = self._load()
        self.assertIsNotNone(info)

    def test_no_top_level_assignment(self):
        # 再明确一次：不允许出现顶层赋值语句。
        # 注释里的说明文字不算 —— 用 compile 的 AST 看真实语句。
        import ast

        text = self.VERSION_FILE.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in tree.body:
            self.assertNotIsInstance(
                node,
                ast.Assign,
                f"version 文件里有顶层赋值（第 {node.lineno} 行）："
                "PyInstaller 用 eval() 加载它，赋值语句会抛 SyntaxError",
            )

    def test_has_required_versioninfo_fields(self):
        # 这些字段是杀软误报申诉会检查的，缺一个都会影响通过率。
        info = self._load()
        table = info.kids[0].table[0]
        fields = {s.key: s.value for s in table.strings}
        for key in (
            "CompanyName",
            "FileDescription",
            "FileVersion",
            "InternalName",
            "OriginalFilename",
            "ProductName",
            "ProductVersion",
        ):
            self.assertIn(key, fields, f"VERSIONINFO 缺字段：{key}")
            self.assertTrue(fields[key].strip(), f"VERSIONINFO 的 {key} 是空的")

    def test_original_filename_matches_exe_name(self):
        # OriginalFilename 要与实际产出的 exe 名一致，否则「属性」里显示的
        # 名字与用户双击的那个对不上。
        info = self._load()
        table = info.kids[0].table[0]
        fields = {s.key: s.value for s in table.strings}
        self.assertEqual(fields["OriginalFilename"], "CWQ.exe")


class IsInteractiveTest(unittest.TestCase):
    """``is_interactive()`` —— 判定「有没有人能在旁边点掉那个模态框」。

    **这条测试是被 CI 挂住逼出来的。** 第一次跑冒烟验证时，Windows runner
    上弹出的 ``MessageBoxW`` 没人点，那一步卡了 11 分钟。
    """

    @contextlib.contextmanager
    def _with_env(self, **env):
        """临时替换环境变量，跑完恢复。"""
        saved = {k: os.environ.get(k) for k in env}
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_ci_env_is_not_interactive(self):
        with self._with_env(CI="1", CWQ_SHELL_NONINTERACTIVE=None):
            self.assertFalse(launcher.is_interactive())

    def test_explicit_noninteractive_switch(self):
        with self._with_env(CI=None, CWQ_SHELL_NONINTERACTIVE="1"):
            self.assertFalse(launcher.is_interactive())

    def test_posix_without_display_is_not_interactive(self):
        original = launcher.platform.system
        launcher.platform.system = lambda: "Linux"
        try:
            with self._with_env(CI=None, CWQ_SHELL_NONINTERACTIVE=None,
                                DISPLAY=None, WAYLAND_DISPLAY=None):
                self.assertFalse(launcher.is_interactive())
        finally:
            launcher.platform.system = original

    def test_posix_with_display_is_interactive(self):
        original = launcher.platform.system
        launcher.platform.system = lambda: "Linux"
        try:
            with self._with_env(CI=None, CWQ_SHELL_NONINTERACTIVE=None,
                                DISPLAY=":0", WAYLAND_DISPLAY=None):
                self.assertTrue(launcher.is_interactive())
        finally:
            launcher.platform.system = original


class DryRunNeverShowsDialogTest(unittest.TestCase):
    """``--dry-run`` 绝不能弹模态框。

    **这条测试是被 CI 挂住逼出来的。** ``MessageBoxW`` 是模态的：它会一直
    等用户点掉。Windows runner 上没人点，那一步就永久阻塞。

    所以 ``--dry-run`` 必须**无条件**抑制弹窗 —— 它的全部意义就是
    「别真做事」，包括别弹框。

    这条测试**不依赖平台**：它断言的是「调用 show_message 时一定带了
    suppress=True」，而不是「运行时真的没弹」。
    """

    def _main_show_message_kwargs(self, argv):
        """跑一次 main()，捕获 show_message 收到的关键字参数。"""
        captured = []
        original = launcher.show_message

        def spy(title, text, **kwargs):
            captured.append(kwargs)

        launcher.show_message = spy
        try:
            with quiet():
                launcher.main(argv)
        finally:
            launcher.show_message = original
        return captured

    def test_bad_url_dry_run_suppresses_dialog(self):
        calls = self._main_show_message_kwargs(
            ["--dry-run", "--url", "file:///etc/passwd"]
        )
        self.assertTrue(calls, "非法 URL 应当触发一次 show_message")
        for kwargs in calls:
            self.assertTrue(
                kwargs.get("suppress"),
                f"dry-run 下的 show_message 必须带 suppress=True，实际是 {kwargs}",
            )

    def test_missing_browser_dry_run_suppresses_dialog(self):
        calls = self._main_show_message_kwargs(
            ["--dry-run", "--browser", "/definitely/not/here/chrome"]
        )
        self.assertTrue(calls, "找不到浏览器应当触发一次 show_message")
        for kwargs in calls:
            self.assertTrue(
                kwargs.get("suppress"),
                f"dry-run 下的 show_message 必须带 suppress=True，实际是 {kwargs}",
            )


class SmokeTestHarnessTest(unittest.TestCase):
    """冒烟验证脚本自身的行为。

    **为什么要有这些。** 第一次跑这条 workflow 时，Windows 上「冒烟验证」
    那一步卡了整整 11 分钟 —— 每条用例都耗满了 120 秒超时。也就是说
    **冒烟脚本自己坏掉时，症状是「CI 挂住」而不是「CI 报红」**，而挂住
    是最难诊断的形态。

    所以这里断言脚本的关键属性：超时预算是有界的、失败会抛可读的错误、
    而且它**不建管道**（Windows 上服务进程没有有效 STDIN 句柄，管道捕获
    是已知隐患）。
    """

    SCRIPT = Path(__file__).resolve().parents[1] / "smoke_test.py"

    def _source(self):
        return self.SCRIPT.read_text(encoding="utf-8")

    def test_script_exists(self):
        self.assertTrue(self.SCRIPT.is_file(), f"缺冒烟脚本：{self.SCRIPT}")

    def test_does_not_use_capture_output(self):
        """脚本**代码**里不得用 ``capture_output=True``。

        用 AST 查而不是文本匹配 —— 因为文档字符串里要解释「为什么不用它」，
        文本匹配会把那段说明也当成违规。

        原因：Windows 上服务进程没有有效 STDIN 句柄，GUI 子系统 exe 与
        管道捕获的组合也是已知隐患。改用文件重定向。
        """
        import ast

        tree = ast.parse(self._source())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "capture_output":
                    offenders.append(node.lineno)
        self.assertEqual(
            offenders,
            [],
            f"冒烟脚本第 {offenders} 行用了 capture_output：Windows 上服务进程"
            "没有有效 STDIN 句柄，且 GUI 子系统 exe 的管道捕获会挂死。"
            "改用文件重定向。",
        )

    def test_passes_stdin_devnull(self):
        # 显式给 stdin=DEVNULL 是 Windows 上跑服务/无人值守进程的必要动作。
        self.assertIn(
            "stdin=subprocess.DEVNULL",
            self._source(),
            "冒烟脚本必须显式传 stdin=subprocess.DEVNULL（Windows 服务进程"
            "没有有效 STDIN 句柄）",
        )

    def test_has_bounded_timeout(self):
        # 超时值乘以用例数就是一次失败的等待上限，所以它必须有界且不大。
        source = self._source()
        self.assertIn("timeout: int = ", source)
        # 抓出默认值，确认不超过 60 秒
        import re

        match = re.search(r"timeout: int = (\d+)", source)
        self.assertIsNotNone(match)
        self.assertLessEqual(
            int(match.group(1)),
            60,
            "冒烟脚本的单次超时不能超过 60 秒：它要乘以用例数，"
            "太长会让一次失败看起来像「CI 卡住了」",
        )

    def test_kills_process_tree_on_timeout(self):
        # 超时后必须杀掉整个进程树，否则 exe 拉起的浏览器会变成孤儿，
        # 继续占着资源，下一条用例接着受影响。
        #
        # **按 AST 查「有没有调用」，不按文本查「叫什么名字」。**
        # 文本查 "_kill_tree" 会把「函数叫什么」和「有没有调用」耦在一起 ——
        # 改个名就误报，而期望本身（超时必须杀进程树）一点没变。
        import ast

        called = False
        for node in ast.walk(ast.parse(self._source())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name and name.endswith("kill_tree"):
                called = True
        self.assertTrue(
            called,
            "超时路径必须调用「杀进程树」的函数 —— 否则 exe 拉起的浏览器会变成孤儿",
        )

    def test_timeout_failure_message_is_actionable(self):
        # 超时时给出的提示要指向真原因，而不是只说「超时了」。
        source = self._source()
        self.assertIn("模态对话框", source)
        self.assertIn("suppress", source)


if __name__ == "__main__":
    unittest.main()
