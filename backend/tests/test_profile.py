"""个人资料：改自己的姓名、改自己的密码。

这两件事过去**没有入口** —— 姓名只能由管理员改，密码也只能由管理员重置。
这一版把自助那条路补上，于是「我能改自己的什么」就成了一个必须被钉住
的边界。下面钉的是五件事：

1. **只能改自己**：请求体里没有「改谁」，所以不存在改到别人头上的可能；
   普通用户改自己的姓名与密码**不该**要管理员权限（自助的意义就在这）。
2. **改密码必须验当前密码**：令牌是可以被拿走的（XSS、共享电脑），
   没有这道关，拿到令牌的人顺手就把账号的密码改了。
3. **当前密码算错要限速**：这个接口验证的也是密码，而它是**持着有效令牌**
   就能调的 —— 登录那边的锁定与 IP 节流都不覆盖它。不另设一套限速，
   等于白送一个不限速的密码猜测机。
4. **改完密码，全部在用令牌当场作废**：否则「密码泄露后改密」这条最常用的
   处置实际上没生效 —— 攻击者手里的旧令牌照常读写到自然过期。
5. **留痕**：姓名与密码都进审计，且**日志里不出现密码与散列**。
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

from app.models import AuditLog, SysUser


@pytest.fixture
def client(app_for_peer):
    """覆盖 conftest 的 ``client``：每个用例一个独立的来源 IP。

    理由同 ``test_auth_audit.py`` —— 本文件要故意地多次验证失败，
    而共用一个来源 IP 会把这些失败堆进同一个节流桶，
    于是「测自助改密」的文件会把「测节流」的用例顶掉。
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


def _login(client, username: str, password: str) -> dict:
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _logs(db, action=None):
    stmt = db.query(AuditLog).order_by(AuditLog.id)
    if action:
        stmt = stmt.filter(AuditLog.action == action)
    return list(stmt.all())


def _flat(entry) -> str:
    return json.dumps({"detail": entry.detail, "before": entry.before,
                       "after": entry.after}, ensure_ascii=False, default=str)


# --------------------------------------------------------------------- 改姓名


def test_改自己的姓名_普通用户就能改_不需要管理员(client, plain_user, plain_auth, db):
    """自助改姓名**不能**要管理员权限 —— 要了就等于没做。

    管理员那一条路改的是**别人**；自己这一条只能动自己，且人人可用。
    """
    r = client.patch("/api/auth/me", json={"real_name": "黄纪幸"},
                     headers=plain_auth)
    assert r.status_code == 200, r.text
    assert r.json()["real_name"] == "黄纪幸"

    db.expire_all()
    assert db.get(SysUser, plain_user.id).real_name == "黄纪幸"


def test_改自己的姓名_改不动别人的(client, plain_user, plain_auth, admin_user, db):
    """请求体里**没有「改谁」这个字段** —— 别人改不动。

    这是这条接口最省事的地方：授权判据与操作对象是同一个东西，
    不存在两道关卡对不上的可能。所以这一条用「多传一个别人的 id 也改不到」
    来钉：多出来的字段被忽略，目标永远是令牌对应的那个人。
    """
    r = client.patch("/api/auth/me",
                     json={"real_name": "被改了", "id": admin_user.id,
                           "user_id": admin_user.id, "username": "hacker"},
                     headers=plain_auth)
    assert r.status_code == 200, r.text
    assert r.json()["real_name"] == "被改了"

    db.expire_all()
    assert db.get(SysUser, admin_user.id).real_name == "测试管理员", \
        "管理员的姓名被一个普通用户改掉了 —— 这一条接口只认令牌里的那个人"


def test_改自己的姓名_没有令牌就是401(client):
    r = client.patch("/api/auth/me", json={"real_name": "没登录"})
    assert r.status_code == 401


def test_改自己的姓名_空body是422_不是把名字清空(client, plain_user, plain_auth, db):
    """发一个空 body 过来 = 422（「你没说改成什么」）。

    若把它当成「清空显示名」，那是一次**没人会主动要求、却发生了**的结果 ——
    界面上的名字忽然没了，而调用方根本没提过这件事。
    """
    r = client.patch("/api/auth/me", json={}, headers=plain_auth)
    assert r.status_code == 422, r.text

    db.expire_all()
    assert db.get(SysUser, plain_user.id).real_name == "测试员工", "名字被清空了"


def test_改自己的姓名_超长是422_不是500(client, plain_auth):
    """超长姓名曾经是 500（数据库截断）—— 那是一句纯入参问题，
    却长得像数据库故障，把排障方向整个带偏（审计 P2-6）。"""
    r = client.patch("/api/auth/me", json={"real_name": "长" * 200},
                     headers=plain_auth)
    assert r.status_code == 422, r.text


