"""审查报告里**已确认的认证与安全缺陷**的回归测试。

**这个文件钉的是什么。** 每一组用例对应一条「改前能红、改后能绿」的边界：

* **P0-1（管理员接管链）**：生产环境没配 ``ADMIN_MFA_LINK_BASE`` 时，
  二次验证**不许**发出指向客户端 Host 头的链接 —— 要么 503，要么链接指向配置值。
  这条是最要紧的：它挡的是「**只凭口令就能完整接管管理员**」。
* **M-11（JWT 载荷）**：令牌必须带 ``iat`` / ``jti`` / ``iss``；``iss`` 对不上
  的一律拒（跨系统复用 ``JWT_SECRET`` 的越权路径）。
* **M-12（``sub`` 缺失）**：签名合法但没有 ``sub`` 的令牌 ⇒ **401**，不是 500。
* **P1-2（PG 原文泄露）**：数据库异常不得把表名/约束名/重复键的值回给客户端。
* **L-02（点链接接口）**：猜错要有退避，不能让未认证请求无限驱动数据库查询。
* **L-04（死代码）**：``source_bucket_key`` 已删除（它定义的键与
  ``login_ip_throttle.ip`` 的列宽对不上，接上去会静默截断）。
* **L-05（CORS）**：开发环境不带凭据（本项目全仓无 Cookie 依赖）。
* **L-07（审计可见性）**：注释说的是事实。
* **L-09（PBKDF2 轮数）**：老散列照样登得进去，且登录成功后自动升级。
* **L-10（汇报口径）**：管理端面板说的阶梯 = 代码的实际行为。

**没写进来的**：口令复杂度要求（产品已拍板不做），以及登录锁定的强度
（只改汇报口径，不改行为）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from types import SimpleNamespace

import jwt
import pytest
from sqlalchemy.exc import IntegrityError

from app.core import lockout, system_settings
from app.core.config import settings
from app.core.security import (create_access_token, decode_access_token,
                               hash_password, password_needs_rehash,
                               verify_password)
from app.models import SysUser, SystemSetting
from app.notify import mailer
from app.notify import mfa as mfa_service

#: 本文件会动的 ``sys_setting`` 行（跨用例存活：接口里的 commit 会提交）。
_TOUCHED_KEYS = (
    "admin_mfa_enabled",
    "smtp_host", "smtp_port", "smtp_username", "smtp_password", "smtp_from",
    "smtp_use_ssl", "smtp_use_tls", "smtp_timeout_seconds",
    "notify_recipients", "notify_enabled",
)


# --------------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _clean_settings(db):
    """把本文件会动的 ``sys_setting`` 行清干净（前前后后各一次）。"""
    def wipe():
        for key in _TOUCHED_KEYS:
            row = db.get(SystemSetting, key)
            if row is not None:
                db.delete(row)
        db.commit()

    wipe()
    yield
    wipe()


@pytest.fixture(autouse=True)
def _reset_verify_counter():
    """点链接接口的失败计数是**进程级**的 —— 用例之间必须清干净。

    不然条用例猜了 10 次，后面所有用例都被退避挡住，症状是「单跑绿、
    全量跑红」，而且**时好时坏**（取决于谁先跑到哪里）。
    """
    mfa_service.reset_verify_state()
    yield
    mfa_service.reset_verify_state()


@pytest.fixture
def prod_env(monkeypatch):
    """把环境判定切成**生产**（显式 ``APP_ENV`` 没配时本来就算 prod）。"""
    monkeypatch.setattr(settings, "app_env", "prod")


@pytest.fixture
def no_link_base(monkeypatch):
    """``ADMIN_MFA_LINK_BASE`` 留空（默认值就是空，这里显式钉住）。"""
    monkeypatch.setattr(settings, "admin_mfa_link_base", "")


@pytest.fixture
def sent_mails(monkeypatch):
    """把真发信换成一个记录器（只换 ``mailer.send_mail``，其余照跑）。"""
    sent: list[dict] = []

    def fake_send_mail(config, to_addresses, subject, body):
        sent.append({"to": list(to_addresses), "subject": subject, "body": body})
        return len(to_addresses)

    monkeypatch.setattr(mailer, "send_mail", fake_send_mail)
    return sent


@pytest.fixture
def mfa_ready(db):
    """二次验证**打开** + SMTP 通道**配好**（与 test_admin_mfa 的同名夹具同义）。"""
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    system_settings.set_value(db, "smtp_host", "smtp.invalid")
    system_settings.set_value(db, "smtp_from", "noreply@example.com")
    system_settings.set_value(db, "notify_recipients", "ops@example.com")
    db.commit()


# --------------------------------------------------------------------- 助手


def _wait_for_mail(sent, timeout=3.0, count=1) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(sent) >= count:
            return sent[count - 1]
        time.sleep(0.02)
    raise AssertionError(f"验证邮件没有发出来（只到了 {len(sent)} 封）")


def _token_from_body(body: str) -> str:
    match = re.search(r"token=([A-Za-z0-9_\-]+)", body)
    assert match, f"邮件正文里没有验证链接：\n{body}"
    return match.group(1)


def _hash_with_iterations(password: str, iterations: int) -> str:
    """造一条**指定轮数**的散列（模拟存量用户的老散列）。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return (f"pbkdf2_sha256${iterations}$"
            f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}")


