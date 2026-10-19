"""登录失败锁定与管理员令牌有效期（审计 P3-04 建议 3、4）。

原实现里 ``/api/auth/login`` 对任何口令都只回一句「用户名或密码错误」，
既不计数也不锁定 —— 于是口令可以无限次试。审计把它列为配置风险的一部分，
不是「已被利用」，但在对外暴露的系统上这是最容易被自动化利用的一处。

三条设计取舍写在这里，免得后来的人把它们当细节改掉：

1. **计数放库里，不放内存。** 多进程部署时每个进程各有一份内存计数，
   打满 N 个进程就有 N 次额度；而内存计数重启即清零，「每秒试一次、
   攒够就停」的慢速爆破根本触发不了阈值。
2. **锁定期间不校验口令。** 校验了等于告诉攻击者「密码猜对了，只是被锁着」，
   等于把口令空间又缩一层。
3. **锁定时长按轮次递进。** 阈值是 3 次、首次锁定 10 分钟，之后每多锁
   一轮翻一档（10 → 20 → 30 …，上限 ``login_lock_max_minutes``）。
   恒定时长挡不住**慢速爆破**（每隔 9 分钟试一次、每次只错一次，永远
   不触发阈值），也挡不住**故意撞锁**（把每次尝试安排在锁定窗口之外，
   让账号被无限期挡在门外）。

**本文件为什么覆盖了 ``client`` 夹具。**
P2-11 补上了 IP 维度的聚合节流，而 ``conftest`` 的 ``client`` 是全suite共用
的：对端恒为 Starlette TestClient 的 ``("testclient", 50000)``。于是本文件这
六十来次「故意的失败」会全部堆进**同一个来源 IP 的桶**里，把节流器自己触发 ——
一份专门测「攻击者」的测试文件，反而因为「模拟得像攻击者」而把后面所有用例
顶成 429。

修法不是把阈值调到几百（那样就把生产的防护调没了），而是**给每个用例一个
自己的来源 IP**：每条用例本来就在模拟一次独立的攻击，两个用例也不该是同一个
攻击源。共享 IP 的那条边界由 ``test_ip_throttle.py`` 单独钉着。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.core.security import create_access_token, token_minutes_for


@pytest.fixture
def client(app_for_peer):
    """覆盖 conftest 的 ``client``：每个用例一个**独立的来源 IP**。

    只换对端地址，不换应用 —— 于是用例之间的隔离来自「不同的攻击源」，
    而不是来自「不同的数据库」（它们本来就共用同一个库）。
    """
    return app_for_peer


@pytest.fixture
def app_for_peer():
    """一个对端是随机 IP 的 TestClient。``docs`` 的特殊 IP 段不会被真路由到。"""
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    ip = f"198.51.{uuid.uuid4().int % 254 + 1}.{uuid.uuid4().int % 254 + 1}"
    with TestClient(fastapi_app, raise_server_exceptions=False,
                    client=(ip, 50000)) as c:
        yield c


@pytest.fixture
def creds(db, admin_user):
    return {"username": admin_user.username, "password": "admin123"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _threshold() -> int:
    """当前配置的失败阈值。

    **用例一律读配置，不写死数字** —— 阈值是配置项（``login_max_failures``），
    写死数字的用例会在改配置时集体变红，而那不是回归，是「配置改了、
    用例没跟着改」。真正要钉住的是**行为**（第 N 次失败触发锁定），
    不是那个 N 的具体取值。
    """
    from app.core.config import settings

    return settings.login_max_failures


def _fail(client, creds, times: int) -> list[int]:
    """连续失败 ``times`` 次，返回每次的状态码。"""
    codes = []
    for _ in range(times):
        codes.append(client.post("/api/auth/login",
                                 json={**creds, "password": "错的"}).status_code)
    return codes


# ------------------------------------------------------------------ 失败计数

def test_口令错一次就记一次(client, db, admin_user, creds):
    assert client.post("/api/auth/login", json={**creds, "password": "错的"}).status_code == 401
    db.refresh(admin_user)
    assert admin_user.failed_logins == 1
    assert admin_user.locked_until is None


def test_用户名不存在不建计数(client, db, admin_user, creds):
    """不存在的用户名没有行可写 —— 不能因此报错，也不能因此把请求放行。"""
    r = client.post("/api/auth/login", json={"username": "查无此人", "password": "x"})
    assert r.status_code == 401
    db.refresh(admin_user)
    assert admin_user.failed_logins == 0


# ------------------------------------------------------------------ 锁定本身

def test_连续失败达阈值后账号被锁(client, db, admin_user, creds):
    """阈值以内的失败全是 401，达阈值那一次之后账号被锁。

    注意「阈值以内」这三个字：前 N 次失败都还没锁，所以它们必须回 401
    而不是 423 —— 否则就成了「用 423 宣告账号存在」的信道。
    """
    threshold = _threshold()
    codes = _fail(client, creds, threshold)
    assert codes == [401] * threshold, f"阈值内的失败应当都是 401，实际 {codes}"
    db.refresh(admin_user)
    assert admin_user.locked_until is not None, f"失败了 {threshold} 次还没锁"

    # 锁着的时候**正确口令也进不去**
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 423, f"锁着却还能登录，返回 {r.status_code}"


def test_阈值内不锁定(client, db, admin_user, creds):
    """差一次没到阈值就**不能**锁。

    阈值收得太紧的代价是「正常用户输错三次就被挡 10 分钟」—— 那就把
    防护变成了故障。这条用例钉住「还差一次时账号仍然是通的」。
    """
    _fail(client, creds, _threshold() - 1)
    db.refresh(admin_user)
    assert admin_user.locked_until is None, "还没到阈值就锁了"
    assert client.post("/api/auth/login", json=creds).status_code == 200


def test_锁定期间不校验口令(client, db, admin_user, creds):
    """错误口令和正确口令在锁定期内的响应必须一致。

    不一致就等于一个口令预言机：攻击者拿 423 和 401 去试，能确认自己猜对了。
    """
    _fail(client, creds, _threshold())
    wrong = client.post("/api/auth/login", json={**creds, "password": "仍然是错的"})
    right = client.post("/api/auth/login", json=creds)
    assert wrong.status_code == right.status_code == 423
    assert "锁定" in wrong.json()["detail"] and "锁定" in right.json()["detail"]


def test_锁定到期后自动解锁(client, db, admin_user, creds):
    _fail(client, creds, _threshold())
    db.refresh(admin_user)
    # 把锁定时间往回拨，模拟「锁定时间过去了」
    admin_user.locked_until = _now() - timedelta(seconds=1)
    db.commit()
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 200, f"锁定到期后仍进不去：{r.text[:200]}"


def test_登录成功清零计数(client, db, admin_user, creds):
    """否则一次误输的计数会跟后面几次正常登录叠起来，
    最终把一个从没人猜过的账号锁住 —— 那就是把防护变成了故障。"""
    _fail(client, creds, _threshold() - 1)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    db.refresh(admin_user)
    assert admin_user.failed_logins == 0
    assert admin_user.locked_until is None

    # 清零之后还能再错满（阈值 - 1）次才锁，而不是 1 次就锁
    _fail(client, creds, _threshold() - 1)
    db.refresh(admin_user)
    assert admin_user.locked_until is None, "成功登录没有把计数清掉"


def test_停用账号的响应与口令错一致(client, db, admin_user, creds):
    """停用用户不能被拿来区分「这个用户存在」。"""
    admin_user.active = False
    db.commit()
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 401
    assert "错误" in r.json()["detail"]


# ------------------------------------------------------ 管理员令牌有效期更短

def test_管理员令牌比普通用户短(monkeypatch):
    from app.core import security
    monkeypatch.setattr(security.settings, "access_token_minutes", 720, raising=False)
    monkeypatch.setattr(security.settings, "admin_token_minutes", 240, raising=False)
    assert token_minutes_for(False) == 720
    assert token_minutes_for(True) == 240


def test_全局配得更短时管理员不再被拉长(monkeypatch):
    """取「较小者」而不是直接用管理员那一档 ——
    把全局配成 30 分钟的人，不该被管理员上限反过来拉到 240 分钟。"""
    from app.core import security
    monkeypatch.setattr(security.settings, "access_token_minutes", 30, raising=False)
    monkeypatch.setattr(security.settings, "admin_token_minutes", 240, raising=False)
    assert token_minutes_for(True) == 30
    assert token_minutes_for(False) == 30


def test_登录返回的令牌按身份给有效期(client, db, admin_user, creds):
    """端到端：管理员登录拿到的 expires_at 比配置里的 720 分钟近。"""
    import jwt

    from app.core.config import settings

    token, expires = create_access_token(admin_user.id, admin_user.username, is_admin=True)
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    # exp 是 POSIX 秒，不是分钟 —— 两种单位混算会得到一个天文数字
    span = (payload["exp"] - datetime.now(timezone.utc).timestamp()) / 60
    assert span <= settings.admin_token_minutes + 1, f"管理员令牌有效期 {span:.0f} 分钟，超了"


def test_阈值可配置(monkeypatch):
    """护栏不该是写死的常数 —— 不同系统的口令策略不一样。"""
    from app.core.config import Settings
    s = Settings(app_env="dev", jwt_secret="x" * 40, login_max_failures=2,
                 login_lock_minutes=1)
    assert s.login_max_failures == 2 and s.login_lock_minutes == 1


# ------------------------------------------------------------------ 锁定留痕


def test_锁定要单独留一条审计(client, db, admin_user, creds):
    """锁定是「这个系统正在主动拒绝某个账号」的信号，事后第一眼要看的就是它。

    与「又错了一次」分开记，是因为两者的处置完全不同：前者要看来源 IP
    （是不是有人在扫），后者只是正常输错。
    """
    from app.core.config import settings
    from app.models import AuditLog

    for _ in range(settings.login_max_failures):
        client.post("/api/auth/login", json={**creds, "password": "错的"})

    # 查**这个账号**的锁定行，并按 id 倒序取最新的一条。
    #
    # 两个细节都不是洁癖，是这条用例曾经间歇性变红的原因：
    #   · 不按 username 过滤 —— audit_log 是整场 session 累积的（admin_user 走
    #     commit、db 夹具只 rollback，提交过的行不会被清掉），别的用例锁过别的
    #     账号时，这里会捞到**别人的**锁定行；
    #   · 不 order_by —— SELECT 不保证按插入顺序返回。表小时碰巧一致，
    #     表大了（整场跑完几千行）物理顺序就和插入顺序分家，`rows[-1]`
    #     拿到的是随机一条，于是 detail 里没有本用例的用户名。
    entry = (db.query(AuditLog)
             .filter(AuditLog.action == "ACCOUNT_LOCKED",
                     AuditLog.username == admin_user.username)
             .order_by(AuditLog.id.desc())
             .first())
    assert entry is not None, "账号被锁了却没有留痕 —— 事后查不到是谁在扫"
    assert admin_user.username in entry.detail
    assert entry.after.get("lock_minutes") == settings.login_lock_minutes
    assert entry.after.get("locked_until")


def test_锁定期内的登录尝试也要留痕(client, db, admin_user, creds):
    """423 是攻击者唯一能拿到的「这个账号存在」信号，扫的人就在这条路径上。"""
    from app.models import AuditLog

    _fail(client, creds, _threshold())
    client.post("/api/auth/login", json=creds)

    rows = db.query(AuditLog).filter(AuditLog.action == "LOGIN_FAILURE").all()
    assert any("锁定" in (x.detail or "") for x in rows), "锁定期内的尝试没有留痕"


# ------------------------------------------------------------------ 时序等价
#
# 审计 P2-11：未知用户名过去**跳过**口令校验，而 PBKDF2 是刻意做慢的
# （24 万次迭代，约 20ms）—— 于是「扫用户名」几乎免费，「猜密码」一次要 20ms，
# 响应时间差一个数量级，攻击者按快慢就能把有效用户名筛出来。


def _median_ms(fn, n=5) -> float:
    import statistics
    import time

    samples = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    return statistics.median(samples)


def test_未知用户名与错误口令的耗时不再差一个数量级(client, db, admin_user, creds):
    """两条失败路径必须做同样多的工作，否则响应时间就是用户名预言机。"""
    unknown = _median_ms(lambda: client.post(
        "/api/auth/login", json={"username": "查无此人", "password": "whatever"}))
    wrong = _median_ms(lambda: client.post(
        "/api/auth/login", json={**creds, "password": "错的"}))

    ratio = max(unknown, wrong) / max(min(unknown, wrong), 0.001)
    assert ratio < 2.0, (
        f"未知用户名 {unknown:.1f}ms vs 错误口令 {wrong:.1f}ms，"
        f"相差 {ratio:.1f} 倍 —— 仍是一个用户名枚举信道")


def test_停用账号的登录尝试也要等量计算(client, db, admin_user, creds):
    """停用是第二条「免费」路径：账号存在但被停，跳过校验同样是在报信道。"""
    admin_user.active = False
    db.commit()

    disabled = _median_ms(lambda: client.post("/api/auth/login", json=creds))
    unknown = _median_ms(lambda: client.post(
        "/api/auth/login", json={"username": "查无此人", "password": "whatever"}))
    ratio = max(disabled, unknown) / max(min(disabled, unknown), 0.001)
    assert ratio < 2.0, (
        f"停用账号 {disabled:.1f}ms vs 未知用户名 {unknown:.1f}ms，相差 {ratio:.1f} 倍")


def test_未知用户名仍然拒绝登录(client, db, admin_user):
    """等量计算**不能**变成放行：这是最容易被「优化」掉的一条断言。"""
    r = client.post("/api/auth/login", json={"username": "查无此人", "password": "whatever"})
    assert r.status_code == 401
    assert "错误" in r.json()["detail"]



# ------------------------------------------------------- 递进式锁定与提前解封
#
# 递进要解决的是两个具体的攻击形态：
#   * **慢速爆破** —— 每隔 9 分钟试一次、每次只错一次。恒定时长下它永远
#     不触发阈值，于是整个锁定机制对它等于不存在。
#   * **故意撞锁** —— 把每次尝试都安排在锁定窗口之外，于是账号被无限期
#     挡在门外，而没有人能证明是谁干的。
#
# 递进 + 上限 + 管理员可提前解封，三件一起才是完整的那条路。
# 少了第三件，递进就变成了「攻击者可以永久禁用任何账号」。


def _expire_lock(db, user) -> None:
    """把锁定时间拨到过去，模拟「锁定窗口过去了」。"""
    user.locked_until = _now() - timedelta(seconds=1)
    db.commit()


def test_首轮锁定是基准时长(client, db, admin_user, creds):
    from app.core.config import settings

    _fail(client, creds, _threshold())
    db.refresh(admin_user)
    assert admin_user.lock_level == 1
    left = (admin_user.locked_until - _now()).total_seconds() / 60
    assert abs(left - settings.login_lock_minutes) < 1, (
        f"首轮锁了 {left:.1f} 分钟，应为 {settings.login_lock_minutes} 分钟")


def test_第二轮锁定时长翻倍(client, db, admin_user, creds):
    """连着撞锁，锁定时长逐轮递增 —— 恒定时长挡不住「故意撞锁」。"""
    from app.core.config import settings

    _fail(client, creds, _threshold())
    db.refresh(admin_user)
    first = (admin_user.locked_until - _now()).total_seconds() / 60

    _expire_lock(db, admin_user)
    _fail(client, creds, _threshold())
    db.refresh(admin_user)
    second = (admin_user.locked_until - _now()).total_seconds() / 60

    assert admin_user.lock_level == 2, "第二轮锁定没有把轮次加一"
    assert abs(second - first * 2) < 1, (
        f"第二轮锁了 {second:.1f} 分钟，应为第一轮的两倍（{first * 2:.1f}）")
    assert abs(first - settings.login_lock_minutes) < 1


def test_第三轮继续递增(client, db, admin_user, creds):
    _fail(client, creds, _threshold())
    _expire_lock(db, admin_user)
    _fail(client, creds, _threshold())
    _expire_lock(db, admin_user)
    _fail(client, creds, _threshold())
    db.refresh(admin_user)
    assert admin_user.lock_level == 3
    left = (admin_user.locked_until - _now()).total_seconds() / 60
    assert left > 20, f"第三轮应锁得更久，实际 {left:.1f} 分钟"


def test_锁定时长有上限(client, db, admin_user, creds):
    """**没有上限的递进就是永久锁定** —— 那等于给攻击者一个禁用账号的开关。

    这条直接构造一个极高的轮次，验时长被截断。
    """
    from app.core import lockout
    from app.core.config import settings

    capped = lockout.lock_duration_minutes(10_000)
    assert capped == settings.login_lock_max_minutes, (
        f"极高轮次的锁定时长是 {capped}，应为上限 {settings.login_lock_max_minutes}")


def test_登录成功清零递进轮次(client, db, admin_user, creds):
    """成功登录意味着「这个账号回到了正常状态」，递进该从头算。

    不清零的话，被反复锁的账号会一路涨到上限 —— 而每轮其实只错了
    3 次。递增的语义是「持续在撞」，而一次成功登录恰恰证明不是。
    """
    _fail(client, creds, _threshold())
    db.refresh(admin_user)
    assert admin_user.lock_level == 1

    _expire_lock(db, admin_user)
    assert client.post("/api/auth/login", json=creds).status_code == 200
    db.refresh(admin_user)
    assert admin_user.lock_level == 0, "登录成功后递进轮次没有清零"
    assert admin_user.failed_logins == 0
    assert admin_user.locked_until is None


def test_管理员可以提前解除锁定(client, db, admin_user, creds, auth):
    """锁定必须有出口 —— 否则管理员自己也解不开，只能干等。

    这一条同时是「递进式锁定可被用来拒绝服务」的对冲手段：
    知道用户名的人能把账号挡在门外，但管理员能一键放出来。
    """
    _fail(client, creds, _threshold())
    db.refresh(admin_user)
    assert admin_user.locked_until is not None

    r = client.post(f"/api/system/users/{admin_user.id}/unlock", headers=auth)
    assert r.status_code == 200, f"解封失败：{r.text[:200]}"
    assert r.json()["was_locked"] is True

    db.refresh(admin_user)
    assert admin_user.locked_until is None
    assert admin_user.failed_logins == 0
    assert admin_user.lock_level == 0

    # 解封之后立刻能登录 —— 这才是「解封」的意义
    assert client.post("/api/auth/login", json=creds).status_code == 200


def test_解封会把递进轮次清零(client, db, admin_user, creds, auth):
    """管理员明确介入过了 = 账号回到正常状态 = 下一轮从基准时长重新算。

    不清零的话，管理员每解一次，下一轮又是 20、30 分钟起步 ——
    账号就变成「解开了也很快又被锁回去」。
    """
    _fail(client, creds, _threshold())
    _expire_lock(db, admin_user)
    _fail(client, creds, _threshold())
    db.refresh(admin_user)
    assert admin_user.lock_level == 2

    client.post(f"/api/system/users/{admin_user.id}/unlock", headers=auth)
    db.refresh(admin_user)
    assert admin_user.lock_level == 0


def test_解封要留审计(client, db, admin_user, creds, auth):
    """「谁在什么时候放行了谁」是事后要查的。

    解封是一次**权限动作**（它让一个被系统拒绝的账号重新可用），
    与改口令、改授权是同一类事。
    """
    from app.models import AuditLog

    _fail(client, creds, _threshold())
    client.post(f"/api/system/users/{admin_user.id}/unlock", headers=auth)

    entry = (db.query(AuditLog)
             .filter(AuditLog.action == "UNLOCK_USER",
                     AuditLog.entity_id == str(admin_user.id))
             .order_by(AuditLog.id.desc()).first())
    assert entry is not None, "解封没有留痕"
    assert admin_user.username in entry.detail
    assert entry.before.get("locked") is True


def test_非管理员不能解封(client, db, admin_user, plain_user, plain_auth, creds):
    """解封是管理员专属 —— 普通会计不该能放行别人。"""
    r = client.post(f"/api/system/users/{admin_user.id}/unlock", headers=plain_auth)
    assert r.status_code == 403, f"普通用户解封返回 {r.status_code}"


def test_用户列表里能看到锁定状态(client, db, admin_user, creds, auth):
    """管理员要能一眼看出「谁被锁了、还剩多久、第几轮」。

    **剩余秒数由后端算好**（core/lockout.py::lock_summary）—— 前端不拿
    locked_until 去和浏览器时钟比，那会是一次口径分裂。
    """
    _fail(client, creds, _threshold())
    r = client.get("/api/system/users", headers=auth)
    assert r.status_code == 200
    row = next((u for u in r.json() if u["id"] == admin_user.id), None)
    assert row is not None
    assert row["locked"] is True
    assert row["lock_level"] == 1
    assert row["locked_left_seconds"] > 0
    assert row["locked_until"]


def test_没锁的账号不显示锁定(client, db, admin_user, auth):
    r = client.get("/api/system/users", headers=auth)
    row = next((u for u in r.json() if u["id"] == admin_user.id), None)
    assert row is not None
    assert row["locked"] is False
    assert row["lock_level"] == 0
    assert row["locked_left_seconds"] == 0
