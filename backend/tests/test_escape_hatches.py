"""「逃生口」的登记与闸门。

对应审计 §3.44 的收紧：那一条带了个 warn-only 豁免开关
（``MCP_ALLOWED_HOSTS_WARN_ONLY``），部署侧还有
``CWQ_DEPLOY_ALLOW_EMPTY_TRUSTED_PROXIES``。
默认值是阻断，所以「条目算修好」；但只要开关开着，**生产姿态**就被削弱了。

（``BACKUP_ENCRYPTION_KEY_WARN_ONLY`` 那个已于 2026-10-26 随客户端加密
那一层一起移除 —— 静态加密改由 OSS 服务端加密承担，不再有「密钥为空」
这个状态需要豁免。）

这里把这个残余风险收口，三条断言：

1. **随产品分发的生产配置模板里不得开着任何逃生口**
   （``.env.example`` / systemd unit）—— 挡住「照抄示例部署就带着口子上线」；
2. **部署脚本里出现的每个 ``CWQ_*`` 名字都必须登记过**
   —— 挡住「新增了一个部署侧开关却忘了登记」（漏登记的开关没有任何检查覆盖）。
3. **持久化配置（``.env``）里开着逃生口 → 生产环境拒绝启动**
   —— 逃生口只允许对**单次运行**生效：写进配置文件，它就从「临时放宽」
   变成了「永久降级」，而且没有任何地方会提醒（豁免本来就是安静放行的）。

**任何一条被放回去，这里立刻红。**
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import Settings, persistent_escape_hatches, validate_production_settings
from app.core.escape_hatches import (
    APP_SIDE_ESCAPE_HATCHES,
    APP_SIDE_ESCAPE_HATCH_FIXES,
    DEPLOY_SIDE_ALL_NAMES,
    DEPLOY_SIDE_ESCAPE_HATCHES,
    DEPLOY_SIDE_NON_ESCAPE_SWITCHES,
    DEPLOY_SIDE_PARAMETERS,
    DEPLOY_SIDE_SWITCHES,
    ESCAPE_HATCH_ENV_VARS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
DEPLOY_SH = REPO_ROOT / "deploy" / "deploy.sh"

#: 判定「这一行把某个名字设成了真」。
#:
#: 不用 pydantic 的解析（那是运行期的事），这里用一条够用的规则：
#: ``NAME=value``，值去掉行尾注释与引号后落在 {1,true,yes,on} 里。
_TRUTHY = {"1", "true", "yes", "on"}


def _enabled_switches(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split("#", 1)[0].strip().strip("\"'").strip().lower()
        if value in _TRUTHY:
            found[key] = value
    return found


# ---------------------------------------------------------------------------
# 一、清单本身
# ---------------------------------------------------------------------------


def test_清单与Settings字段一一对应():
    """清单里的每个名字都要有对应字段，且每个字段都要登记。

    「多一个」（字段没登记）才是要紧的那个：漏登记的开关**不会被任何检查
    覆盖**，它就成了一道没人看得见的、永远开着的口子。
    """
    fields = set(Settings.model_fields)
    for hatch in APP_SIDE_ESCAPE_HATCHES:
        assert hatch.lower() in fields, f"{hatch} 在 Settings 里没有对应字段"
    for field in sorted(fields):
        if field.endswith("_warn_only"):
            assert field.upper() in APP_SIDE_ESCAPE_HATCHES, (
                f"Settings 的 {field} 是逃生口但没登记 —— 漏登记的开关不会被检查覆盖"
            )


def test_每个逃生口都有正式配法():
    """报错时要能直接告诉运维「那该怎么配」。

    只说「不许开」而不说「该怎么配」，等于把判断又丢回给运维。
    """
    for hatch in APP_SIDE_ESCAPE_HATCHES:
        assert hatch in APP_SIDE_ESCAPE_HATCH_FIXES, f"{hatch} 没有写正式配法"
        assert APP_SIDE_ESCAPE_HATCH_FIXES[hatch].strip(), hatch


def test_收紧项不算逃生口():
    """``CWQ_DEPLOY_REQUIRE_BACKUP_ARTIFACT=1`` 是**收紧**，不是放宽。

    把收紧项算成逃生口，会让清单失去意义 —— 「清单里的都是弱点」这条性质
    一旦破了，就没人再看它了。
    """
    assert "CWQ_DEPLOY_REQUIRE_BACKUP_ARTIFACT" not in ESCAPE_HATCH_ENV_VARS
    assert "CWQ_DEPLOY_REQUIRE_BACKUP_ARTIFACT" in DEPLOY_SIDE_NON_ESCAPE_SWITCHES


# ---------------------------------------------------------------------------
# 二、随产品分发的生产配置模板
# ---------------------------------------------------------------------------


def test_env_example里没有任何逃生口被打开():
    """照抄 ``.env.example`` 部署的机器，不该一上来就带着口子。"""
    assert ENV_EXAMPLE.is_file(), ".env.example 不在仓库里"
    enabled = _enabled_switches(ENV_EXAMPLE.read_text(encoding="utf-8"))
    for hatch in APP_SIDE_ESCAPE_HATCHES:
        assert hatch not in enabled, f".env.example 把 {hatch} 设成了 {enabled[hatch]}"


def test_systemd模板里没有任何逃生口被打开():
    """systemd 的 ``Environment=`` 也是**随产品分发**的一部分。"""
    units = sorted(SYSTEMD_DIR.glob("*.service"))
    assert units, "systemd 模板不在仓库里"
    for unit in units:
        enabled = _enabled_switches(unit.read_text(encoding="utf-8"))
        for hatch in APP_SIDE_ESCAPE_HATCHES:
            assert hatch not in enabled, f"{unit.name} 把 {hatch} 设成了 {enabled[hatch]}"


# ---------------------------------------------------------------------------
# 三、部署侧的开关必须登记过
# ---------------------------------------------------------------------------


def test_deploy脚本里的每个开关都登记过():
    """新增部署侧开关却忘了登记 → 没有任何检查覆盖它。

    登记的**方向**也要对：清单里的名字必须真的在 deploy.sh 里出现，
    否则清单会慢慢变成一份没人维护的旧账。

    **扫的是全部 ``CWQ_*`` 名字，不是只有 ``CWQ_DEPLOY_*`` 前缀。**
    以前只扫前缀，于是 ``CWQ_ALLOW_ANY_HOST``（跳过目标机守卫）与
    ``CWQ_HOST_IP``（注入守卫用来判身份的 IP，同样能绕过目标机守卫）
    这两个**真实的逃生口**正好落在断言之外 —— 它们既没登记，
    也没有任何检查覆盖。这就是「同一个量只允许一个权威实现」被破坏的样子：
    权威清单缺了两项，于是那两项就成了没人管的暗门。
    """
    assert DEPLOY_SH.is_file(), "deploy.sh 不在仓库里"
    found = set(re.findall(r"\bCWQ_[A-Z0-9_]+", DEPLOY_SH.read_text(encoding="utf-8")))
    unregistered = found - set(DEPLOY_SIDE_ALL_NAMES)
    assert not unregistered, f"这些部署侧名字没有登记：{sorted(unregistered)}"
    stale = set(DEPLOY_SIDE_ALL_NAMES) - found
    assert not stale, f"清单里这些名字 deploy.sh 里已经没有了：{sorted(stale)}"


def test_目标机守卫的两个逃生口都登记过():
    """这两个能绕过「本机是不是那台被授权的机器」，必须显式登记。

    ``CWQ_ALLOW_ANY_HOST`` 是显式开关；``CWQ_HOST_IP`` 长得像个参数 ——
    它直接替换掉守卫判本机身份用的那个 IP，注成被授权的 IP 就都过了。
    把它当参数看待，就等于把一个逃生口当成无害的配置项。
    """
    for name in ("CWQ_ALLOW_ANY_HOST", "CWQ_HOST_IP"):
        assert name in DEPLOY_SIDE_ESCAPE_HATCHES, f"{name} 是逃生口但没登记"
        assert name in ESCAPE_HATCH_ENV_VARS, f"{name} 不在总清单里"


# ---------------------------------------------------------------------------
# 四、启动闸门：持久化配置里开着逃生口 → 生产环境拒绝启动
# ---------------------------------------------------------------------------


def _production_settings():
    """一份「其它项都合格」的生产配置（只关心逃生口这一项）。"""
    return Settings(
        jwt_secret="a-very-long-production-secret-value-0123456789",
        database_url="postgresql+psycopg://cwq:a-long-database-password@localhost:15432/cwq",
        jwt_algorithm="HS256",
        admin_email_mfa_link_base="https://acct.example.com",
        trusted_proxies="127.0.0.1",
        mcp_allowed_hosts="acct.example.com",
    )


def test_持久化配置里开着逃生口时生产环境拒绝启动(tmp_path):
    """写进 ``.env`` 的逃生口 = 这台机器的常态 → 拒绝启动。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+psycopg://cwq:x@localhost:15432/cwq\n"
        "TRUSTED_PROXIES_WARN_ONLY=true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        validate_production_settings(_production_settings(), env="prod", env_file=env_file)
    message = str(exc.value)
    assert "TRUSTED_PROXIES_WARN_ONLY" in message, message
    # 报错要直接给出「那该怎么配」，而不是只说「不许开」。
    assert "把 TRUSTED_PROXIES 配成" in message, message
    assert "单次启动" in message, message


