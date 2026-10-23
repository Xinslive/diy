"""登录路径上的每一条出口都必须留痕（审计 P1-6）。

审计的原话：``_record_failure`` 只改失败计数与 ``locked_until``，
而全仓 ``action=`` 里 grep 不到任何登录相关动作。于是「有人在爆破管理员账号」
这件事在 ``/api/system/audit-logs`` 里**一条痕迹都没有** —— 而那张表恰恰是
出事之后第一个被翻的地方。停用、锁定、爆破，全都查不出来。

这里钉的不只是「有日志」，还有两条容易顺手破坏的边界：

1. **审计写失败不能把登录搞挂。** 审计是观测组件，一旦它成了可用性单点，
   「审计坏了」就等于「整个系统登不进去」。
2. **日志里不能出现口令、散列、令牌。** 登录是唯一一个「用户正在提交口令」
   的接口，稍不注意就会把提交的东西原样抄进审计表。
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.models import AuditLog


@pytest.fixture
def client(app_for_peer):
    """覆盖 conftest 的 ``client``：每个用例一个独立的来源 IP。

    理由同 ``test_login_lockout.py`` 开头那段 —— 本文件也要故意地多次登录失败，
    而 conftest 的 client 是全suite共用的同一个对端。共用一个来源 IP 意味着
    这些失败会堆进同一个节流桶，于是「测审计」的文件会把「测节流」的用例顶掉。
    """
    return app_for_peer


@pytest.fixture
def app_for_peer():
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    ip = f"198.51.{uuid.uuid4().int % 254 + 1}.{uuid.uuid4().int % 254 + 1}"
    with TestClient(fastapi_app, raise_server_exceptions=False,
                    client=(ip, 50000)) as c:
        yield c



def _admin(client, admin_user) -> dict:
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "admin123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _logs(db, action=None):
    stmt = db.query(AuditLog).order_by(AuditLog.id)
    if action:
        stmt = stmt.filter(AuditLog.action == action)
    return list(stmt.all())


def _flat(entry) -> str:
    return json.dumps({"detail": entry.detail, "before": entry.before, "after": entry.after},
                      ensure_ascii=False, default=str)


# --------------------------------------------------------------------- 成功 / 失败


def test_登录成功要留审计(client, db, admin_user):
    """成功的登录同样要留痕：出事之后「谁进来过」是第一条要答的问题。"""
    _admin(client, admin_user)

    rows = _logs(db, "LOGIN_SUCCESS")
    assert rows, "登录成功没有留审计记录"
    entry = rows[-1]
    assert admin_user.username in entry.detail
    assert entry.user_id == admin_user.id


def test_登录失败要留审计(client, db, admin_user):
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "错的"})
    assert r.status_code == 401

    rows = _logs(db, "LOGIN_FAILURE")
    assert rows, "登录失败没有留审计记录 —— 爆破在日志里不留任何痕迹"
    entry = rows[-1]
    assert admin_user.username in entry.detail
    assert "密码" in entry.detail
    assert entry.user_id == admin_user.id


def test_登录失败要记下来源IP(client, db, admin_user):
    """IP 是这类日志唯一能回答「从哪扫过来」的东西（nginx 在前面时取 XFF 首段）。"""
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "错的"},
                    headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    assert r.status_code == 401
    entry = _logs(db, "LOGIN_FAILURE")[-1]
    assert "203.0.113.7" in _flat(entry), f"审计里没有来源 IP：{_flat(entry)}"


def test_审计口径与判定口径是两套(client, db, admin_user):
    """**最容易顺手破坏的一条边界**：审计记 IP 与节流判定 IP 故意不是同一套。

    审计这一侧取 ``X-Forwarded-For`` 的**最左**元素（``_client_ip``）：那是
    客户端自己写的第一个值，可能是假的，但在取证这件事上「宁可记一个可能
    被伪造的值，也别把真实来源弄丢」是对的 —— 多代理链下最左元素往往正是
    真正的发起方。

    节流判定那侧（``_throttle_ip``）只认 TCP 对端，除非对端在
    ``TRUSTED_PROXIES`` 里 —— 因为**判定**会把值拿去当「谁在攻击」的判据，
    采信一个能随便写的头等于把限流的钥匙交给攻击者。

    两者都写在这里，是因为下一次有人「顺手统一一下」就会把其中一个弄坏，
    而弄坏哪一个都不会让任何一条既有用例变红。
    """
    from app.api.auth import _client_ip, _throttle_ip

    class _Req:
        client = type("C", (), {"host": "198.51.100.5"})()

        def __init__(self):
            self.headers = {"x-forwarded-for": "1.2.3.4, 198.51.100.5"}

    req = _Req()
    assert _client_ip(req) == "1.2.3.4", "审计口径应当取 XFF 最左（取证视角）"
    assert _throttle_ip(req) == "198.51.100.5", (
        "判定口径必须只认 TCP 对端 —— 采信可伪造的头，这一层防护等于不存在")

    # 端到端再确认一次：审计里记的是 XFF 最左，节流状态行记的是 TCP 对端
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app, raise_server_exceptions=False,
                    client=("198.51.100.5", 50000)) as c:
        c.post("/api/auth/login",
               json={"username": admin_user.username, "password": "错的"},
               headers={"X-Forwarded-For": "1.2.3.4"})

    assert "1.2.3.4" in _flat(_logs(db, "LOGIN_FAILURE")[-1])
    db.expire_all()
    from app.models.system import LoginIpThrottle

    rows = [r for r in db.query(LoginIpThrottle).all() if r.ip == "198.51.100.5"]
    assert rows, "伪造的 XFF 被拿去当节流判据了 —— 这一层防护等于不存在"


def test_用户名不存在也要留审计(client, db, admin_user):
    """扫用户名是自动化攻击的第一步 —— 它不留痕，后面的爆破也就无从关联。"""
    r = client.post("/api/auth/login",
                    json={"username": "查无此人", "password": "whatever"})
    assert r.status_code == 401

    entry = [x for x in _logs(db, "LOGIN_FAILURE") if "查无此人" in (x.detail or "")][-1]
    assert "不存在" in entry.detail, entry.detail
    assert entry.user_id is None, "不存在的用户不该挂到某个 user_id 上"
    assert entry.after.get("user_exists") is False


def test_停用账号的登录尝试要留审计(client, db, admin_user, plain_user):
    """停用是「用户存在」的信道：过去它连计数器都不写，审计里完全隐形。"""
    plain_user.active = False
    db.commit()
    client.post("/api/auth/login",
                json={"username": plain_user.username, "password": "staff123"})
    entry = _logs(db, "LOGIN_FAILURE")[-1]
    assert "停用" in entry.detail, entry.detail


# --------------------------------------------------------------------- 脱敏


@pytest.mark.parametrize("secret", ["hunter2hunter2", "口令里带中文123", "s3cret!"])
def test_审计里绝不能出现口令或散列或令牌(client, db, admin_user, secret):
    """登录是唯一一个「用户正在提交口令」的接口。"""
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": secret})
    assert r.status_code == 401

    for entry in _logs(db):
        blob = _flat(entry)
        assert secret not in blob, f"提交的口令被写进了审计日志：{entry.action}"
        assert "pbkdf2_sha256" not in blob, f"口令散列写进了审计日志：{entry.action}"
        assert "access_token" not in blob, f"令牌写进了审计日志：{entry.action}"


# --------------------------------------------------------------------- 登出 / 注册


def test_登出要留审计(client, db, admin_user):
    """共享账号、离职交接、攻击者登进去看一眼就走 —— 都靠这条日志。

    登出本身**不撤销令牌**（JWT 无状态，服务端没会话可删），它只留痕。
    """
    hdr = _admin(client, admin_user)
    r = client.post("/api/auth/logout", headers=hdr)
    assert r.status_code == 200, r.text

    entry = _logs(db, "LOGOUT")[-1]
    assert admin_user.username in entry.detail
    assert entry.user_id == admin_user.id


def test_登出本身不撤销令牌(client, db, admin_user):
    """「点一下退出」不该把这个人**所有设备**的会话一起踢掉 ——
    那是 ``token_version`` 的语义（改密 / 停用），不是登出的。"""
    hdr = _admin(client, admin_user)
    client.post("/api/auth/logout", headers=hdr)
    assert client.get("/api/auth/me", headers=hdr).status_code == 200


def test_登出必须先登录(client):
    assert client.post("/api/auth/logout").status_code == 401


def test_注册要留审计(client, db, admin_user):
    """自助注册是**唯一**一条不经过管理员就能往 sys_user 加行的路径。"""
    from app.core.config import settings

    settings.registration_enabled = True
    try:
        r = client.post("/api/auth/register",
                        json={"username": "audit_newbie01", "password": "abcd1234"})
        assert r.status_code == 201, r.text
    finally:
        settings.registration_enabled = False

    rows = [x for x in _logs(db, "REGISTER_USER") if "audit_newbie01" in (x.detail or "")]
    assert rows, "自助注册没有留审计 —— 谁进过这个系统查不出来"
    assert rows[-1].after.get("username") == "audit_newbie01"
    assert "password_hash" not in _flat(rows[-1])


# --------------------------------------------------------------------- 审计不得成为攻击面


def test_审计写失败不能把登录搞挂(client, db, admin_user, monkeypatch):
    """登录路径上唯一的「审计失败 ≠ 安全事件」：观测组件不得当可用性单点。

    过去登录本身不写审计，所以这个问题不存在；现在写审计了，就必须显式
    保证它失败时不至于把整个认证面拖垮。
    """
    import app.api.auth as auth_mod

    def boom(*a, **kw):
        raise RuntimeError("audit 表不可写")

    monkeypatch.setattr(auth_mod, "write_audit", boom)

    assert client.post("/api/auth/login",
                       json={"username": admin_user.username, "password": "错的"}
                       ).status_code == 401
    # 失败计数照常落库 —— 这才是这条路径上真正要保住的东西
    db.refresh(admin_user)
    assert admin_user.failed_logins == 1, "审计写失败把失败计数也一起吞了"

    # 恢复之后同一个会话仍然可用（SAVEPOINT 之后的会话没被拖坏）
    monkeypatch.undo()
    assert client.post("/api/auth/login",
                       json={"username": admin_user.username, "password": "admin123"}
                       ).status_code == 200


def test_审计写失败时成功登录依然发得出令牌(client, admin_user, monkeypatch):
    import app.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "write_audit",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "admin123"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
