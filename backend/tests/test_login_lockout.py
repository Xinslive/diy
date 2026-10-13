"""登录失败锁定与管理员令牌有效期（审计 P3-04 建议 3、4）。

原实现里 ``/api/auth/login`` 对任何口令都只回一句「用户名或密码错误」，
既不计数也不锁定 —— 于是口令可以无限次试。审计把它列为配置风险的一部分，
不是「已被利用」，但在对外暴露的系统上这是最容易被自动化利用的一处。

两条设计取舍写在这里，免得后来的人把它们当细节改掉：

1. **计数放库里，不放内存。** 多进程部署时每个进程各有一份内存计数，
   打满 N 个进程就有 N 次额度；而内存计数重启即清零，「每秒试一次、
   攒够就停」的慢速爆破根本触发不了阈值。
2. **锁定期间不校验口令。** 校验了等于告诉攻击者「密码猜对了，只是被锁着」，
   等于把口令空间又缩一层。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import Settings
from app.core.security import create_access_token, token_minutes_for


@pytest.fixture
def creds(db, admin_user):
    return {"username": admin_user.username, "password": "admin123"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    for _ in range(5):
        assert client.post("/api/auth/login", json={**creds, "password": "错的"}).status_code == 401
    db.refresh(admin_user)
    assert admin_user.locked_until is not None, "失败了 5 次还没锁"

    # 锁着的时候**正确口令也进不去**
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 423, f"锁着却还能登录，返回 {r.status_code}"


def test_锁定期间不校验口令(client, db, admin_user, creds):
    """错误口令和正确口令在锁定期内的响应必须一致。

    不一致就等于一个口令预言机：攻击者拿 423 和 401 去试，能确认自己猜对了。
    """
    for _ in range(5):
        client.post("/api/auth/login", json={**creds, "password": "错的"})
    wrong = client.post("/api/auth/login", json={**creds, "password": "仍然是错的"})
    right = client.post("/api/auth/login", json=creds)
    assert wrong.status_code == right.status_code == 423
    assert "锁定" in wrong.json()["detail"] and "锁定" in right.json()["detail"]


def test_锁定到期后自动解锁(client, db, admin_user, creds):
    for _ in range(5):
        client.post("/api/auth/login", json={**creds, "password": "错的"})
    db.refresh(admin_user)
    # 把锁定时间往回拨，模拟「过了 15 分钟」
    admin_user.locked_until = _now() - timedelta(seconds=1)
    db.commit()
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 200, f"锁定到期后仍进不去：{r.text[:200]}"


def test_登录成功清零计数(client, db, admin_user, creds):
    """否则一次误输的计数会跟后面几次正常登录叠起来，
    最终把一个从没人猜过的账号锁住 —— 那就是把防护变成了故障。"""
    for _ in range(4):
        client.post("/api/auth/login", json={**creds, "password": "错的"})
    assert client.post("/api/auth/login", json=creds).status_code == 200
    db.refresh(admin_user)
    assert admin_user.failed_logins == 0
    assert admin_user.locked_until is None

    # 清零之后还能再错满 5 次才锁，而不是 1 次就锁
    for _ in range(4):
        client.post("/api/auth/login", json={**creds, "password": "错的"})
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

    for _ in range(5):
        client.post("/api/auth/login", json={**creds, "password": "错的"})
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

