"""IP 维度的登录节流（审计 P2-11 的另一半）。

账号锁定（``sys_user.failed_logins``）只回答「这个账号被打了多少次」。轮换
用户名的攻击源压根不碰同一个账号两次：它对上万个账号各试 5 次，每次只锁一个
账号，锁定几乎不生效，审计里留下的也只是一堆互不相干的 ``LOGIN_FAILURE``。

这份文件钉的是四件事，按「最容易做错」的顺序排：

1. **轮换账号仍然被聚合判定**（节流存在的理由）。
2. **共享 IP 不被误伤**（节流最容易变成故障的地方）。
3. **伪造 X-Forwarded-For 换不了桶**（最容易做错的一条 —— 无条件信任代理头
   等于把这一层防护整个作废）。
4. **状态会过期、清理入口可测**（只增不减的表是 P2-23 同一种病）。

另外钉住「**不做 IP 硬锁**」这条设计结论本身：超阈值只回 429，
一个账号都不许被锁 —— 不是因为锁账号没好处，而是因为 NAT 后面几百人共用一个
IP，硬锁会误伤他们，而那个动作攻击者可以主动触发。
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AuditLog
from app.models.system import LoginIpThrottle

# --------------------------------------------------------------------- 夹具


@contextlib.contextmanager
def peer(ip: str):
    """一个「对端是指定 IP」的 TestClient —— 用来模拟不同的来源。

    必须显式指定对端：Starlette TestClient 默认是全suite共用的
    ``("testclient", 50000)``，那是一个**共享 IP**，几十个用例的失败会堆进
    同一个节流桶（见 test_login_lockout.py 开头那段）。
    """
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app, raise_server_exceptions=False,
                    client=(ip, 50000)) as c:
        yield c


def uniq_ip() -> str:
    """一个当前测试里没人用过的来源 IP（TEST-NET-2 段，不会被真路由到）。"""
    n = uuid.uuid4().int
    return f"198.51.{(n >> 8) % 254 + 1}.{n % 254 + 1}"


@pytest.fixture
def make_user(db):
    """建一批普通账号，用来测「同一个源轮换多个账号」。"""
    from app.core.security import hash_password
    from app.models import SysUser

    def _make(n: int = 1) -> list:
        out = []
        for i in range(n):
            name = f"victim_{uuid.uuid4().hex[:10]}_{i}"
            user = SysUser(username=name, password_hash=hash_password("pw123456"),
                           real_name=name, active=True, is_admin=False)
            db.add(user)
            out.append(user)
        db.commit()
        for user in out:
            db.refresh(user)
        return out

    return _make


@pytest.fixture
def throttle(monkeypatch):
    """把阈值调小、延迟调成 0，让节流行为在测试里**立刻**可见。

    阈值调小不是为了「测起来快」：默认 30 次 / 15 分钟正是为共享出口 IP 选的
    安全值（见 config.py 的注释），而节流的**语义**与阈值大小无关。
    延迟必须调 0：默认有递增 sleep，那是用来惩罚真实攻击者的，
    不该让测试套件为它付出墙钟时间。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "login_ip_max_failures", 5, raising=False)
    monkeypatch.setattr(settings, "login_ip_window_seconds", 900, raising=False)
    monkeypatch.setattr(settings, "login_ip_delay_step_ms", 200, raising=False)
    monkeypatch.setattr(settings, "login_ip_delay_max_ms", 0, raising=False)
    monkeypatch.setattr(settings, "login_ip_hard_lock", False, raising=False)
    monkeypatch.setattr(settings, "trusted_proxies", "", raising=False)
    return settings


def _state(db, ip: str):
    db.expire_all()
    return db.query(LoginIpThrottle).filter(LoginIpThrottle.ip == ip).one_or_none()


def _logs(db, action: str):
    db.expire_all()
    return list(db.query(AuditLog).filter(AuditLog.action == action)
                .order_by(AuditLog.id).all())


def _fail(client, username: str, **headers):
    return client.post("/api/auth/login",
                       json={"username": username, "password": "错的-绝对不对"},
                       headers=headers or None)


# ==================================================================== 1. 聚合


