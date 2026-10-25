"""生产启动闸门：三条「只告警」必须升级成**阻断**（审查 §3.8 / §3.44 / §4.3）。

这一组钉的是**三件事的同一个形状**：配置缺了一样东西，而这件事的后果
不是「功能不好看」，是**数据或系统被人拿走/被人锁死** —— 于是在启动时
就该拦住，而不是在运行期打一条告警。

* **§3.8 ``BACKUP_ENCRYPTION_KEY``**：配了 OSS 备份就必须配备份加密密钥。
  备份管线是 ``gzip → SHA256 → OSS``，而 SHA256 是**校验和不是加密** ——
  OSS 上的对象是**明文 gzip 压缩包**；桶的读权限一旦泄露，全部账套的凭证
  与金额就一起出去了。消费者的 fail-open 分支在
  ``app/backup/service.py``：密钥为空时只打一条 warning，然后照旧上传明文。
  而配置里那句承诺（「生产校验会要求它非空」）**从来没兑现过** ——
  ``validate_production_settings`` 里根本没有这一项。

* **§3.44 ``TRUSTED_PROXIES``**：留空时 IP 维度只按 TCP 对端计，于是反代
  （nginx → 127.0.0.1:8000）后面的所有请求**共用一个桶**。实测一个
  **未认证**的匿名请求发 30 次错误登录就能把**全公司**的登录锁死 15 分钟，
  且每一条登录审计的来源 IP 都退化成 127.0.0.1。

* **§4.3-2 ``MCP_ALLOWED_HOSTS``**：``MCP_ENABLED`` 默认 **true**，而白名单
  默认空 —— 于是「照抄 ``.env.example`` 的部署」正好落在「MCP 开着 +
  传输层不校验 Host 头」这个形态里，而启动闸门对它是**完全沉默**的。

**三条都留了同一个形状的豁免口**（``*_WARN_ONLY=true``）：本机直连的部署
确实不需要那些配置，让它能**显式**地说「我知道我在做什么」。
"""
from __future__ import annotations

import base64
import os

import pytest

from app.core.config import Settings, validate_production_settings

GOOD_SECRET = "a" * 48
GOOD_DB = "postgresql+psycopg://cwq:Str0ngP%40ssXYZwAB@db.internal:5432/cwq"
#: 一把合法的 32 字节 base64 密钥。
GOOD_KEY = base64.b64encode(os.urandom(32)).decode()


def _prod(**kw):
    """一份**合规**的生产配置，改哪一项就改哪一项。"""
    base = dict(
        jwt_secret=GOOD_SECRET,
        database_url=GOOD_DB,
        trusted_proxies="127.0.0.1",
        mcp_allowed_hosts="acct.example.com:*",
    )
    base.update(kw)
    return Settings(**base)


def _check(s):
    """按**生产**环境校验（必须显式传 env="prod"，否则 conftest 的
    ``APP_ENV=test`` 会让校验整体跳过）。"""
    return validate_production_settings(s, env="prod")


def _oss_ready(**kw):
    """把 OSS 的四个参数配齐（于是备份真的会落到对象存储上）。"""
    return _prod(
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_access_key_id="ak",
        oss_access_key_secret="sk",
        oss_bucket="bucket",
        **kw,
    )


# --------------------------------------------------------------------- 基线


def test_合规的生产配置通过校验():
    """前提：下面所有「必须报错」的用例，都是在**这一份**配置上改一项。"""
    checks = _check(_prod())
    assert any("JWT_SECRET" in c for c in checks)


# --------------------------------------------------------------------- §3.8


def test_配了OSS备份就必须配备份加密密钥():
    with pytest.raises(ValueError) as e:
        _check(_oss_ready(backup_encryption_key=""))
    assert "BACKUP_ENCRYPTION_KEY" in str(e.value)


def test_备份密钥必须是合法base64():
    with pytest.raises(ValueError) as e:
        _check(_oss_ready(backup_encryption_key="不是base64!!"))
    assert "BACKUP_ENCRYPTION_KEY" in str(e.value)


def test_备份密钥必须正好32字节():
    short = base64.b64encode(os.urandom(16)).decode()
    with pytest.raises(ValueError) as e:
        _check(_oss_ready(backup_encryption_key=short))
    assert "BACKUP_ENCRYPTION_KEY" in str(e.value)


def test_备份密钥合法就放行():
    _check(_oss_ready(backup_encryption_key=GOOD_KEY))


def test_没配OSS就不要求备份密钥():
    """**没配 OSS 就没有备份制品**，也就没有「明文躺在对象存储上」这件事。"""
    _check(_prod(backup_encryption_key=""))


def test_备份密钥可以显式豁免():
    """豁免口是**显式**的：知情故犯可以，但要说出来。"""
    checks = _check(_oss_ready(backup_encryption_key="",
                               backup_encryption_key_warn_only=True))
    assert any("BACKUP_ENCRYPTION_KEY" in c or "明文" in c for c in checks)


# --------------------------------------------------------------------- §3.44


def test_可信代理为空时拒绝启动():
    with pytest.raises(ValueError) as e:
        _check(_prod(trusted_proxies=""))
    assert "TRUSTED_PROXIES" in str(e.value)


def test_可信代理可以显式豁免():
    checks = _check(_prod(trusted_proxies="", trusted_proxies_warn_only=True))
    assert any("TRUSTED_PROXIES" in c for c in checks), checks


def test_可信代理豁免时告警里要说清后果(caplog):
    """豁免**不是**「当作没这回事」—— 那条告警必须把后果说清楚。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        _check(_prod(trusted_proxies="", trusted_proxies_warn_only=True))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "TRUSTED_PROXIES" in text, text


def test_配了可信代理就放行():
    _check(_prod(trusted_proxies="127.0.0.1"))


# --------------------------------------------------------------------- §4.3-2


def test_MCP开着但没配Host白名单就拒绝启动():
    with pytest.raises(ValueError) as e:
        _check(_prod(mcp_enabled=True, mcp_allowed_hosts=""))
    assert "MCP_ALLOWED_HOSTS" in str(e.value)


def test_MCP白名单可以显式豁免():
    checks = _check(_prod(mcp_enabled=True, mcp_allowed_hosts="",
                           mcp_allowed_hosts_warn_only=True))
    assert any("MCP" in c for c in checks), checks


def test_MCP没开时不要求白名单():
    """MCP 关着就没有这个端点 —— 为一个没开的功能拒绝启动是拿可用性换风险。"""
    _check(_prod(mcp_enabled=False, mcp_allowed_hosts=""))


def test_MCP白名单配了就放行():
    _check(_prod(mcp_enabled=True, mcp_allowed_hosts="acct.example.com:*"))


# --------------------------------------------------------------------- 其它


def test_dev环境整体跳过():
    """开发机不该因为这些项起不来。"""
    validate_production_settings(
        _prod(trusted_proxies="", mcp_allowed_hosts="",
              backup_encryption_key=""), env="dev")


def test_一次报出全部问题():
    """配置错了就让人一项项试错，是把人往「干脆全用默认值」推。"""
    with pytest.raises(ValueError) as e:
        _check(_prod(trusted_proxies="", mcp_allowed_hosts="",
                     backup_encryption_key="",
                     oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
                     oss_access_key_id="ak", oss_access_key_secret="sk",
                     oss_bucket="bucket"))
    msg = str(e.value)
    assert "TRUSTED_PROXIES" in msg
    assert "MCP_ALLOWED_HOSTS" in msg
    assert "BACKUP_ENCRYPTION_KEY" in msg