class _FakePgError(Exception):
    """一条**像 PG 的**违例异常：带 sqlstate 与 diag.message_primary。"""

    def __init__(self, message: str, sqlstate: str = "23505") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(message_primary=message)


# ============================================================================
# P0-1：MFA 验证链接的基址不许取自客户端可控的 Host 头
# ============================================================================


def test_生产环境没配站点地址时登录回503且不发邮件(client, admin_user,
                                                  mfa_ready, sent_mails,
                                                  prod_env, no_link_base):
    """**这一条是整组里最要紧的。**

    改前：链接退回 ``request.base_url``（= 客户端的 Host 头）⇒ 攻击者带
    ``Host: evil.example`` 登录一次，系统就发出一封**链接指向攻击者域名**
    的验证邮件 → 令牌落到对方手里 → 对方换到管理员访问令牌。

    改后：生产环境没配站点地址 ⇒ **不发那封信**，登录回 503（配置问题，
    不是程序坏了）。
    """
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "admin123"},
                    headers={"Host": "evil.example"})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "ADMIN_MFA_LINK_BASE" in detail, detail
    assert "Host" in detail, detail          # 说清「为什么不退回 Host 头」
    assert "access_token" not in r.text

    time.sleep(0.2)
    assert sent_mails == [], "没配站点地址却发出了验证邮件"


def test_生产环境配了站点地址时链接指向配置值而不是请求的Host(
        client, admin_user, mfa_ready, sent_mails, prod_env, monkeypatch):
    """配了就不许再看 Host 头 —— 邮件里的链接必须落在配置的那一个域名上。"""
    monkeypatch.setattr(settings, "admin_mfa_link_base", "https://acct.example.com")
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "admin123"},
                    headers={"Host": "evil.example"})
    assert r.status_code == 200, r.text
    mail = _wait_for_mail(sent_mails)
    assert "https://acct.example.com/api/auth/mfa/verify?token=" in mail["body"], \
        mail["body"]
    assert "evil.example" not in mail["body"], \
        f"验证链接指向了请求的 Host 头：{mail['body']}"


def test_生产环境没配站点地址时link_base直接抛异常(prod_env, no_link_base):
    """函数层的口径：生产 ⇒ 抛异常，而不是「打个警告继续用 Host 头」。"""
    class _Req:
        base_url = "http://evil.example/"

    with pytest.raises(mfa_service.MfaLinkBaseNotConfigured):
        mfa_service.link_base(_Req())


def test_非生产环境仍然退回请求的base_url(monkeypatch):
    """**开发环境的可用性不能被这条修复打死。**

    本地跑不必先配一遍域名（那里没有「别人点你的链接」这件事）。
    """
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "admin_mfa_link_base", "")

    class _Req:
        base_url = "http://127.0.0.1:8000/"

    assert mfa_service.link_base(_Req()) == "http://127.0.0.1:8000"


