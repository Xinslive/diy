#!/usr/bin/env python3
"""探针脚本（``launch_path_probe.py``）的单元测试。

**为什么探针也要有单元测试。**
探针本身是 CI 在 ``windows-latest`` 上跑的一道闸，而那条 workflow 每跑一次
要 2~4 分钟、还要吃 GitHub 的免费额度。探针里那些**纯逻辑**（沙箱布局、
环境变量覆盖、期望参数）完全不需要 Windows 就能验 —— 把它们放到 ubuntu
上跑，改一行布局就能立刻看到红，而不是等下一次 Windows 跑批才发现。

真正依赖 Windows 的部分（真 spawn、真拉起浏览器）由探针自己在 Windows 上跑，
这里不重复。

跑法：
    python3 -m unittest discover -s tools/desktop-shell/tests -t tools/desktop-shell/tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 壳不是包，从仓库任何位置跑都要能找到它
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import launcher  # noqa: E402
import launch_path_probe as probe  # noqa: E402
from smoke_test import SmokeFailure  # noqa: E402


@contextlib.contextmanager
def quiet():
    """吞掉探针往 stdout 打的汇报。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield buffer


class LayoutTest(unittest.TestCase):
    """沙箱布局。"""

    def test_stub_lands_at_real_edge_install_path(self):
        """替身必须落在**真实的 Edge 安装位置**，否则验不到发现逻辑。"""
        layout = probe.build_layout(Path("/sandbox"))
        self.assertEqual(
            layout["stub"],
            Path("/sandbox")
            / "Program Files (x86)"
            / "Microsoft"
            / "Edge"
            / "Application"
            / "msedge.exe",
        )

    def test_stub_filename_matches_launcher_candidate(self):
        """替身的文件名必须与 launcher.py 里 BROWSER_CANDIDATES 写的一致。

        对不上就意味着壳在那个位置找不到它 —— 探针会验到「没找到浏览器」
        那条降级路径，而不是「找到了并拉起来了」。
        """
        self.assertIn(
            probe.STUB_FILENAME,
            "".join(path for cand in launcher.BROWSER_CANDIDATES for path in cand.paths),
        )

    def test_profile_dir_holds_chinese_app_name(self):
        """profile 目录名里有中文 —— 这正是要验「中文路径没被编码破坏」的地方。"""
        layout = probe.build_layout(Path("/sandbox"))
        self.assertIn("财务记账系统", str(layout["profile"]))
        self.assertEqual(layout["profile"].name, "browser-profile")

    def test_log_path_is_under_local_appdata(self):
        layout = probe.build_layout(Path("/sandbox"))
        self.assertEqual(layout["log"].name, "shell.log")
        self.assertEqual(layout["log"].parent.name, "logs")
        self.assertEqual(layout["log"].parent.parent.name, "财务记账系统")


class SetEnvTest(unittest.TestCase):
    """环境变量覆盖。

    **这些用例守的是一条真实的坑**：Windows 的环境变量名大小写不敏感，
    而 CPython 构造子进程环境块时把键原样写进去。两条同名不同大小写的
    变量并存时，子进程读到哪一条不受控 —— 沙箱一旦失效，壳就会去启动
    runner 上**真的** Edge。
    """

    def test_replaces_existing_variant(self):
        env = {"PROGRAMFILES(X86)": r"C:\Program Files (x86)"}
        probe.set_env(env, "ProgramFiles(x86)", r"D:\sandbox")
        self.assertEqual(env, {"ProgramFiles(x86)": r"D:\sandbox"})

    def test_removes_every_case_variant(self):
        env = {"programfiles(x86)": "a", "PROGRAMFILES(X86)": "b", "ProgramFiles(x86)": "c"}
        probe.set_env(env, "ProgramFiles(x86)", "d")
        self.assertEqual(len(env), 1)
        self.assertEqual(env["ProgramFiles(x86)"], "d")

    def test_does_not_touch_unrelated_variables(self):
        env = {"PATH": r"C:\Windows", "TEMP": r"C:\Temp"}
        probe.set_env(env, "LOCALAPPDATA", r"D:\local")
        self.assertEqual(env["PATH"], r"C:\Windows")
        self.assertEqual(env["TEMP"], r"C:\Temp")
        self.assertEqual(env["LOCALAPPDATA"], r"D:\local")


