"""账号生命周期要留审计（审计 P2-03）。

审计的原话：``create_user`` / ``update_user`` / ``delete_user`` 全部直接提交，
而隔壁的 ``set_books`` 会逐账套写授权变更日志。于是出现了很别扭的局面 ——
「改了权限有日志、开号时给的初始权限没有」「停用账号一个字都没留下」。

这类操作的影响面比授权还大（它决定谁能进这个系统），却比授权更难追溯：
账号删掉之后连「曾经有过这么一个账号」都没地方查，而账上留下的凭证
created_by 记的还是那个用户名。

**这里额外钉一条：日志里绝不能出现口令或口令散列。**
``/api/system/audit-logs`` 是全局可读的，任何管理员令牌都能拉到全文；
散列落进日志等于把离线爆破的素材抄送给所有管理员。
"""
from __future__ import annotations

import json

import pytest

from app.models import AuditLog, SysUser


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
    return json.dumps(
        {"detail": entry.detail, "before": entry.before, "after": entry.after},
        ensure_ascii=False, default=str)


# --------------------------------------------------------------------- 创建


def test_建号要留审计(client, db, admin_user, book):
    hdr = _admin(client, admin_user)
    r = client.post("/api/system/users", headers=hdr, json={
        "username": "zhangsan", "password": "secret123", "real_name": "张三",
        "books": [{"book_id": book.id, "role": "ACCOUNTANT"}],
    })
    assert r.status_code == 201, r.text

    rows = [x for x in _logs(db, "CREATE_USER") if "zhangsan" in (x.detail or "")]
    assert rows, "建号没有留审计记录"
    entry = rows[-1]
    assert entry.username == admin_user.username, "没记下是谁开的号"
    assert entry.after and entry.after.get("username") == "zhangsan"


def test_建号时要记下初始授权(client, db, admin_user, book):
    """这是它与 set_books 的不对称之处：改授权有日志，开号时的初始授权没有。
    事后只能靠「现在有哪些授权」倒推，而改过的就再也看不出初始值。"""
    hdr = _admin(client, admin_user)
    client.post("/api/system/users", headers=hdr, json={
        "username": "lisi", "password": "secret123",
        "books": [{"book_id": book.id, "role": "OWNER"}],
    })

    entry = [x for x in _logs(db, "CREATE_USER") if "lisi" in (x.detail or "")][-1]
    assert entry.after.get("books") == {str(book.id): "OWNER"}, (
        f"初始授权没记下来：{entry.after}")


# --------------------------------------------------------------------- 停用 / 启用 / 改密


def test_停用账号要留审计(client, db, admin_user, plain_user):
    hdr = _admin(client, admin_user)
    r = client.patch(f"/api/system/users/{plain_user.id}", headers=hdr,
                     json={"active": False})
    assert r.status_code == 200, r.text

    entry = [x for x in _logs(db, "UPDATE_USER") if str(x.entity_id) == str(plain_user.id)][-1]
    assert "停用" in entry.detail, entry.detail
    assert entry.before and entry.before["active"] is True
    assert entry.after and entry.after["active"] is False


def test_重新启用要留审计(client, db, admin_user, plain_user):
    hdr = _admin(client, admin_user)
    client.patch(f"/api/system/users/{plain_user.id}", headers=hdr, json={"active": False})
    client.patch(f"/api/system/users/{plain_user.id}", headers=hdr, json={"active": True})

    rows = [x for x in _logs(db, "UPDATE_USER") if str(x.entity_id) == str(plain_user.id)]
    assert any("启用" in x.detail for x in rows), "启用没有留痕"


def test_改密要留审计(client, db, admin_user, plain_user):
    hdr = _admin(client, admin_user)
    client.patch(f"/api/system/users/{plain_user.id}", headers=hdr,
                 json={"password": "brandnew123"})

    entry = [x for x in _logs(db, "UPDATE_USER") if str(x.entity_id) == str(plain_user.id)][-1]
    assert "重置口令" in entry.detail, entry.detail


@pytest.mark.parametrize("secret", ["secret123", "brandnew123"])
def test_审计里绝不能出现口令或散列(client, db, admin_user, plain_user, secret):
    """日志是全局可读的：散列落进去等于把爆破素材抄送给每个管理员。"""
    hdr = _admin(client, admin_user)
    client.patch(f"/api/system/users/{plain_user.id}", headers=hdr,
                 json={"password": secret, "active": False, "real_name": "改名了"})

    for entry in _logs(db):
        blob = _flat(entry)
        assert secret not in blob, f"明文口令写进了审计日志：{entry.action}"
        assert "password_hash" not in blob, f"散列写进了审计日志：{entry.action}"
        assert "pbkdf2_sha256" not in blob, f"散列写进了审计日志：{entry.action}"


def test_没改任何东西时不产生噪音日志(client, db, admin_user, plain_user):
    """提交了与现值相同的字段不算一次变更，日志不该凭空多一条。"""
    hdr = _admin(client, admin_user)
    before = len(_logs(db, "UPDATE_USER"))
    client.patch(f"/api/system/users/{plain_user.id}", headers=hdr,
                 json={"real_name": plain_user.real_name, "active": True})
    assert len(_logs(db, "UPDATE_USER")) == before


# --------------------------------------------------------------------- 删除


def test_删号要留审计(client, db, admin_user, plain_user, book):
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()
    uid, username = plain_user.id, plain_user.username

    hdr = _admin(client, admin_user)
    r = client.delete(f"/api/system/users/{uid}", headers=hdr)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(SysUser, uid) is None, "账号没删掉"

    entry = [x for x in _logs(db, "DELETE_USER") if str(x.entity_id) == str(uid)][-1]
    assert username in entry.detail
    assert entry.before and entry.before["username"] == username, (
        "删完之后人就查不到了，删之前的快照必须留着")


def test_不能删除自己(client, db, admin_user):
    hdr = _admin(client, admin_user)
    assert client.delete(f"/api/system/users/{admin_user.id}", headers=hdr).status_code == 400


def test_普通用户看不到也改不了账号(client, db, plain_user, plain_auth):
    assert client.get("/api/system/users", headers=plain_auth).status_code == 403
    r = client.post("/api/system/users", headers=plain_auth, json={
        "username": "backdoor", "password": "secret123"})
    assert r.status_code == 403, r.text
