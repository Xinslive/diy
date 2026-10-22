"""注册 + 权限边界。

这两块以前是**没有测试的**，所以出过两个洞：
1. `GET /system/users`、`GET /system/books`、`GET /system/audit-logs` 没加管理员
   门禁 —— 任何登录用户都能拿到全部用户名和全部账套清单。
2. 管理员判定是「用户名恰好等于 admin」。测试夹具造不出管理员，于是这层
   逻辑长期没被跑到过。

这里把两件事都钉死。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import SysUser, SystemSetting, UserBook


# --------------------------------------------------------------------- 注册
#
# ``registration_enabled`` 的**默认值现在是 False**（审计 P3-04 第 2 条）：
# 对外暴露时开放自助注册等于把系统变成谁都能建号的公共资源。
# 所以这几条测试要测「注册开着的时候行为对不对」，得自己把开关打开 ——
# 就像真实部署里显式设 REGISTRATION_ENABLED=true 那样。
# 默认关闭这件事由 test_prod_config_guard.py 单独钉住。
#
# **2026-10-19 起这个开关也存进了 ``sys_setting``**（管理员在「用户与授权」页
# 上就能改，见 app/core/system_settings.py::registration_enabled）。所以下面
# 出现了两种「打开」的方式，它们都要被覆盖：库里那行说了算，没有那行才读 env。


@pytest.fixture
def registration_on(db):
    """通过 **env** 打开自助注册（库里没有那一行时的老路径）。

    同时清掉**注册节流桶**（``ri:`` / ``ru:`` 前缀）：节流状态存在库里而
    xdist 按 worker 分库、同一个 worker 的所有用例共用一个库 —— 不清的话
    前面的用例把 IP 桶涨上去，后面的用例一注册就 429，那是测试互相污染，
    不是缺陷。
    """
    from app.core.config import settings
    from app.models.system import LoginIpThrottle

    def clear_buckets():
        db.query(LoginIpThrottle).filter(
            LoginIpThrottle.ip.like("ri:%") | LoginIpThrottle.ip.like("ru:%")
        ).delete(synchronize_session=False)
        db.commit()

    clear_buckets()
    settings.registration_enabled = True
    try:
        yield
    finally:
        settings.registration_enabled = False
        clear_buckets()


@pytest.fixture
def registration_in_db(client, db):
    """通过**库里那一行**打开自助注册（老路径之外的那条：管理员在界面上打开）。"""
    from app.core import system_settings

    system_settings.set_registration_enabled(db, True, updated_by="pytest")
    db.commit()
    yield


@pytest.fixture(autouse=True)
def _clean_registration_setting(db):
    """每个用例前后都清掉 ``sys_setting`` 里那一行开关。

    这一行一旦留下就会**盖住 env**，而 xdist 是按 worker 分库的：同一个 worker
    里跑在后面的用例会看到上一个用例留下的状态，于是「默认应该关着」那几条
    在单跑时全绿、放进全量就红 —— 症状和「测试自己互相污染」一样难看，方向却
    相反（是测试写错了，不是产品行为变了）。所以清理必须是每个用例前后各一次，
    而不是只在某几个用例的 finally 里补。
    """
    from app.core import system_settings

    def wipe():
        row = db.get(SystemSetting, system_settings.REGISTRATION_KEY)
        if row is not None:
            db.delete(row)
            db.commit()

    wipe()
    yield
    wipe()


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
    ({"username": "abc", "password": "12345"}, "密码不足 6 位"),
    ({"username": "张三", "password": "abcd1234"}, "用户名含中文"),
    ({"username": "has space", "password": "abcd1234"}, "用户名含空格"),
    ({"username": "ab", "password": "abcd1234"}, "用户名过短"),
])
def test_注册参数被拒(client, payload, why, registration_on):
    r = client.post("/api/auth/register", json=payload)
    assert r.status_code == 422, f"{why} 竟然通过了：{r.text}"


def test_注册口令下限是六位(client, registration_on):
    """**注册的下限是 6 位**（用户 2026-10-22 定的口径）。

    与管理员建号**同一把尺** —— 过去自助注册是 8 位、比管理员建号更严，
    改完后两边都是 6 位。这条用例钉住的是**边界**：5 位被拒、6 位能注册。
    """
    r = client.post("/api/auth/register",
                    json={"username": "fivechar01", "password": "ab12c"})
    assert r.status_code == 422, f"5 位口令被收了：{r.text[:200]}"

    r = client.post("/api/auth/register",
                    json={"username": "sixchar01", "password": "ab12cd"})
    assert r.status_code == 201, f"6 位口令被拒了：{r.text[:200]}"


def test_用户名不可重复(client, registration_on):
    """**重名不再是一个可区分的响应**（审查报告安全域 P2-2）。

    这条断言以前是「重名 → 400 且文案含『已被占用』」——
    那正是这条缺陷本身：重名返回 400、成功返回 201，两种响应可区分，
    于是「注册一下试试」成了一条比登录更省事的用户名枚举信道
    （登录要付 20ms 的 PBKDF2，注册不用）。

    现在的口径：**重名与成功的响应完全一样**（201 + 同一个 body），
    只是**不建号**。差别只留在管理员看得见的审计里。
    「截断行为被当成规范断言下来」是这轮审查点名的问题（P-43）——
    这条断言当时就在把缺陷钉成规范，现在一并改掉。
    """
    body = {"username": "dupuser01", "password": "abcd1234"}
    first = client.post("/api/auth/register", json=body)
    assert first.status_code == 201
    again = client.post("/api/auth/register", json=body)

    # **响应必须逐字相同** —— 不只是状态码相同。
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    assert "已被占用" not in again.text

    # 「不建号」是另一件事：它必须在**库里**看得见 —— 账号数不变。
    from app.models import SysUser
    rows = client.get("/api/auth/config")
    assert rows.status_code == 200
    # 账号仍然只能用**原来的口令**登录（重名那次没有覆盖口令）。
    r = client.post("/api/auth/login", json=body)
    assert r.status_code == 200, r.text


def test_手机号能当用户名注册并登录(client, registration_on):
    """自助注册的「用户名」已经改成手机号 —— 11 位号码必须真的能建号、能登录。

    这条钉的是**接口层面**：前端只是把占位符和校验正则改了，后端的
    ``^[A-Za-z0-9_]{3,32}$`` 本来就放行纯数字，所以号码一直是能过的。
    真哪天有人把那个 pattern 收紧成「不能全是数字」，这里会第一个红。
    """
    phone = "13800138000"
    r = client.post("/api/auth/register", json={"username": phone, "password": "abcd1234"})
    assert r.status_code == 201, r.text
    assert r.json()["username"] == phone
    r = client.post("/api/auth/login", json={"username": phone, "password": "abcd1234"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == phone


def test_注册不填姓名时默认会计(client, registration_on):
    """姓名输入框已经去掉，后端必须兜住「没填」。

    这里验的是**兜底的取值**，不是「有默认值」—— 原来的兜底是
    ``payload.username``，改用手机号注册后它会把一串号码存成人的名字。
    """
    phone = "13900139000"
    r = client.post("/api/auth/register", json={"username": phone, "password": "abcd1234"})
    assert r.status_code == 201, r.text
    r = client.post("/api/auth/login", json={"username": phone, "password": "abcd1234"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["real_name"] == "会计"


@pytest.mark.parametrize("real_name", ["", "   "], ids=["空串", "纯空白"])
def test_注册姓名填空白也兜成会计(client, registration_on, real_name):
    """纯空白也要兜。``"   " or "会计"`` 在 Python 里是真值，挡不住。"""
    phone = "1390013" + str(len(real_name) + 1).zfill(4)
    r = client.post("/api/auth/register",
                    json={"username": phone, "password": "abcd1234", "real_name": real_name})
    assert r.status_code == 201, r.text
    r = client.post("/api/auth/login", json={"username": phone, "password": "abcd1234"})
    assert r.json()["user"]["real_name"] == "会计"


def test_注册填了姓名就用填的(client, registration_on):
    """默认值只兜空值，别写成一刀切 —— 脚本和将来的接口仍要能指定姓名。"""
    phone = "13700137000"
    r = client.post("/api/auth/register",
                    json={"username": phone, "password": "abcd1234", "real_name": " 张三 "})
    assert r.status_code == 201, r.text
    r = client.post("/api/auth/login", json={"username": phone, "password": "abcd1234"})
    assert r.json()["user"]["real_name"] == "张三", "前后空白应被去掉，但填的值要留下"


def test_注册开关关掉后拒绝(client):
    """默认就该是关的 —— 这条不设夹具，钉的是默认值本身。"""
    from app.core.config import settings

    assert settings.registration_enabled is False, "自助注册默认必须是关闭的"
    r = client.post("/api/auth/register", json={"username": "closed01", "password": "abcd1234"})
    assert r.status_code == 403
    assert "未开放自助注册" in r.json()["detail"]


# --------------------------------------------------- 注册开关：库里那一行说了算


def test_库里打开即可注册不依赖env(client, registration_in_db):
    """管理员在界面上打开开关之后，env 里那个 false 必须让位。

    这条钉的是「库优先」：**服务器上的 .env 里几乎一定写着
    ``REGISTRATION_ENABLED=false``**，而界面上刚打开的开关不该被它压回去
    —— 那正是「界面上改了却没生效，而没人知道为什么」。
    """
    from app.core.config import settings

    assert settings.registration_enabled is False, "这条测试的前提就是 env 里是关的"
    phone = "13600136001"
    r = client.post("/api/auth/register",
                    json={"username": phone, "password": "abcd1234"})
    assert r.status_code == 201, r.text


def test_库里关掉即可覆盖env里开着(client, db):
    """反方向同样要成立：库里写「关」，env 开着也不许注册。

    没有这条的话就会出现更糟的状态 —— 有人在 .env 里把开关开着（那是老做法），
    界面上一关，注册接口却还在放行。
    """
    from app.core.config import settings
    from app.core import system_settings

    settings.registration_enabled = True
    system_settings.set_registration_enabled(db, False, updated_by="pytest")
    db.commit()
    try:
        r = client.post("/api/auth/register",
                        json={"username": "13600136002", "password": "abcd1234"})
        assert r.status_code == 403, f"库里已关闭却注册成功：{r.status_code}"
    finally:
        settings.registration_enabled = False
        # sys_setting 里那一行由 _clean_registration_setting 负责清


def test_登录页能匿名读到注册开关(client, registration_in_db):
    """登录页此刻**还没有令牌**，所以这条必须匿名可读。

    顺带钉住响应里只有那一个布尔：这个接口对匿名开放。
    """
    r = client.get("/api/auth/config")
    assert r.status_code == 200, r.text
    assert r.json() == {"registration_enabled": True}


def test_匿名读到的注册开关默认是关的(client, db):
    from app.core import system_settings

    assert db.get(SystemSetting, system_settings.REGISTRATION_KEY) is None, (
        "库里不该有残留的开关行，会盖住默认值")
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json()["registration_enabled"] is False


def test_关掉之后匿名立刻读到关(client, db):
    """开关是即时的：管理员改完不需要重启，登录页下一次刷新就该看不到注册。"""
    from app.core import system_settings

    system_settings.set_registration_enabled(db, True, updated_by="pytest")
    db.commit()
    assert client.get("/api/auth/config").json()["registration_enabled"] is True

    system_settings.set_registration_enabled(db, False, updated_by="pytest")
    db.commit()
    assert client.get("/api/auth/config").json()["registration_enabled"] is False


def test_管理员能改开关并留痕(client, auth, db, admin_user):
    """打开它的那一次必须有据可查 —— 这个开关决定「陌生人能不能往 sys_user
    里加行」，事后追的是「谁、何时、从关到开」。"""
    from app.models import AuditLog

    r = client.put("/api/system/registration", headers=auth, json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["registration_enabled"] is True
    assert client.get("/api/auth/config").json()["registration_enabled"] is True

    rows = db.scalars(select(AuditLog)
                      .where(AuditLog.action == "SYSTEM_REGISTRATION_UPDATE")).all()
    rows = [x for x in rows if (x.after or {}).get("registration_enabled") is True]
    assert rows, "开放自助注册没有留审计 —— 事后没人知道是谁开的"
    assert rows[-1].username == admin_user.username
    assert rows[-1].before["registration_enabled"] is False


@pytest.mark.parametrize("bad", ["false", 1, "是", None], ids=["字符串false", "整数1", "中文", "null"])
def test_开关只认真布尔(client, auth, bad):
    """``StrictBool`` 挡的是这类事故：把非布尔值写进去，之后被当成真。

    （``bool("false") is True`` —— 这不是假设，是 Python 的实际行为。）
    """
    r = client.put("/api/system/registration", headers=auth, json={"enabled": bad})
    assert r.status_code == 422, f"{bad!r} 被当成合法布尔收了：{r.status_code} {r.text}"
    assert client.get("/api/auth/config").json()["registration_enabled"] is False


# --------------------------------------------------------------------- 权限边界

#: (说明, 方法, 路径, 请求体) —— 普通用户每一条都必须是 403
#:
#: 路径里的 ``{other}`` 是**被操作的那个用户**（管理员）。写死成 ``1`` 是不行的：
#: 用户 id 由数据库序列分配，写死的 1 只在「这个 worker 还没建过用户」时
#: 恰好存在 —— 那种「碰巧对」会让「改**他人**资料」这条变成改一个不存在的人，
#: 名不副实。占位符在下面按真实 id 填。
FORBIDDEN = [
    ("列全部用户",   "post", "/api/system/users", {"username": "hack1", "password": "abcd1234"}),
    ("列全部账套",   "get",  "/api/system/books", None),
    ("全局审计日志", "get",  "/api/system/audit-logs", None),
    ("改他人资料",   "patch", "/api/system/users/{other}", {"password": "abcd1234"}),
    ("删他人",       "delete", "/api/system/users/{other}", None),
    ("给别人授权账套", "put", "/api/system/users/{other}/books", []),
    ("看注册开关", "get", "/api/system/registration", None),
    ("改注册开关", "put", "/api/system/registration", {"enabled": True}),
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
    make_voucher("2026-01-05", dr=[("1002", 10, "x")], cr=[("5001", 10, "x")])
    r = client.get(f"/api/books/{book.id}/audit-logs", headers=auth)
    assert r.status_code == 200
    assert any(x["action"] == "CREATE" for x in r.json())


@pytest.mark.parametrize("label,method,path,body", FORBIDDEN, ids=[c[0] for c in FORBIDDEN])
def test_普通用户一律被拒(client, plain_auth, admin_user, label, method, path, body):
    path = path.format(other=admin_user.id)
    r = getattr(client, method)(path, headers=plain_auth, **({"json": body} if body is not None else {}))
    assert r.status_code == 403, f"{label} 没有拦住：{r.status_code} {r.text}"


def test_管理员仍然畅通(client, auth):
    """收紧不能误伤管理员自己。"""
    assert client.get("/api/system/users", headers=auth).status_code == 200
    assert client.get("/api/system/books", headers=auth).status_code == 200
    assert client.get("/api/system/audit-logs", headers=auth).status_code == 200


def test_授权项缺book_id是400不是500(client, auth, book):
    """``books:[{"role":"OWNER"}]`` 原来在 ``_apply_links`` 里撞 ``KeyError``。

    同一段流程里 ``_check_roles`` 用的是 ``it.get(...)``，两处对畸形输入
    的写法不一致，于是同一个请求的缺字段表现成 500 —— 调用方看不出是自己的
    payload 不对。

    要求：给一个能指出问题的 4xx，而且**不要**因此授权成功。
    """
    r = client.post("/api/system/users", headers=auth, json={
        "username": f"缺字段{uuid.uuid4().hex[:6]}",
        "password": "abcd1234",
        "books": [{"role": "OWNER"}],
    })
    assert r.status_code == 422 or r.status_code == 400, (
        f"缺 book_id 变成了 {r.status_code}：{r.text}")
    assert 500 != r.status_code


def test_合法授权仍然正常生效(client, auth, book, db):
    """补上缺字段的防御不能把正常授权路径也堵了。"""
    name = f"正常授权{uuid.uuid4().hex[:6]}"
    r = client.post("/api/system/users", headers=auth, json={
        "username": name,
        "password": "abcd1234",
        "books": [{"book_id": book.id, "role": "ACCOUNTANT"}],
    })
    assert r.status_code == 201, r.text
    user = db.scalar(select(SysUser).where(SysUser.username == name))
    assert user is not None
    link = db.scalar(select(UserBook).where(
        UserBook.user_id == user.id, UserBook.book_id == book.id))
    assert link is not None and link.role == "ACCOUNTANT"


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
