"""MFA 的两个限流桶必须**按攻击者伪造不出来的量**分桶（审查 §3.3 / §3.4）。

**§3.3 —— 点链接接口的共享 IP 桶 → 管理员全域锁定 DoS。**

``GET /api/auth/mfa/verify`` 是**未认证**端点（邮件网关会预取它）。原来
失败次数按**来源 IP** 记，而在「nginx 后面且 ``TRUSTED_PROXIES`` 没配」的
部署里所有请求的对端都是 ``127.0.0.1`` —— **全站共用一个桶**：

    攻击者点 10 次垃圾链接  →  桶被封锁 60 秒
    → 管理员点**真**链接      →  429「请 60 秒后再试」

而 ``verify`` 是管理员完成二次验证的**唯一**路径，所以这不是「这次登录被
拒」，是**管理员再也登不进系统**，而触发它**不需要任何凭据**。

**§3.4 —— 轮询限流以客户端可控的 ``challenge_id`` 为键。**

``poll_guard`` 在**校验 ``client_secret`` 之前**被调用，而预算按
``challenge_id`` 全局共享 —— 于是攻击者拿**别人的** id 配一个错的 secret
请求，被 401 拒掉（对），但**受害者的轮询额度已经被扣掉了**；合法客户端
接着就收到 429，累计 300 次之后那个挑战**永远**没法被任何人消费。

两条的修法是同一个：**把键换成攻击者算不出来的那一串**（``email_token``
的 sha256 摘要 / ``client_secret`` 的 sha256 摘要）。
"""
from __future__ import annotations

import re
import time

import pytest

from app.core import system_settings
from app.models import AdminMfaChallenge
from app.notify import mailer
from app.notify import mfa as mfa_service


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    """两个限流桶都是**进程级**状态 —— 用例之间必须清干净。

    不然「这条用例猜了 5 次」会把后面所有用例一起挡住，症状是「单跑绿、
    全量跑红」，而且时好时坏（取决于谁先跑到哪里）。
    """
    mfa_service.reset_verify_state()
    mfa_service._poll_state.clear()
    yield
    mfa_service.reset_verify_state()
    mfa_service._poll_state.clear()


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
    """二次验证**打开** + SMTP 通道**配好**（否则登录不会产生挑战）。"""
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    system_settings.set_value(db, "smtp_host", "smtp.invalid")
    system_settings.set_value(db, "smtp_from", "noreply@example.com")
    system_settings.set_value(db, "notify_recipients", "ops@example.com")
    db.commit()


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


def _start_challenge(client, admin_user, sent_mails) -> tuple[dict, str]:
    """走一遍登录 → 拿到挑战与邮件里的真链接。"""
    body = client.post("/api/auth/login",
                       json={"username": admin_user.username,
                             "password": "admin123"}).json()
    assert body.get("mfa_required") is True, body
    mail = _wait_for_mail(sent_mails)
    return body, _token_from_body(mail["body"])


# ==================================================================== §3.3


def test_攻击者的垃圾点击挡不住管理员的真链接(client, admin_user, db,
                                              sent_mails, mfa_ready):
    """**这一条是 §3.3 的核心断言。**

    攻击者与管理员在同一个部署里**共用同一个来源地址**（测试客户端的
    对端永远是同一个），所以改前这条必然红：攻击者的 10 次垃圾点击会把
    那个共享桶封锁，管理员的真链接拿到 429。
    """
    body, token = _start_challenge(client, admin_user, sent_mails)

    # 攻击者：10 次**各不相同**的垃圾 token（同一个来源）。
    for i in range(10):
        r = client.get("/api/auth/mfa/verify", params={"token": f"garbage-{i}"})
        assert r.status_code == 200, r.text

    # 管理员点**真**链接 —— 必须成功。
    r = client.get("/api/auth/mfa/verify", params={"token": token})
    assert r.status_code == 200, r.text
    assert "无效" not in r.text, r.text
    db.expire_all()
    assert db.get(AdminMfaChallenge, body["challenge_id"]).status == "VERIFIED"


def test_同一串错链接反复点会被退避(client, admin_user, sent_mails, mfa_ready):
    """**限流本身没被拿掉** —— 只是键换了。

    同一串错 token 连错到阈值就退避（不再打库），而**受害者的真链接**
    不受这个桶影响。
    """
    for _ in range(mfa_service.VERIFY_FAIL_LIMIT):
        r = client.get("/api/auth/mfa/verify", params={"token": "same-wrong-token"})
        assert r.status_code == 200, r.text

    r = client.get("/api/auth/mfa/verify", params={"token": "same-wrong-token"})
    assert r.status_code == 429, r.text
    assert r.headers.get("Retry-After"), r.text