# ============================================================================
# M-11：JWT 载荷的 iat / jti / iss
# ============================================================================


def test_签发的令牌带iat_jti_iss():
    token, _ = create_access_token(1, "someone", is_admin=False)
    payload = jwt.decode(token, settings.jwt_secret,
                          algorithms=[settings.jwt_algorithm],
                          issuer=settings.jwt_issuer)
    assert payload["iat"] > 0, payload
    assert payload["jti"], payload
    assert payload["iss"] == settings.jwt_issuer, payload
    # 老字段一个都没少
    assert payload["sub"] == "1"
    assert "tv" in payload


def test_两枚令牌的jti不同():
    """``jti`` 是「只吊销这一枚」的粒度 —— 两枚不能撞。"""
    a, _ = create_access_token(1, "someone")
    b, _ = create_access_token(1, "someone")
    pa = jwt.decode(a, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
                    issuer=settings.jwt_issuer)
    pb = jwt.decode(b, settings.jwt_secret, algorithms=[settings.jwt_algorithm],
                    issuer=settings.jwt_issuer)
    assert pa["jti"] != pb["jti"]


def test_iss不匹配的令牌被拒():
    """跨系统复用同一把 ``JWT_SECRET`` 时，别处签的令牌不许在本服务生效。"""
    bad = jwt.encode({"sub": "1", "iss": "another-service"},
                     settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(Exception):
        decode_access_token(bad)


def test_没有iss的存量令牌被拒():
    """**向后兼容的策略是「拒绝」。**

    存量令牌最长 12 小时（管理员 4 小时），而改密/停用会立刻作废它们
    （``token_version``）；拒绝的代价是一次可预期的重新登录，放行的代价
    是「别的服务签的令牌在本服务照样能用」—— 后者贵得多。
    """
    old = jwt.encode({"sub": "1", "username": "someone"},
                     settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(Exception):
        decode_access_token(old)


def test_别处签发的令牌访问接口是401(client, admin_user):
    bad = jwt.encode({"sub": str(admin_user.id), "iss": "another-service",
                       "exp": int(time.time()) + 600},
                      settings.jwt_secret, algorithm=settings.jwt_algorithm)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401, r.text


# ============================================================================
# M-12：缺 sub 的令牌 ⇒ 401（不是 500）
# ============================================================================


def test_缺sub的令牌是401不是500(client):
    """签名合法、``iss`` 也对，但**没有 ``sub``** —— 解码成功，下一步才炸。

    改前那一步是 ``int(payload["sub"])`` ⇒ KeyError ⇒ 500 + 堆栈进日志。
    """
    token = jwt.encode({"iss": settings.jwt_issuer, "username": "someone",
                         "exp": int(time.time()) + 600},
                        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


def test_sub不是数字的令牌也是401(client):
    token = jwt.encode({"iss": settings.jwt_issuer, "sub": "not-a-number",
                         "exp": int(time.time()) + 600},
                        settings.jwt_secret, algorithm=settings.jwt_algorithm)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


# ============================================================================
# P1-2：PG 原文不回给客户端
# ============================================================================


def test_改号接口不再把PG原文回给客户端(client, auth, book, monkeypatch):
    """**改前**这条路由自己捕获 ``DBAPIError``，把 ``diag.message_primary``
    原样塞进 detail —— 客户端因此读得到表名、约束名与重复键的值。

    **改后**异常上抛给全局处理器：约束类是 4xx，但文案是**脱敏的**。
    """
    from app.accounting import organize as svc_organize

    def boom(*_a, **_kw):
        raise IntegrityError(
            "UPDATE voucher SET voucher_no = %s",
            {"voucher_no": "记0001"},
            _FakePgError(
                'duplicate key value violates unique constraint '
                '"uq_voucher_period_no" DETAIL: Key (period_id, voucher_no)='
                '(1, 记0001) already exists.'))

    monkeypatch.setattr(svc_organize, "apply_organize", boom)

    r = client.post(f"/api/books/{book.id}/vouchers/organize",
                    json={"strategy": "RENUM"}, headers=auth)
    assert r.status_code == 400, r.text
    assert "uq_voucher_period_no" not in r.text, r.text
    assert "duplicate key" not in r.text.lower(), r.text
    assert "记0001" not in r.text, r.text
    assert r.headers.get("X-Request-ID"), "脱敏的响应必须带请求号（排查靠它）"


def test_全局处理器不回唯一约束的原文():
    """**连带修的那一处**：23505 的文案被加了中文前缀之后，
    「有没有汉字」这个判据就永远为真 —— 于是 PG 自带的英文原文
    （表名、约束名、**重复键的值**）被当成「我们自己写的中文护栏」回了出去。"""
    from app.main import dbapi_error_handler

    exc = IntegrityError(
        "UPDATE voucher SET voucher_no = %s", {"voucher_no": "记0001"},
        _FakePgError('duplicate key value violates unique constraint '
                     '"uq_voucher_period_no" DETAIL: Key (period_id, voucher_no)='
                     '(1, 记0001) already exists.'))
    request = SimpleNamespace(state=SimpleNamespace(request_id="rid-test"),
                              method="POST", url=SimpleNamespace(path="/x"))
    resp = asyncio.run(dbapi_error_handler(request, exc))
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert "uq_voucher_period_no" not in body["detail"], body
    assert "记0001" not in body["detail"], body
    assert resp.headers.get("x-request-id") == "rid-test"


def test_我们自己写的中文护栏文案仍然回给客户端():
    """**不是把话都堵死**：我们自己写的护栏（``db_init.sql`` 里的中文
    ``RAISE EXCEPTION``）本来就是给人看的提示，必须照旧回给用户。"""
    from app.main import dbapi_error_handler

    exc = IntegrityError(
        "UPDATE voucher", {},
        _FakePgError("已记账的凭证不允许删除", sqlstate="23514"))
    request = SimpleNamespace(state=SimpleNamespace(request_id="rid-test"),
                              method="POST", url=SimpleNamespace(path="/x"))
    resp = asyncio.run(dbapi_error_handler(request, exc))
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert "已记账的凭证不允许删除" in body["detail"], body


# ============================================================================
# L-02：点链接接口的失败退避
# ============================================================================


def test_点链接猜错到阈值会退避(client):
    """这个端点**必然是未认证可达的**（邮件网关会预取它）。

    令牌是 256 位随机，猜不中 —— **不是认证绕过**；但它每次都要查一次库，
    于是「谁都能打」等于「谁都能驱动数据库查询」。所以要有退避。

    **桶的键变了**（审查 §3.3）：原来按**来源 IP** 记，于是「nginx 后面
    且 ``TRUSTED_PROXIES`` 没配」的部署里**全站共用一个桶** —— 攻击者点
    10 次垃圾链接就能把管理员点**真**链接也挡住（那是「管理员再也登不进
    系统」）。现在按**这串 token 自己的摘要**记：攻击者算不出受害者那串
    的摘要，所以动不到受害者的桶。

    所以这条用例的形态也变了：**同一串**错 token 反复点才会退避
    （原来是**一串不同**的错 token 攒够次数就会退避）。
    """
    limit = mfa_service.VERIFY_FAIL_LIMIT
    for _ in range(limit):
        r = client.get("/api/auth/mfa/verify", params={"token": "bogus-token"})
        assert r.status_code == 200, r.text

    r = client.get("/api/auth/mfa/verify", params={"token": "bogus-token"})
    assert r.status_code == 429, r.text
    assert r.headers.get("Retry-After"), r.text


def test_点对了就把失败计数清零(client, admin_user, mfa_ready, sent_mails):
    """正常使用不该背着旧账：点对一次，之前的失败计数归零。"""
    for i in range(mfa_service.VERIFY_FAIL_LIMIT - 1):
        client.get("/api/auth/mfa/verify", params={"token": f"bogus-{i}"})

    client.post("/api/auth/login",
                json={"username": admin_user.username, "password": "admin123"})
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])

    ok = client.get("/api/auth/mfa/verify", params={"token": token})
    assert ok.status_code == 200, ok.text

    # 计数已清零 ⇒ 再猜错仍然是「照常回页面」，不是 429
    r = client.get("/api/auth/mfa/verify", params={"token": "bogus-after-success"})
    assert r.status_code == 200, r.text


def test_已用过和已过期的链接不计失败(client, admin_user, mfa_ready, sent_mails):
    """「点错链接的真人」不该被当成「在猜」——那会误伤正常使用。"""
    client.post("/api/auth/login",
                json={"username": admin_user.username, "password": "admin123"})
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])

    first = client.get("/api/auth/mfa/verify", params={"token": token})
    assert first.status_code == 200
    second = client.get("/api/auth/mfa/verify", params={"token": token})
    assert second.status_code == 200, second.text
    assert "无效" in second.text or "用过" in second.text or "过期" in second.text