def test_轮换多个不同账号_失败仍聚到同一个源(client, db, throttle, make_user):
    """**P2-11 的正题**：账号锁定管不到的那件事。

    同一个来源对 5 个**不同**账号各失败一次 —— 按账号口径看，这 5 个账号每个
    都只错了一次（阈值 5，差得远），一个都没锁，登录接口看上去毫无异样。
    换句话说：只看账号，这个攻击源从头到尾没有触发过任何东西。

    按来源口径看，这 5 次是同一件事的第 1..5 次 —— 已经到阈值。
    """
    victims = make_user(5)
    ip = uniq_ip()
    with peer(ip) as c:
        for v in victims:
            assert _fail(c, v.username).status_code == 401, "每个账号只错一次，不该被拒"

    row = _state(db, ip)
    assert row is not None, "来源完全没有被记录 —— 聚合判断根本没发生"
    assert row.failures == 5, f"轮换了 5 个账号，来源计数却是 {row.failures}"
    # 一个账号都没被锁：这条正是「锁定机制对上万个账号几乎不生效」的实证。
    for v in victims:
        db.refresh(v)
        assert v.failed_logins == 1 and v.locked_until is None, (
            f"{v.username} 不该被锁 —— 它只被试了一次")


def test_同一来源跨过阈值后_下一个来源内的任何账号都被拒(client, db, throttle, make_user):
    """聚合判定一旦成立就对这个**源**生效，与「在试哪个账号」无关。"""
    victims = make_user(6)
    ip = uniq_ip()
    with peer(ip) as c:
        for v in victims[:5]:
            _fail(c, v.username)
        # 第 6 次：这次换的是一个**全新的、从未被试过**的账号
        r = _fail(c, victims[5].username)
    assert r.status_code == 429, f"跨过阈值后仍在放行，返回 {r.status_code}"
    assert "频繁" in r.json()["detail"]
    assert r.headers.get("Retry-After"), "429 必须告诉客户端什么时候再试"


def test_另一个源不受牵连(client, db, throttle, make_user):
    """聚合必须是**按源**的。共用一个网关出口的两家公司不能互相连坐。"""
    make_user(6)
    other = uniq_ip()
    with peer(uniq_ip()) as attacker, peer(other) as bystander:
        for i in range(5):
            _fail(attacker, "不存在的账号%d" % i)
        assert _fail(attacker, "再试一次").status_code == 429
        # 另一个源同一个账号（本来就不该被锁）正常失败
        assert _fail(bystander, "不存在的账号0").status_code == 401
    assert _state(db, other).failures == 1


def test_跨过阈值后整个源都要等_但到点自动恢复(client, db, throttle, make_user):
    """**本方案明确接受的代价**，连同它与「锁死」的区别一起钉住。

    超阈值之后，这个来源上的**所有人**（包括那个用户名密码都没错的同事）
    都会拿到 429 —— 因为判定就是按 IP 做的，看不见「谁在打」。
    方案接受这一点，换来的是另外两条：

    * 它**不是锁**：到窗口滑完自动恢复，不需要任何人工解锁，也不会在
      库里留下一个「这个 IP 被锁了」的长期状态；
    * 它**没有不可逆的后果**：账号一个都没被锁，攻击者也无法把某个 IP
      永久挡在门外 —— 而按 IP 硬锁恰恰能给到这个（从公司网络里打满阈值
      即可让全公司长时间登不进来）。

    所以断言三件事：所有人都等、到点恢复、账号一个都没被锁。
    """
    from datetime import timedelta as _td

    users = make_user(6)
    ip = uniq_ip()
    with peer(ip) as c:
        for v in users[:5]:
            _fail(c, v.username)
        assert _fail(c, users[5].username).status_code == 429
        # 源上那个「什么都没做错」的同事 —— 也得等
        assert _fail(c, users[5].username).status_code == 429

    row = _state(db, ip)
    assert row.blocked_until is None, "写了 blocked_until —— 这就成锁了"
    for v in users:
        db.refresh(v)
        assert v.locked_until is None, f"{v.username} 被连坐锁了"

    # 窗口滑过之后，同一个源立刻恢复，**不需要任何人去解锁**
    row.last_failed_at = datetime.now(timezone.utc).replace(tzinfo=None) - _td(seconds=1000)
    db.commit()
    with peer(ip) as c:
        r = c.post("/api/auth/login",
                   json={"username": users[5].username, "password": "pw123456"})
    assert r.status_code == 200, f"窗口过了仍然进不去 —— 这是锁死不是节流：{r.text[:200]}"



# ============================================================== 2. 共享 IP


