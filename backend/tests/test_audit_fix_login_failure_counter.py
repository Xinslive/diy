"""登录失败计数**并发下不能丢更新**（审查 §3.7）。

**原来的机制。** ``_record_failure`` 是纯 Python 侧的读改写：

    user.failed_logins = (user.failed_logins or 0) + 1

而这一行 ``user`` 是在 **PBKDF2（约 50ms）之前**用普通 SELECT 加载的 ——
于是 N 个并发请求各自读到**同一个旧值**、各自写 ``+1``，**只有最后一次
写生效**。

**实测（阈值 3）**：20 次**并发**错误口令之后，计数只到 2、锁根本没上
（20 次真实口令校验全部执行）；而 20 次**顺序**尝试之后是
``failed_logins=0, lock_level=1``（锁上了）。**阈值 3 实际放过了 33 次。**

代码在每个延迟档位都把计数清零，于是丢更新的影响被进一步放大。而 IP 维度
的节流是**并发安全**的（一条原子 upsert），所以账号计数器是「分布式喷洒」
的**唯一**防线 —— 偏偏它就是失效的那个。

**修法**：把这一行**锁住再读**（``SELECT ... FOR UPDATE`` +
``populate_existing``），把「读-改-写」整段变成临界区。

**这个文件里的用例分两层，作用完全不同 —— 别说成一样。**

* **服务层的确定性交错**（两个 Session 手动排出交错）：不依赖任何时序运气。
  它钉的是「读-改-写」这个**语义**：第二次必须看到第一次的结果。
* **接口层的真并发**（``Barrier`` + ``ThreadPoolExecutor``）：让所有请求
  **确定性地**在口令校验那一步会合，于是它们全都持有**同一个旧值** ——
  这正是丢更新的那个窗口。**它守的是「计数不会少记」这个可观测结果**，
  **不是**「那个行锁还在」。

**接口层那一组当时是个假牙（2026-10-25 复核实测）。** 把
``_record_failure`` 里的 ``.with_for_update()`` 去掉（``populate_existing``
保留），**接口层的用例一条都没红**：

    [3.7 失败计数去掉行锁（populate_existing 保留）] ✗ 没红（假牙！） 4 passed
    [3.7 失败计数连 populate_existing 一起去掉]     ✓ 变红  3 failed, 1 passed

原因：**同一 IP 的 ``login_ip_throttle`` upsert 把请求偶然串行了** —— 于是
接口层根本进不到那个丢更新的窗口。§3.7 的价值**全在那个行锁上**，而守住它
的用例当时是假牙。

所以文件末尾那三条**服务层**的用例是本轮补上的，它们才是行锁的真正守卫：
**必须绕到服务层（多个独立 Session）才测得出差别。**
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event, select

from app.api import auth as auth_mod
from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models import SysUser

#: 并发请求数。**不能超过连接池的上限**（``db_pool_size=5`` +
#: ``db_pool_max_overflow=10`` = 15），否则多余的请求会卡在连接池上，
#: 而它们又都在等 ``Barrier`` —— 那是一个自己把自己锁死的死锁。
CONCURRENCY = 8


# --------------------------------------------------------------------- 服务层


def test_并发失败计数不丢更新(db, admin_user):
    """**确定性交错**：两个会话手动排出「都读到旧值」的顺序。

    改前的写法在这一步就会丢更新：第二个会话把计数加到 1 并提交之后，
    第一个会话手里那份**陈旧**的对象仍然是 0，于是它写回去的是 1 ——
    第二个会话的 +1 被覆盖掉了。
    """
    # 会话 A（db 夹具）手里已经有一个**陈旧**的 admin_user（failed_logins=0）。
    stale = db.get(SysUser, admin_user.id)
    assert stale.failed_logins == 0

    # 会话 B：先加一次并提交。
    with SessionLocal() as other:
        row = other.get(SysUser, admin_user.id)
        assert auth_mod._record_failure(other, row) is None
        other.commit()

    # 会话 A 接着加一次 —— 它**必须**看到 B 提交的那个 1。
    assert auth_mod._record_failure(db, stale) is None
    db.commit()

    db.expire_all()
    final = db.get(SysUser, admin_user.id)
    assert final.failed_logins == 2, (
        f"两次失败只记到 {final.failed_logins} —— 第二次把第一次覆盖掉了"
        "（读-改-写没有锁住那一行）。")


def test_加锁之后读到的必须是库里的最新值(db, admin_user):
    """**回归点**：``populate_existing`` 不能省。

    ORM 默认**不会**用查询结果覆盖已加载实例的属性 —— 不加它，
    ``FOR UPDATE`` 只是把行锁住、拿到的却仍是那个陈旧的旧值。
    """
    stale = db.get(SysUser, admin_user.id)

    with SessionLocal() as other:
        row = other.get(SysUser, admin_user.id)
        auth_mod._record_failure(other, row)
        other.commit()

    # 不调 _record_failure，直接验那一次「锁住再读」看到的是 1。
    from sqlalchemy import select

    refreshed = db.scalar(
        select(SysUser).where(SysUser.id == admin_user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert refreshed.failed_logins == 1, (
        f"锁住再读拿到的是 {refreshed.failed_logins}，库里已经是 1 —— "
        "populate_existing 没生效（ORM 不会自动覆盖已加载实例的属性）。")
    assert stale.failed_logins == 1, "同一个 Session 里的实例也应当被刷新"


# --------------------------------------------------------------------- 接口层


def test_并发错误口令的计数一个都不丢(client, db, admin_user, monkeypatch):
    """**真并发**：8 个请求确定性地在口令校验那一步会合。

    会合之后它们全都持有**同一个旧值**（``failed_logins=0``）—— 这正是
    丢更新的那个窗口。阈值调高（1000）是为了让这一轮**不触发锁定**，
    于是计数可以干净地等于请求数。
    """
    monkeypatch.setattr(settings, "login_max_failures", 1000)

    barrier = threading.Barrier(CONCURRENCY)
    real_verify = auth_mod.verify_password

    def slow_verify(password, stored):
        # 所有请求都在这里会合：**每一个都已经在 PBKDF2 之前读过了那一行**。
        barrier.wait(timeout=30)
        return real_verify(password, stored)

    monkeypatch.setattr(auth_mod, "verify_password", slow_verify)

    def fire():
        return client.post("/api/auth/login",
                           json={"username": admin_user.username,
                                 "password": "错的"})

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        responses = [f.result() for f in
                     [pool.submit(fire) for _ in range(CONCURRENCY)]]

    assert all(r.status_code == 401 for r in responses), \
        [(r.status_code, r.text) for r in responses]

    db.expire_all()
    row = db.get(SysUser, admin_user.id)
    assert row.failed_logins == CONCURRENCY, (
        f"{CONCURRENCY} 次并发失败只记到 {row.failed_logins} 次 —— "
        "读-改-写丢了更新。")


def test_并发错误口令照样会触发锁定(client, db, admin_user, monkeypatch):
    """**阈值必须真的生效**：并发也一样。

    改前 20 次并发只记到 2，阈值 3 形同虚设；改后计数一个不丢，于是
    阈值照常触发 —— 账号进入锁定状态。
    """
    barrier = threading.Barrier(CONCURRENCY)
    real_verify = auth_mod.verify_password

    def slow_verify(password, stored):
        barrier.wait(timeout=30)
        return real_verify(password, stored)

    monkeypatch.setattr(auth_mod, "verify_password", slow_verify)

    def fire():
        return client.post("/api/auth/login",
                           json={"username": admin_user.username,
                                 "password": "错的"})

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        responses = [f.result() for f in
                     [pool.submit(fire) for _ in range(CONCURRENCY)]]

    assert all(r.status_code == 401 for r in responses), \
        [(r.status_code, r.text) for r in responses]

    db.expire_all()
    row = db.get(SysUser, admin_user.id)
    assert (row.lock_level or 0) >= 1, (
        f"并发 {CONCURRENCY} 次失败之后 lock_level={row.lock_level}、"
        f"failed_logins={row.failed_logins} —— 阈值 {settings.login_max_failures} "
        "没有触发锁定（计数丢了）。")
    assert row.locked_until is not None, "锁定了却没有 locked_until"


# --------------------------------------------------------------------- 行锁的守卫
#
# **下面这三条是 2026-10-25 复核后补上的**（复核方在 /tmp 的副本上实测：
# 去掉 ``.with_for_update()`` 之后，上面那几条用例一条都没红）。
#
# 判据必须是**确定性的**，所以：
#
# * 不靠 sleep 撞运气（那是 flaky）；
# * 用**锁语义**做判据：会话 A 先 ``SELECT ... FOR UPDATE`` 占住那一行，
#   另一个线程里的 ``_record_failure`` 就必须**等**它 —— 「是否阻塞」就是
#   判据；
# * 再补一条**形态断言**（发出的 SELECT 里带 ``FOR UPDATE``）—— 它不 flaky，
#   但**单独用不够**（形态对了不等于行为对），所以两条都要。

#: 服务层并发用例的并发数。
#:
#: 每个工作线程占一条连接（主线程再占一条）—— 别把它开得比连接池还大。
SERVICE_CONCURRENCY = 4

#: 判定「线程是不是被行锁挡住了」的观察窗（秒）。
#:
#: 这是个**否定**断言（「这段时间里它没有返回」），所以窗口给宽一点也无所谓：
#: 没有行锁时那次调用是**毫秒级**返回的（一条普通 SELECT），1 秒足够让
#: 「它其实没被挡住」暴露出来；而有行锁时它是**无限期**阻塞的。
BLOCK_OBSERVE_SECONDS = 1.0


def test_失败计数那一行必须真的被锁住(db, admin_user):
    """**锁语义**是确定性判据：有行锁 ⇒ 别的会话的同一次调用会**等**。

    做法：会话 A（``db`` 夹具）先 ``SELECT ... FOR UPDATE`` 占住那一行
    （**不提交** ⇒ 锁一直拿着），然后在另一个线程里调 ``_record_failure``：

    * **有行锁时**：那次调用会**阻塞**在 ``SELECT ... FOR UPDATE`` 上 ——
      直到 A 放锁才返回；
    * **没有行锁时**：它**立刻返回**（那只是一条普通 SELECT，MVCC 下读得到
      旧版本，根本不必等锁）。

    于是「是否阻塞」就是判据 —— **不用 sleep 撞运气**。

    为什么必须绕到服务层：HTTP 层测不出差别（同一 IP 的
    ``login_ip_throttle`` upsert 把请求偶然串行了）。
    """
    # 会话 A：占住那一行（不提交 ⇒ 锁一直拿着）。
    db.scalar(
        select(SysUser).where(SysUser.id == admin_user.id).with_for_update()
    )

    reached = threading.Event()
    returned = threading.Event()

    def worker():
        with SessionLocal() as other:
            row = other.get(SysUser, admin_user.id)
            reached.set()
            auth_mod._record_failure(other, row)
            # **返回点就在这里**：有行锁时它到不了这一行。
            returned.set()
            other.commit()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    assert reached.wait(timeout=10), "工作线程没跑到 _record_failure"

    blocked = not returned.wait(timeout=BLOCK_OBSERVE_SECONDS)

    # 先放锁，让工作线程收尾（无论它是「被挡住」还是「没被挡住」）。
    db.rollback()
    t.join(timeout=30)
    assert not t.is_alive(), "工作线程卡住了"

    assert blocked, (
        "另一个会话已经用 SELECT ... FOR UPDATE 占住了那一行，_record_failure "
        "却立刻返回了 —— 它没有用锁定读，丢更新（§3.7）没有被挡住。")

    # 放行之后那一次失败**必须真的记上了**（不是「没被挡住」就完事）。
    db.expire_all()
    assert db.get(SysUser, admin_user.id).failed_logins == 1


def test_失败计数那条SELECT必须是锁定读(db, admin_user):
    """**形态断言**：发出的那条 SELECT 必须带 ``FOR UPDATE``。

    这条不 flaky，但它**单独用不够** —— 形态对了不等于行为对（值仍可能
    被算错）。所以它与上面那条「锁语义」用例配对使用。
    """
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        with SessionLocal() as other:
            row = other.get(SysUser, admin_user.id)
            auth_mod._record_failure(other, row)
            other.commit()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    selects = [s for s in statements
               if "sys_user" in s and s.lstrip().upper().startswith("SELECT")]
    assert selects, "没有抓到任何针对 sys_user 的 SELECT"
    assert any("FOR UPDATE" in s.upper() for s in selects), (
        "失败计数那次读不是锁定读（SELECT ... FOR UPDATE）：\n"
        + "\n".join(selects))


def test_服务层并发失败计数一个都不丢(db, admin_user, monkeypatch):
    """**服务层**的并发实测 —— 多个**独立 Session**，交错是**排出来**的。

    为什么不能只在 HTTP 层测：同一 IP 的 ``login_ip_throttle`` upsert 会把
    请求**偶然串行**（复核方实测：HTTP 层根本测不出差别）。所以这里绕到
    服务层，用 4 个**互不相干的 Session**。

    交错怎么排：主线程先 ``SELECT ... FOR UPDATE`` 占住那一行，于是 N 个
    工作线程的 ``_record_failure`` 全部**堵在**那条锁定读上；主线程放锁
    之后，它们一个接一个地读到**上一个线程刚提交的值** —— 于是计数必须
    正好等于并发数。

    **去掉行锁（``populate_existing`` 保留）时会发生什么**：那些 SELECT 变成
    普通读，于是它们**全部读到同一个旧值**（0）→ 全部写回同一个数 →
    **计数丢失**（实测：4 次并发只记到 1）。
    """
    # 阈值调高（1000）是为了让这一轮**不触发锁定**，计数可以干净地
    # 等于并发数。
    monkeypatch.setattr(settings, "login_max_failures", 1000)

    n = SERVICE_CONCURRENCY
    # 屏障：工作线程在**发出**那条 SELECT 之前会合，主线程是最后一个到场的。
    # 于是主线程放锁的那一刻，N 条 SELECT 都**已经发出去了**。
    gate = threading.Barrier(n + 1)
    local = threading.local()
    errors: list[Exception] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        # 只拦「工作线程已经准备进 _record_failure」之后的那条 SELECT。
        if not getattr(local, "armed", False):
            return
        if "sys_user" not in statement:
            return
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        local.seen = getattr(local, "seen", 0) + 1
        if local.seen == 1:
            gate.wait(timeout=30)

    def worker():
        try:
            with SessionLocal() as other:
                row = other.get(SysUser, admin_user.id)
                local.armed = True
                auth_mod._record_failure(other, row)
                other.commit()
        except Exception as exc:                     # noqa: BLE001
            errors.append(exc)
            gate.abort()

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        # 主线程先占住那一行。
        db.scalar(
            select(SysUser).where(SysUser.id == admin_user.id).with_for_update()
        )
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
        for t in threads:
            t.start()
        # 等所有工作线程都把 SELECT 发出去了。
        try:
            gate.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        # 放锁：它们一个接一个地读到上一个线程刚提交的值。
        db.rollback()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "工作线程卡住了"
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert not errors, errors
    db.expire_all()
    row = db.get(SysUser, admin_user.id)
    assert row.failed_logins == n, (
        f"{n} 次并发失败只记到 {row.failed_logins} 次 —— 读-改-写丢了更新"
        "（那一次「锁住再读」不见了）。")
