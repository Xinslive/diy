"""``/api/system/*`` 的**穷举式**门禁守卫 + 账号管理面的几条硬闸。

审计 P3-1：「``/api/system/*`` 仍没有穷举式管理员门禁守卫测试」。

**为什么这条文件值得单独存在。** 过去 CI 里断言管理员门禁的只有两份**手写
名单**：``test_viewer_matrix.py`` 把 ``/api/system/`` 整个前缀豁免掉，
``test_auth_permissions.py`` 的 ``FORBIDDEN`` 是 8 条。这两份名单的共同点是
**它们不会因为多了一条路由而变红** —— 于是「新增一条 system 路由、忘了挂
管理员门禁」这件事在 CI 里是完全静默的，而那类路由的典型形态正是
「开号、改他人口令、改授权、读全量审计、读 OSS 凭据」。

这份文件的做法是**摊平路由表、逐条打一遍**：

* **穷举**：路由表里每一条 ``/api/system/*`` 都要被非管理员打一次，
  断言 401/403。新增一条路由而忘了挂门禁 ⇒ 这里立刻红。
* **不靠手写名单**：路由是从 ``app.routes`` 里读出来的，不是抄进来的。
* **门禁在依赖图里**（``APIRouter(dependencies=[Depends(require_admin)])``），
  所以它在**请求体校验之前**就拦 —— 非管理员拿到的永远是 403/401，
  不会因为「请求体填错了」而变成 422（那会让这条测试变成在测入参校验）。

同一份文件里还钉住了账号管理面的几条硬闸：最后一个管理员不许删（R2-1）、
口令策略建号与重置同口径（P2-1）、``real_name`` 超长是 422 不是 500（P2-6）、
用户名含换行被拒且**审计日志里不多出伪造行**（P2-7）。
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.models import AuditLog, SysUser, SystemSetting
from app.notify import mfa as mfa_service

# --------------------------------------------------------------------- 路由表


def _iter_routes():
    """摊平 ``app.routes``，**把 include_router 进来的子路由也取出来**。

    FastAPI 0.143 的 ``app.routes`` 里，被 ``include_router`` 进来的路由
    是一个 ``_IncludedRouter`` 对象而不是被摊平的 ``APIRoute`` —— 只看
    ``app.routes`` 会得到「一条系统路由都没有」，而这个文件要是照着那个
    写，就会变成**一条都测不到却全绿**的守卫测试（比没有还糟）。
    """
    from app.main import app

    for route in app.routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from original.routes
        else:
            yield route


def _system_routes() -> list[tuple[str, str]]:
    """摊平出所有 ``/api/system/*`` 的 (方法, 路径)。

    **从 ``app.routes`` 里读，不是手抄的** —— 这是这份测试的全部意义：
    路由表里多一条，这里就多打一次。
    """
    out: list[tuple[str, str]] = []
    for route in _iter_routes():
        path = getattr(route, "path", "")
        if not path.startswith("/api/system"):
            continue
        methods = sorted(m for m in (getattr(route, "methods", None) or set())
                         if m not in {"HEAD", "OPTIONS"})
        for method in methods:
            out.append((method, path))
    return sorted(out, key=lambda item: (item[1], item[0]))


#: 路径参数占位符 → 一个**合法的**取值。
#:
#: 必须是合法的（``user_id`` 得是整数），否则会先吃一个 422，
#: 而 422 会让「门禁有没有生效」这件事测不出来（门禁在依赖图里，
#: 本该在参数转换之前就拦下来 —— 所以这里传错类型反而能**证明**
#: 门禁确实在参数校验之前）。
_PATH_VALUES = {
    "user_id": "1",
    "book_id": "1",
    "plugin_id": "no-such-plugin",
    "task_name": "no-such-task",
    "name": "x",
}


def _fill(path: str) -> str:
    def repl(match: "re.Match[str]") -> str:
        name = match.group(1)
        return _PATH_VALUES.get(name, "x")

    return re.sub(r"\{([^}:]+)[^}]*\}", repl, path)


def _request(client, method: str, path: str, headers: dict | None):
    kwargs: dict = {"headers": headers or {}}
    if method in {"POST", "PUT", "PATCH"}:
        # 门禁在依赖图里 ⇒ 请求体**不会被校验**，所以这里给什么都不会
        # 先吃一个 422。给 ``{}`` 是为了不依赖 multipart 之类的东西。
        kwargs["json"] = {}
    return getattr(client, method.lower())(_fill(path), **kwargs)


#: 路由表不能是空的 —— 空了就说明这个文件什么也没测（而 CI 照样绿）。
def test_路由表不是空的():
    routes = _system_routes()
    assert len(routes) >= 25, f"只摊出 {len(routes)} 条 /api/system 路由，摊平逻辑坏了"
    assert any(path.endswith("/users") for _m, path in routes)


def test_每条系统路由都要求认证():
    """**没登录 ⇒ 一律 401。**

    静态检查（依赖图里有 ``get_current_user``）＋ 动态检查（真的打一遍）。
    静态那一半能在**不依赖请求体**的情况下覆盖全部路由；动态那一半兜底。
    """
    checked = 0
    for route in _iter_routes():
        path = getattr(route, "path", "")
        if not path.startswith("/api/system"):
            continue
        names = _dependency_names(route.dependant)
        assert "get_current_user" in names, (
            f"{path} 的依赖里没有 get_current_user —— 这条路由不需要登录就能调")
        checked += 1
    assert checked >= 25


def _dependency_names(dependant) -> set[str]:
    """摊平依赖图里的**函数名**（含传递依赖）。"""
    names: set[str] = set()
    for dep in getattr(dependant, "dependencies", []) or []:
        call = getattr(dep, "call", None)
        names.add(getattr(call, "__name__", str(call)))
        names |= _dependency_names(dep)
    return names


# --------------------------------------------------------------------- 门禁


@pytest.mark.parametrize("method,path", _system_routes(),
                         ids=[f"{m} {p}" for m, p in _system_routes()])
def test_普通用户拿不到任何系统路由(client, plain_auth, method, path):
    """**穷举**：每一条 ``/api/system/*``，普通用户都是 403。

    这是 P3-1 要的那条守卫：**新增端点时这条测试会红**。
    """
    r = _request(client, method, path, plain_auth)
    assert r.status_code in (401, 403), (
        f"{method} {path} 对普通用户返回了 {r.status_code}（{r.text[:200]}）—— "
        "系统管理面漏了管理员门禁")


@pytest.mark.parametrize("method,path", _system_routes(),
                         ids=[f"{m} {p}" for m, p in _system_routes()])
def test_没登录拿不到任何系统路由(client, method, path):
    """没登录一律 401 —— **包括写操作**。"""
    r = _request(client, method, path, None)
    assert r.status_code in (401, 403), (
        f"{method} {path} 匿名返回了 {r.status_code}（{r.text[:200]}）")


# --------------------------------------------------------------------- 最后一个管理员
#
# **判据只有一处**：``api/system.py::_active_admin_count`` ——
# 数「库里还剩几个 is_admin=True **且** active=True 的账号」。
#
# ⚠️ **两条路共用它，而真正可达的那条是「停用」**：
#
# * **删除**这条路：执行者自己就是一个在用的管理员，算上他永远还剩至少
#   一个 ⇒ 「删掉之后变成 0」在接口上**结构性地不可达**。守卫仍然留着
#   （第二层：将来若放宽「不能删自己」那条，它就是拦住的那道）。
# * **停用**这条路可达：把**最后一个**在用的管理员停用（**包括把自己
#   停用**）⇒ 0 个在用的管理员 ⇒ 没有人能开号、改口令、授权 ⇒ 管理面
#   锁死，唯一出口是运维跑脚本。
#
# 所以下面两组用例分别钉住这两条路。

def _admin_count(db) -> int:
    from sqlalchemy import func

    return int(db.scalar(select(func.count()).select_from(SysUser).where(
        SysUser.is_admin.is_(True), SysUser.active.is_(True))) or 0)


@pytest.fixture
def 只有这一个管理员(db, admin_user):
    """把库里的**其它管理员**清掉 —— 让「最后一个管理员」这件事是确定的。

    **为什么需要它**：测试库是按 worker 复用的，而别的用例建的管理员是
    **提交**过的（``db`` 夹具只回滚）。于是「库里现在有几个管理员」这件事
    取决于前面跑过什么 —— 守卫本身是对的，用例却会时绿时红。
    """
    db.execute(
        SysUser.__table__.delete().where(
            SysUser.id != admin_user.id, SysUser.is_admin.is_(True)))
    db.commit()
    db.expire_all()
    return admin_user


def test_停用最后一个管理员会被拒(client, db, auth, 只有这一个管理员):
    """R2-1（可达的那条路）：把最后一个在用的管理员停用 ⇒ 409。

    停用之后没有人能开号、改口令、授权 —— **系统管理面就此锁死**。
    """
    admin_user = 只有这一个管理员
    assert _admin_count(db) == 1
    r = client.patch(f"/api/system/users/{admin_user.id}", headers=auth,
                     json={"active": False})
    assert r.status_code == 409, f"停用最后一个管理员没有被拦：{r.status_code} {r.text[:200]}"
    assert "管理员" in r.json()["detail"]
    db.expire_all()
    assert db.get(SysUser, admin_user.id).active is True, "账号还是被停用了"


def test_停用自己作为最后一个管理员会被拒(client, db, auth, 只有这一个管理员):
    """**自己停用自己**是同一个洞的另一扇门 —— 它必须一样被拦。"""
    admin_user = 只有这一个管理员
    r = client.patch(f"/api/system/users/{admin_user.id}", headers=auth,
                     json={"active": False})
    assert r.status_code == 409, r.text
    db.expire_all()
    assert db.get(SysUser, admin_user.id).active is True


def test_还有别的管理员时停用是允许的(client, db, auth, 只有这一个管理员):
    """**还剩一个**管理员时，停用另一个是允许的 —— 守卫不能挡成「不许停用管理员」。"""
    admin_user = 只有这一个管理员
    other = SysUser(username="备用管理员", password_hash=admin_user.password_hash,
                    real_name="备用管理员", active=True, is_admin=True)
    db.add(other)
    db.commit()
    db.refresh(other)
    r = client.patch(f"/api/system/users/{other.id}", headers=auth,
                     json={"active": False})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(SysUser, other.id).active is False


def test_删掉一个管理员但还剩一个是允许的(client, db, auth, 只有这一个管理员):
    """删一个**别的**管理员是允许的 —— 执行者自己就还剩一个。"""
    admin_user = 只有这一个管理员
    other = SysUser(username="备用管理员2", password_hash=admin_user.password_hash,
                    real_name="备用管理员2", active=True, is_admin=True)
    db.add(other)
    db.commit()
    db.refresh(other)
    r = client.delete(f"/api/system/users/{other.id}", headers=auth)
    assert r.status_code == 200, r.text
    # 用 count 查而不是 ``db.get``：行已经被**另一个会话**删掉了，
    # 拿会话里那个陈旧对象再 ``get`` 一次会抛 ObjectDeletedError ——
    # 那说的是「这个对象过期了」，不是「行还在」。
    from sqlalchemy import func

    assert db.scalar(select(func.count()).select_from(SysUser)
                     .where(SysUser.id == other.id)) == 0


def test_删普通账号不影响管理员数(client, db, auth, 只有这一个管理员, plain_user):
    """**普通账号**被删到只剩一个管理员，是允许的。

    「能不能删最后一个管理员」与「这个账号是不是管理员」是两件事。
    """
    before = _admin_count(db)
    r = client.delete(f"/api/system/users/{plain_user.id}", headers=auth)
    assert r.status_code == 200, r.text
    assert _admin_count(db) == before


def test_停用普通账号不受这个守卫管(client, db, auth, plain_user):
    """守卫只看管理员 —— 普通账号停用是日常操作，不能被卡住。"""
    r = client.patch(f"/api/system/users/{plain_user.id}", headers=auth,
                     json={"active": False})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------- 口令策略


def test_建号与重置两条路径的下限一致(client, db, auth, admin_user, plain_user):
    """P2-1：两条路径**同一个下限**。

    过去建号是 6 位、重置**没有**下限 —— 而「管理员把某个账号的口令重置成
    1 个字符，系统照收」正是最危险的那条。现在两边共用一套判据。
    """
    weak = "a1"                      # 2 位：两条路径都必须拒
    r = client.post("/api/system/users", headers=auth,
                    json={"username": "weak_create_user", "password": weak})
    assert r.status_code == 422, f"建号路径收了 2 位口令：{r.status_code} {r.text[:200]}"

    r = client.patch(f"/api/system/users/{plain_user.id}", headers=auth,
                     json={"password": weak})
    assert r.status_code == 422, f"重置路径收了 2 位口令：{r.status_code} {r.text[:200]}"


def test_只有一类字符的口令被拒(client, auth, admin_user):
    """复杂度下限：至少两类字符。「aaaaaaaa」看着长，字典一撞就开。"""
    r = client.post("/api/system/users", headers=auth,
                    json={"username": "weak_class_user", "password": "aaaaaaaa"})
    assert r.status_code == 422, f"单一类字符的口令被收了：{r.status_code} {r.text[:200]}"


def test_合规的口令能建号也能重置(client, db, auth, admin_user, plain_user):
    r = client.post("/api/system/users", headers=auth,
                    json={"username": "strong_user_1", "password": "abcd1234"})
    assert r.status_code == 201, r.text
    r = client.patch(f"/api/system/users/{plain_user.id}", headers=auth,
                     json={"password": "abcd1234"})
    assert r.status_code == 200, r.text


def test_只改姓名不触发口令校验(client, db, auth, plain_user):
    """**存量用户不受影响**：改别的字段时**不**要求重设口令。

    历史口令可能只有 6 位，而「改个电话也要重设口令」是把存量用户逼着
    改口令的最坏理由 —— 顺带还把「管理员随手改个备注」变成一次故障。
    """
    db.execute(
        SysUser.__table__.update()
        .where(SysUser.id == plain_user.id)
        .values(password_hash="pbkdf2_sha256$1$old$old")
    )
    db.commit()
    r = client.patch(f"/api/system/users/{plain_user.id}", headers=auth,
                     json={"real_name": "改了名字"})
    assert r.status_code == 200, f"只改姓名被口令策略挡住了：{r.status_code} {r.text[:200]}"
    db.refresh(plain_user)
    assert plain_user.real_name == "改了名字"
    assert plain_user.password_hash == "pbkdf2_sha256$1$old$old", "只改姓名不该动口令"


# --------------------------------------------------------------------- 入参形状


def test_real_name超长是422不是500(client, auth):
    """P2-6：超长 ⇒ **422**，不是 500。

    500 的文案是「数据库内部错误」—— 那是一句纯入参问题，却长得像数据库
    故障，把排障方向整个带偏（库里那一列是 ``String(64)``）。
    """
    for payload in ({"username": "len_user_1", "password": "abcd1234",
                     "real_name": "名" * 65},
                    {"username": "len_user_2", "password": "abcd1234",
                     "real_name": "名" * 200}):
        r = client.post("/api/system/users", headers=auth, json=payload)
        assert r.status_code == 422, (
            f"real_name 长度 {len(payload['real_name'])} 返回了 {r.status_code}："
            f"{r.text[:200]}")


def test_real_name超长改也是422(client, auth, plain_user):
    r = client.patch(f"/api/system/users/{plain_user.id}", headers=auth,
                     json={"real_name": "名" * 65})
    assert r.status_code == 422, f"改超长姓名返回了 {r.status_code}：{r.text[:200]}"


def test_用户名含换行被拒且审计里不多出伪造行(client, db, auth):
    """P2-7：用户名含换行 ⇒ 422，**且审计日志里不多出一行**。

    用户名会原样进审计行的正文与告警邮件的正文，一个换行就能把一行撑成
    两行 —— 看起来像多了一条记录。这一条钉的是「**没有记录被写进去**」，
    不只是「请求被拒」。
    """
    before = db.scalar(select(AuditLog).where(AuditLog.action == "CREATE_USER")
                        .order_by(AuditLog.id.desc()).limit(1))
    before_id = before.id if before else 0

    r = client.post("/api/system/users", headers=auth,
                    json={"username": "evil\nFORGED LOG LINE", "password": "abcd1234"})
    assert r.status_code == 422, f"含换行的用户名返回了 {r.status_code}：{r.text[:200]}"

    rows = db.scalars(select(AuditLog).where(AuditLog.action == "CREATE_USER",
                                             AuditLog.id > before_id)).all()
    assert not rows, "含换行的用户名仍然写进了审计日志"


@pytest.mark.parametrize("bad", ["a b", "a\tb", "a\x00b", "a\x7fb"])
def test_用户名的形状约束(client, auth, bad):
    """空白与控制字符一律拒（换行是其中最要紧的一个）。"""
    r = client.post("/api/system/users", headers=auth,
                    json={"username": bad, "password": "abcd1234"})
    assert r.status_code == 422, f"用户名 {bad!r} 返回了 {r.status_code}：{r.text[:200]}"


def test_中文用户名仍然允许(client, db, auth):
    """**放行中文**：这套系统里中文姓名是常态，而「用户名必须 ASCII」没有
    业务依据。把中文挡在门外只是把「防日志注入」做成了「防中文」。
    """
    r = client.post("/api/system/users", headers=auth,
                    json={"username": "会计张三", "password": "abcd1234",
                          "real_name": "张三"})
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------- 二次验证开关


@pytest.fixture(autouse=True)
def _clean_mfa_setting(db):
    """每个用例前后都清掉 ``sys_setting`` 里那个二次验证开关**与 SMTP 通道状态**。

    **必须清开关**：这个开关一开，管理员的登录与高危动作就都要走邮箱二次验证
    —— 它会**污染同 worker 里后面的每一个用例**（xdist 按 worker 分库，
    症状与「测试自己互相污染」一模一样，但方向相反）。所以清理是每个用例
    前后各一次。

    **必须清通道状态**：本文件里有两条用例会 ``set_value`` 写
    ``smtp_host`` / ``notify_recipients`` 并 **commit** —— commit 之后夹具
    的 rollback 撤不回来，那两行会留在会话级测试库里。而
    ``test_通道没就绪时开不了二次验证`` 要求通道**没配**。CI 用
    ``pytest -n 3``、本机用 ``-n 6``，用例顺序不定：写状态的那两条排在
    前面时，后面那条就会看到「通道已就绪」，接口返回 200 而不是 409，
    这条断言就成了掷骰子。

    2026-10-21 在 CI 上真的红过一次（run 39523024253）::

        FAILED tests/test_system_user_guard.py::test_通道没就绪时开不了二次验证
        AssertionError: 没有 SMTP 却开成了：200 {... "smtp_ready": true,
        "recipient_hint": "o***s@x.com" ...}

    ``o***s@x.com`` 就是那条用例写的 ``boss@example.com`` —— 泄漏源一眼可见。

    治法是**隔离共享状态**，不是调松断言（同 conftest 里
    ``_reset_notify_budget`` 治的是同一类病，只是那里的共享状态在进程内存里、
    这里在数据库里）。清完之后，那条用例测的才是它自己声明的性质
    ——「通道没配时开不了」，而不是「数据库碰巧残留了什么」。
    """
    #: 这几行合起来决定「通道就绪」：smtp_ready() = 有 host 且有收件人。
    #: 少清一个都可能让「通道没就绪」那条用例变成掷骰子。
    _CHANNEL_KEYS = ("smtp_host", "smtp_port", "smtp_username",
                     "smtp_password", "smtp_from", "smtp_use_ssl",
                     "smtp_use_tls", "notify_recipients")

    def wipe():
        row = db.get(SystemSetting, mfa_service.MFA_ENABLED_KEY)
        if row is not None:
            db.delete(row)
        for key in _CHANNEL_KEYS:
            row = db.get(SystemSetting, key)
            if row is not None:
                db.delete(row)
        db.commit()

    wipe()
    yield
    wipe()


def test_二次验证状态可读且不含任何凭据(client, auth):
    """GET /api/system/mfa：只有可展示信息，**没有口令/密钥**。"""
    r = client.get("/api/system/mfa", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("enabled", "active", "grace_minutes", "smtp_ready",
                "recipient_count", "channel_error"):
        assert key in body, f"响应里少了 {key}"
    raw = r.text.lower()
    for forbidden in ("password", "secret", "token", "access_key"):
        assert forbidden not in raw, f"响应里出现了 {forbidden}"


def test_通道没就绪时开不了二次验证(client, db, auth):
    """**开关打开而 SMTP 没就绪 ⇒ 明确拒绝。**

    不让管理员点一下开关就把所有管理员锁在门外：验证链接发不出去 ⇒
    管理员登不进来 ⇒ 管理面锁死（而且当场就发作）。

    **前提自己摆平，再自己断言一遍。** 同文件的另两条用例会 ``set_value``
    写 SMTP 配置并 commit，那两行会跨用例存活（夹具的 rollback 只回滚它自己
    那个会话）。CI 用 ``pytest -n 3``、本机用 ``-n 6``，用例顺序不定 ——
    前提靠环境碰巧，这条断言就变成了掷骰子。所以这里先把通道状态清干净，
    再断言它**确实是未就绪**，然后才测接口的拒绝：前提不成立时这句话会自己
    喊出来，而不是让接口的 200 冒充「没有 SMTP 却开成了」。
    """
    for key in ("smtp_host", "smtp_port", "smtp_username", "smtp_password",
                "smtp_from", "smtp_use_ssl", "smtp_use_tls",
                "notify_recipients"):
        row = db.get(SystemSetting, key)
        if row is not None:
            db.delete(row)
    db.commit()

    assert not mfa_service.smtp_ready(db), "通道状态没清干净，前提不成立"

    r = client.put("/api/system/mfa", headers=auth, json={"enabled": True})
    assert r.status_code == 409, f"没有 SMTP 却开成了：{r.status_code} {r.text[:200]}"
    assert "SMTP" in r.json()["detail"]


def test_通道就绪后可以开二次验证(client, db, auth):
    from app.core import system_settings

    system_settings.set_value(db, "smtp_host", "smtp.example.com", updated_by="test")
    system_settings.set_value(db, "notify_recipients", "boss@example.com", updated_by="test")
    db.commit()

    r = client.put("/api/system/mfa", headers=auth, json={"enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    assert r.json()["active"] is True
    assert r.json()["smtp_ready"] is True


def test_开关开着时高危动作要近期重新认证(client, db, auth, admin_user):
    """**二次验证开着 ⇒ 高危动作要「近期重新认证」**。

    这一条是「把二次验证挂到高危动作上」这个整件事的价值所在：光有开关
    而没有接线，等于一个假开关。

    （关着的时候这条依赖**不拦人** —— 二次验证是可选的组织决策，
    默认关；这一点由上面那条「通道没就绪时开不了」与这里一起钉住。）
    """
    from app.core import system_settings

    system_settings.set_value(db, "smtp_host", "smtp.example.com", updated_by="test")
    system_settings.set_value(db, "notify_recipients", "boss@example.com", updated_by="test")
    db.commit()
    assert client.put("/api/system/mfa", headers=auth,
                      json={"enabled": True}).status_code == 200

    # 开关开着，而这个令牌**没有** mfa_at（它是在开关关着的时候签发的）
    r = client.patch(f"/api/system/users/{admin_user.id}", headers=auth,
                     json={"real_name": "改个名字"})
    assert r.status_code == 403, f"开关开着却放行了高危动作：{r.status_code} {r.text[:200]}"
    assert "二次验证" in r.json()["detail"]


# --------------------------------------------------------------------- 备份配置


def test_保存OSS配置时拒掉明文endpoint(client, auth):
    """P1-1：**保存时就拒**明文 endpoint，不要等到凌晨三点备份失败才发现。"""
    r = client.put("/api/system/backup", headers=auth,
                   json={"endpoint": "http://oss-cn-hangzhou.aliyuncs.com"})
    assert r.status_code == 400, f"明文 endpoint 被收了：{r.status_code} {r.text[:200]}"
    assert "https" in r.json()["detail"]


def test_登录防护状态可读(client, auth):
    """P2-3：分桶口径**显式化** —— 管理员能看到「是不是全站一个桶」。"""
    r = client.get("/api/system/login-protection", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "source_bucketing" in body
    assert "shared_bucket" in body["source_bucketing"]
    assert "account" in body and "delay_ladder_seconds" in body["account"]