def test_逃生口关着时生产环境正常通过(tmp_path):
    """默认形态（关着）不该被这条闸门误伤。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+psycopg://cwq:x@localhost:15432/cwq\n"
        "TRUSTED_PROXIES_WARN_ONLY=false\n",
        encoding="utf-8",
    )
    checks = validate_production_settings(_production_settings(), env="prod", env_file=env_file)
    assert any("JWT_SECRET" in item for item in checks)


def test_逃生口写在进程环境里不算持久化(tmp_path):
    """单次运行的豁免是**合规用法**，不该被这条闸门拦。

    ``TRUSTED_PROXIES_WARN_ONLY=true .venv/bin/uvicorn ...`` —— 这一次完了
    就没了，没有留下任何常态。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+psycopg://cwq:x@localhost:15432/cwq\n",
        encoding="utf-8",
    )
    assert persistent_escape_hatches(env_file) == []
    # 进程环境里的同名变量不在这条闸门的检查范围里（读的是文件，不是环境）。
    import os

    os.environ["TRUSTED_PROXIES_WARN_ONLY"] = "true"
    try:
        assert persistent_escape_hatches(env_file) == []
        validate_production_settings(_production_settings(), env="prod", env_file=env_file)
    finally:
        os.environ.pop("TRUSTED_PROXIES_WARN_ONLY", None)


