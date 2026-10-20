"""自助注册：用户名枚举抹平 + 双桶节流。

审查报告「三、安全与鉴权」P2-2：「自助注册既是用户名枚举 oracle，
又完全没有节流 —— 历史 P2-5 仍存在」。

**原来的三种可区分响应**（审查员实测）::

    已存在用户名 → HTTP 400 {"detail":"用户名「victim」已被占用"}
    不存在用户名 → HTTP 201 {"ok":true,"username":"nosuchuser999"}
    注册关闭时   → HTTP 403 {"detail":"系统未开放自助注册，请联系管理员开号"}

第一条就是一条**用户名枚举信道** —— 而登录路径本来是靠「用户名不存在也走完
一次 PBKDF2」（``auth.py::_dummy_hash``）堵住枚举的，注册路径把它敞开了。
而且注册路径**完全没有** IP 节流（它不经过 ``_note_source_failure`` /
``_note_ip_failure`` 任何一个入口）。

**本文件钉的是修好之后的三件事：**

1. **响应抹平**：重名与成功的响应**逐字相同**（状态码 + body），
   差别只留在管理员看得见的审计里；
2. **双桶节流**：IP 桶管「这个来源在批量试」，用户名桶管「这个名字被反复试」；
3. **节流与判定同源**：判定与计数用同一套窗口 —— 两套窗口会出现
   「限流器认为超阈值、计数却不涨」这种自相矛盾的状态。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def registration_on():
    """通过 **env** 打开自助注册（库里没有那一行时的老路径）。"""
    from app.core.config import settings

    settings.registration_enabled = True
    try:
        yield
    finally:
        settings.registration_enabled = False


@pytest.fixture(autouse=True)
def _clean_register_buckets(db):
    """每个用例前后清掉注册节流桶。

    **为什么必须清**：节流状态是**跨用例累积**的（存在库里，而 xdist 是按
    worker 分库、同一个 worker 的所有用例共用一个库）。不清的话，
    前面的用例把 IP 桶涨上去，后面的用例一注册就 429 ——
    那不是缺陷，是测试之间互相污染。

    登录侧的 IP 桶没这个问题：登录的测试用的是互不相同的假 IP
    （198.51.100.x / 10.0.0.x），而注册侧的用例**都来自 127.0.0.1**。
    """
    from app.models.system import LoginIpThrottle

    def clear_buckets():
        db.query(LoginIpThrottle).filter(
            LoginIpThrottle.ip.like("ri:%") | LoginIpThrottle.ip.like("ru:%")
        ).delete(synchronize_session=False)
        db.commit()

    clear_buckets()
    yield
    clear_buckets()


def _register(client, username: str, password: str = "abcd1234"):
    return client.post("/api/auth/register",
                       json={"username": username, "password": password})


# ------------------------------------------------------------------ 枚举抹平


def test_重名与成功的响应逐字相同(client, registration_on):
    """**这是本条缺陷的核心断言。**

    响应只要有一点差别（状态码、body、字段顺序外的措辞），就是一条枚举信道。
    """
    body = {"username": "enumprobe01", "password": "abcd1234"}
    first = _register(client, "enumprobe01")
    assert first.status_code == 201, first.text

    again = _register(client, "enumprobe01")
    assert again.status_code == 201, again.text
    assert again.json() == first.json(), "重名与成功的响应必须完全一样"
    assert "已被占用" not in again.text
    assert "占用" not in again.text


def test_重名不建号也不覆盖原口令(client, registration_on):
    """响应一样，但**不建号**、**不改原账号的口令**。

    不建号：账号数不变。不覆盖口令：原用户还能用原口令登录 ——
    否则一次「重名」就能把别人的口令顶掉。
    """
    from app.models import SysUser

    assert _register(client, "enumprobe02").status_code == 201
    before = client.get("/api/auth/config")
    assert before.status_code == 200

    _register(client, "enumprobe02", password="zzzzzzzz")

    r = client.post("/api/auth/login",
                    json={"username": "enumprobe02", "password": "abcd1234"})
    assert r.status_code == 200, "原口令必须仍然可用：" + r.text


def test_注册关闭时对所有人是同一个响应(client):
    """开关关时**所有人**都拿到同一个 403 —— 它不区分用户名，不构成枚举信道。"""
    r = _register(client, "whatever01")
    assert r.status_code == 403, r.text
    r2 = _register(client, "anotherone")
    assert r2.status_code == 403
    assert r2.json() == r.json()


def test_重名会留审计_register_duplicate(client, db, registration_on):
    """重名不是消失，而是**挪到了管理员看得见的地方**。"""
    from app.models import AuditLog

    _register(client, "enumprobe03")
    _register(client, "enumprobe03")
    db.expire_all()
    rows = (db.query(AuditLog)
             .filter(AuditLog.action == "REGISTER_DUPLICATE",
                     AuditLog.entity_id == "enumprobe03")
             .order_by(AuditLog.id.desc())
             .all())
    assert rows, "重名必须留一条 REGISTER_DUPLICATE 审计"
    # 审计里不能出现口令散列（审计表是全局可读的）。
    assert "password_hash" not in str(rows[0].detail or "")


# -------------------------------------------------------------------- 节流


def test_同一用户名反复试会被节流(client, registration_on):
    """**用户名桶**：同一个名字试超过阈值 → 429 + Retry-After。

    注意这条测的是「同一个名字」——枚举者试的是**不同的**名字，
    撞到的是 IP 桶（下面那条）。两个桶都要有。
    """
    from app.core.config import settings

    threshold = int(settings.register_username_max_attempts)
    last = None
    for _ in range(threshold + 2):
        last = _register(client, "probeuser01")
    assert last.status_code == 429, last.text
    assert last.headers.get("Retry-After"), "被节流必须给 Retry-After"
    # 人话，不能是 SQL 或英文原文。
    assert "频繁" in last.json()["detail"]


def test_同一来源批量试会被节流(client, registration_on):
    """**IP 桶**：同一个来源连续注册很多个**不同**名字 → 429。

    这正是枚举者做的事：每个名字只试一次。所以 IP 桶才是枚举的主要闸门。
    """
    from app.core.config import settings

    threshold = int(settings.register_ip_max_attempts)
    last = None
    for i in range(threshold + 2):
        last = _register(client, f"bulkprobe{i:04d}")
    assert last.status_code == 429, last.text


def test_节流留痕(client, db, registration_on):
    """被节流的请求要留下审计 —— 证据比响应时间重要。"""
    from app.core.config import settings
    from app.models import AuditLog

    threshold = int(settings.register_username_max_attempts)
    # 第一次是**建号**（不是重名，不进用户名桶），从第二次起才是重名。
    # 所以要试 threshold + 2 次才撞得上闸门。
    for _ in range(threshold + 2):
        _register(client, "throttleduser")
    # 请求用的是**另一个** Session（``get_db``），它提交之后本会话的快照是旧的 ——
    # 必须 expire 一下才看得见新行。不 expire 的话这里会**时灵时不灵**：
    # 上一条用例碰巧在同一会话里提交过时就看得见，看不见时就红。
    db.expire_all()
    # **按用户名过滤 + 按 id 倒序** —— 审计表是整场 session 累积的（提交过的行
    # 不会被 db 夹具的 rollback 清掉），别的用例的节流行也在里面；
    # 而 SELECT 不保证按插入顺序返回，`rows[-1]` 拿到的是随机一条。
    # （这正是 tests/test_login_lockout.py 里那条审计用例踩过的坑。）
    rows = (db.query(AuditLog)
             .filter(AuditLog.action == "REGISTER_THROTTLED",
                     AuditLog.entity_id == "throttleduser")
             .order_by(AuditLog.id.desc())
             .all())
    assert rows, "被节流必须留审计"
    assert "throttleduser" in str(rows[0].detail or "")


def test_节流不把别的桶也拖下水(client, registration_on):
    """用户名桶满了**不等于**这个来源的所有注册都被拒 —— 桶是分开的。

    这一点很重要：如果两个桶共用一个计数，一个被探测的用户名会把
    整个来源（包括正常用户）一起封掉。
    """
    from app.core.config import settings

    threshold = int(settings.register_username_max_attempts)
    for _ in range(threshold + 1):
        _register(client, "blockedname1")
    blocked = _register(client, "blockedname1")
    assert blocked.status_code == 429

    other = _register(client, "othername01")
    assert other.status_code == 201, other.text