class ChildEnvTest(unittest.TestCase):
    """传给壳子进程的环境。"""

    def setUp(self):
        self.layout = probe.build_layout(Path("/sandbox"))
        self.base = {
            "PATH": r"C:\Windows",
            "ProgramFiles(x86)": r"C:\Program Files (x86)",
            "LOCALAPPDATA": r"C:\Users\runner\AppData\Local",
        }
        self.env = probe.build_child_env(
            self.base, self.layout, Path("/sandbox/argv.json")
        )

    def test_program_files_redirected_to_sandbox(self):
        self.assertEqual(self.env["ProgramFiles(x86)"], str(self.layout["pf86"]))
        self.assertEqual(self.env["ProgramFiles"], str(self.layout["pf64"]))

    def test_real_program_files_entry_is_gone(self):
        """真实的那条必须**消失**，不能两条并存。

        并存时子进程可能读到真目录 → 真的去启动 runner 上的 Edge。
        """
        matches = [k for k in self.env if k.upper() == "PROGRAMFILES(X86)"]
        self.assertEqual(matches, ["ProgramFiles(x86)"])

    def test_local_appdata_redirected_to_sandbox(self):
        self.assertEqual(self.env["LOCALAPPDATA"], str(self.layout["local"]))
        matches = [k for k in self.env if k.upper() == "LOCALAPPDATA"]
        self.assertEqual(len(matches), 1)

    def test_noninteractive_flags_set(self):
        """无人值守标记必须置上 —— 否则壳弹模态框会把进程**永久阻塞**。

        CI 上真卡过 11 分钟：模态框在没人值守的 runner 上等不到人点。
        """
        self.assertTrue(self.env["CI"])
        self.assertTrue(self.env["CWQ_SHELL_NONINTERACTIVE"])

    def test_record_path_passed_to_stub(self):
        self.assertEqual(
            self.env[probe.RECORD_ENV], str(Path("/sandbox/argv.json"))
        )


class ExpectedArgsTest(unittest.TestCase):
    """期望传给浏览器的参数。"""

    def setUp(self):
        self.layout = probe.build_layout(Path("/sandbox"))
        self.args = probe.expected_browser_args(self.layout)

    def test_shape(self):
        self.assertEqual(len(self.args), 5)
        self.assertEqual(self.args[0], "--app=https://acct.xins.live")
        self.assertEqual(self.args[2], "--window-size=1360,880")
        self.assertEqual(self.args[3], "--no-first-run")
        self.assertEqual(self.args[4], "--no-default-browser-check")

    def test_profile_arg_is_absolute(self):
        """相对路径会被解析到随机的工作目录 —— 双击时那个目录是随机的。"""
        with tempfile.TemporaryDirectory() as tmp:
            layout = probe.build_layout(Path(tmp))
            args = probe.expected_browser_args(layout)
            profile = args[1].split("=", 1)[1]
            self.assertTrue(Path(profile).is_absolute(), profile)

    def test_profile_arg_keeps_chinese_intact(self):
        self.assertIn("财务记账系统", self.args[1])


class ExpectationDriftTest(unittest.TestCase):
    """探针的期望值与产品实际行为的一致性检查。"""

    def test_probe_expectation_matches_product_arguments(self):
        """探针写死的期望参数必须与产品实际会传的参数一致。

        **为什么要加这道交叉检查。**
        探针的期望值是**独立写死**的（不 import 产品的函数）——
        只有这样，它在运行时才能独立地失败：产品改了行为，探针会红。

        但独立也意味着**会漂移**：有人给 ``build_app_args`` 加了一个参数，
        探针还按旧的期望，于是要等下一次 Windows 跑批（慢、贵、还要额度）
        才发现。这道测试把这种漂移提前到 ubuntu 上，几秒钟就红。

        注意它**不是**期望值的来源 —— 期望值仍然写死在探针里。
        这条测试只是「漂了就早说一声」。
        """
        with tempfile.TemporaryDirectory() as tmp:
            layout = probe.build_layout(Path(tmp))
            product = launcher.build_app_args(
                probe.EXPECTED_URL,
                str(layout["profile"]),
                probe.EXPECTED_WINDOW_SIZE,
            )
            self.assertEqual(probe.expected_browser_args(layout), product)


