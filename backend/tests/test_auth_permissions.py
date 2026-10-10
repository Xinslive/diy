"""注册 + 权限边界。

这两块以前是**没有测试的**，所以出过两个洞：
1. `GET /system/users`、`GET /system/books`、`GET /system/audit-logs` 没加管理员
   门禁 —— 任何登录用户都能拿到全部用户名和全部账套清单。
2. 管理员判定是「用户名恰好等于 admin」。测试夹具造不出管理员，于是这层
   逻辑长期没被跑到过。

这里把两件事都钉死。
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------- 注册


def test_注册成功但拿不到任何账套(client, plain_auth):
    """注册只开账号。**不自动授权任何账套** —— 授权是管理员的事。"""
    r = client.post("/api/auth/register",
                    json={"username": "brandnew01", "password": "abcd1234", "real_name": "新同事"})
    assert r.status_code == 201, r.text

    r = client.post("/api/auth/login", json={"username": "brandnew01", "password": "abcd1234"})
    assert r.status_code == 200, r.text
    tk = {"Authorization": f"Bearer {r.json()['access_token']}"}

    assert client.get("/api/books", headers=tk).json() == []
    assert client.get("/api/auth/me", headers=tk).json()["books"] == []


@pytest.mark.parametrize("payload,why", [
    ({"username": "abc", "password": "12345"}, "密码不足 8 位"),
    ({"username": "张三", "password": "abcd1234"}, "用户名含中文"),
    ({"username": "has space", "password": "abcd1234"}, "用户名含空格"),
    ({"username": "ab", "password": "abcd1234"}, "用户名过短"),
])
def test_注册参数被拒(client, payload, why):
    r = client.post("/api/auth/register", json=payload)
    assert r.status_code == 422, f"{why} 竟然通过了：{r.text}"


def test_用户名不可重复(client):
    body = {"username": "dupuser01", "password": "abcd1234"}
    assert client.post("/api/auth/register", json=body).status_code == 201
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 400
    assert "已被占用" in r.json()["detail"]


def test_注册开关关掉后拒绝(client):
    from app.core.config import settings

    settings.registration_enabled = False
    try:
        r = client.post("/api/auth/register", json={"username": "closed01", "password": "abcd1234"})
        assert r.status_code == 403
        assert "未开放自助注册" in r.json()["detail"]
    finally:
        settings.registration_enabled = True


# --------------------------------------------------------------------- 权限边界

#: (说明, 方法, 路径, 请求体) —— 普通用户每一条都必须是 403
FORBIDDEN = [
    ("列全部用户",   "post", "/api/system/users", {"username": "hack1", "password": "abcd1234"}),
    ("列全部账套",   "get",  "/api/system/books", None),
    ("全局审计日志", "get",  "/api/system/audit-logs", None),
    ("改他人资料",   "patch", "/api/system/users/1", {"password": "abcd1234"}),
    ("删他人",       "delete", "/api/system/users/1", None),
    ("给别人授权账套", "put", "/api/system/users/1/books", []),
    ("新建账套",     "post", "/api/books", {"company_name": "X", "name": "X", "start_year": 2026}),
]


#: 操作日志里的「快照」等同全量凭证副本，普通会计不该看到别人的操作痕迹
def test_操作日志只有管理员能看(client, db, plain_auth, plain_user, book):
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="ACCOUNTANT"))
    db.commit()

    r = client.get(f"/api/books/{book.id}/audit-logs", headers=plain_auth)
    assert r.status_code == 403, f"会计能看到操作日志：{r.status_code} {r.text}"


def test_管理员仍然看得到操作日志(client, auth, book, make_voucher):
    make_voucher("2026-01-05", dr=[("100201", 10, "x")], cr=[("600101", 10, "x")])
    r = client.get(f"/api/books/{book.id}/audit-logs", headers=auth)
    assert r.status_code == 200
    assert any(x["action"] == "CREATE" for x in r.json())


@pytest.mark.parametrize("label,method,path,body", FORBIDDEN, ids=[c[0] for c in FORBIDDEN])
def test_普通用户一律被拒(client, plain_auth, label, method, path, body):
    r = getattr(client, method)(path, headers=plain_auth, **({"json": body} if body is not None else {}))
    assert r.status_code == 403, f"{label} 没有拦住：{r.status_code} {r.text}"


def test_管理员仍然畅通(client, auth):
    """收紧不能误伤管理员自己。"""
    assert client.get("/api/system/users", headers=auth).status_code == 200
    assert client.get("/api/system/books", headers=auth).status_code == 200
    assert client.get("/api/system/audit-logs", headers=auth).status_code == 200


def test_普通用户只能看自己被授权的账套(client, plain_auth, db, plain_user, book):
    """sys_user_book 才是账套可见性的唯一依据。"""
    assert client.get("/api/books", headers=plain_auth).json() == []

    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()

    got = client.get("/api/books", headers=plain_auth).json()
    assert [b["id"] for b in got] == [book.id]
    assert got[0]["role"] == "VIEWER"


def test_停用后不能登录(client, db, plain_user):
    plain_user.active = False
    db.commit()
    r = client.post("/api/auth/login",
                    json={"username": plain_user.username, "password": "staff123"})
    assert r.status_code == 401


def test_登录响应带管理员标记(client, auth, plain_auth):
    """前端不再拿用户名猜管理员，直接用后端下发的字段。"""
    assert client.get("/api/auth/me", headers=auth).json()["is_admin"] is True
    assert client.get("/api/auth/me", headers=plain_auth).json()["is_admin"] is False


def test_管理员标记不靠用户名():
    """判定依据是 is_admin 字段，与用户名无关。"""
    from app.api.system import _is_admin
    from app.models import SysUser

    assert _is_admin(SysUser(username="admin", is_admin=False)) is False
    assert _is_admin(SysUser(username="zhangsan", is_admin=True)) is True