def test_共享IP下的日常噪声不触发节流(client, db, make_user):
    """**默认配置**下，一个共享出口 IP 上的正常业务噪声不会把人挡在门外。

    这里刻意用**默认值**（阈值 30），而不是 ``throttle`` 夹具里的 5 ——
    这条用例要证明的就是「默认值对共享 IP 是安全的」，把阈值调小去测它
    等于没测。同一个 IP 上错满 10 次（一个办公室月结时很正常的量级），
    之后**别的账号**依然能正常登录。
    """
    from app.core.config import settings

    assert settings.login_ip_max_failures >= 30, (
        "这条用例断言的是默认阈值的安全性；调小了它就失去意义")

    noisy = make_user(4)
    others = make_user(3)
    ip = uniq_ip()
    with peer(ip) as c:
        # 轮流错、每个账号不超过 4 次 —— 账号维度的阈值是 5，这里刻意不碰它，
        # 要测的只有 IP 那一层。共享办公室里的真实形态也正是这样：
        # 十几个人轮流按错密码，而不是一个人连着按 10 下。
        for _ in range(3):
            for v in noisy:
                assert _fail(c, v.username).status_code == 401, (
                    "同一个 IP 上 12 次输错不该被拒 —— 共享出口 IP 上这是正常噪声")
        for u in others:
            r = c.post("/api/auth/login",
                       json={"username": u.username, "password": "pw123456"})
            assert r.status_code == 200, f"{u.username} 被共享 IP 的噪声连坐了：{r.text[:120]}"

    # 这 12 次全部落在同一个源上，只是没到阈值
    assert _state(db, ip).failures == 12



def test_超阈值也不锁任何账号(client, db, throttle, make_user):
    """**「不做 IP 硬锁」这条设计结论本身**，钉成一条断言。

    账号锁定（5 次错就锁 15 分钟）在**账号维度**上是刻意要有的；
    但同一套语义搬到 IP 维度就变成了 DoS 放大器：NAT 后面几百人共用一个
    出口 IP，而触发它的动作攻击者从公司网络里就能做。
    所以这里逐个账号断言「一个都没被锁」。
    """
    victims = make_user(6)
    ip = uniq_ip()
    with peer(ip) as c:
        for v in victims[:5]:
            _fail(c, v.username)
        _fail(c, "第六次")
    for v in victims:
        db.refresh(v)
        assert v.locked_until is None, (
            f"{v.username} 被按 IP 锁了 —— 这是本条明确否掉的方案")
    assert _state(db, ip).blocked_until is None, (
        "库里写了 blocked_until —— IP 硬锁默认必须是关的")


def test_账号维度的锁定本身没有因为IP节流而失效(client, db, throttle, make_user):
    """加了一层节流不等于把原来那层弄丢了。

    刻意用**另一个来源**去试那把锁：同源的话，IP 节流（429）会先于账号锁定
    返回，而那正是下面那条用例要单独验证的排序问题。

    **这一条断言以前是「达阈值 → 423 硬锁」** —— 那正是审查报告安全域 P2-4
    点的缺陷（连错三次就能把别人的账号挡在门外十分钟，时长还逐轮递增）。
    现在账号维度的防护是**递增延迟**（429 + Retry-After），第 3 轮起才升级
    成硬锁 —— 所以这里断言的是「**另一个来源也拿不到 200**」，而不是具体的
    状态码：**账号层的那道闸还在**，只是形态变了。
    """
    victim = make_user(1)[0]
    with peer(uniq_ip()) as c:
        for _ in range(5):
            _fail(c, victim.username)
    db.refresh(victim)
    assert victim.locked_until is not None, "账号级防护被这一层改动弄丢了"

    with peer(uniq_ip()) as other:
        r = other.post("/api/auth/login",
                       json={"username": victim.username, "password": "pw123456"})
    assert r.status_code != 200, f"账号级防护失效，返回 {r.status_code}"
    # 具体形态：账号层是**递增延迟**（429），不是硬锁。
    assert r.status_code == 429, f"账号级应当是递增延迟，实际 {r.status_code}"


