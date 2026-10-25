"""登录/注册路径的**时序等价**必须真的等价（审查 §3.5 / §3.6）。

**§3.5 —— 存量低轮数账号是精确的账号枚举预言机。**

``verify_password`` 原来按**散列里记着的**轮数算，而未知用户名走的是
``_dummy_hash()``（恒用当前 60 万轮）。于是：

    不存在的账号（dummy 600k）：中位数 64.03ms（min 60.09）
    存量账号（库里记着 240k）：中位数 34.51ms（max 46.71）

两条分布**完全不相交**（legacy max < non-existent min）—— 不需要统计就能
完美区分。而 ``password_needs_rehash`` 只在**登录成功之后**才触发重算，
所以从没成功登录过的存量账号会永远停在低轮数上。

修法是**把差额补算掉**：验证路径付出的 PBKDF2 工作量恒等于当前轮数。

**§3.6 —— 注册接口的响应时间是完美枚举预言机。**

「用户名已被占用」那个分支在 ``hash_password`` **之前**就返回了
（省掉 600k 次 PBKDF2 ≈ 51ms），实测重名 14.71ms vs 新建 65.74ms，
分布同样完全不相交。**响应的形状本来就是一样的**（201 + 同一个 body）——
于是一条更精确的信道被留在了响应时间上。

**为什么这里断言的是「做了多少工作」而不是「花了多少毫秒」。**
墙钟时间受机器负载、CPU 频率、别的用例干扰影响 —— 拿它当判据的用例
是 flaky 的（本机上一会儿 64ms、一会儿 120ms）。真正要钉住的是
**工作量**：散列路径累计跑了多少次 PBKDF2 迭代。这可以用一个探针
数出来，是确定性的。
"""
from __future__ import annotations

import base64
import hashlib
import os

import pytest

from app.api import auth as auth_mod
from app.core import security, system_settings
from app.core.security import verify_password