def test_改自己的姓名_留痕(client, plain_user, plain_auth, db):
    """姓名改了要记一笔，且**记下改前与改后**。"""
    before = len(_logs(db, "UPDATE_PROFILE"))
    r = client.patch("/api/auth/me", json={"real_name": "新名字"},
                     headers=plain_auth)
    assert r.status_code == 200, r.text
    logs = _logs(db, "UPDATE_PROFILE")
    assert len(logs) == before + 1
    entry = logs[-1]
    assert entry.entity == "user"
    assert entry.entity_id == str(plain_user.id)
    assert "测试员工" in entry.detail and "新名字" in entry.detail


def test_改自己的姓名_没变就不留痕(client, plain_user, plain_auth, db):
    """没改就不该凭空多一条审计 —— 否则「点一下保存」也在刷日志。"""
    before = len(_logs(db, "UPDATE_PROFILE"))
    r = client.patch("/api/auth/me", json={"real_name": "测试员工"},
                     headers=plain_auth)
    assert r.status_code == 200, r.text
    assert len(_logs(db, "UPDATE_PROFILE")) == before


# --------------------------------------------------------------------- 改密码


def test_改密码_必须先验当前密码(client, plain_user, plain_auth, db):
    """当前密码不对 ⇒ 拒绝，且**不动**库里的密码。

    这一道关的意义：会话令牌是可以被拿走的（XSS、共享电脑、令牌落在别人
    手里）。没有这道关，拿到令牌的人顺手就把账号的密码改了。
    """
    old_hash = plain_user.password_hash
    r = client.post("/api/auth/me/password",
                    json={"current_password": "错的", "new_password": "newPass123"},
                    headers=plain_auth)
    assert r.status_code == 400, r.text
    assert "当前密码不正确" in r.json()["detail"]

    db.expire_all()
    assert db.get(SysUser, plain_user.id).password_hash == old_hash, \
        "当前密码没验过，密码却已经换了"


def test_改密码_当前密码算错要计入锁定(client, plain_user, plain_auth, db):
    """**当前密码算错也要计入登录那套锁定** —— 这里是这条接口最要紧的一条。

    这个接口验证的也是密码，而它是**持着有效令牌**就能调的：登录那边的
    账号锁定与 IP 节流**都不覆盖它**。不另设一套限速，就是白送一个
    不限速的密码猜测机 —— PBKDF2 只把单次成本抬高，抬高不出「限速」。
    """
    from app.core.config import settings

    for _ in range(settings.login_max_failures):
        r = client.post("/api/auth/me/password",
                        json={"current_password": "错的",
                              "new_password": "newPass123"},
                        headers=plain_auth)
        assert r.status_code in (400, 429), r.text

    db.expire_all()
    u = db.get(SysUser, plain_user.id)
    assert (u.failed_logins or 0) > 0 or (u.lock_level or 0) > 0, \
        "验证失败一次都没计数 —— 这条接口成了一个不限速的密码猜测机"


def test_改密码_锁定期间直接拒掉(client, plain_user, plain_auth, db):
    """账号正处在硬锁中时，改密码这条路也关上。

    否则「正在被撞」这件事就有了第二条不受限速的路。
    """
    from app.core import lockout

    u = db.get(SysUser, plain_user.id)
    u.lock_level = lockout.ACCOUNT_HARD_LOCK_AFTER_ROUNDS
    u.locked_until = lockout.utcnow() + timedelta(minutes=10)
    db.commit()

    r = client.post("/api/auth/me/password",
                    json={"current_password": "staff123",
                          "new_password": "newPass123"},
                    headers=plain_auth)
    assert r.status_code == 423, r.text


def test_改密码_成功之后旧令牌立刻作废(client, plain_user, plain_auth, db):
    """**改完密码，全部在用令牌当场作废** —— 审计 P2-01 的机制。

    否则「密码泄露后改密」这条最常用的处置实际没生效：攻击者手里的旧令牌
    照常读写账套，最长再活 12 小时。
    """
    r = client.post("/api/auth/me/password",
                    json={"current_password": "staff123",
                          "new_password": "newPass123"},
                    headers=plain_auth)
    assert r.status_code == 200, r.text
    assert r.json()["reauth_required"] is True

    # 旧令牌：立刻失效
    r2 = client.get("/api/auth/me", headers=plain_auth)
    assert r2.status_code == 401, r2.text

    # 旧密码：登不进去
    r3 = client.post("/api/auth/login",
                     json={"username": plain_user.username,
                           "password": "staff123"})
    assert r3.status_code == 401, r3.text

    # 新密码：登得进去
    r4 = client.post("/api/auth/login",
                     json={"username": plain_user.username,
                           "password": "newPass123"})
    assert r4.status_code == 200, r4.text