def test_节流先于账号锁定返回_且不泄漏账号存在性(client, db, throttle, make_user):
    """一个源同时踩中两层时，谁先说话。

    IP 节流在前是**刻意**的：被节流的来源每一次都不必付那 20ms 的 PBKDF2，
    而且 429 对「账号存在且被锁」与「账号不存在」的回答完全一样 ——
    比 423 少泄漏一个信号（423 只在**已经锁定过的账号**上出现，
    攻击者可以拿它确认「这个账号存在且被锁过」）。
    """
    victim = make_user(1)[0]
    ip = uniq_ip()
    with peer(ip) as c:
        for _ in range(5):
            _fail(c, victim.username)
        locked = _fail(c, victim.username)          # 第 6 次：IP 已过阈值
        unknown = _fail(c, "一个根本不存在的账号")     # 同样 429
    assert locked.status_code == unknown.status_code == 429, (
        f"两种情况回答不一致：{locked.status_code} vs {unknown.status_code} —— "
        "那是一个账号枚举信道")


# ================================================= 3. 伪造 XFF 绕不过去


def test_伪造XForwardedFor换不了桶(client, db, throttle):
    """**最容易做错的一条**：无条件信任 XFF = 把限流的钥匙交给攻击者。

    他每次请求换一个头，就换一个「来源」，于是 IP 维度的聚合判断对他
    完全不存在 —— 而这恰好是这一层唯一的价值。

    正确做法：只认 TCP 对端（默认一个代理都不信），于是这些伪造的头
    全部被忽略，十次失败仍然落在同一个桶里。
    """
    ip = uniq_ip()
    with peer(ip) as c:
        for i in range(5):
            r = _fail(c, "不存在的%d" % i,
                      **{"X-Forwarded-For": f"10.0.0.{i}"})
            assert r.status_code == 401, f"第 {i + 1} 次就被放行/拒绝了？{r.status_code}"
        assert _fail(c, "再来一次").status_code == 429

    row = _state(db, ip)
    assert row is not None and row.failures == 5, (
        f"伪造的头把计数分散了：来源 {ip} 的计数是 "
        f"{row.failures if row else '（没有行）'}")
    db.expire_all()
    forged_rows = db.query(LoginIpThrottle).filter(
        LoginIpThrottle.ip.like("10.0.0.%")).count()
    assert forged_rows == 0, "伪造的 XFF 竟然各自建了一行 —— 头被采信了"


def test_XRealIP在不可信对端上也不采信(client, db, throttle):
    """``X-Real-IP`` 比 XFF 好骗（nginx 是覆盖语义），但对端不认识就一样是废话。"""
    from app.api.auth import _throttle_ip

    class _Req:
        client = type("C", (), {"host": "203.0.113.9"})()

        def __init__(self):
            self.headers = {"x-real-ip": "1.2.3.4"}

    assert _throttle_ip(_Req()) == "203.0.113.9", (
        "不在 TRUSTED_PROXIES 里的对端，它自称的 X-Real-IP 不能算数")


def test_配了可信代理才读XRealIP(monkeypatch):
    """配了 ``TRUSTED_PROXIES`` 之后，nginx 覆盖写的那个头才是可信的。"""
    from app.api.auth import _throttle_ip
    from app.core.config import settings

    class _Req:
        def __init__(self, host, headers):
            self.client = type("C", (), {"host": host})()
            self.headers = headers

    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1,10.0.0.0/8",
                        raising=False)
    peer_host = "127.0.0.1"
    assert _throttle_ip(_Req(peer_host, {"x-real-ip": "203.0.113.9"})) == "203.0.113.9", (
        "可信代理 + X-Real-IP：这就是 deploy/nginx/cwq.conf 那条 "
        "proxy_set_header X-Real-IP $remote_addr 覆盖出来的值")
    # nginx 后面没有 X-Real-IP 时退回 XFF，但取**最右**元素
    assert _throttle_ip(_Req(peer_host, {"x-forwarded-for": "1.1.1.1, 9.9.9.9"})) == "9.9.9.9", (
        "XFF 要取最右元素 —— 最左那个是客户端自己写的，可以随便写；"
        "nginx 的 $proxy_add_x_forwarded_for 是追加语义，右边才是代理亲眼见到的")
    # CIDR 也要认（云上 LB / k8s 的常见形态）
    assert _throttle_ip(_Req("10.1.2.3", {"x-real-ip": "5.6.7.8"})) == "5.6.7.8"
    # 换个不在白名单里的对端，同样的头立刻不作数
    assert _throttle_ip(_Req("11.0.0.1", {"x-real-ip": "1.1.1.1"})) == "11.0.0.1"