# ============================================================================
# L-04：死代码 source_bucket_key 已删除
# ============================================================================


def test_source_bucket_key已删除():
    """它从来没被调用过，而且它生成的键（最长 48 字符）与目标列
    ``login_ip_throttle.ip`` 的 ``String(45)`` 对不上 —— 接上去会静默截断。"""
    assert not hasattr(lockout, "source_bucket_key")
    assert not hasattr(lockout, "BUCKET_PREFIX_IP")
    assert not hasattr(lockout, "BUCKET_PREFIX_PEER")
    # 分桶的**可观测口径**还在（那是给人看的诊断形态）
    assert "mode" in lockout.source_bucketing()


# ============================================================================
# L-05：开发环境 CORS 不带凭据
# ============================================================================


def test_开发环境CORS不带凭据():
    """本项目全仓没有 Cookie 依赖（令牌走 Authorization 头 + localStorage），
    所以去掉 ``allow_credentials`` 对前端没有任何影响。"""
    from starlette.middleware.cors import CORSMiddleware

    from app.main import app

    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert cors, "开发/测试环境应当装 CORS 中间件"
    for m in cors:
        assert m.kwargs.get("allow_credentials") is False, m.kwargs


# ============================================================================
# L-07：审计可见性的注释说的是事实
# ============================================================================