class WaitForRecordTest(unittest.TestCase):
    """等替身把命令行写出来。"""

    def test_returns_recorded_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "argv.json"
            path.write_text(
                json.dumps(["C:\\x\\msedge.exe", "--app=https://acct.xins.live"]),
                encoding="utf-8",
            )
            argv = probe.wait_for_record(path, timeout=5)
            self.assertEqual(argv, ["C:\\x\\msedge.exe", "--app=https://acct.xins.live"])

    def test_keeps_chinese_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "argv.json"
            path.write_text(
                json.dumps(["msedge.exe", "--user-data-dir=C:\\财务记账系统"], ensure_ascii=False),
                encoding="utf-8",
            )
            argv = probe.wait_for_record(path, timeout=5)
            self.assertEqual(argv[1], "--user-data-dir=C:\\财务记账系统")

    def test_timeout_raises(self):
        """文件一直不出现就必须**报错退出**，不能静默通过。

        静默通过 = 这道闸形同虚设。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            with self.assertRaises(SmokeFailure):
                probe.wait_for_record(path, timeout=0.3, interval=0.05)


class CheckPeTest(unittest.TestCase):
    """替身必须是真 PE。"""

    def test_accepts_mz_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stub.exe"
            path.write_bytes(b"MZ" + b"\x00" * 64)
            probe.check_pe(path, "替身")

    def test_rejects_batch_file_renamed(self):
        """``.bat`` 改名成 .exe 会被 Windows 拒绝执行（错误 193）。

        替身不合格的症状看起来像「壳坏了」，所以要在这里挡掉。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stub.exe"
            path.write_bytes(b"@echo off\r\necho hi\r\n")
            with self.assertRaises(SmokeFailure):
                probe.check_pe(path, "替身")


class InstallStubTest(unittest.TestCase):
    """把替身装到真实的 Edge 安装位置。"""

    def test_copies_to_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.exe"
            src.write_bytes(b"MZstub")
            dest = (
                Path(tmp)
                / "Program Files (x86)"
                / "Microsoft"
                / "Edge"
                / "Application"
                / "msedge.exe"
            )
            probe.install_stub(src, dest)
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_bytes(), b"MZstub")


class RecordPathConsistencyTest(unittest.TestCase):
    """替身记录路径只能有**一处**决定。"""

    def test_env_record_path_matches_polled_path(self):
        """环境变量里的记录路径必须与探针轮询的路径**完全一致**。

        **为什么要有这条。**
        第一次在 Wine 下跑探针时，这两条路径不一样：环境变量告诉替身
        「记到 ``argv.json``」，而探针在等 ``argv-discovery.json`` ——
        替身明明把文件写对了，探针却在等另一个文件，最后报「没记录到参数」。

        那个症状极具误导性：看起来像「壳没把浏览器拉起来」，其实壳完全正常
        （诊断时发现逻辑、拉起、5 个参数、中文路径全对）。这种「报了个
        假故障」的闸比没有闸更糟 —— 它会让人去查错方向。

        所以路径必须**只从 run_scenario 这一个入口进来**，环境现造。
        """
        captured: dict[str, object] = {}

        def fake_run_exe(exe, *args, timeout=45, env=None):
            captured["env"] = dict(env or {})
            return 0, "", ""

        def fake_wait(path, timeout, interval=0.2, log_path=None):
            captured["polled"] = path
            return ["stub.exe"]

        with mock.patch.object(probe, "run_exe", fake_run_exe), mock.patch.object(
            probe, "wait_for_record", fake_wait
        ):
            probe.run_scenario(
                Path("shell.exe"),
                (),
                Path("/sandbox/argv-x.json"),
                {"PATH": "x"},
                probe.build_layout(Path("/sandbox")),
                5.0,
            )

        self.assertEqual(
            captured["env"][probe.RECORD_ENV],
            str(captured["polled"]),
            "替身被告知的记录路径与探针轮询的路径不是同一个 —— "
            "替身写对了文件探针也会等不到",
        )


class PlatformGuardTest(unittest.TestCase):
    """平台守卫。"""

    def test_refuses_to_run_on_posix(self):
        """在 macOS/Linux 上必须**拒绝运行**并给出说明。

        原因：它要直接 ``Popen`` 一个 PE 文件，在 POSIX 上根本跑不起来。
        让人以为「跑过了」比「跑不了」糟。
        """
        with mock.patch.object(probe.platform, "system", return_value="Linux"):
            with quiet() as buffer:
                rc = probe.main(
                    ["launch_path_probe", "--exe", "no.exe", "--stub", "no.exe"]
                )
        self.assertEqual(rc, 1)
        self.assertIn("Windows", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