def test_可信代理配置解析_认IP也认CIDR():
    """白名单要能写单个 IP 也要能写网段（k8s / 云上 LB 的常见形态）。"""
    from app.core.config import is_trusted_proxy, trusted_proxy_networks

    raw = "127.0.0.1, 10.0.0.0/8"
    assert len(trusted_proxy_networks(raw)) == 2
    assert is_trusted_proxy("127.0.0.1", raw) is True
    assert is_trusted_proxy("10.9.9.9", raw) is True
    assert is_trusted_proxy("11.0.0.1", raw) is False
    # 写错的条目被跳过，而不是让服务起不来
    assert len(trusted_proxy_networks("127.0.0.1, 不是一个IP")) == 1
    # 默认值 = 一个都不信
    assert trusted_proxy_networks("") == ()


@pytest.mark.parametrize("raw", ["0.0.0.0/0", "::/0", "0.0.0.0/1", "0.0.0.0/7"])
def test_过宽的代理网段被拒(raw):
    """``0.0.0.0/0`` 语法合法，但它把「信任代理」变成了「信任所有人」。

    这份白名单**唯一**的作用是决定敢不敢读 X-Forwarded-For。读了就是采信
    客户端随便能写的那个头，于是登录 IP 限流（``_throttle_ip``）被彻底绕过：
    换一个头就换一个限流桶。与其让它「配了就生效」地废掉限流，不如拒掉。
    """
    from app.core.config import is_trusted_proxy, trusted_proxy_networks

    assert trusted_proxy_networks(raw) == ()
    assert is_trusted_proxy("8.8.8.8", raw) is False


def test_正常的代理网段仍然放行():
    """下限是 /8，别把它收紧到误伤正常部署。"""
    from app.core.config import trusted_proxy_networks

    assert len(trusted_proxy_networks("127.0.0.1, 10.0.0.0/8, 172.16.0.0/12")) == 3


# ============================================================== 4. 留痕


def test_节流拒绝要单独留一条审计(client, db, throttle, make_user):
    """429 是「这台系统在主动拒绝某个来源」的信号，事后第一眼要看的也是它。

    与 ``LOGIN_FAILURE`` 分开记，是因为两者的处置完全不同：前者要看来源
    （是不是有人在扫），后者只是正常输错。混在一起，「有人在扫」这件事
    就又消失在噪声里了 —— 那正是 P2-11 要补的那个洞。
    """
    ip = uniq_ip()
    users = make_user(6)
    with peer(ip) as c:
        for v in users[:5]:
            _fail(c, v.username)
        _fail(c, users[5].username)

    rows = _logs(db, "LOGIN_IP_THROTTLED")
    assert rows, "被节流的请求没有留痕 —— 事后查不到是谁在被拦"
    entry = rows[-1]
    assert entry.detail and "软节流" in entry.detail
    assert entry.after.get("ip") == ip
    assert entry.after.get("failures") == 5
    assert entry.after.get("max_failures") == 5
    assert entry.after.get("hard_lock") is False
    assert entry.after.get("retry_after", 0) > 0


def test_节流期不再重复写LOGIN_FAILURE(client, db, throttle, make_user):
    """一次请求只留一条，否则「这个源失败了几次」在任何统计里都翻倍。

    而翻倍后的数字恰恰是用来调这个阈值的 —— 被自己的日志污染的阈值
    没有人调得准。
    """
    ip = uniq_ip()
    users = make_user(7)
    with peer(ip) as c:
        for v in users[:5]:
            _fail(c, v.username)
        before = len(_logs(db, "LOGIN_FAILURE"))
        for v in users[5:]:
            assert _fail(c, v.username).status_code == 429
    assert len(_logs(db, "LOGIN_FAILURE")) == before, (
        "被节流的请求又写了一条 LOGIN_FAILURE —— 同一次请求留了两行")


def test_跨过阈值的那一次要单独留一条扫描事件(client, db, throttle, make_user):
    """「这个源开始扫了」是一条**独立**信号，而不是刷屏。

    它只触发一次：跨过阈值之后，请求都在开头被 429 挡掉，走不到写这一条
    的代码。留一次「跨线事件」，管理员在审计里一眼能看见起点。
    """
    ip = uniq_ip()
    users = make_user(7)
    with peer(ip) as c:
        for v in users[:5]:
            _fail(c, v.username)
        _fail(c, users[5].username)   # 跨线后被 429，后续到不了这里
    scans = [x for x in _logs(db, "LOGIN_IP_SCAN") if x.after.get("ip") == ip]
    assert len(scans) == 1, f"跨线事件应恰好一条，实际 {len(scans)} 条"
    assert scans[0].after.get("failures") == 5