def test_退避的键是这串token自己而不是来源(client, admin_user, db,
                                            sent_mails, mfa_ready):
    """**来源维度不再拦人** —— 它只是二级维度。

    这条把「按来源拦」这件事钉死：同一个来源（测试客户端）对**另一串**
    垃圾 token 的请求**照样放行**。改前（按来源）这里会 429。
    """
    for _ in range(mfa_service.VERIFY_FAIL_LIMIT + 2):
        client.get("/api/auth/mfa/verify", params={"token": "same-wrong-token"})

    r = client.get("/api/auth/mfa/verify", params={"token": "another-wrong-token"})
    assert r.status_code == 200, r.text


def test_管理员点对了就清掉这串token的失败计数(client, admin_user, db,
                                                sent_mails, mfa_ready):
    """正常使用不该背着旧账。"""
    _body, token = _start_challenge(client, admin_user, sent_mails)

    for _ in range(mfa_service.VERIFY_FAIL_LIMIT - 1):
        client.get("/api/auth/mfa/verify", params={"token": "same-wrong-token"})

    ok = client.get("/api/auth/mfa/verify", params={"token": token})
    assert ok.status_code == 200, ok.text

    # 计数已清零 ⇒ 同一串错 token 再点仍然是「照常回页面」，不是 429。
    r = client.get("/api/auth/mfa/verify", params={"token": "same-wrong-token"})
    assert r.status_code == 200, r.text


# ==================================================================== §3.4


def test_攻击者用错secret的轮询不吃掉受害者的额度(client, admin_user,
                                                  sent_mails, mfa_ready):
    """**这一条是 §3.4 的核心断言。**

    攻击者拿着受害者的 ``challenge_id`` 配一个错的 ``client_secret``：
    他拿到 401（对），但**受害者的轮询额度不能被扣掉**。
    改前这条必然红 —— 合法客户端接着就收到 429「请 2 秒后再试」。
    """
    body, _token = _start_challenge(client, admin_user, sent_mails)

    r = client.post("/api/auth/mfa/poll",
                    json={"challenge_id": body["challenge_id"],
                          "client_secret": "wrong-secret"})
    assert r.status_code in (401, 403), r.text

    # 合法客户端：必须照常拿到「待验证」。
    r = client.post("/api/auth/mfa/poll",
                    json={"challenge_id": body["challenge_id"],
                          "client_secret": body["client_secret"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING", r.text


def test_合法客户端自己的最短间隔仍然生效(client, admin_user, sent_mails,
                                        mfa_ready):
    """**轮询限流本身没被拿掉** —— 同一个 secret 连着问还是会被挡。"""
    body, _token = _start_challenge(client, admin_user, sent_mails)
    payload = {"challenge_id": body["challenge_id"],
               "client_secret": body["client_secret"]}

    first = client.post("/api/auth/mfa/poll", json=payload)
    assert first.status_code == 200, first.text

    second = client.post("/api/auth/mfa/poll", json=payload)
    assert second.status_code == 429, second.text
    assert "2 秒" in second.text, second.text


def test_轮询预算按挑战加secret一起分桶(client, admin_user, sent_mails,
                                      mfa_ready):
    """**键里含 secret 的摘要** —— 不同的 secret 是不同的桶。

    直接验函数层：同一个 ``challenge_id``、两个不同的 ``client_secret``，
    第二个**不该**被第一个的额度挡住。
    """
    assert mfa_service.poll_guard(1, "secret-a")[0] is True
    # 同一个 secret 连着问 → 被最短间隔挡住。
    assert mfa_service.poll_guard(1, "secret-a")[0] is False
    # 另一个 secret → 另一个桶，放行。
    assert mfa_service.poll_guard(1, "secret-b")[0] is True


def test_清轮询状态会把该挑战的各个桶都清掉(client, admin_user, sent_mails,
                                          mfa_ready):
    """``reset_poll_state`` 现在按前缀清 —— 别把「用错 secret 试过」的桶留下。"""
    assert mfa_service.poll_guard(7, "secret-a")[0] is True
    assert mfa_service.poll_guard(7, "secret-b")[0] is True
    mfa_service.reset_poll_state(7)
    assert mfa_service.poll_guard(7, "secret-a")[0] is True
    assert mfa_service.poll_guard(7, "secret-b")[0] is True
