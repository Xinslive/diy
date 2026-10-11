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
#
# ``registration_enabled`` 的**默认值现在是 False**（审计 P3-04 第 2 条）：
# 对外暴露时开放自助注册等于把系统变成谁都能建号的公共资源。
# 所以这几条测试要测「注册开着的时候行为对不对」，得自己把开关打开 ——
# 就像真实部署里显式设 REGISTRATION_ENABLED=true 那样。
# 默认关闭这件事由 test_prod_config_guard.py 单独钉住。


@pytest.fixture
def registration_on():
    from app.core.config import settings

    settings.registration_enabled = True
    try:
        yield
    finally:
        settings.registration_enabled = False


def test_注册成功但拿不到任何账套(client, plain_auth, registration_on):
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
def test_注册参数被拒(client, payload, why, registration_on):
    r = client.post("/api/auth/register", json=payload)
    assert r.status_code == 422, f"{why} 竟然通过了：{r.text}"


def test_用户名不可重复(client, registration_on):
    body = {"username": "dupuser01", "password": "abcd1234"}
    assert client.post("/api/auth/register", json=body).status_code == 201
    r = client.post("/api/auth/register", json=body)
    assert r.status_code == 400
    assert "已被占用" in r.json()["detail"]


def test_注册开关关掉后拒绝(client):
    """默认就该是关的 —— 这条不设夹具，钉的是默认值本身。"""
    from app.core.config import settings

    assert settings.registration_enabled is False, "自助注册默认必须是关闭的"
    r = client.post("/api/auth/register", json={"username": "closed01", "password": "abcd1234"})
    assert r.status_code == 403
    assert "未开放自助注册" in r.json()["detail"]


# --------------------------------------------------------------------- 权限边界

#: (说明, 方法, 路径, 请求体) —— 普通用户每一条都必须是 403
FORBIDDEN = [
    ("列全部用户",   "post", "/api/system/users", {"username": "hack1", "password": "abcd1234"}),
    ("列全部账套",   "get",  "/api/system/books", None),
    ("全局审计日志", "get",  "/api/system/audit-logs", None),
    ("改他人资料",   "patch", "/api/system/users/1", {"password": "abcd1234"}),
    ("删他人",       "delete", "/api/system/users/1", None),
    ("给别人授权账套", "put", "/api/system/users/1/books", []),
]
# 注意「新建账套」不在上面这份名单里：自助注册开着，再卡管理员开户的话，
# 新同事得先找人开户才能干活。建账的人自动是 OWNER，边界落在
# 「只能授权别人的账套」，不是「只有管理员能建账」。


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


# --------------------------------------------------------------------- 建账套的口子


def test_普通用户可以自己建账套(client, db, plain_auth, plain_user):
    """自助注册开着，普通用户就该能自己开账干活，不必先找人开户。"""
    from app.models import UserBook

    r = client.post("/api/books", headers=plain_auth,
                    json={"company_name": "新同事公司", "name": "自己开的账", "start_year": 2026})
    assert r.status_code == 201, f"普通用户建账被拒：{r.status_code} {r.text}"
    got = client.get("/api/books", headers=plain_auth).json()
    assert [b["name"] for b in got] == ["自己开的账"]
    # 建账的人自动是 OWNER：改账套名这些得靠这个角色
    link = db.query(UserBook).filter_by(user_id=plain_user.id, book_id=got[0]["id"]).one_or_none()
    assert link is not None and link.role == "OWNER", "建账的人没拿到 OWNER"


def test_自己建的账套周期表科目表都齐(client, plain_auth):
    """建账不只是插一行 book：12 个会计期间和整套科目表都要装好，
    否则新账套一进去就是空的，没法记第一笔。"""
    r = client.post("/api/books", headers=plain_auth,
                    json={"company_name": "甲", "name": "乙", "start_year": 2026})
    book_id = r.json()["id"]
    periods = client.get(f"/api/books/{book_id}/periods", headers=plain_auth).json()
    assert len(periods) == 12, f"新建账套应有 12 个期间，实际 {len(periods)}"
    subjects = client.get(f"/api/books/{book_id}/subjects", headers=plain_auth).json()
    assert len(subjects) > 30, "科目表没装上"


def test_建账留了审计(client, plain_auth, book, db):
    """口子放开后，「这个账套是谁开的」必须查得到。"""
    from app.models import AuditLog

    client.post("/api/books", headers=plain_auth,
                json={"company_name": "留痕公司", "name": "留痕账套", "start_year": 2026})
    rows = db.query(AuditLog).filter(AuditLog.entity == "book",
                                    AuditLog.action == "CREATE").all()
    assert rows, "新建账套没有写审计日志"
    assert any("留痕账套" in (r.detail or "") for r in rows)


def test_重名账套还是不让建(client, plain_auth):
    """口子开了，但同一个公司下的账套名仍要唯一 —— 否则报表页眉会分不清。"""
    body = {"company_name": "同名公司", "name": "同名账套", "start_year": 2026}
    assert client.post("/api/books", headers=plain_auth, json=body).status_code == 201
    again = client.post("/api/books", headers=plain_auth, json=body)
    assert again.status_code == 400, f"重名账套没拦住：{again.status_code} {again.text}"


def test_没登录不能建账套(client):
    assert client.post("/api/books",
                       json={"company_name": "X", "name": "X", "start_year": 2026}).status_code == 401