def test_扫不存在的用户名也会留下扫描事件(client, db, throttle):
    """**最常见的爆破形态**同样要留信号。

    「扫用户名」不需要知道任何口令，所以它是自动化攻击的第一步、也是最省事
    的那一步。早期版本只在「口令错」那条路上留扫描事件，于是扫用户名的人
    在审计里完全没有留下「这个源开始扫了」这一条 —— 而 P2-11 要补的正是这个洞。
    """
    ip = uniq_ip()
    with peer(ip) as c:
        for i in range(5):
            assert _fail(c, f"根本不存在的账号{i}").status_code == 401
    scans = [x for x in _logs(db, "LOGIN_IP_SCAN") if x.after.get("ip") == ip]
    assert len(scans) == 1, "扫不存在的用户名时没有留下扫描事件"
    assert scans[0].after.get("failures") == 5


# ================================================== 5. 递增惩罚与 Retry-After


def test_超阈值越多_延迟越大但有上限(client, db, monkeypatch, make_user):
    """惩罚是**渐进**的（fail2ban 那一类），而且**必须有上限**。

    上限这条更要紧：服务端 sleep 占的是 worker，而「占住 worker」正是
    IP 硬锁那条路被否掉的同一种资源消耗。更大的惩罚交给 Retry-After。

    直接测判定函数，而不是真去打 10 次 —— 被测的是「惩罚曲线」，
    而曲线在一次判定里就能看全。
    """
    from app.api import auth as auth_mod
    from app.core.config import settings

    monkeypatch.setattr(settings, "login_ip_max_failures", 5, raising=False)
    monkeypatch.setattr(settings, "login_ip_delay_step_ms", 200, raising=False)
    monkeypatch.setattr(settings, "login_ip_delay_max_ms", 1000, raising=False)

    now = auth_mod._utcnow()
    ip_at_5, ip_at_15 = uniq_ip(), uniq_ip()
    db.add(LoginIpThrottle(ip=ip_at_5, failures=5, window_started_at=now,
                           last_failed_at=now))
    db.add(LoginIpThrottle(ip=ip_at_15, failures=15, window_started_at=now,
                           last_failed_at=now))
    db.commit()

    d5 = auth_mod._ip_verdict(db, ip_at_5, now).delay_ms
    d15 = auth_mod._ip_verdict(db, ip_at_15, now).delay_ms
    assert d5 == 200, f"刚过阈值应该是最小的一档延迟，实际 {d5}ms"
    assert d15 == 1000, f"延迟应当随超阈值次数递增并停在 1 秒上限，实际 {d15}ms"
    assert d15 > d5, "惩罚没有随次数递增"
    assert d15 <= settings.login_ip_delay_max_ms


def test_429带RetryAfter且随窗口剩余时间变化(client, db, throttle, make_user):
    """Retry-After 给的是「到窗口滑完还剩多久」，不是一句「稍后」。"""
    from datetime import timedelta as _td

    ip = uniq_ip()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with peer(ip) as c:
        for v in make_user(5):
            _fail(c, v.username)
        r = _fail(c, "再来")
    assert r.status_code == 429
    retry = int(r.headers["Retry-After"])
    assert 0 < retry <= 900, f"Retry-After={retry} 不在窗口长度内"

    # 把最近一次失败推到窗口的边缘，Retry-After 就该显著变短
    row = _state(db, ip)
    row.last_failed_at = now - _td(seconds=800)
    db.commit()
    with peer(ip) as c:
        r2 = _fail(c, "再来")
    assert r2.status_code == 429
    assert int(r2.headers["Retry-After"]) < retry, (
        f"窗口快结束了还让客户端等同样久：{r2.headers['Retry-After']} vs {retry}")


# ================================================== 5. 状态过期与清理


def test_窗口滑过之后计数清零(client, db, throttle):
    """滑动窗口：连续一整个窗口没有失败，才整体清零。

    **窗口内绝不衰减**是故意的（fail-closed）：正在被打的时候计数只涨不落，
    否则「慢慢打、每打一次等窗口过半」就能把计数摊平。
    """
    from app.api.auth import _bump_ip_failure

    ip = uniq_ip()
    for _ in range(4):
        _bump_ip_failure(db, ip, datetime.now(timezone.utc).replace(tzinfo=None))
    assert _state(db, ip).failures == 4

    # 刚刚还在窗口内：只涨不落
    assert _bump_ip_failure(db, ip, datetime.now(timezone.utc).replace(tzinfo=None)) == 5
    # 往回拨一个多窗口：清零重算
    later = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1000)
    assert _bump_ip_failure(db, ip, later) == 1, "窗口滑过之后没有清零"


