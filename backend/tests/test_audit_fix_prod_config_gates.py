"""生产启动闸门：两条「只告警」必须升级成**阻断**（审查 §3.44 / §4.3）。

这一组钉的是**两件事的同一个形状**：配置缺了一样东西，而这件事的后果
不是「功能不好看」，是**系统被人锁死 / 被人拿走** —— 于是在启动时
就该拦住，而不是在运行期打一条告警。

* **§3.44 ``TRUSTED_PROXIES``**：留空时 IP 维度只按 TCP 对端计，于是反代
  （nginx → 127.0.0.1:8000）后面的所有请求**共用一个桶**。实测一个
  **未认证**的匿名请求发 30 次错误登录就能把**全公司**的登录锁死 15 分钟，
  且每一条登录审计的来源 IP 都退化成 127.0.0.1。

* **§4.3-2 ``MCP_ALLOWED_HOSTS``**：``MCP_ENABLED`` 默认 **true**，而白名单
  默认空 —— 于是「照抄 ``.env.example`` 的部署」正好落在「MCP 开着 +
  传输层不校验 Host 头」这个形态里，而启动闸门对它是**完全沉默**的。

**两条都留了同一个形状的豁免口**（``*_WARN_ONLY=true``）：本机直连的部署
确实不需要那些配置，让它能**显式**地说「我知道我在做什么」。

**§3.8 ``BACKUP_ENCRYPTION_KEY`` 那一段已于 2026-10-26 移除**：备份制品
的静态加密改由 **OSS 服务端加密（SSE）** 承担，客户端不再加密，也就没有
「密钥为空」这个状态需要拦了。新的事实由
``tests/test_backup_server_side_encryption.py`` 守着。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings, validate_production_settings

GOOD_SECRET = "a" * 48
GOOD_DB = "postgresql+psycopg://cwq:Str0ngP%40ssXYZwAB@db.internal:5432/cwq"


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


def test_配了OSS备份不会因为加密密钥被拦住():
    """**客户端加密那层已经没有了** —— 配了 OSS 就不会因为「没配备份加密
    密钥」而被启动闸门拦住。

    这条用例钉的是**移除**这件事：谁把 ``BACKUP_ENCRYPTION_KEY`` 那一项
    加回启动校验（哪怕是"顺手补上"），这里立刻红。
    """
    checks = _check(_oss_ready())
    assert any("服务端加密" in c for c in checks), checks


# --------------------------------------------------------------------- 基线


def test_合规的生产配置通过校验():
    """前提：下面所有「必须报错」的用例，都是在**这一份**配置上改一项。"""
    checks = _check(_prod())
    assert any("JWT_SECRET" in c for c in checks)


# --------------------------------------------------------------------- §3.8（已于 2026-10-26 移除）
#
# 原来这里钉的是「配了 OSS 备份就必须配备份加密密钥」那道启动闸。
# 客户端加密那一层整段移除了（静态加密改由 OSS 服务端加密承担），
# 于是没有「密钥为空」这个状态，也就没有这道闸。
#
# **留下的替代物**：桶上的 SSE 规则状态是**看得见的事实**，由
# ``tests/test_backup_server_side_encryption.py`` 守着
# （「测试连接」报出来、每轮备份记进日志、没开时告警）。


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


def test_报错文案必须说清两种写法都要给():
    """**只写 `example.com:*` 是不够的** —— 照着报错里的示例配会得到
    「启动过去了，但每个 MCP 请求都被传输层挡成 421」。

    传输层（MCP SDK 的 ``TransportSecurityMiddleware``）的匹配规则是：

        * ``host in allowed_hosts`` → 精确命中；
        * 或 ``host.startswith(base + ":")`` → **必须带冒号**。

    所以 ``example.com:*`` **匹配不到不带端口的 Host**（``example.com``）——
    而 nginx 传给后端的 ``Host`` 恰恰是 ``$host``（**不带端口**）。

    这条是**真踩过**的：生产发布时按报错里的示例只写了 ``:*``，
    启动闸门过了、站点起来了，但 MCP 端点一个请求都进不去。
    """
    with pytest.raises(ValueError) as e:
        _check(_prod(mcp_enabled=True, mcp_allowed_hosts=""))
    msg = str(e.value)
    assert "acct.example.com,acct.example.com:*" in msg, (
        "报错里没有给出「两种写法都要给」的完整示例 —— "
        "照着它配会得到「启动过去了但 MCP 全被 421 挡下」")
    assert "不带端口" in msg, msg


def test_只写域名带端口的形式_闸门放行但传输层匹配不到裸域名():
    """把「闸门放行」与「传输层真能匹配上」这两件事**分开**钉。

    闸门只看「有没有配」；传输层看「配的那几条能不能匹配上真实的 Host」。
    **两边都绿**才是真的绿 —— 只钉闸门那一半，会得到一个
    「启动检查过了、但功能是坏的」的部署。
    """
    from mcp.server.transport_security import TransportSecurityMiddleware

    def middleware(hosts):
        return TransportSecurityMiddleware(
            type("Sec", (), {"allowed_hosts": hosts,
                             "allowed_origins": [],
                             "enable_dns_rebinding_protection": True})())

    # nginx 传给后端的 Host：$host —— **不带端口**。
    host = "acct.example.com"

    # 只写 :* 的形式：闸门放行（配了），但传输层匹配不上裸域名。
    only_wildcard = ["acct.example.com:*"]
    assert not middleware(only_wildcard)._validate_host(host), (
        "只写 :* 竟然匹配上了裸域名 —— 那这条用例的前提不成立")

    # 两种都写：裸域名与带端口都能匹配上，非法的一律匹配不上。
    both = ["acct.example.com", "acct.example.com:*"]
    mw = middleware(both)
    assert mw._validate_host(host), "裸域名匹配不上"
    assert mw._validate_host("acct.example.com:443"), "带端口匹配不上"
    assert not mw._validate_host("evil.example"), "非法 Host 竟然放行了"
    assert not mw._validate_host("acct.example.com.evil.test"), "后缀 spoof 放行了"


# --------------------------------------------------------------------- 其它


def test_dev环境整体跳过():
    """开发机不该因为这些项起不来。"""
    validate_production_settings(
        _prod(trusted_proxies="", mcp_allowed_hosts=""), env="dev")


def test_一次报出全部问题():
    """配置错了就让人一项项试错，是把人往「干脆全用默认值」推。"""
    with pytest.raises(ValueError) as e:
        _check(_prod(trusted_proxies="", mcp_allowed_hosts="",
                     oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
                     oss_access_key_id="ak", oss_access_key_secret="sk",
                     oss_bucket="bucket"))
    msg = str(e.value)
    assert "TRUSTED_PROXIES" in msg
    assert "MCP_ALLOWED_HOSTS" in msg
