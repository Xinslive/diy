"""改密 / 停用之后，已签发的令牌必须立刻失效（审计 P2-01）。

**漏洞的形状：改完口令，令牌照样好使。**

JWT 是自签名的，服务端不存会话，所以验证一个令牌的全部依据只有三样：
签名对不对、有没有过期、用户还 active 吗。管理员重置口令时只改了
``password_hash`` —— 已经发出去的 bearer token 一个都没作废。

于是口令泄露之后最常用的那条处置（改密）实际上没生效：攻击者手里的
旧令牌还能读账、写凭证，直到自然过期（普通用户 720 分钟 = 12 小时）。
停用账号能挡住，因为 ``active`` 每次都被查；但改密没有对应的字段。

修法是一个整数：``token_version`` 签发时进载荷，鉴权时与库里比一次。
"""
from __future__ import annotations

import pytest

from app.core.security import create_access_token


def _login(client, username, password) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"登录失败：{r.text}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def staff(client, db, plain_user, book):
    """一个能进账套的普通用户：管理员授权 → 旧令牌才有「能访问的东西」可失去。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="ACCOUNTANT"))
    db.commit()
    return plain_user


def test_重置密码后旧令牌立刻失效(client, db, admin_user, staff, book):
    old = _login(client, staff.username, "staff123")
    r = client.get(f"/api/books/{book.id}/subjects", headers=old)
    assert r.status_code == 200, r.text

    r = client.patch(f"/api/system/users/{staff.id}", headers=_login(client, admin_user.username, "admin123"),
                     json={"password": "newpass456"})
    assert r.status_code == 200, r.text

    r = client.get(f"/api/books/{book.id}/subjects", headers=old)
    assert r.status_code == 401, f"改完口令旧令牌还能用：{r.status_code}"
    assert "失效" in r.json()["detail"]


def test_改密后重新登录的令牌可用(client, db, admin_user, staff, book):
    old = _login(client, staff.username, "staff123")
    client.patch(f"/api/system/users/{staff.id}",
                 headers=_login(client, admin_user.username, "admin123"),
                 json={"password": "newpass456"})

    fresh = _login(client, staff.username, "newpass456")
    r = client.get(f"/api/books/{book.id}/subjects", headers=fresh)
    assert r.status_code == 200, r.text

    r = client.get(f"/api/books/{book.id}/subjects", headers=old)
    assert r.status_code == 401


def test_停用账号后旧令牌失效(client, db, admin_user, staff, book):
    """停用过去靠 active 就能挡住，但也要确认它没被改坏 ——
    而且重新启用之后，旧令牌**仍然**不该复活。"""
    old = _login(client, staff.username, "staff123")
    hdr = _login(client, admin_user.username, "admin123")

    client.patch(f"/api/system/users/{staff.id}", headers=hdr, json={"active": False})
    assert client.get(f"/api/books/{book.id}/subjects", headers=old).status_code == 401

    client.patch(f"/api/system/users/{staff.id}", headers=hdr, json={"active": True})
    again = _login(client, staff.username, "staff123")
    assert client.get(f"/api/books/{book.id}/subjects", headers=again).status_code == 200
    assert client.get(f"/api/books/{book.id}/subjects", headers=old).status_code == 401, (
        "重新启用后，停用之前签发的令牌不该复活")


def test_没动口令的账号令牌不受牵连(client, db, admin_user, staff, book):
    """撤销必须只影响本人：给 A 改口令不能把 B 踢下线。"""
    mine = _login(client, staff.username, "staff123")
    theirs = _login(client, admin_user.username, "admin123")

    client.patch(f"/api/system/users/{staff.id}", headers=theirs,
                 json={"password": "newpass456"})

    assert client.get(f"/api/books/{book.id}/subjects", headers=mine).status_code == 401
    assert client.get(f"/api/books/{book.id}/subjects", headers=theirs).status_code == 200


def test_升级前签发的老令牌仍然可用(client, db, staff, book):
    """载荷里没有 tv 的令牌（升级前签发的）不能因为这次升级全被踢下线 ——
    存量库里它们的 token_version 也是 0，两边对得上。"""
    token, _ = create_access_token(staff.id, staff.username)
    hdr = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/api/books/{book.id}/subjects", headers=hdr).status_code == 200


def test_令牌里带的是当前版本号(client, db, staff):
    import jwt

    from app.core.config import settings

    token, _ = _token_with_version(staff)
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert payload["tv"] == (staff.token_version or 0), (
        "载荷里没有带令牌版本 —— 鉴权时无从核对")


def _token_with_version(staff):
    db_version = staff.token_version or 0
    return create_access_token(staff.id, staff.username, token_version=db_version)


def test_版本号每次撤销加一(client, db, admin_user, staff):
    from app.models import SysUser

    hdr = _login(client, admin_user.username, "admin123")
    before = staff.token_version or 0

    client.patch(f"/api/system/users/{staff.id}", headers=hdr, json={"password": "a1234567"})
    db.expire_all()
    after_first = db.get(SysUser, staff.id).token_version
    assert after_first == before + 1

    client.patch(f"/api/system/users/{staff.id}", headers=hdr, json={"password": "b1234567"})
    db.expire_all()
    assert db.get(SysUser, staff.id).token_version == before + 2, (
        "第二次改密后版本号没继续递增 —— 第一次改密之前签发的令牌会复活")


def test_普通用户不能改别人口令(client, db, plain_user, plain_auth, staff):
    r = client.patch(f"/api/system/users/{staff.id}", headers=plain_auth,
                     json={"password": "hijack000"})
    assert r.status_code == 403, r.text


def test_停用与撤销都能追到审计(client, db, admin_user, staff):
    """撤销动作本身必须留痕 —— 否则「谁把这个人的会话作废了」没人答得上。"""
    hdr = _login(client, admin_user.username, "admin123")
    client.patch(f"/api/system/users/{staff.id}", headers=hdr, json={"password": "newpass456"})

    r = client.get("/api/system/audit-logs?limit=50", headers=hdr)
    assert r.status_code == 200, r.text
    hits = [x for x in r.json() if x["action"] == "UPDATE_USER" and str(x["entity_id"]) == str(staff.id)]
    assert hits, "改密没有留审计记录"