def test_被节流的请求不延长窗口(client, db, throttle, make_user):
    """「N 分钟后再试」必须真的兑现。

    若节流中的请求也刷新 ``last_failed_at``，那么一个照着 Retry-After
    每分钟重试一次的人（或攻击者）会把窗口无限续上，永远解不开。
    """
    ip = uniq_ip()
    with peer(ip) as c:
        for v in make_user(5):
            _fail(c, v.username)
        before = _state(db, ip).last_failed_at
        assert _fail(c, "被节流的一次").status_code == 429
        assert _state(db, ip).last_failed_at == before, (
            "被节流的请求仍然刷新了窗口 —— 窗口永远滑不过去")


def test_清理入口删掉过期状态(client, db, throttle):
    """这张表只增不减，所以清理入口必须存在**且可测**（P2-23 同一种病）。

    保留期取窗口的 9 倍：刚出窗口的那几行还要用来判定「这个源是不是刚被
    放过」，删早了等于让一次节流凭空消失。
    """
    from app.api.auth import purge_expired_login_ip_state

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fresh, stale = uniq_ip(), uniq_ip()
    db.add(LoginIpThrottle(ip=fresh, failures=99, window_started_at=now,
                           last_failed_at=now))
    db.add(LoginIpThrottle(ip=stale, failures=3,
                           window_started_at=now - timedelta(days=9),
                           last_failed_at=now - timedelta(days=9)))
    db.commit()

    removed = purge_expired_login_ip_state(db)
    db.commit()
    assert removed == 1, f"应删掉恰好 1 行，实际 {removed}"
    assert _state(db, fresh) is not None, "窗口内的状态被误删了"
    assert _state(db, stale) is None, "过期状态没被清掉"


def test_登录路径自己会顺带清理(client, db, throttle, make_user):
    """不依赖谁来调：下一次登录失败就会顺手回收过期行。

    这条是为了让「运维忘了调清理脚本」不成立 —— 挂在一个真正会发生的事件上，
    比写在文档里等人记得强。
    """
    from app.api import auth as auth_mod

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale = uniq_ip()
    db.add(LoginIpThrottle(ip=stale, failures=3, window_started_at=now - timedelta(days=9),
                           last_failed_at=now - timedelta(days=9)))
    db.commit()

    # 强制「距上次清理已过很久」，模拟隔了一段时间才又有登录失败
    auth_mod._last_purge_at = 0.0
    with peer(uniq_ip()) as c:
        _fail(c, "触发一次失败")
    assert _state(db, stale) is None, "过期行没被顺带清掉"


