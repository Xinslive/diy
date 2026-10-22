#!/usr/bin/env python3
"""桌面壳的单元测试。

**为什么用 unittest 而不是 pytest**：这个壳是零依赖工具，它的测试也不该
拖上一个 pytest。``python -m unittest`` 就能跑；pytest 同样能收集
``unittest.TestCase``，所以仓库里那套 pytest 也能一并跑它 —— 两种方式
都能跑，不是二选一。

**测的是什么**：全部是**平台无关的纯逻辑** —— 路径展开、浏览器发现、
参数拼装、URL 校验、配置解析、启动计划。真正依赖 Windows 的部分
（拉起进程、消息框）由 CI 在 ``windows-latest`` 上用 ``--dry-run``
产出的 JSON 来断言，那条闸在 ``.github/workflows/desktop-shell.yml``。

跑法：
    python3 -m unittest discover -s tools/desktop-shell/tests -v
    # 或
    python3 -m pytest tools/desktop-shell/tests
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