def test_审计掩码的注释说的是事实():
    """两个审计读取接口都要求管理员 —— 注释不该再写「给所有人看的」。"""
    doc = system_settings.mask_secret.__doc__ or ""
    assert "给所有人看的" not in doc, doc
    assert "管理员" in doc, doc
    # 掩码口径本身不许动
    assert system_settings.mask_secret("supersecretvalue") == "…alue"


# ============================================================================
# L-09：PBKDF2 轮数升级（验证用老轮数、登录后重算）
# ============================================================================


def test_老散列照样验证通过且登录后被升级(client, db, admin_user):
    """**平滑升级**：老散列按库里记着的轮数算，照样登得进去；
    登录成功之后自动用当前轮数重算并写回。"""
    old = _hash_with_iterations("admin123", 240_000)
    assert verify_password("admin123", old), "老散列必须仍然验证通过"
    assert password_needs_rehash(old) is True

    admin_user.password_hash = old
    db.commit()

    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "admin123"})
    assert r.status_code == 200, r.text

    db.expire_all()
    user = db.get(SysUser, admin_user.id)
    assert user.password_hash != old, "登录之后散列没有被升级"
    assert user.password_hash.startswith("pbkdf2_sha256$600000$"), \
        user.password_hash
    assert verify_password("admin123", user.password_hash)


def test_当前轮数的散列不需要重算():
    current = hash_password("whatever")
    assert password_needs_rehash(current) is False


