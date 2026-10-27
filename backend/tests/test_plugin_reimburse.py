"""民宿报销插件：科目规则、账套级覆盖与表页映射。

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

from tests.plugin_fixtures import plugin_env  # noqa: F401  (夹具要导入才注册)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins/com.cwq.reimburse"
PLUGIN_ENTRY = PLUGIN_ROOT / "backend/main.py"

# 用 skipif 而不是模块级 pytest.skip：模块级跳过会让 pytest 认为「一条都没
# 收集到」，退出码是 5；单跑这一个文件时 CI 会把它当成失败。skipif 仍然收集
# 用例、只是逐条跳过，退出码是 0。
pytestmark = pytest.mark.skipif(
    not PLUGIN_ENTRY.is_file(), reason="本机没有民宿报销插件源码")


def _load_plugin():
    # 加载插件源码时**不要写 .pyc**：插件目录是要原样打进 .cwp 安装包的，
    # 落一个 __pycache__ 进去会让打包被静态扫描以 RESOURCE_TYPE 拒掉。
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("cwq_reimburse_plugin", PLUGIN_ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin() if PLUGIN_ENTRY.is_file() else None


# ---------------------------------------------------------------------------
# 规则标识
# ---------------------------------------------------------------------------

def test_rule_key_is_keyword_combination():
    rule = {"all": ["医", "社保"], "any": [], "not": [], "code": "221104"}
    assert plugin._rule_key(rule) == "医+社保||"
    # 关键词改了就是另一条规则，覆盖不会串过去
    assert plugin._rule_key(rule) != plugin._rule_key(
        {"all": ["社保"], "any": [], "not": [], "code": "221104"})


def test_rule_key_normalizes_like_the_frontend():
    # 全角、空格、大小写都要归一化，否则前端算出来的标识后端认不出
    assert plugin._rule_key({"all": ["　医　"], "any": [], "not": []}) == "医||"
    assert plugin._rule_key({"all": ["ＡＢＣ"], "any": [], "not": []}) == "abc||"


# ---------------------------------------------------------------------------
# 账套级覆盖
# ---------------------------------------------------------------------------

def _config():
    return {
        "counterparty_code": "1001",
        "pending_code": "221104",
        "subject_rules": [
            {"all": ["医", "社保"], "any": [], "not": [], "code": "221104"},
            {"all": ["工资"], "any": [], "not": [], "code": "560107"},
        ],
    }


def test_override_wins_over_shared_rules():
    config = _config()
    config["book_overrides"] = {"7": {"subject_codes": {"医+社保||": "56021202"}}}
    applied = plugin._apply_book_override(config, 7)
    codes = {rule["all"][0]: rule["code"] for rule in applied["subject_rules"]}
    assert codes["医"] == "56021202"   # 这本账覆盖了
    assert codes["工资"] == "560107"   # 没覆盖的沿用共用


def test_override_also_covers_counterparty_subject():
    config = _config()
    config["book_overrides"] = {"7": {"counterparty_code": "1002"}}
    applied = plugin._apply_book_override(config, 7)
    assert applied["counterparty_code"] == "1002"
    # 别的账套不受影响
    other = plugin._apply_book_override(config, 8)
    assert other["counterparty_code"] == "1001"


def test_book_without_override_keeps_shared_config():
    config = _config()
    config["book_overrides"] = {"7": {"subject_codes": {"医+社保||": "56021202"}}}
    applied = plugin._apply_book_override(config, 9)
    assert applied["subject_rules"][0]["code"] == "221104"
    assert applied["counterparty_code"] == "1001"


def test_clean_overrides_drops_junk_and_keeps_valid():
    cleaned = plugin._clean_overrides({
        "7": {"subject_codes": {"医+社保||": "56021202", "工资||": ""},
              "counterparty_code": "1002", "pending_code": ""},
        "8": {"subject_codes": {}},
        "9": "不是对象",
    })
    assert cleaned == {"7": {"subject_codes": {"医+社保||": "56021202"},
                             "counterparty_code": "1002"}}


def test_clean_overrides_ignores_non_dict():
    assert plugin._clean_overrides(None) == {}
    assert plugin._clean_overrides(["7"]) == {}


# ---------------------------------------------------------------------------
# 端到端：把**真实插件**装进宿主，走 HTTP 调 /rules
# ---------------------------------------------------------------------------

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
            continue
        relative = path.relative_to(PLUGIN_ROOT).as_posix()
        if relative == "plugin.json":
            continue
        files[relative] = path.read_bytes()

    def edit(target):
        for key, value in manifest.items():
            if key not in ("files", "signature"):
                target[key] = value

    raw, built, _keys, _private = build(files=files, edit=edit)
    return raw, built


def test_real_plugin_round_trips_book_overrides_over_http(plugin_env, client, auth, admin_user):
    build, restart = plugin_env
    raw, manifest = _real_package(build)
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install",
                       files={"file": ("reimburse.cwp", raw)}, headers=auth).status_code == 200
    restart()
    assert client.post(f"/api/system/plugins/{pid}/users/{admin_user.id}", headers=auth).status_code == 200

    rules = client.get(f"/api/plugins/{pid}/rules", headers=auth)
    assert rules.status_code == 200, rules.text
    config = rules.json()["config"]
    assert config["book_overrides"] == {}
    assert config["subject_rules"], "预置的关键词规则应当读得到"

    config["book_overrides"] = {"7": {"subject_codes": {"医+社保||": "56021202"},
                                      "counterparty_code": "1002"}}
    saved = client.post(f"/api/plugins/{pid}/rules", headers=auth, json={"config": config})
    assert saved.status_code == 200, saved.text

    again = client.get(f"/api/plugins/{pid}/rules", headers=auth)
    assert again.status_code == 200, again.text
    stored = again.json()["config"]["book_overrides"]
    assert stored["7"]["subject_codes"]["医+社保||"] == "56021202"
    assert stored["7"]["counterparty_code"] == "1002"


def test_real_plugin_no_longer_serves_suggest_endpoint(plugin_env, client, auth, admin_user):
    """「建议」列去掉了，后端那条按关键词给候选的接口也一并撤掉。"""
    build, restart = plugin_env
    raw, manifest = _real_package(build)
    pid = manifest["id"]
    assert client.post("/api/system/plugins/install",
                       files={"file": ("reimburse.cwp", raw)}, headers=auth).status_code == 200
    restart()
    assert client.post(f"/api/system/plugins/{pid}/users/{admin_user.id}", headers=auth).status_code == 200
    response = client.get(f"/api/plugins/{pid}/suggest", headers=auth, params={"book_id": 1})
    assert response.status_code == 404, response.text