def test_显式关闭节流时不留任何状态(client, db, monkeypatch, make_user):
    """关掉是真的不写库（不是「写了但不生效」）—— 否则表会一直涨。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "login_ip_throttle_enabled", False, raising=False)
    ip = uniq_ip()
    with peer(ip) as c:
        for i in range(8):
            assert _fail(c, "不存在的%d" % i).status_code == 401
    assert _state(db, ip) is None


# ==================================================== 6. 默认值与硬锁开关


def test_默认值站在共享IP安全的那一侧():
    """阈值 30 / 窗口 15 分钟 / 递增延迟封顶 1 秒，且**硬锁默认关闭**。

    取值理由写在 config.py 里，这里只钉住「别被顺手改小」：
    阈值一旦掉到 5（账号那一档），一个共享出口 IP 上几十个人正常输错口令
    就会把全办公室挡在门外 —— 那是把防护变成了故障。
    """
    from app.core.config import Settings

    s = Settings(app_env="dev", jwt_secret="x" * 40)
    assert s.login_ip_throttle_enabled is True
    assert s.login_ip_max_failures == 30
    assert s.login_ip_window_seconds == 900
    assert s.login_ip_delay_step_ms == 200
    assert s.login_ip_delay_max_ms == 1000
    assert s.login_ip_hard_lock is False, "IP 硬锁默认必须是关的"
    assert s.trusted_proxies == "", "默认一个代理都不信（否则 XFF 可伪造）"
    # 清理保留期必须是窗口的整数倍且明显更长：刚出窗口的那几行还要用来判定
    # 「这个源是不是刚被放过」，删早了等于让一次节流凭空消失。
    assert s.login_ip_state_ttl_hours * 3600 >= s.login_ip_window_seconds * 9, (
        f"保留期 {s.login_ip_state_ttl_hours}h 相对窗口 "
        f"{s.login_ip_window_seconds}s 太短，会把还在用的状态清掉")


def test_硬锁开关打开后行为完全不同(client, db, monkeypatch, make_user):
    """默认关闭的那条路也要**测到**，否则它是没人跑过的代码。

    打开后语义变成「锁住这个来源」：423，且 blocked_until 被写上。
    它适合的是登录页完全对外暴露、且一人一户的部署。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "login_ip_max_failures", 3, raising=False)
    monkeypatch.setattr(settings, "login_ip_hard_lock", True, raising=False)
    monkeypatch.setattr(settings, "login_ip_hard_lock_minutes", 15, raising=False)

    victims = make_user(4)
    ip = uniq_ip()
    with peer(ip) as c:
        for v in victims[:3]:
            _fail(c, v.username)
        r = _fail(c, victims[3].username)
    assert r.status_code == 423, f"开了硬锁却回 {r.status_code}"

    row = _state(db, ip)
    assert row is not None and row.blocked_until is not None, "硬锁没有写 blocked_until"
    # 账号仍然一个都没被锁 —— 硬锁锁的是「源」，不是「账号」，这两件事不同
    for v in victims:
        db.refresh(v)
        assert v.locked_until is None


def test_硬锁锁期只延长不缩短(client, db, monkeypatch, make_user):
    """连续失败把锁期往后推，而不是各算各的又互相盖掉。"""
    from app.api.auth import _bump_ip_failure
    from app.core.config import settings

    monkeypatch.setattr(settings, "login_ip_hard_lock", True, raising=False)
    monkeypatch.setattr(settings, "login_ip_hard_lock_minutes", 10, raising=False)
    monkeypatch.setattr(settings, "login_ip_max_failures", 2, raising=False)

    ip = uniq_ip()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _bump_ip_failure(db, ip, now)
    _bump_ip_failure(db, ip, now)
    first = _state(db, ip).blocked_until
    later = now + timedelta(seconds=30)
    _bump_ip_failure(db, ip, later)
    db.commit()
    second = _state(db, ip).blocked_until
    assert second > first, "锁期应当被推到更远，而不是被后来的请求拉近"


# ============================================================ 7. 结构与幂等


def test_表由迁移建出来且与模型一致(db):
    """这张表必须由 revision 建、而不是靠 create_all（P3-01 的口径）。"""
    from sqlalchemy import text

    cols = set(db.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'login_ip_throttle'")).scalars())
    assert cols == {"id", "ip", "failures", "window_started_at", "last_failed_at",
                    "blocked_until", "created_at", "updated_at"}, sorted(cols)
    names = set(db.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'login_ip_throttle'"
    )).scalars())
    # uq_ 那条是唯一约束自动建的索引，所以断言的是「索引齐了」而不是「只有一条」
    assert {"uq_login_ip_throttle_ip", "ix_login_ip_throttle_last_failed"} <= names, sorted(names)


def test_0008_重复执行不改变结构(db, monkeypatch):
    """幂等：运维会重跑 ``upgrade head``，重跑不该在这张表上多出第二份结构。

    直接调 ``upgrade()`` 本体（而不是 ``command.upgrade``）：后者靠版本号
    决定跑不跑，跑第二次时它压根不会调用本函数，测不到幂等。
    """
    import importlib.util
    import pathlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" \
        / "0008_login_ip_throttle.py"
    spec = importlib.util.spec_from_file_location("rev_0008", path)
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)

    conn = db.connection()

    def snapshot():
        from sqlalchemy import text

        return (
            tuple(sorted(conn.execute(text(
                "SELECT column_name || ':' || data_type FROM information_schema.columns "
                "WHERE table_name = 'login_ip_throttle'")).scalars())),
            tuple(sorted(conn.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'login_ip_throttle'"
            )).scalars())),
        )

    monkeypatch.setattr(rev, "op", Operations(MigrationContext.configure(conn)))
    before = snapshot()
    rev.upgrade()
    rev.upgrade()
    assert snapshot() == before, "0008 不幂等：重复执行改动了结构"