def _hash_with_iterations(password: str, iterations: int) -> str:
    """造一条**指定轮数**的散列（模拟存量用户的老散列）。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return (f"pbkdf2_sha256${iterations}$"
            f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}")


@pytest.fixture
def pbkdf2_spy(monkeypatch):
    """数一数这次验证**总共跑了多少次 PBKDF2 迭代**。

    探针装在 ``hashlib.pbkdf2_hmac`` 上（``app.core.security`` 就是这么调的），
    记录每次调用的迭代数，求和就是「这一轮验证付出的工作量」。
    """
    calls: list[int] = []
    real = hashlib.pbkdf2_hmac

    def spy(hash_name, password, salt, iterations, dklen=None):
        calls.append(iterations)
        if dklen is None:
            return real(hash_name, password, salt, iterations)
        return real(hash_name, password, salt, iterations, dklen)

    monkeypatch.setattr(security.hashlib, "pbkdf2_hmac", spy)
    return calls


# ==================================================================== §3.5


def test_存量低轮数账号的验证补算到当前轮数(pbkdf2_spy):
    """**工作量的口径**：验证一条 24 万轮的存量散列，代价与当前轮数**相同**。"""
    legacy = _hash_with_iterations("老口令", 240_000)
    pbkdf2_spy.clear()          # 造散列本身的那次不算进来

    assert verify_password("老口令", legacy) is True

    assert sum(pbkdf2_spy) == security._ITERATIONS, (
        f"存量散列的验证只跑了 {sum(pbkdf2_spy)} 次迭代，"
        f"当前轮数是 {security._ITERATIONS} —— 耗时仍然与存量轮数相关，"
        f"账号枚举预言机还在。")


def test_当代账号的验证工作量不变(pbkdf2_spy):
    """当代账号**不该**被补算（它本来就是当前轮数）。"""
    modern = _hash_with_iterations("新口令", security._ITERATIONS)
    pbkdf2_spy.clear()

    assert verify_password("新口令", modern) is True
    assert sum(pbkdf2_spy) == security._ITERATIONS


def test_口令错的时候同样补算(pbkdf2_spy):
    """**失败路径也要等量** —— 否则「口令对不对」就成了计时信道。"""
    legacy = _hash_with_iterations("老口令", 240_000)
    pbkdf2_spy.clear()

    assert verify_password("错的", legacy) is False
    assert sum(pbkdf2_spy) == security._ITERATIONS


def test_比当前值更高的存量散列不被补算(pbkdf2_spy):
    """轮数**比当前值高**时（有人调低了 ``_ITERATIONS``），验证本来就更贵。

    这时补算只会白白加倍 —— 按原样验。
    """
    heavier = _hash_with_iterations("口令", security._ITERATIONS + 100_000)
    pbkdf2_spy.clear()

    assert verify_password("口令", heavier) is True
    assert sum(pbkdf2_spy) == security._ITERATIONS + 100_000


def test_散列不合法时不算(pbkdf2_spy):
    """散列本身坏了 ⇒ 直接失败，**不付** PBKDF2 的代价（它不是「猜口令」）。"""
    assert verify_password("口令", "pbkdf2_sha256$abc$xx$yy") is False
    assert pbkdf2_spy == []


def test_登录成功之后仍然会把老散列升级成当前轮数(db, admin_user):
    """**补算不等于放弃升级。** 重算写回的逻辑一个字没变。"""
    legacy = _hash_with_iterations("老口令", 240_000)
    admin_user.password_hash = legacy
    db.commit()

    assert security.password_needs_rehash(legacy) is True
    assert security.password_needs_rehash(_hash_with_iterations(
        "新口令", security._ITERATIONS)) is False


# ==================================================================== §3.6


@pytest.fixture
def registration_on(db):
    """打开自助注册。"""
    system_settings.set_value(db, system_settings.REGISTRATION_KEY, "1")
    db.commit()


@pytest.fixture
def hash_spy(monkeypatch):
    """数一数 ``hash_password`` 被调用了几次。"""
    calls: list[str] = []
    real = auth_mod.hash_password

    def spy(password):
        calls.append(password)
        return real(password)

    monkeypatch.setattr(auth_mod, "hash_password", spy)
    return calls


def test_重名分支也要付一次口令散列(client, db, registration_on, hash_spy):
    """**两条分支的散列次数必须一样** —— 这是「耗时等价」的判据。"""
    client.post("/api/auth/register",
                json={"username": "dup_user01", "password": "passw0rd"})

    calls_before = len(hash_spy)
    r = client.post("/api/auth/register",
                    json={"username": "dup_user01", "password": "passw0rd"})

    assert r.status_code == 201, r.text
    assert len(hash_spy) - calls_before == 1, (
        f"重名分支只算了 {len(hash_spy) - calls_before} 次散列 —— "
        "它比新建分支少付了一次 PBKDF2，响应时间就成了枚举信道。")


def test_新建分支也要付一次口令散列(client, db, registration_on, hash_spy):
    calls_before = len(hash_spy)
    r = client.post("/api/auth/register",
                    json={"username": "fresh_user01", "password": "passw0rd"})

    assert r.status_code == 201, r.text
    assert len(hash_spy) - calls_before == 1


def test_两条分支的响应完全一样(client, db, registration_on):
    """**响应的形状与内容**也必须一样（这条是改前就有的，钉住别退回去）。"""
    client.post("/api/auth/register",
                json={"username": "same_user01", "password": "passw0rd"})

    taken = client.post("/api/auth/register",
                        json={"username": "same_user01", "password": "passw0rd"})
    fresh = client.post("/api/auth/register",
                        json={"username": "other_user01", "password": "passw0rd"})

    assert taken.status_code == fresh.status_code == 201
    # **形状**一样：键集合相同，且都只回显输入的用户名。
    assert set(taken.json()) == set(fresh.json()) == {"ok", "username"}
    assert taken.json()["ok"] is True and fresh.json()["ok"] is True
    assert taken.json()["username"] == "same_user01"
    assert "已被占用" not in taken.text


def test_重名时确实没有建出第二个号(client, db, registration_on):
    """响应一样**不等于**真建了号 —— 重名分支不建号。"""
    client.post("/api/auth/register",
                json={"username": "once_user01", "password": "passw0rd"})
    client.post("/api/auth/register",
                json={"username": "once_user01", "password": "passw0rd"})

    from app.models import SysUser

    db.expire_all()
    assert db.query(SysUser).filter(SysUser.username == "once_user01").count() == 1
