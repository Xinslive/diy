"""周期记账插件的变量引擎与凭证构造。

插件源码在 ``plugins/`` 下，而 ``plugins/`` 是 gitignore 的（见
``docs/audit-remediation-2026-10-25.md``）：**干净 checkout 里没有这份源码**。
所以源码缺失时整份用例跳过，而不是失败 —— 在本机全绿、在 CI 必红的用例
等于没有用例。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.plugin_fixtures import plugin_env

# 加载插件源码时**不要写 .pyc**：插件目录是要原样打进 .cwp 安装包的，落一个
# __pycache__ 进去会让打包被静态扫描以 RESOURCE_TYPE 拒掉（1.5.0 打包时真踩过）。
sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ENTRY = REPO_ROOT / "plugins/com.cwq.periodic/backend/main.py"

# 用 skipif 而不是模块级 pytest.skip：模块级跳过会让 pytest 认为「一条都没
# 收集到」，退出码是 5；单跑这一个文件时 CI 会把它当成失败。skipif 仍然收集
# 用例、只是逐条跳过，退出码是 0。
pytestmark = pytest.mark.skipif(
    not PLUGIN_ENTRY.is_file(), reason="本机没有周期记账插件源码")


def _load_plugin():
    spec = importlib.util.spec_from_file_location("cwq_periodic_plugin", PLUGIN_ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin() if PLUGIN_ENTRY.is_file() else None


def _values(year, month, rule=None, voucher_date=None):
    return {item["name"]: item["value"]
            for item in plugin._variable_catalog(year, month, rule, voucher_date)}


def _rule(entries=None, **extra):
    row = {
        "id": 1,
        "name": "养老保险",
        "day_of_month": 25,
        "entries": entries or [],
        "remark": "",
        "attachments": 1,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# 变量表
# ---------------------------------------------------------------------------

def test_catalog_covers_month_year_and_context():
    values = _values(2026, 3)
    assert values["本月"] == "202603"
    assert values["本月起"] == "20260301"
    assert values["本月止"] == "20260331"
    assert values["本月区间"] == "20260301-20260331"
    assert values["上月"] == "202602"
    assert values["上月区间"] == "20260201-20260228"
    assert values["下月"] == "202604"
    assert values["上上月"] == "202601"
    assert values["本年"] == "2026"
    assert values["本年区间"] == "20260101-20261231"
    assert values["去年"] == "2025"
    assert values["期间"] == "2026年3月"


def test_catalog_crosses_year_boundary():
    values = _values(2026, 1)
    assert values["上月"] == "202512"
    assert values["上月区间"] == "20251201-20251231"
    assert values["去年"] == "2025"


def test_catalog_handles_leap_february():
    assert _values(2028, 3)["上月区间"] == "20280201-20280229"
    assert _values(2026, 3)["上月区间"] == "20260201-20260228"


def test_catalog_adds_voucher_and_rule_context():
    when = plugin._resolve_day(2026, 3, 31)
    values = _values(2026, 3, {"name": "办公室租金"}, when)
    assert values["规则名"] == "办公室租金"
    assert values["凭证日期"] == "2026-03-31"
    assert values["凭证年月日"] == "20260331"


def test_catalog_every_entry_has_name_value_and_description():
    for item in plugin._variable_catalog(2026, 3):
        assert item["name"]
        assert "value" in item
        assert item["desc"]


# ---------------------------------------------------------------------------
# 模板解析
# ---------------------------------------------------------------------------

def test_resolve_replaces_known_variables():
    values = _values(2026, 3)
    text, unknown = plugin._resolve_template("养老保险费{本月区间}", values)
    assert text == "养老保险费20260301-20260331"
    assert unknown == []


def test_resolve_handles_previous_month_range():
    """用户提的场景：3 月交上个月的个税。"""
    values = _values(2026, 3)
    text, _ = plugin._resolve_template("个人所得税{上月区间}", values)
    assert text == "个人所得税20260201-20260228"


def test_resolve_keeps_unknown_variables_and_reports_them():
    values = _values(2026, 3)
    text, unknown = plugin._resolve_template("摘要{上月区間}", values)
    assert text == "摘要{上月区間}"
    assert unknown == ["上月区間"]


def test_resolve_tolerates_literal_braces():
    values = _values(2026, 3)
    text, unknown = plugin._resolve_template("金额 {} 元", values)
    assert text == "金额 {} 元"
    assert unknown == []


# ---------------------------------------------------------------------------
# 凭证构造：变量真的进凭证
# ---------------------------------------------------------------------------

def _draft(row, year, month):
    resolved = {"1001": 11, "1002": 12}
    draft, problem, warnings = plugin._build_draft(None, row, resolved, year, month)
    assert problem == ""
    return draft, warnings


def test_build_draft_expands_variables_into_entry_summary():
    row = _rule(entries=[
        {"code": "1001", "direction": "DEBIT", "amount": "100.00",
         "summary": "养老保险费{本月区间}"},
        {"code": "1002", "direction": "CREDIT", "amount": "100.00",
         "summary": "养老保险费{本月区间}"},
    ])
    draft, warnings = _draft(row, 2026, 3)
    assert draft["entries"][0]["summary"] == "养老保险费20260301-20260331"
    assert draft["entries"][1]["summary"] == "养老保险费20260301-20260331"
    assert warnings == []


def test_build_draft_expands_variables_into_remark():
    row = _rule(entries=[
        {"code": "1001", "direction": "DEBIT", "amount": "100.00", "summary": ""},
        {"code": "1002", "direction": "CREDIT", "amount": "100.00", "summary": ""},
    ], remark="缴{上月区间}社保")
    draft, warnings = _draft(row, 2026, 3)
    assert draft["remark"] == "缴20260201-20260228社保"
    assert warnings == []


def test_build_draft_keeps_auto_summary_when_summary_is_blank():
    row = _rule(entries=[
        {"code": "1001", "direction": "DEBIT", "amount": "100.00", "summary": ""},
        {"code": "1002", "direction": "CREDIT", "amount": "100.00", "summary": ""},
    ])
    draft, _ = _draft(row, 2026, 3)
    assert draft["entries"][0]["summary"] == "2026年3月 养老保险"


def test_build_draft_warns_about_unknown_variable_but_still_builds():
    row = _rule(entries=[
        {"code": "1001", "direction": "DEBIT", "amount": "100.00", "summary": "社保{上月区間}"},
        {"code": "1002", "direction": "CREDIT", "amount": "100.00", "summary": ""},
    ])
    draft, warnings = _draft(row, 2026, 3)
    assert draft["entries"][0]["summary"] == "社保{上月区間}"
    assert len(warnings) == 1
    assert "上月区間" in warnings[0]


def test_build_draft_variables_follow_the_posted_month_not_today():
    """补记 2 月时，{上月区间} 指 1 月 —— 跟着要记的那个月走，不跟着今天走。

    否则 4 月补记 2 月的凭证，摘要会和当初 2 月自动生成的那张对不上。
    """
    row = _rule(entries=[
        {"code": "1001", "direction": "DEBIT", "amount": "100.00", "summary": "{本月区间}"},
        {"code": "1002", "direction": "CREDIT", "amount": "100.00", "summary": "{上月区间}"},
    ])
    february, _ = _draft(row, 2026, 2)
    assert february["entries"][0]["summary"] == "20260201-20260228"
    assert february["entries"][1]["summary"] == "20260101-20260131"


def test_build_draft_truncates_expanded_summary():
    row = _rule(entries=[
        {"code": "1001", "direction": "DEBIT", "amount": "100.00", "summary": "{本月区间}"},
        {"code": "1002", "direction": "CREDIT", "amount": "100.00", "summary": ""},
    ])
    row["entries"][0]["summary"] = "x" * 250 + "{本月区间}"
    draft, _ = _draft(row, 2026, 3)
    assert len(draft["entries"][0]["summary"]) <= plugin.ENTRY_SUMMARY_MAX


# ---------------------------------------------------------------------------
# /variables 接口
# ---------------------------------------------------------------------------

class _FakeRegistrar:
    def __init__(self):
        self.routes = {}
        self.hooks = {}
        self.tasks = {}

    def get(self, path, endpoint):
        self.routes[("GET", path)] = endpoint

    def post(self, path, endpoint):
        self.routes[("POST", path)] = endpoint

    def hook(self, name, handler):
        self.hooks[name] = handler

    def task(self, name, handler):
        self.tasks[name] = handler


class _FakeCtx:
    pass


def _variables_endpoint():
    registrar = _FakeRegistrar()
    plugin.register(registrar, _FakeCtx())
    assert ("POST", "/variables") in registrar.routes
    return registrar.routes[("POST", "/variables")]


def test_variables_endpoint_returns_catalog_and_resolution():
    endpoint = _variables_endpoint()
    data = endpoint({"year": 2026, "month": 3, "template": "费{上月区间}"})
    assert data["year"] == 2026
    assert data["month"] == 3
    assert data["resolved"] == "费20260201-20260228"
    assert data["unknown"] == []
    names = {item["name"] for item in data["variables"]}
    assert "本月区间" in names
    assert "上月区间" in names


def test_variables_endpoint_reports_unknown_variable():
    endpoint = _variables_endpoint()
    data = endpoint({"year": 2026, "month": 3, "template": "{不存在}"})
    assert data["resolved"] == "{不存在}"
    assert data["unknown"] == ["不存在"]


def test_variables_endpoint_defaults_to_current_month():
    endpoint = _variables_endpoint()
    data = endpoint({"template": "{本月}"})
    today = plugin._today()
    assert data["year"] == today.year
    assert data["month"] == today.month


def test_variables_endpoint_includes_rule_context():
    endpoint = _variables_endpoint()
    data = endpoint({"year": 2026, "month": 3, "day_of_month": 25,
                     "rule_name": "办公室租金", "template": "{凭证日期}"})
    assert data["resolved"] == "2026-03-25"
    values = {item["name"]: item["value"] for item in data["variables"]}
    assert values["规则名"] == "办公室租金"


def test_variables_endpoint_rejects_bad_day():
    endpoint = _variables_endpoint()
    with pytest.raises(Exception):
        endpoint({"year": 2026, "month": 3, "day_of_month": 40})


# ---------------------------------------------------------------------------
# 端到端：把**真实插件**装进宿主，走 HTTP 调 /variables
#
# 上面那些用例直接调插件函数，绕过了宿主的路由装配、body 分发、静态扫描与
# 迁移。这一段走的是真实安装闸门：签名 → 扫描 → 迁移 → 装进隔离测试库 →
# 重启加载 → HTTP 调用。
# ---------------------------------------------------------------------------

PLUGIN_ROOT = REPO_ROOT / "plugins/com.cwq.periodic"


def _real_package(build):
    """用真实的插件源码与 manifest 打一个测试签名包。"""
    manifest = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    files = {}
    for path in sorted(PLUGIN_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.name == ".DS_Store":
            # Finder 元数据不是插件资源：真实打包器会排除它，测试打包器不会，
            # 带进去会被静态扫描以 RESOURCE_TYPE 拒掉整个包。
            continue
        relative = path.relative_to(PLUGIN_ROOT).as_posix()
        # plugin.json 是 manifest 本身，不进 files 清单（清单里再出现一次会被
        # 判成「重复或包含 plugin.json」）。
        if relative == "plugin.json":
            continue
        files[relative] = path.read_bytes()

    def edit(target):
        # 换成插件自己的 manifest（id、权限、表、钩子、定时任务），
        # files 沿用夹具按同一份源码算出来的清单。
        for key, value in manifest.items():
            if key not in ("files", "signature"):
                target[key] = value

    raw, built, _keys, _private = build(files=files, edit=edit)
    return raw, built


def test_real_plugin_installs_and_serves_variables_over_http(plugin_env, client, auth, admin_user):
    build, restart = plugin_env
    raw, manifest = _real_package(build)
    pid = manifest["id"]

    install = client.post("/api/system/plugins/install",
                           files={"file": ("periodic.cwp", raw)}, headers=auth)
    assert install.status_code == 200, install.text
    restart()
    assert client.post(f"/api/system/plugins/{pid}/users/{admin_user.id}", headers=auth).status_code == 200

    response = client.post(f"/api/plugins/{pid}/variables", headers=auth,
                           json={"year": 2026, "month": 3, "template": "养老保险费{本月区间}"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["resolved"] == "养老保险费20260301-20260331"
    assert data["unknown"] == []
    names = {item["name"] for item in data["variables"]}
    assert {"本月区间", "上月区间"} <= names
    # 凭证日期、规则名是**规则上下文**里的变量：不带规则来问，就不该出现在清单里。
    assert "凭证日期" not in names

    with_rule = client.post(f"/api/plugins/{pid}/variables", headers=auth,
                            json={"year": 2026, "month": 3, "day_of_month": 25,
                                  "rule_name": "养老保险", "template": "{凭证日期}"})
    assert with_rule.status_code == 200, with_rule.text
    assert with_rule.json()["resolved"] == "2026-03-25"


def test_real_plugin_reports_unknown_variable_over_http(plugin_env, client, auth, admin_user):
    build, restart = plugin_env
    raw, manifest = _real_package(build)
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install",
                       files={"file": ("periodic.cwp", raw)}, headers=auth).status_code == 200
    restart()
    assert client.post(f"/api/system/plugins/{pid}/users/{admin_user.id}", headers=auth).status_code == 200
    response = client.post(f"/api/plugins/{pid}/variables", headers=auth,
                           json={"year": 2026, "month": 3, "template": "社保{上月区間}"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["resolved"] == "社保{上月区間}"
    assert data["unknown"] == ["上月区間"]