def test_散列串不合法时验证失败而不是崩掉():
    for broken in ("", "not-a-hash", "pbkdf2_sha256$abc$x$y",
                   "pbkdf2_sha256$99999999999$x$y", "md5$1$x$y"):
        assert verify_password("x", broken) is False
        assert password_needs_rehash(broken) is False


# ============================================================================
# P0-1（网关层）：443 端口也要有 Host 白名单
# ============================================================================
#
# 后端那条防线（不退回 Host 头）**不能是唯一一道**：``$host`` 会被 nginx
# 一路传下去（``proxy_set_header Host $host;``），所以网关层自己也要按
# 同一份白名单把未知 Host 掐掉。
#
# 原来只有 80 端口查表、443 完全不看 Host —— 那个不对称正是这条用例要钉的。

import re  # noqa: E402
from pathlib import Path  # noqa: E402

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "nginx"
NGINX_CONF = DEPLOY / "cwq.conf"
HOST_MAP = DEPLOY / "cwq-host-map.conf"


def _server_block(text: str, listen: str) -> str:
    """取 ``listen <listen>;`` 那个 server 块的正文（**按花括号配对**）。

    不能用正则 ``server\\s*\\{.*?\\n\\}``：块里有嵌套的 ``location`` 与
    ``if``，非贪婪匹配会在第一个 ``\\n}`` 处停下，拿到半截。
    """
    for m in re.finditer(r"server\s*\{", text):
        start = m.end() - 1
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    body = text[start:i + 1]
                    if re.search(rf"listen\s+{re.escape(listen)}\b", body):
                        # 去掉注释行：注释里往往把旧的错误写法完整抄了一遍，
                        # 不剥掉的话断言会命中注释而非真实指令。
                        return "\n".join(
                            line for line in body.splitlines()
                            if not line.lstrip().startswith("#")
                        )
                    break
    raise AssertionError(f"配置里没有 listen {listen} 的 server 块")


def test_443端口也按白名单拒绝未知Host():
    """**80 与 443 必须是同一份白名单** —— 两张表迟早漂移，而漂移的后果
    就是「80 跳到某处、443 却不认」，那正是这次要消灭的不对称。"""
    body = _server_block(NGINX_CONF.read_text(encoding="utf-8"), "443 ssl")
    assert "$cwq_https_redirect" in body, \
        "443 端口不看 Host 白名单 —— $host 会一路传到后端（验证链接按 base_url 拼）"
    assert "444" in body, "443 端口没有拒绝未知 Host"


def test_白名单文件存在且默认拒绝():
    assert HOST_MAP.exists(), f"缺少 {HOST_MAP.name} —— nginx 会因「未知变量」启动失败"
    text = HOST_MAP.read_text(encoding="utf-8")
    assert re.search(r"^\s*map\s+\$host\s+\$cwq_https_redirect\s*\{", text, re.M)
    assert re.search(r'default\s+""\s*;', text), \
        'map 的 default 必须留空 —— 它就是「未知 Host 不接待」这条规则的实现'


# ============================================================================
# L-10：汇报口径 = 实际行为
# ============================================================================


def test_保护面板汇报的阶梯与实际行为一致():
    """**只改汇报口径，不改行为。**

    改前面板报的是 ``[5,10,20,40,80]`` 秒 —— 而 20/40/80 那些档位在运行时
    **永远走不到**（第 3 轮起就是硬锁）。现在面板按**逐轮的实际形态**汇报。
    """
    summary = lockout.protection_summary()["account"]
    rounds = summary["rounds"]

    assert rounds[0]["mode"] == "delay"
    assert rounds[0]["delay_seconds"] == lockout.account_delay_seconds(1) == 5
    assert rounds[1]["mode"] == "delay"
    assert rounds[1]["delay_seconds"] == lockout.account_delay_seconds(2) == 10
    assert rounds[2]["mode"] == "hard_lock"
    assert rounds[2]["lock_minutes"] == lockout.lock_duration_minutes(3) == 30

    # 秒级延迟阶梯只列**真正会走到的**档位
    assert summary["delay_ladder_seconds"] == [5, 10]
    assert summary["hard_lock_after_rounds"] == lockout.ACCOUNT_HARD_LOCK_AFTER_ROUNDS

    # 每一轮都与代码里的判定一致（不允许面板与实现漂移）
    for row in rounds:
        level = row["round"]
        if lockout.should_hard_lock(level):
            assert row["mode"] == "hard_lock", row
            assert row["lock_minutes"] == lockout.lock_duration_minutes(level), row
            assert row["delay_seconds"] is None, row
        else:
            assert row["mode"] == "delay", row
            assert row["delay_seconds"] == lockout.account_delay_seconds(level), row
            assert row["lock_minutes"] is None, row