def test_改密码_管理员也一样_改完被登出(client, admin_user, db):
    """管理员走的是同一条路 —— 自助这一条不给管理员开后门，
    但也不给管理员额外收紧（他自己知道自己的密码）。"""
    auth = _login(client, admin_user.username, "admin123")
    r = client.post("/api/auth/me/password",
                    json={"current_password": "admin123",
                          "new_password": "admin456"},
                    headers=auth)
    assert r.status_code == 200, r.text
    assert client.get("/api/auth/me", headers=auth).status_code == 401
    assert client.post("/api/auth/login",
                       json={"username": admin_user.username,
                             "password": "admin456"}).status_code == 200


def test_改密码_不合策略的拒绝(client, plain_user, plain_auth):
    """新密码要过**同一把尺**（与管理员建号、重置同一套策略）。

    自助不比管理员松，也不比管理员紧：紧了就有人去求管理员改，
    两条路最后都回到同一把尺。
    """
    r = client.post("/api/auth/me/password",
                    json={"current_password": "staff123", "new_password": "abc"},
                    headers=plain_auth)
    assert r.status_code == 400, r.text
    assert "密码" in r.json()["detail"]

    # **全部数字也认**（用户 2026-10-23 定的口径）：复杂度下限已撤，
    # 现在只有「位数」这一条规则。
    r2 = client.post("/api/auth/me/password",
                     json={"current_password": "staff123",
                           "new_password": "123456"},
                     headers=plain_auth)
    assert r2.status_code == 200, r2.text


def test_改密码_被拒绝的请求不改变任何状态(client, plain_user, plain_auth, db):
    """**请求被拒就该什么都没发生。**

    计数清零放在全部检查通过之后 —— 放在前面的话，「新密码太弱」这种
    **根本没改**的请求会顺手把失败计数清了：等于给猜密码的人白送额度
    （一次就能把攒着的额度清零，再接着猜）。
    """
    u = db.get(SysUser, plain_user.id)
    u.failed_logins = 2
    db.commit()

    r = client.post("/api/auth/me/password",
                    json={"current_password": "staff123", "new_password": "abc"},
                    headers=plain_auth)
    assert r.status_code == 400, r.text

    db.expire_all()
    assert db.get(SysUser, plain_user.id).failed_logins == 2, \
        "被拒的请求把失败计数清了 —— 攒着的额度被白送掉了"


def test_改密码_与当前密码相同要拒绝(client, plain_user, plain_auth):
    """新密码与当前密码相同 ⇒ 拒绝。

    它不是「错误」，但**必须挡住**：改密码会撤销全部令牌（把人踢下线），
    却什么也没改 —— 白挨一次重新登录。
    """
    r = client.post("/api/auth/me/password",
                    json={"current_password": "staff123",
                          "new_password": "staff123"},
                    headers=plain_auth)
    assert r.status_code == 400, r.text
    assert "相同" in r.json()["detail"]


def test_改密码_留痕_且日志里没有密码(client, plain_user, plain_auth, db):
    """留痕，且**日志里不许出现密码与散列**。

    审计表是出事之后第一个被翻的地方 —— 它自己漏了凭据，就是把泄漏
    写进了「追查材料」里。
    """
    r = client.post("/api/auth/me/password",
                    json={"current_password": "staff123",
                          "new_password": "newPass123"},
                    headers=plain_auth)
    assert r.status_code == 200, r.text

    logs = _logs(db, "CHANGE_PASSWORD")
    assert len(logs) == 1
    text = _flat(logs[0])
    assert "newPass123" not in text, "审计里出现了新密码的明文"
    assert "staff123" not in text, "审计里出现了旧密码的明文"
    assert plain_user.password_hash not in text, "审计里出现了密码散列"


def test_改密码_失败也要留痕(client, plain_user, plain_auth, db):
    """验证失败同样要留痕 —— 「有人拿着令牌在猜我的密码」正是要查的那类事。"""
    r = client.post("/api/auth/me/password",
                    json={"current_password": "错的", "new_password": "newPass123"},
                    headers=plain_auth)
    assert r.status_code == 400
    logs = _logs(db, "CHANGE_PASSWORD_FAILED")
    assert len(logs) == 1
    assert "当前密码不正确" in logs[0].detail
    assert "错的" not in _flat(logs[0]), "审计里把提交上来的密码抄进去了"
