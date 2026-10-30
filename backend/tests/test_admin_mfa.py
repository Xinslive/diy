"""管理员二次验证（邮箱链接）—— 审查报告 P0-1 的整改测试。

**这个文件钉的是什么。** 管理员口令一旦泄露（钓鱼、撞库、旧备份里的
散列被离线爆破），持码者立刻获得「开号 / 改口令 / 装插件 / 读 OSS 凭据 /
轮换调度密钥」的全套权限 —— 而这些权限恰好是普通会计永远拿不到的。
二次验证把「拿到口令」与「拿到会话」拆成两件事。

**形态是邮箱链接**，复用项目已有的 SMTP 通道。整套设计里最要紧的一条是
**分工**：邮件链接里的那串东西**绝不能直接当会话令牌**（它会被邮件网关
扫描、被手机锁屏预览、被 IT 留档），所以：

* 点链接 → 只把挑战标成 VERIFIED，**拿不到任何会话**；
* 客户端拿着 ``challenge_id + client_secret`` 轮询 → 换到真正的令牌。

于是「邮件链接被第三方点开」的最坏后果，只是浪费掉一次挑战。

**下面 9 组断言各自对应一条不能破的边界**，尤其第 3 条（点链接不能直接
拿到令牌）与第 5 条（同一个挑战只能换出一个令牌）—— 这两条一旦破了，
整个设计就退化成「点一下邮件就登进去了」。
"""
from __future__ import annotations

import re
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core import system_settings
from app.core.config import settings
from app.core.security import decode_access_token
from app.models import AdminMfaChallenge, AuditLog, SystemSetting
from app.notify import mailer
from app.notify import mfa as mfa_service

#: 这些 key 会被本文件改写。**跨用例存活**（接口里的 commit 会提交，
#: 而夹具的 rollback 只回滚它自己那个会话），所以每个用例前后各清一次 ——
#: 不然「没配 SMTP 时应当报错」这条会在下一个用例里莫名其妙地不成立。
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
def _fast_poll():
    """轮询限流在测试里放开。

    真实限流是「同一个挑战两次轮询至少隔 2 秒」—— 那对测试是纯粹的
    时间浪费（每条用例都要等 2 秒）。**限流本身另有一条用例专门验**，
    那条会把这里的值改回真实值。
    """
    old = mfa_service.POLL_MIN_INTERVAL_SECONDS
    mfa_service.POLL_MIN_INTERVAL_SECONDS = 0.0
    yield
    mfa_service.POLL_MIN_INTERVAL_SECONDS = old


@pytest.fixture
def sent_mails(monkeypatch):
    """把真发信换成一个记录器。

    **只换 ``mailer.send_mail``**，不换整条链路 —— 配置读取、并发上限、
    守护线程都照跑。测试要验的正是「信发到了没有、内容对不对」。
    """
    sent: list[dict] = []

    def fake_send_mail(config, to_addresses, subject, body):
        sent.append({"to": list(to_addresses), "subject": subject, "body": body})
        return len(to_addresses)

    monkeypatch.setattr(mailer, "send_mail", fake_send_mail)
    return sent


@pytest.fixture
def mfa_ready(db):
    """二次验证**打开** + SMTP 通道**配好**。

    只改 ``sys_setting``（界面上的开关与收件人），不动 env ——
    「库里优先、env 兜底」这套约定正是这里要验的。
    """
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    system_settings.set_value(db, "smtp_host", "smtp.invalid")
    system_settings.set_value(db, "smtp_from", "noreply@example.com")
    system_settings.set_value(db, "notify_recipients", "ops@example.com")
    db.commit()


@pytest.fixture
def mfa_enabled_only(db):
    """**只**把开关打开，SMTP 与收件人一个字都不配。"""
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()


# --------------------------------------------------------------------- 助手


def _login(client, user, password="admin123"):
    return client.post("/api/auth/login",
                        json={"username": user.username, "password": password})