def test_dev与test环境不受这条闸门约束(tmp_path):
    """开发机不该被拦住 —— 拦掉开发机只会让人学会用绕过开关。"""
    env_file = tmp_path / ".env"
    env_file.write_text("TRUSTED_PROXIES_WARN_ONLY=true\n", encoding="utf-8")
    for env in ("dev", "test"):
        checks = validate_production_settings(_production_settings(), env=env, env_file=env_file)
        assert checks and "跳过生产配置校验" in checks[0]


def test_读取逃生口时的解析规则(tmp_path):
    """引号、行尾注释、空格、大小写都要认对。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join([
            "# 一行注释",
            "TRUSTED_PROXIES_WARN_ONLY=true",
            'MCP_ALLOWED_HOSTS_WARN_ONLY = "1"   # 带引号和行尾注释',
            "TRUSTED_PROXIES=127.0.0.1",
            "SOMETHING_ELSE=true",
        ]),
        encoding="utf-8",
    )
    found = dict(persistent_escape_hatches(env_file))
    assert set(found) == {
        "TRUSTED_PROXIES_WARN_ONLY",
        "MCP_ALLOWED_HOSTS_WARN_ONLY",
    }
    assert found["MCP_ALLOWED_HOSTS_WARN_ONLY"] == "1"