def test_登录锁定的强度没有被这次整改动过():
    """**只改汇报口径** —— 常量与判定一个都不许动。"""
    assert lockout.ACCOUNT_HARD_LOCK_AFTER_ROUNDS == 3
    assert lockout.ACCOUNT_DELAY_BASE_SECONDS == 5
    assert lockout.ACCOUNT_DELAY_MAX_SECONDS == 300
    assert lockout.should_hard_lock(2) is False
    assert lockout.should_hard_lock(3) is True
    assert lockout.account_delay_seconds(3) == 20     # 公式没变，只是走不到
    assert lockout.lock_duration_minutes(3) == 30


# ---------------------------------------------------------------------------
# MCP 侧的同一处漏洞（审查补漏）
#
# W3 修掉了 ``main.py`` 里"有没有汉字"这个判据的漏洞之后，**MCP 层复制了
# 同一套判据** —— 于是同一个漏洞在 MCP 侧原样存在：
#
#   PG 的违例原文里会夹带中文**数据**（重复键的值可能是凭证号「记0001」），
#   于是"含中文 ⇒ 是我们自己写的中文护栏"这个推断会被骗过 ——
#   带中文数据的 23505 原文（表名、约束名、**重复键的值**）会被当成
#   自家提示**原样回给 agent**。
#
# 父应用的 ``dbapi_error_handler`` 覆盖不到 MCP（独立的一层 ASGI 栈），
# 所以 MCP 必须自己脱敏 —— 也就必须自己判对。
# ---------------------------------------------------------------------------


def test_MCP不会把带中文数据的PG原文回给agent():
    """PG 的违例原文即使夹带中文数据，也不许当成自家护栏原样回出去。"""
    from app.mcp.errors import _is_own_guardrail_text

    # PG 的 23505 原文，**夹带中文数据**（重复键的值是凭证号）
    pg_with_chinese = (
        'duplicate key value violates unique constraint "uq_voucher_period_no"\n'
        "DETAIL:  Key (voucher_no, period_id)=(记0001, 2026-09) already exists."
    )
    assert _is_own_guardrail_text(pg_with_chinese) is False, (
        "带中文数据的 PG 原文被当成了自家护栏 —— 表名/约束名/重复键的值会回给 agent")

    # 纯英文的 PG 原文
    assert _is_own_guardrail_text(
        'duplicate key value violates unique constraint "uq_voucher_period_no"'
    ) is False

    # 我们自己写的中文护栏（db_init.sql 里的 RAISE EXCEPTION）
    assert _is_own_guardrail_text("已记账的凭证不允许删除") is True


def test_MCP与父应用的判据保持一致():
    """两处必须同口径 —— 不然父应用堵住的漏洞在 MCP 侧又开了。"""
    from app.main import _is_own_guardrail_text as main_judge
    from app.mcp.errors import _is_own_guardrail_text as mcp_judge

    samples = [
        "已记账的凭证不允许删除",
        'duplicate key value violates unique constraint "uq_voucher_period_no"',
        "duplicate key value violates unique constraint \"uq_voucher_period_no\"\n"
        "DETAIL:  Key (voucher_no, period_id)=(记0001, 2026-09) already exists.",
        'violates foreign key constraint "fk_entry_subject"',
        "null value in column \"summary\" violates not-null constraint",
    ]
    for text in samples:
        assert main_judge(text) == mcp_judge(text), f"两处判据不一致：{text!r}"