def _wait_for_mail(sent, timeout=3.0, count=1) -> dict:
    """等第 ``count`` 封验证邮件发出来（它是**异步**的）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(sent) >= count:
            return sent[count - 1]
        time.sleep(0.02)
    raise AssertionError(f"验证邮件没有发出来（只到了 {len(sent)} 封）")


def _token_from_body(body: str) -> str:
    """从邮件正文里取出验证链接里的那串 token。"""
    match = re.search(r"token=([A-Za-z0-9_\-]+)", body)
    assert match, f"邮件正文里没有验证链接：\n{body}"
    return match.group(1)


def _challenge(db, challenge_id: int) -> AdminMfaChallenge:
    return db.get(AdminMfaChallenge, challenge_id)


def _audit_rows(db, action: str) -> list[AuditLog]:
    return list(db.query(AuditLog).filter(AuditLog.action == action).all())


class _FakeRequest:
    """一个只带 ``state.auth_payload`` 的请求替身。

    依赖 ``require_recent_admin_mfa`` 只读 ``request.state``，所以不必造
    一个真的 Starlette Request。
    """

    def __init__(self, payload: dict | None):
        self.state = SimpleNamespace(auth_payload=payload)


# ============================================================ 1. 登录的分叉


def test_管理员开二次验证后登录只回挑战_不给令牌(client, admin_user, mfa_ready):
    """管理员 + 开关开着 + 通道就绪 → 登录**不发令牌**。"""
    r = _login(client, admin_user)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("mfa_required") is True
    assert "access_token" not in body, "二次验证开着的时候竟然发了访问令牌"
    assert body.get("challenge_id", 0) > 0
    assert body.get("client_secret")
    assert body.get("expires_in", 0) > 0
    # 收件地址必须脱敏
    assert body.get("email_hint") == "o***s@example.com", body.get("email_hint")


def test_非管理员登录照旧直接拿到令牌(client, plain_user, mfa_ready):
    """二次验证**只针对管理员**。普通会计不该被卷进来。"""
    r = client.post("/api/auth/login",
                    json={"username": plain_user.username, "password": "staff123"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("mfa_required") is not True
    assert body["access_token"]
    assert body["user"]["is_admin"] is False


def test_邮件真的发出去了且正文里有验证链接(client, admin_user, mfa_ready,
                                             sent_mails):
    _login(client, admin_user)
    mail = _wait_for_mail(sent_mails)
    assert mail["to"] == ["ops@example.com"]
    assert "token=" in mail["body"]
    # 正文里**不能**出现 client_secret —— 它只发给发起登录的客户端。
    assert "client_secret" not in mail["body"]


# ============================================================ 2. 点邮件链接


def test_点一次链接挑战变已验证_再点一次失败(client, admin_user, mfa_ready,
                                            sent_mails, db):
    body = _login(client, admin_user).json()
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])

    r = client.get("/api/auth/mfa/verify", params={"token": token})
    assert r.status_code == 200
    db.expire_all()
    assert _challenge(db, body["challenge_id"]).status == "VERIFIED"

    # 再点一次必须失败（状态机不许回退）
    r2 = client.get("/api/auth/mfa/verify", params={"token": token})
    assert r2.status_code == 200
    assert "无效" in r2.text or "用过" in r2.text or "过期" in r2.text


def test_过期的链接点不动(client, admin_user, mfa_ready, sent_mails, db):
    """过期链接 → 明确的失败，而不是「其实还能用」。"""
    body = _login(client, admin_user).json()
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])

    # 把过期时间拨到过去
    row = _challenge(db, body["challenge_id"])
    row.expires_at = mfa_service.now_utc().replace(year=2000)
    db.commit()

    r = client.get("/api/auth/mfa/verify", params={"token": token})
    assert r.status_code == 200
    assert "access_token" not in r.text
    db.expire_all()
    assert _challenge(db, body["challenge_id"]).status == "EXPIRED"


# ============================================================ 3. 点链接 ≠ 登录


def test_点链接绝不能直接拿到访问令牌(client, admin_user, mfa_ready,
                                      sent_mails):
    """**这条是整个设计最要紧的一处。**

    邮件链接会被网关扫描、被手机锁屏预览、被 IT 留档。让它直接等于会话
    令牌，等于「谁截获了那封信谁就是管理员」。
    """
    _login(client, admin_user)
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])

    r = client.get("/api/auth/mfa/verify", params={"token": token})
    assert r.status_code == 200
    assert "access_token" not in r.text
    assert "eyJ" not in r.text, "响应里出现了 JWT —— 点链接居然发了令牌"


# ============================================================ 4. poll 的凭据


def test_poll_必须challenge_id与client_secret都对(client, admin_user,
                                                  mfa_ready, sent_mails):
    body = _login(client, admin_user).json()
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])
    client.get("/api/auth/mfa/verify", params={"token": token})

    # secret 错 → 拒绝
    r = client.post("/api/auth/mfa/poll",
                    json={"challenge_id": body["challenge_id"],
                          "client_secret": "wrong-secret"})
    assert r.status_code in (401, 403), r.text
    assert "access_token" not in r.text

    # challenge_id 错 → 拒绝
    r = client.post("/api/auth/mfa/poll",
                    json={"challenge_id": body["challenge_id"] + 99999,
                          "client_secret": body["client_secret"]})
    assert r.status_code in (401, 403), r.text
    assert "access_token" not in r.text

    # 两个都对 → 给令牌
    r = client.post("/api/auth/mfa/poll",
                    json={"challenge_id": body["challenge_id"],
                          "client_secret": body["client_secret"]})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


def test_还没点链接时poll只回待验证(client, admin_user, mfa_ready):
    body = _login(client, admin_user).json()
    r = client.post("/api/auth/mfa/poll",
                    json={"challenge_id": body["challenge_id"],
                          "client_secret": body["client_secret"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "PENDING"
    assert not r.json().get("access_token")


# ============================================================ 5. 只能换一次


def test_同一个挑战只能换出一个令牌(client, admin_user, mfa_ready,
                                    sent_mails):
    body = _login(client, admin_user).json()
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])
    client.get("/api/auth/mfa/verify", params={"token": token})

    payload = {"challenge_id": body["challenge_id"],
               "client_secret": body["client_secret"]}
    first = client.post("/api/auth/mfa/poll", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["access_token"]

    second = client.post("/api/auth/mfa/poll", json=payload)
    assert second.status_code in (401, 403, 409, 410), second.text
    assert "access_token" not in second.text


# ============================================================ 6. 关掉时的回归


def test_开关关闭时登录行为与修复前完全一致(client, admin_user, db):
    """回归：二次验证没开时，登录**一个字都不许变**。"""
    r = _login(client, admin_user)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body
    assert body.get("mfa_required") is not True
    assert body["token_type"] == "bearer"
    # 登录令牌没有到期时间（§9.9）—— 到期时刻是 ``None``，不是 0（0 是谎话）。
    assert body["expires_at"] is None
    assert body["user"]["username"] == admin_user.username
    assert body["user"]["is_admin"] is True
    # 普通用户的令牌载荷里**不能**多出 mfa 字段
    payload = decode_access_token(body["access_token"])
    assert "mfa_at" not in payload
    assert "amr" not in payload
    # 二次验证没开着的时候，载荷里也不该有到期时间
    assert "exp" not in payload


def test_库里有开关时优先于env(client, admin_user, db):
    """库里优先、env 兜底 —— 与自助注册开关同一套约定。"""
    assert settings.admin_mfa_enabled is False
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()
    assert mfa_service.mfa_enabled(db) is True
    mfa_service.set_mfa_enabled(db, False, updated_by="pytest")
    db.commit()
    assert mfa_service.mfa_enabled(db) is False


# ============================================================ 7. 通道没配好


def test_开关开着但没配SMTP时返回明确错误_不是500_也不是放行(
        client, admin_user, mfa_enabled_only):
    """**绝不静默降级成免验证。**

    放行等于把一个安全开关变成一个假开关：界面上写着「已开启二次
    验证」，实际却一个字节都没生效。
    """
    r = _login(client, admin_user)
    assert r.status_code != 500, r.text
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "二次验证通道未配置" in detail
    assert "access_token" not in r.text


def test_开关开着但没有收件人也算通道没配好(client, admin_user, db):
    """SMTP 有主机、但收件人是空的 —— 同样配不好。"""
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    system_settings.set_value(db, "smtp_host", "smtp.invalid")
    system_settings.set_value(db, "notify_recipients", "")
    db.commit()
    r = _login(client, admin_user)
    assert r.status_code == 503, r.text
    assert "二次验证通道未配置" in r.json()["detail"]


# ============================================================ 8. 不泄露细节


def test_错误token与过期挑战都不泄露内部细节(client, admin_user, mfa_ready,
                                            db):
    """错误文案里不能有表名、SQL、堆栈。"""
    forbidden = ("admin_mfa_challenge", "SELECT", "INSERT", "UPDATE",
                 "Traceback", "psycopg", "sqlalchemy", "sys_user")

    r = client.get("/api/auth/mfa/verify", params={"token": "totally-bogus"})
    assert r.status_code == 200
    for word in forbidden:
        assert word not in r.text, f"响应里泄露了内部细节：{word}"

    r = client.post("/api/auth/mfa/poll",
                    json={"challenge_id": 424242, "client_secret": "nope"})
    assert r.status_code in (401, 403, 410)
    text = r.text
    for word in forbidden:
        assert word not in text, f"响应里泄露了内部细节：{word}"


# ============================================================ 9. 应急码


def test_应急码能过一次_用过即废(client, admin_user, mfa_ready, db):
    """应急码是「邮箱通道坏了」时唯一的出路，所以它必须能用 —— 但**用一次即废**。"""
    old = settings.admin_mfa_emergency_code
    settings.admin_mfa_emergency_code = "EMERGENCY-CODE-1"
    mfa_service.reset_emergency_code()
    try:
        body = _login(client, admin_user).json()
        r = client.post("/api/auth/mfa/poll",
                        json={"challenge_id": body["challenge_id"],
                              "client_secret": body["client_secret"],
                              "emergency_code": "EMERGENCY-CODE-1"})
        assert r.status_code == 200, r.text
        assert r.json()["access_token"]

        # 再开一次挑战，用**同一串**应急码 —— 必须不再管用
        body2 = _login(client, admin_user).json()
        r2 = client.post("/api/auth/mfa/poll",
                         json={"challenge_id": body2["challenge_id"],
                               "client_secret": body2["client_secret"],
                               "emergency_code": "EMERGENCY-CODE-1"})
        assert "access_token" not in r2.text, "应急码用了还能再用一次"
        assert r2.json()["status"] == "PENDING"
    finally:
        settings.admin_mfa_emergency_code = old
        mfa_service.reset_emergency_code()


def test_应急码错误时不放行(client, admin_user, mfa_ready):
    old = settings.admin_mfa_emergency_code
    settings.admin_mfa_emergency_code = "EMERGENCY-CODE-1"
    mfa_service.reset_emergency_code()
    try:
        body = _login(client, admin_user).json()
        r = client.post("/api/auth/mfa/poll",
                        json={"challenge_id": body["challenge_id"],
                              "client_secret": body["client_secret"],
                              "emergency_code": "WRONG"})
        assert "access_token" not in r.text
        assert r.json()["status"] == "PENDING"
    finally:
        settings.admin_mfa_emergency_code = old
        mfa_service.reset_emergency_code()


# ============================================================ 重新发送


def test_重新发送受最短间隔约束(client, admin_user, mfa_ready, sent_mails):
    body = _login(client, admin_user).json()
    _wait_for_mail(sent_mails)

    r = client.post("/api/auth/mfa/resend",
                    json={"challenge_id": body["challenge_id"],
                          "client_secret": body["client_secret"]})
    assert r.status_code == 429, r.text
    assert r.headers.get("Retry-After")
    assert "过于频繁" in r.json()["detail"]


def test_重新发送会换新挑战并让旧链接作废(client, admin_user, mfa_ready,
                                         sent_mails, db):
    body = _login(client, admin_user).json()
    mail = _wait_for_mail(sent_mails)
    old_token = _token_from_body(mail["body"])

    # 把「上次发出」的时刻拨到足够远之前 —— 这样不用真的等 60 秒
    row = _challenge(db, body["challenge_id"])
    row.last_sent_at = mfa_service.now_utc() - timedelta(seconds=300)
    db.commit()

    r = client.post("/api/auth/mfa/resend",
                    json={"challenge_id": body["challenge_id"],
                          "client_secret": body["client_secret"]})
    assert r.status_code == 200, r.text
    new_body = r.json()
    assert new_body["challenge_id"] != body["challenge_id"]
    assert new_body["client_secret"] != body["client_secret"]

    mail2 = _wait_for_mail(sent_mails, timeout=3.0, count=2)
    new_token = _token_from_body(mail2["body"])
    assert new_token != old_token

    # 旧链接当场作废
    old = client.get("/api/auth/mfa/verify", params={"token": old_token})
    assert "access_token" not in old.text


# ============================================================ 轮询限流


def test_轮询太快会被限流(client, admin_user, mfa_ready):
    """轮询接口不能变成放大器。"""
    body = _login(client, admin_user).json()
    old = mfa_service.POLL_MIN_INTERVAL_SECONDS
    mfa_service.POLL_MIN_INTERVAL_SECONDS = 2.0
    try:
        payload = {"challenge_id": body["challenge_id"],
                   "client_secret": body["client_secret"]}
        first = client.post("/api/auth/mfa/poll", json=payload)
        assert first.status_code == 200, first.text
        second = client.post("/api/auth/mfa/poll", json=payload)
        assert second.status_code == 429, second.text
        assert second.headers.get("Retry-After")
    finally:
        mfa_service.POLL_MIN_INTERVAL_SECONDS = old


# ============================================================ 近期重新认证


def test_高危动作要求近期通过邮箱二次验证(client, admin_user, mfa_ready,
                                         sent_mails, db):
    """令牌里没有 ``mfa_at`` → 一律要求重新验证（fail-closed）。"""
    from app.core.deps import require_recent_admin_mfa

    body = _login(client, admin_user).json()
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])
    client.get("/api/auth/mfa/verify", params={"token": token})
    polled = client.post("/api/auth/mfa/poll",
                         json={"challenge_id": body["challenge_id"],
                               "client_secret": body["client_secret"]}).json()
    access = polled["access_token"]

    payload = decode_access_token(access)
    assert payload.get("amr") == "pwd+mfa"
    assert payload.get("mfa_at")

    # 通过了二次验证的令牌 → 依赖放行
    ok = require_recent_admin_mfa(user=admin_user, db=db,
                                  request=_FakeRequest(payload))
    assert ok is admin_user

    # 没通过二次验证的令牌 → 403，且文案要说清去哪儿做
    with pytest.raises(Exception) as exc:
        require_recent_admin_mfa(user=admin_user, db=db,
                                 request=_FakeRequest({}))
    # detail 是**结构化**的（{code, message}）：前端要靠 code 判断
    # 「这个 403 该给『去重新验证』的引导」，靠文案匹配是拿协议去猜协议
    # （见 core/deps.py::MFA_REAUTH_REQUIRED 的说明）。
    detail = getattr(exc.value, "detail", "")
    assert isinstance(detail, dict), detail
    assert detail["code"] == "MFA_REAUTH_REQUIRED", detail
    assert "重新验证" in detail["message"]
    # **别把实现细节漏给用户**：令牌里那个字段名对用户没有意义。
    assert "mfa_at" not in detail["message"]


def test_二次验证过期后高危动作被拒(client, admin_user, mfa_ready,
                                  sent_mails, db):
    old_grace = settings.admin_mfa_grace_minutes
    settings.admin_mfa_grace_minutes = 0
    try:
        body = _login(client, admin_user).json()
        mail = _wait_for_mail(sent_mails)
        token = _token_from_body(mail["body"])
        client.get("/api/auth/mfa/verify", params={"token": token})
        polled = client.post("/api/auth/mfa/poll",
                             json={"challenge_id": body["challenge_id"],
                                   "client_secret": body["client_secret"]}).json()
        payload = decode_access_token(polled["access_token"])
        from app.core.deps import require_recent_admin_mfa
        with pytest.raises(Exception) as exc:
            require_recent_admin_mfa(user=admin_user, db=db,
                                     request=_FakeRequest(payload))
        detail = getattr(exc.value, "detail", "")
        assert isinstance(detail, dict), detail
        assert detail["code"] == "MFA_REAUTH_REQUIRED", detail
        assert "重新验证" in detail["message"]
    finally:
        settings.admin_mfa_grace_minutes = old_grace


def test_开关关闭时高危动作依赖不拦人(client, admin_user, db):
    """二次验证是**可选的组织决策**。没开它就不该把管理员全锁在门外。"""
    from app.core.deps import require_recent_admin_mfa

    r = _login(client, admin_user)
    payload = decode_access_token(r.json()["access_token"])
    ok = require_recent_admin_mfa(user=admin_user, db=db,
                                  request=_FakeRequest(payload))
    assert ok is admin_user


def test_重新验证接口用当前登录用户建挑战(client, admin_user, mfa_ready,
                                        sent_mails, db):
    """挑战的 user_id 必须来自当前登录用户，**不能由请求方指定**。"""
    body = _login(client, admin_user).json()
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])
    client.get("/api/auth/mfa/verify", params={"token": token})
    polled = client.post("/api/auth/mfa/poll",
                         json={"challenge_id": body["challenge_id"],
                               "client_secret": body["client_secret"]}).json()
    headers = {"Authorization": f"Bearer {polled['access_token']}"}

    r = client.post("/api/auth/mfa/reauth", headers=headers, json={})
    assert r.status_code == 200, r.text
    challenge_id = r.json()["challenge_id"]
    row = _challenge(db, challenge_id)
    assert row.user_id == admin_user.id
    assert row.reason == "reauth"


# ============================================================ 审计


def test_二次验证的每一步都留审计且不含密钥(client, admin_user, mfa_ready,
                                          sent_mails, db):
    body = _login(client, admin_user).json()
    mail = _wait_for_mail(sent_mails)
    token = _token_from_body(mail["body"])
    client.get("/api/auth/mfa/verify", params={"token": token})
    client.post("/api/auth/mfa/poll",
                json={"challenge_id": body["challenge_id"],
                      "client_secret": body["client_secret"]})
    db.expire_all()

    sent = _audit_rows(db, "ADMIN_MFA_SENT")
    verified = _audit_rows(db, "ADMIN_MFA_VERIFIED")
    consumed = _audit_rows(db, "ADMIN_MFA_CONSUMED")
    assert sent, "没有 ADMIN_MFA_SENT 审计"
    assert verified, "没有 ADMIN_MFA_VERIFIED 审计"
    assert consumed, "没有 ADMIN_MFA_CONSUMED 审计"

    # 审计里绝不能出现 token / secret 原文
    blob = " ".join(
        f"{row.detail} {row.after}" for row in (sent + verified + consumed)
    )
    assert token not in blob
    assert body["client_secret"] not in blob
