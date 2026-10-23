#!/usr/bin/env python3
"""CI 接线自检：workflow 与本地构建脚本**配置层**的护栏断言。

**为什么需要这一层。**

桌面壳有三道闸：单元测试（ubuntu）、真跑一次 exe（windows）、真 spawn
（windows）。第三道闸是本轮才加的，而「它到底有没有被接进去」这件事，
**目前没有任何东西在看**：

    · 有人给硬闸加上 ``continue-on-error`` —— 闸还在，但已经不算闸了；
    · 有人把「上传 exe 产物」挪到验证之前 —— 验证不过，产物照样发出去；
    · 有人把某个 action 从 pin 的 SHA 改回 ``@v4`` —— 供应链防护悄悄没了；
    · 有人把 ``needs: [test]`` 删掉 —— Windows 闸不再依赖单测闸。

这四种改动**不会让任何一行代码测试失败**，因为它们改的不是代码，是接线。
接线得有人盯着。

**为什么不用 PyYAML。**

壳是**零依赖工具**（它的设计前提就是「拷过去就能跑」），它的测试也就该零依赖。
为了读一个 YAML 就往 CI 里塞 ``pip install pyyaml``，等于给一把螺丝刀装个
计量器 —— 而且还得按仓库规矩做哈希锁定，那又是一份要维护的锁文件。

所以这里用一个**只够用的**迷你扫描器：按缩进切出 job、step 与 step 的
直接键。它不解 YAML 语义，只回答这几个问题：

    · 有哪些 job、每个 job 有哪些 step；
    · 每个 step 的**直接键**（``continue-on-error`` / ``uses`` / ``run`` …）；
    · 每个 step 的 ``run:`` 块里有哪些**真正的命令行**。

这三个问题的答案对这个文件是确定的，而且只要文件结构大改（比如改成用
anchor 或把 steps 挪进别的层级），下面这些用例会**响亮地失败**，而不是
悄悄跳过 —— 宁可红，也不要"看起来还在"。

**PyYAML 交叉检查。**

文件末尾另有一组用例：环境里装了 PyYAML 就用**真的 YAML 解析器**把同样的
结论再验一遍。文本扫描器有可能被注释骗（YAML 注释里出现
``continue-on-error`` 字样），解析器不会。这是"用第二双眼睛看一遍"，
不是两套规则。

跑法：
    python3 -m unittest discover -s tools/desktop-shell/tests -t tools/desktop-shell/tests -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "desktop-shell.yml"
BUILD_PS1 = REPO_ROOT / "tools" / "desktop-shell" / "build_windows.ps1"

# 闸 3 的步骤标题片段。改名了这里就会红 —— 那是故意的：改了名就得让人
# 知道"护栏的名字变了"，而不是无声地继续生效或无声地失效。
GATE_TITLE = "真的把浏览器拉起来"
UPLOAD_EXE_TITLE = "上传 exe 产物"
SMOKE_TITLE = "冒烟验证"

# 允许软失败的步骤白名单。
#
# **只有安装包那三步可以是 continue-on-error。** 理由写在 workflow 里：
# Inno Setup 靠 chocolatey 装，版本会漂，那是**环境**问题而不是代码问题，
# 让外部源的抖动把整条流水线染红，会让人开始习惯性忽略红色。
#
# 白名单而不是"禁止一切 continue-on-error"：安装包是次要产物，exe 才是主
# 产物，安装包出不来时 exe 照样能用。真正的主产物那几步必须是硬闸。
SOFT_FAIL_ALLOWED = {"装 Inno Setup", "编译安装包", "上传安装包产物"}

# ----------------------------------------------------------------- 迷你扫描器

_JOB_RE = re.compile(r"^  (?P<name>[A-Za-z0-9_.-]+):\s*$")
_STEP_RE = re.compile(r"^      - (?P<rest>.*)$")
# step 的**直接键**：正好 8 个空格缩进。
#
# 正好 8 个空格是关键 —— ``run:`` 块里的内容缩进 10+、``with:`` 块里的
# 内容也是 10+，都匹配不上；step 之间那段 6 空格缩进的 YAML 注释同样
# 匹配不上。所以这一条正则只捞得到 step 自己的键，捞不到块里的东西。
_STEP_KEY_RE = re.compile(r"^        (?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<value>.*)$")
# job 的**直接键**：正好 4 个空格缩进（``needs:`` / ``runs-on:`` / ``env:`` …）。
_JOB_KEY_RE = re.compile(r"^    (?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<value>.*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _split_jobs(lines: list[str]) -> dict[str, list[str]]:
    """把 workflow 切成 ``job 名 -> 该 job 的所有行``。

    只认 ``jobs:`` 之后的 2 空格缩进键 —— 因为 ``on:`` 底下也有 2 空格
    缩进的 ``push:`` / ``pull_request:``，把它们当 job 会得到一堆空壳。
    """
    start = None
    for index, line in enumerate(lines):
        if line.rstrip() == "jobs:":
            start = index
            break
    if start is None:
        raise AssertionError("workflow 里找不到顶层 jobs: 键 —— 文件结构变了？")

    jobs: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1 :]:
        match = _JOB_RE.match(line)
        if match:
            current = match.group("name")
            jobs[current] = []
            continue
        if current is not None:
            jobs[current].append(line)
    return jobs


def _split_steps(job_lines: list[str]) -> list[dict]:
    """把一个 job 的 ``steps:`` 切成若干 step。

    step 的起点是 6 空格缩进的 ``- ``。step 之间那段 6 空格缩进的注释
    （workflow 里解释「为什么」的那些）会被并到**上一个** step 的行里 ——
    这没关系，因为取直接键的规则只认 8 空格缩进，注释进不来。
    """
    try:
        begin = job_lines.index("    steps:")
    except ValueError:
        return []

    steps: list[dict] = []
    current: dict | None = None
    for line in job_lines[begin + 1 :]:
        match = _STEP_RE.match(line)
        if match:
            current = {"first": match.group("rest"), "lines": []}
            steps.append(current)
            continue
        if current is not None:
            current["lines"].append(line)
    return steps


def _scalar(value: str) -> str:
    """标量值，剥掉行内注释。

    YAML 里 `` #`` 之后是注释，所以 ``uses: actions/checkout@SHA # v4`` 的
    值是 ``actions/checkout@SHA`` 而不是带注释的整行。不剥的话 SHA 断言
    会把「加了版本注释」误判成「没 pin SHA」。
    """
    text = value.strip()
    if text.startswith("|") or text.startswith(">"):
        return text
    return re.sub(r"\s+#.*$", "", text).strip()


def _step_keys(step: dict) -> dict[str, str]:
    """step 的直接键 → 值。

    第一行（``- uses: …`` / ``- name: …``）里的键也算数。
    """
    keys: dict[str, str] = {}
    first = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$", step["first"])
    if first:
        keys[first.group(1)] = _scalar(first.group(2))
    for line in step["lines"]:
        match = _STEP_KEY_RE.match(line)
        if match:
            keys[match.group("key")] = _scalar(match.group("value"))
    return keys


def _nested_lines(step: dict, key: str) -> list[str]:
    """某个直接键下面的**嵌套块**的行（例如 ``with:`` 的内容）。"""
    lines = step["lines"]
    result: list[str] = []
    index = 0
    while index < len(lines):
        match = _STEP_KEY_RE.match(lines[index])
        if match and match.group("key") == key:
            index += 1
            while index < len(lines):
                line = lines[index]
                if not line.strip():
                    index += 1
                    continue
                if len(line) - len(line.lstrip()) <= 8:
                    break
                result.append(line)
                index += 1
            return result
        index += 1
    return result


def _run_lines(step: dict) -> list[str]:
    """step 的 ``run:`` 块里的**命令行**（注释行剔掉）。

    剔注释不是洁癖：workflow 里到处是解释「为什么」的注释，注释里出现
    ``continue-on-error``、``launch_path_probe.py`` 这种字眼时，要断言的
    是**脚本真的做了**，不是**有人在注释里提过一句**。
    """
    lines = step["lines"]
    result: list[str] = []
    index = 0
    while index < len(lines):
        match = _STEP_KEY_RE.match(lines[index])
        if match and match.group("key") == "run":
            value = match.group("value").strip()
            if value.startswith("|") or value.startswith(">"):
                index += 1
                while index < len(lines):
                    line = lines[index]
                    if not line.strip():
                        index += 1
                        continue
                    if len(line) - len(line.lstrip()) <= 8:
                        break
                    result.append(line)
                    index += 1
                continue
            result.append(value)
        index += 1
    return [line for line in result if not line.strip().startswith("#")]


def _step_name(step: dict) -> str:
    """step 的名字。``uses`` 型 step 用 uses 串当名字。"""
    keys = _step_keys(step)
    if "name" in keys:
        return keys["name"]
    if "uses" in keys:
        return f"(uses) {keys['uses']}"
    return "(无名 step)"


def _find_step(steps: list[dict], title: str) -> dict | None:
    for step in steps:
        if title in _step_name(step):
            return step
    return None


def _index_of_step(steps: list[dict], title: str) -> int:
    for index, step in enumerate(steps):
        if title in _step_name(step):
            return index
    raise AssertionError(f"步骤里找不到标题含「{title}」的 step —— 步骤改名了？")


# 中文数字 → 阿拉伯数字。说明书写成「做八件事」还是「做 8 件事」都是对的，
# 断言不该因为这个挑刺 —— 但「说八件、实际八段」要对得上。
_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _normalize_count(text: str) -> str:
    """把「八」或「8」统一成「8」。认不出来就原样返回（让断言去红）。"""
    if text.isdigit():
        return text
    if text in _CN_DIGITS:
        return str(_CN_DIGITS[text])
    return text


# ----------------------------------------------------------------- workflow


class WorkflowGateTest(unittest.TestCase):
    """``.github/workflows/desktop-shell.yml`` 的接线。"""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = _split_jobs(cls.text.splitlines())
        cls.steps = {job: _split_steps(lines) for job, lines in cls.jobs.items()}

    def _gate(self) -> dict:
        gate = _find_step(self.steps["build-windows"], GATE_TITLE)
        self.assertIsNotNone(
            gate,
            f"build-windows job 里找不到标题含「{GATE_TITLE}」的 step —— "
            "闸 3 被删了或改了名？请同步改这条断言，并确认闸 3 还在。",
        )
        return gate

    def test_gate_step_exists(self):
        """闸 3（真 spawn）必须还在。"""
        self._gate()

    def test_gate_step_cannot_fail_soft(self):
        """闸 3 **不能**是软失败 —— 不能失败的闸等于没有闸。"""
        gate = self._gate()
        self.assertNotIn(
            "continue-on-error",
            _step_keys(gate),
            "真 spawn 闸被加上了 continue-on-error：这一步现在失败了也不会挡住流水线。"
            "验证不过就不能往下走，否则等于没设闸。",
        )

    def test_only_installer_steps_may_fail_soft(self):
        """软失败只允许出现在安装包那三步。

        这条是白名单：任何**别**的 step 被打上 ``continue-on-error`` 都会红。
        防止「顺手加一个 continue-on-error」把别的闸悄悄作废。
        """
        soft = set()
        for job, steps in self.steps.items():
            for step in steps:
                if "continue-on-error" in _step_keys(step):
                    soft.add(_step_name(step))
        for name in soft - SOFT_FAIL_ALLOWED:
            self.fail(
                f"步骤「{name}」被加上了 continue-on-error。"
                f"允许软失败的只有：{sorted(SOFT_FAIL_ALLOWED)}。"
            )
        # 白名单不许被掏空：安装包三步应当都还在、都还是软失败。
        self.assertEqual(soft, SOFT_FAIL_ALLOWED)

    def test_gate_runs_before_artifact_upload(self):
        """闸 3 必须在「上传 exe 产物」**之前**。

        顺序反了的话，验证没过、产物照样发出去 —— 这正是把安装包步骤
        顺延到验证之后的原因，同样适用于 exe 上传。
        """
        steps = self.steps["build-windows"]
        self.assertLess(
            _index_of_step(steps, GATE_TITLE),
            _index_of_step(steps, UPLOAD_EXE_TITLE),
            "「上传 exe 产物」排在了真 spawn 闸之前：验证不过也会把产物发出去。",
        )

    def test_gate_step_really_runs_the_probe(self):
        """闸 3 里的命令行必须真的调起探针与替身浏览器。

        只看步骤名是不够的：步骤在、名字对，但里面的命令被换掉了，闸就空了。
        """
        run = _run_lines(self._gate())
        joined = "\n".join(run)
        self.assertIn(
            "launch_path_probe.py",
            joined,
            "真 spawn 闸里没有调用 launch_path_probe.py —— 这道闸现在是空的。",
        )
        self.assertIn(
            "stub_browser.py",
            joined,
            "真 spawn 闸里没有打替身浏览器（stub_browser.py）—— 没有替身就接不住参数。",
        )
        self.assertIn("--exe", joined, "探针调用没有传 --exe。")
        self.assertIn("--stub", joined, "探针调用没有传 --stub。")

    def test_smoke_gate_cannot_fail_soft(self):
        """冒烟闸（真跑一次 exe）同样必须是硬闸。"""
        smoke = _find_step(self.steps["build-windows"], SMOKE_TITLE)
        self.assertIsNotNone(smoke, "找不到冒烟验证那一步 —— 它被删了？")
        self.assertNotIn("continue-on-error", _step_keys(smoke))

    def test_artifact_upload_errors_when_exe_missing(self):
        """exe 没打出来时，上传步骤必须报错而不是悄悄放过。

        ``if-no-files-found: error`` 是把「静默失败」挡在闸外的一道。
        """
        upload = _find_step(self.steps["build-windows"], UPLOAD_EXE_TITLE)
        self.assertIsNotNone(upload)
        with_block = "\n".join(_nested_lines(upload, "with"))
        self.assertIn(
            "if-no-files-found: error",
            with_block,
            "exe 上传步骤没写 if-no-files-found: error —— 产物不存在时这一步会显示成功。",
        )

    def test_every_action_is_pinned_to_a_commit_sha(self):
        """所有 action 必须 pin 到 40 位 commit SHA。

        标签（``@v4``）指向可变引用：仓库可以把 ``v4`` 重新指向另一个 commit，
        于是「昨天还干净的 action」今天就能跑完全不同的代码 —— 而流水线里
        跑的是有 token 权限的代码。
        """
        offenders = []
        for job, steps in self.steps.items():
            for step in steps:
                uses = _step_keys(step).get("uses")
                if not uses:
                    continue
                if "@" not in uses:
                    offenders.append(f"{job} / {_step_name(step)}: {uses}（没有 @ref）")
                    continue
                ref = uses.rsplit("@", 1)[1]
                if not _SHA_RE.match(ref):
                    offenders.append(f"{job} / {_step_name(step)}: {uses}")
        self.assertEqual(offenders, [], f"这些 action 没 pin 到 commit SHA：{offenders}")

    def test_windows_job_depends_on_unit_test_job(self):
        """Windows 闸必须 ``needs: [test]`` —— 单测不过就别占 Windows 额度。

        额度是按月算的（免费账户 2,000 分钟/月），让 Windows runner 在
        单测已经红了的情况下白跑，是实打实的浪费。
        """
        job_keys = {}
        for line in self.jobs["build-windows"]:
            match = _JOB_KEY_RE.match(line)
            if match:
                job_keys[match.group("key")] = _scalar(match.group("value"))
        self.assertIn("needs", job_keys, "build-windows job 没有 needs: —— 它不再依赖单测闸。")
        needs = job_keys["needs"].strip().strip("[]")
        listed = {item.strip() for item in needs.split(",") if item.strip()}
        self.assertIn("test", listed, f"build-windows 的 needs 是 {listed}，不含 test。")

    def test_workflow_triggers_on_its_own_changes(self):
        """改这个 workflow 自己也要能触发它。

        不然会出现"改了闸但没跑"：接线被改坏时，workflow 静静地看着自己。
        """
        self.assertRegex(
            self.text,
            r"-\s*'\.github/workflows/desktop-shell\.yml'",
            "workflow 的 paths 里没有它自己 —— 改了这个文件不会触发这次检查。",
        )


# ----------------------------------------------------------------- 本地脚本


class LocalBuildScriptTest(unittest.TestCase):
    """``tools/desktop-shell/build_windows.ps1`` 的接线。

    本地构建脚本和 workflow 是**两套**调用点 —— 两套都会漂。所以本地
    这套也得有人盯着。
    """

    @classmethod
    def setUpClass(cls):
        cls.text = BUILD_PS1.read_text(encoding="utf-8")
        cls.lines = cls.text.splitlines()

    def _find_invoke(self, needle: str) -> int:
        """找到**真的**在调用某东西的那一行（注释不算）。"""
        for index, line in enumerate(self.lines):
            if line.strip().startswith("#"):
                continue
            if needle in line:
                return index
        raise AssertionError(f"脚本里找不到调用「{needle}」的行 —— 它被删了或改了名？")

    def test_script_runs_the_probe(self):
        """本地构建脚本必须真的调起探针。"""
        self._find_invoke("launch_path_probe.py")

    def test_script_builds_the_stub_browser(self):
        """本地构建脚本必须真的打替身浏览器。"""
        self._find_invoke("stub_browser.py")

    def test_probe_failure_stops_the_build(self):
        """探针没过必须 ``Fail``（停下），不能是软失败。

        本地脚本没有 CI 那种"红着也能看产物"的语境 —— 验证不过就意味着
        这份 exe 别拿去用。
        """
        index = self._find_invoke("launch_path_probe.py")
        follower = index + 1
        while follower < len(self.lines):
            stripped = self.lines[follower].strip()
            if stripped and not stripped.startswith("#"):
                break
            follower += 1
        self.assertLess(follower, len(self.lines), "探针调用后面没有别的行了。")
        guard = self.lines[follower]
        self.assertIn(
            "$LASTEXITCODE",
            guard,
            f"探针调用之后没有检查退出码（{guard!r}）—— 探针红了也会被当成通过。",
        )
        self.assertIn("Fail", guard, f"探针退出码非 0 时没有 Fail（{guard!r}）。")

    def test_probe_runs_before_installer(self):
        """探针必须在出安装包**之前** —— 验证不过就别再往下做。"""
        probe_index = self._find_invoke("launch_path_probe.py")
        installer_index = self._find_invoke("installer.iss")
        self.assertLess(
            probe_index,
            installer_index,
            "安装包步骤排在了真 spawn 验证之前。",
        )

    def test_unit_tests_run_before_packaging(self):
        """单元测试必须在打包**之前**跑。

        PyInstaller 对 Python 语法的错误很宽容，打包成功不代表跑得起来。
        测试放在打包前面，绿了再打。
        """
        test_index = self._find_invoke("-m unittest discover")
        pack_index = self._find_invoke("--distpath")
        self.assertLess(test_index, pack_index, "单元测试排在了打包之后。")

    def test_synopsis_matches_actual_steps(self):
        """脚本头的 ``.DESCRIPTION`` 必须与脚本实际步骤数一致。

        这是实测出来的漂移：加过冒烟与真 spawn 两步之后，说明书还写着
        "做六件事"，脚本里已经有 8 段。文档比代码旧，比代码没坏更糟 ——
        它会让人以为某一步不存在。
        """
        sections = re.findall(r"^# -+ (\d+)\. ", self.text, flags=re.MULTILINE)
        self.assertTrue(sections, "脚本里找不到编号的段落标题。")
        count = len(sections)

        described = re.search(r"这份脚本做(\S+?)件事", self.text)
        self.assertIsNotNone(described, "``.DESCRIPTION`` 里没有「这份脚本做N件事」。")
        self.assertEqual(
            _normalize_count(described.group(1)),
            str(count),
            f"``.DESCRIPTION`` 说是 {described.group(1)} 件事，脚本里实际有 {count} 段。",
        )

        # 每一段都得在说明里出现过，而且顺序一致。
        synopsis = self.text.split(".DESCRIPTION", 1)[-1].split(".PARAMETER", 1)[0]
        keywords = [
            "虚拟环境",
            "PyInstaller",
            "图标",
            "单元测试",
            "打包",
            "冒烟",
            "真实启动路径",
            "安装包",
        ]
        positions = []
        for keyword in keywords:
            index = synopsis.find(keyword)
            self.assertNotEqual(
                index,
                -1,
                f"``.DESCRIPTION`` 里没提到「{keyword}」，但脚本里有这一步。",
            )
            positions.append((keyword, index))
        for (earlier, before), (later, after) in zip(positions, positions[1:]):
            self.assertLess(
                before,
                after,
                f"``.DESCRIPTION`` 里「{earlier}」排在「{later}」后面 —— 顺序和脚本不一致。",
            )


# ----------------------------------------------------------------- 交叉检查

try:  # pragma: no cover - 取决于环境有没有 PyYAML
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@unittest.skipIf(
    yaml is None,
    "本环境没装 PyYAML（壳是零依赖工具，CI 侧不装它）；文本扫描已覆盖同一批断言",
)
class YamlCrossCheckTest(unittest.TestCase):
    """用**真的 YAML 解析器**把同一批结论再验一遍。

    文本扫描器可能被注释骗过，解析器不会。这一组是"第二双眼睛"，
    不是另一套规则 —— 两边结论必须一致。
    """

    @classmethod
    def setUpClass(cls):
        cls.data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_workflow_parses(self):
        self.assertIn("jobs", self.data)

    def test_gate_step_has_no_continue_on_error(self):
        steps = self.data["jobs"]["build-windows"]["steps"]
        gate = next(step for step in steps if GATE_TITLE in str(step.get("name", "")))
        self.assertNotIn("continue-on-error", gate)

    def test_gate_precedes_upload(self):
        steps = self.data["jobs"]["build-windows"]["steps"]
        names = [str(step.get("name", "")) for step in steps]
        gate_index = next(i for i, name in enumerate(names) if GATE_TITLE in name)
        upload_index = next(i for i, name in enumerate(names) if UPLOAD_EXE_TITLE in name)
        self.assertLess(gate_index, upload_index)

    def test_only_installer_steps_soft_fail(self):
        soft = set()
        for job in self.data["jobs"].values():
            for step in job["steps"]:
                if step.get("continue-on-error"):
                    soft.add(str(step.get("name", "")))
        self.assertEqual(soft, SOFT_FAIL_ALLOWED)

    def test_every_action_is_pinned_to_a_commit_sha(self):
        offenders = []
        for job in self.data["jobs"].values():
            for step in job["steps"]:
                uses = step.get("uses")
                if not uses:
                    continue
                ref = str(uses).rsplit("@", 1)[-1]
                if not _SHA_RE.match(ref):
                    offenders.append(str(uses))
        self.assertEqual(offenders, [])

    def test_windows_job_needs_unit_test_job(self):
        needs = self.data["jobs"]["build-windows"].get("needs")
        self.assertIn("test", [str(item) for item in (needs or [])])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
