"""管理员写路由的「近期二次验证」闸门 —— 复核（2026-10-25）实测出的四条绕过。

**这个文件钉的是什么。** ``require_recent_admin_mfa``（``core/deps.py``）的全部
意义是「把危险动作与『令牌刚被偷』解耦」：一枚**没有** ``mfa_at`` 的管理员
令牌（MFA 开关打开之前签发的，最长还有 4 小时有效）不该能做高危动作。

复核方在实测里跑通了四条绕过路径，本文件逐条钉死。**两头都钉**：

* 没有 ``mfa_at`` ⇒ **403**（连数据都不能落地）；
* 带着 ``mfa_at`` ⇒ **照常能做事**（闸门不是「一律拒绝」）。

**「两头都钉」不是形式主义。** 只钉「拒绝」的那一半，会让「把依赖挂上」
这件事看起来像实现了 —— 实际上一个**永远拒绝**的闸门也能让拒绝用例全绿，
而系统就此瘫掉（管理员再也改不了任何配置）。只钉「放行」的那一半，则
挡不住真正的旁路。

---- 一、缺陷①：``PUT /api/system/notify`` 不挂二次验证 → 整道闸门从侧翼被击穿

复核方跑通的**完整链条**（修复前实测）::

    [旁路 0] 建号(受管控)              -> 403「该操作需要近期完成邮箱二次验证…」
    [旁路 1] PUT /api/system/notify     -> 200   ← 这条路由没挂二次验证
    [旁路 1] 现在的收件人 = ['attacker@evil.test']
    [旁路 2] POST /api/auth/mfa/reauth  -> 200，邮件真的发给了 attacker@evil.test
    [旁路 3] 点真链接                     -> 200
    [旁路 4] POST /api/auth/mfa/poll     -> 200，拿到一枚带 mfa_at 的令牌
    [旁路 5] 建号(受管控)              -> 201，账号真的建出来了

**根因**：管理员二次验证邮件**没有 per-user 收件人**（``sys_user`` 表里没有
邮箱列），统一发给全局 ``notify_config.recipients``。于是「改收件人」这个动作
**恰恰就是让二次验证生效的那个开关**，而它当时不需要二次验证。

**两道锁，缺一不可**：

#. 路由挂上 ``require_recent_admin_mfa``（挡住旁路 1）；
#. **投递目标一变，作废所有未完成的挑战**（挡住「先改收件人、再等一个
   **旧**挑战被点开」这条路径 —— 那一封邮件是按旧投递目标投出去的）。

---- 二、缺陷②：``POST /api/system/mcp-tokens`` 不挂二次验证 → 铸出永不过期的读凭据

复核方实测（修复前）::

    [MCP 旁路 0] 建号(受管控)              -> 403
    [MCP 旁路 1] POST /api/system/mcp-tokens -> 201，明文 = mcp_…（长度 47）
    [MCP 旁路 2] 用该令牌调 /api/mcp        -> http=200（读到了账套清单）

**为什么它与「铸出比令牌活得久的新凭据」同档**：这个模块自己的说明写着
「**因为令牌永不过期，吊销是唯一的安全阀**」（``app/api/mcp_admin.py`` 模块
注释）。管理员访问令牌 4 小时后失效，这枚 MCP 令牌**永久有效**，且能读该
用户能读的一切账套。

**吊销类路由刻意不挂**：吊销是安全阀，令牌被盗时管理员要能立刻吊销，而
那时他很可能正处在「没有 ``mfa_at``」的状态上（token 刚被偷）。给安全阀
加锁是把安全阀焊死。

---- 三、缺陷④：其余未挂二次验证的管理员写路由

逐条判断见 ``app/api/system.py`` / ``app/api/mcp_admin.py`` 里各路由的注释。
本文件钉住**决定要挂**的那些（403 / 放行两头）与**决定不挂**的那些
（安全阀必须仍然好用）。
"""
from __future__ import annotations

import re
import time

import pytest

from app.core import system_settings
from app.core.security import create_access_token
from app.models import AdminMfaChallenge, AuditLog, McpToken, SysUser
from app.models.mfa import STATUS_EXPIRED, STATUS_PENDING, STATUS_VERIFIED
from app.models.plugin import PluginRegistry
from app.models.scheduler import ScheduledTask
from app.notify import config as notify_config
from app.notify import mailer
from app.notify import mfa as mfa_service
from app.scheduler import service as scheduler_service


# --------------------------------------------------------------------- 夹具


@pytest.fixture
def mfa_switch_on(db):
    """把二次验证的**开关**打开（只改库里那一位）。

    只改 ``sys_setting``、不动 env —— 「库里优先、env 兜底」是这套约定
    本身要验的东西。
    """
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()


@pytest.fixture
def stolen_admin_token(client, admin_user, db):
    """一枚**没有** ``mfa_at`` 的管理员令牌（MFA 开关已开）。

    复现的是真实的攻击窗口：令牌在 MFA 开关打开**之前**签发，之后开关
    被打开 —— 令牌本身还没过期（管理员令牌 4 小时），但它**没有** ``mfa_at``。
    """
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "0")
    db.commit()
    r = client.post("/api/auth/login",
                    json={"username": admin_user.username, "password": "admin123"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    # 闸门随后打开（模拟运维在某个时刻打开了二次验证）。
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fresh_admin_token(admin_user, db):
    """一枚**带** ``mfa_at`` 的管理员令牌（刚做完二次验证）。"""
    system_settings.set_value(db, mfa_service.MFA_ENABLED_KEY, "1")
    db.commit()
    token, _ = create_access_token(
        admin_user.id, admin_user.username, is_admin=True,
        token_version=admin_user.token_version or 0,
        mfa_at=int(time.time()))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sent_mails(monkeypatch):
    """把真发信换成一个记录器（只换 ``mailer.send_mail``，其余照跑）。"""
    sent: list[dict] = []

    def fake_send_mail(config, to_addresses, subject, body):
        sent.append({"to": list(to_addresses), "subject": subject, "body": body})
        return len(to_addresses)

    monkeypatch.setattr(mailer, "send_mail", fake_send_mail)
    return sent


@pytest.fixture
def plugin_task(db):
    """一条**插件**的定时任务（开关那条路由只认插件自己的任务）。"""
    plugin_id = "probe-plugin"
    task_name = "nightly"
    digest = "d" * 64
    db.add(PluginRegistry(
        plugin_id=plugin_id, version="1.0.0", plugin_digest=digest,
        manifest_version=1, desired_enabled=True, desired_revision="rev-1",
        access_default="none", runtime_status="PENDING_RESTART",
        tables=[], manifest_metadata={"id": plugin_id}))
    db.add(ScheduledTask(
        task_key=scheduler_service.task_key(plugin_id, task_name),
        owner_type="PLUGIN", plugin_id=plugin_id, task_name=task_name,
        plugin_digest=digest, schedule={"type": "interval", "minutes": 30},
        enabled=True, next_run_at=scheduler_service.utcnow()))
    db.commit()
    return plugin_id, task_name


# --------------------------------------------------------------------- 助手


def _wait_for_mail(sent, timeout=3.0, count=1) -> dict:
    """等第 ``count`` 封验证邮件发出来（它是**异步**的）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(sent) >= count:
            return sent[count - 1]
        time.sleep(0.02)
    raise AssertionError(f"验证邮件没有发出来（只到了 {len(sent)} 封）")


def _open_challenge(db, user, *, verified: bool = False) -> AdminMfaChallenge:
    """造一条未完成的挑战（``PENDING`` 或已点开的 ``VERIFIED``）。"""
    row, email_token, _secret = mfa_service.create_challenge(db, user, reason="login")
    if verified:
        ok, reason, _row = mfa_service.verify_email_token(db, email_token)
        assert ok and reason == "ok", (ok, reason)
    db.commit()
    db.expire_all()
    return db.get(AdminMfaChallenge, row.id)


def _audit_rows(db, action: str) -> list[AuditLog]:
    db.expire_all()
    return list(db.query(AuditLog).filter(AuditLog.action == action).all())


# ================================================================== 缺陷 ①


def test_没有近期二次验证改不了收件人(client, db, admin_user, stolen_admin_token):
    """**旁路 1 必须是 403** —— 改收件人就是改「二次验证本身的那个开关」。"""
    r = client.put("/api/system/notify",
                   json={"recipients": "attacker@evil.test"},
                   headers=stolen_admin_token)
    assert r.status_code == 403, r.text
    assert "二次验证" in r.text, r.text

    # **403 必须真的没改** —— 只回一句提示却把收件人换了是最坏的形态。
    db.expire_all()
    assert "attacker@evil.test" not in notify_config.recipients(db), \
        notify_config.recipients(db)


def test_没有近期二次验证发不了测试信(client, stolen_admin_token):
    """``POST /api/system/notify/test`` 用的是**当前**收件人 —— 同样要闸。

    没有这道闸，一个刚改完收件人的攻击者就能确认「自己的地址真的收得到信」。
    """
    r = client.post("/api/system/notify/test", headers=stolen_admin_token)
    assert r.status_code == 403, r.text
    assert "二次验证" in r.text, r.text


def test_带近期二次验证就能改收件人(client, db, admin_user, fresh_admin_token):
    """**放行的那一半也要钉**：闸门不是「一律拒绝」。"""
    r = client.put("/api/system/notify",
                   json={"recipients": "ops@example.com"},
                   headers=fresh_admin_token)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert "ops@example.com" in notify_config.recipients(db)


def test_改收件人会作废未完成的挑战(client, db, admin_user, fresh_admin_token):
    """**第二道锁**：投递目标一变，所有未完成的挑战一律作废。

    挡住的是「先改收件人、再等一个**旧**挑战被点开」：那一封邮件是按旧
    投递目标投出去的，截获了它的人仍然能点开那条链接换到令牌。
    """
    row = _open_challenge(db, admin_user)
    assert row.status == STATUS_PENDING

    r = client.put("/api/system/notify",
                   json={"recipients": "ops@example.com"},
                   headers=fresh_admin_token)
    assert r.status_code == 200, r.text

    db.expire_all()
    assert db.get(AdminMfaChallenge, row.id).status == STATUS_EXPIRED, \
        "改了收件人，之前发出去的挑战还活着 —— 截获了旧邮件的人仍能换到令牌"


def test_改收件人也会作废已点开但未换令牌的挑战(client, db, admin_user,
                                                fresh_admin_token):
    """``VERIFIED`` 也是「还没用掉的凭据」—— 链接点开了不代表令牌已经换走。"""
    row = _open_challenge(db, admin_user, verified=True)
    assert row.status == STATUS_VERIFIED

    r = client.put("/api/system/notify",
                   json={"recipients": "ops@example.com"},
                   headers=fresh_admin_token)
    assert r.status_code == 200, r.text

    db.expire_all()
    assert db.get(AdminMfaChallenge, row.id).status == STATUS_EXPIRED


def test_改发信服务器同样作废挑战(client, db, admin_user, fresh_admin_token):
    """换掉 SMTP 服务器 = 换掉二次验证的**投递通道**，同属投递目标变更。"""
    row = _open_challenge(db, admin_user)

    r = client.put("/api/system/notify",
                   json={"host": "smtp2.example.com"},
                   headers=fresh_admin_token)
    assert r.status_code == 200, r.text

    db.expire_all()
    assert db.get(AdminMfaChallenge, row.id).status == STATUS_EXPIRED


def test_没改投递目标就不作废挑战(client, db, admin_user, fresh_admin_token):
    """**反向确认**：改个端口不该把别人正在进行的登录打断掉。"""
    row = _open_challenge(db, admin_user)

    r = client.put("/api/system/notify",
                   json={"port": 2525},
                   headers=fresh_admin_token)
    assert r.status_code == 200, r.text

    db.expire_all()
    assert db.get(AdminMfaChallenge, row.id).status == STATUS_PENDING


def test_作废挑战要留审计(client, db, admin_user, fresh_admin_token):
    """作废了几条必须查得到 —— 否则事后只剩「有人改过收件人」这一句话。"""
    _open_challenge(db, admin_user)

    r = client.put("/api/system/notify",
                   json={"recipients": "ops@example.com"},
                   headers=fresh_admin_token)
    assert r.status_code == 200, r.text

    rows = _audit_rows(db, "SYSTEM_MFA_CHALLENGES_INVALIDATED")
    assert rows, "作废了二次验证挑战却没有留审计"
    assert rows[-1].after.get("expired_challenges") == 1, rows[-1].after
    assert "ops@example.com" in str(rows[-1].after), rows[-1].after


def test_改收件人的审计本来就覆盖收件人与口令提示(client, db, admin_user,
                                              fresh_admin_token):
    """``SYSTEM_NOTIFY_CONFIG_UPDATE`` 的 before/after 必须能看到收件人变化。"""
    client.put("/api/system/notify",
               json={"recipients": "ops@example.com", "password": "s3cret"},
               headers=fresh_admin_token)

    rows = _audit_rows(db, "SYSTEM_NOTIFY_CONFIG_UPDATE")
    assert rows, "改通知配置没有留审计"
    entry = rows[-1]
    assert "ops@example.com" in str(entry.after), entry.after
    assert entry.after.get("password_hint"), entry.after


def test_改收件人这条旁路已经封死(client, db, admin_user, stolen_admin_token,
                                fresh_admin_token, sent_mails):
    """**复核方那条完整链条，现在在第 1 步就断了。**

    这里跑的是**整条链**，不是单点断言：把收件人配好（用带 ``mfa_at`` 的
    令牌），再用**没有** ``mfa_at** 的那枚去改 —— 改不动，于是下一次
    ``reauth`` 的邮件仍然发给**原来那个**收件人，攻击者拿不到令牌，
    整条链在第 2 步就断了。
    """
    # 先把通道配好（这一步用的是**合法**的、带 mfa_at 的令牌）。
    r = client.put("/api/system/notify",
                   json={"host": "smtp.example.com", "port": 25,
                         "from_address": "noreply@example.com",
                         "recipients": "ops@example.com"},
                   headers=fresh_admin_token)
    assert r.status_code == 200, r.text

    # 旁路 0：受管控的动作确实被 403 拦着（对照）。
    r0 = client.post("/api/system/users",
                     json={"username": "probe_bypass", "password": "probe123456"},
                     headers=stolen_admin_token)
    assert r0.status_code == 403, r0.text

    # 旁路 1：改收件人 → **403**，收件人没变。
    r1 = client.put("/api/system/notify",
                    json={"host": "smtp.evil.test", "port": 25,
                          "username": "u", "password": "p",
                          "from_address": "a@evil.test",
                          "recipients": "attacker@evil.test"},
                    headers=stolen_admin_token)
    assert r1.status_code == 403, r1.text
    db.expire_all()
    assert "attacker@evil.test" not in notify_config.recipients(db)
    assert "ops@example.com" in notify_config.recipients(db)

    # 旁路 2：仍然可以发起 reauth（它本来就不是高危动作），但那封邮件
    # **发不到攻击者那里** —— 它发给了原来的收件人。
    r2 = client.post("/api/auth/mfa/reauth", headers=stolen_admin_token)
    assert r2.status_code == 200, r2.text
    mail = _wait_for_mail(sent_mails)
    assert mail["to"] == ["ops@example.com"], mail["to"]

    # 旁路 3/4：攻击者手里没有那封邮件里的链接 ⇒ 换不到令牌。
    # （他的 client_secret 只能换到自己那条挑战，而那条挑战没被点开 ⇒
    #   轮询只会回「PENDING」，**拿不到令牌**。）
    out = r2.json()
    r4 = client.post("/api/auth/mfa/poll",
                     json={"challenge_id": out["challenge_id"],
                           "client_secret": out["client_secret"]})
    assert r4.status_code == 200, r4.text
    assert r4.json().get("status") == "PENDING", r4.text
    assert not r4.json().get("access_token"), r4.text

    # 旁路 5：建号仍然被 403 拦着。
    r5 = client.post("/api/system/users",
                     json={"username": "probe_bypass_ok", "password": "probe123456"},
                     headers=stolen_admin_token)
    assert r5.status_code == 403, r5.text
    db.expire_all()
    assert db.query(SysUser).filter(SysUser.username == "probe_bypass_ok").count() == 0


# ================================================================== 缺陷 ②


def test_没有近期二次验证铸不出MCP令牌(client, db, admin_user, stolen_admin_token):
    """**MCP 令牌永不过期** —— 它是「比令牌活得久的新凭据」，必须同档管控。"""
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "probe-agent", "user_id": admin_user.id},
                    headers=stolen_admin_token)
    assert r.status_code == 403, r.text
    assert "二次验证" in r.text, r.text
    db.expire_all()
    assert db.query(McpToken).count() == 0, "被拒的请求居然铸出了令牌"


def test_带近期二次验证可以铸MCP令牌(client, db, admin_user, fresh_admin_token):
    """**放行的那一半也要钉。**"""
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "probe-agent", "user_id": admin_user.id},
                    headers=fresh_admin_token)
    assert r.status_code == 201, r.text
    assert r.json()["token"], r.text


def test_没有近期二次验证不能轮换MCP令牌(client, db, admin_user,
                                        fresh_admin_token, stolen_admin_token):
    """**轮换 = 又一次铸出永久凭据**（新的那枚从头到尾不过期）。"""
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "probe-agent", "user_id": admin_user.id},
                    headers=fresh_admin_token)
    assert r.status_code == 201, r.text
    token_id = r.json()["id"]

    r2 = client.post(f"/api/system/mcp-tokens/{token_id}/rotate",
                     headers=stolen_admin_token)
    assert r2.status_code == 403, r2.text
    assert "二次验证" in r2.text, r2.text

    # **旧令牌不能因为「轮换被拒」而失效** —— 那会把外部 agent 直接断线。
    db.expire_all()
    row = db.get(McpToken, token_id)
    assert row.revoked_at is None, "轮换被拒却把旧令牌作废了"
    assert row.grace_until is None, "轮换被拒却让旧令牌进了宽限期"


def test_带近期二次验证可以轮换MCP令牌(client, db, admin_user, fresh_admin_token):
    """**放行的那一半也要钉。**"""
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "probe-agent", "user_id": admin_user.id},
                    headers=fresh_admin_token)
    assert r.status_code == 201, r.text
    token_id = r.json()["id"]

    r2 = client.post(f"/api/system/mcp-tokens/{token_id}/rotate",
                     headers=fresh_admin_token)
    assert r2.status_code == 200, r2.text
    assert r2.json()["token"], r2.text


def test_吊销MCP令牌不挂二次验证(client, db, admin_user, fresh_admin_token,
                                stolen_admin_token):
    """**安全阀不能被焊死。**

    令牌刚被偷时，管理员手里那枚令牌多半**没有** ``mfa_at`` —— 他必须
    能立刻吊销。所以吊销类路由刻意不挂这道闸。
    """
    r = client.post("/api/system/mcp-tokens",
                    json={"name": "probe-agent", "user_id": admin_user.id},
                    headers=fresh_admin_token)
    assert r.status_code == 201, r.text
    token_id = r.json()["id"]

    r2 = client.delete(f"/api/system/mcp-tokens/{token_id}",
                       headers=stolen_admin_token)
    assert r2.status_code == 200, r2.text
    db.expire_all()
    assert db.get(McpToken, token_id).revoked_at is not None, \
        "没有 mfa_at 的管理员竟然没能吊销令牌 —— 安全阀被焊死了"


def test_批量吊销MCP令牌不挂二次验证(client, db, admin_user, fresh_admin_token,
                                  stolen_admin_token):
    """批量吊销同属安全阀。"""
    client.post("/api/system/mcp-tokens",
                json={"name": "probe-agent", "user_id": admin_user.id},
                headers=fresh_admin_token)

    r = client.delete(f"/api/system/mcp-tokens?user_id={admin_user.id}",
                      headers=stolen_admin_token)
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] >= 1, r.text


# ================================================================== 缺陷 ④
#
# 下面两组分别钉「决定要挂的」与「决定不挂的」。
# **不挂的那一组同样重要**：给止损类路由加锁是把安全阀焊死。

@pytest.mark.parametrize("method,path,kwargs", [
    ("put", "/api/system/ocr", {"json": {"api_url": "https://ocr.example.com",
                                          "api_token": "third-party-secret"}}),
    ("post", "/api/system/users/1/unlock", {}),
])
def test_改写凭据类与放松限制类的写路由都要近期二次验证(client, admin_user,
                                                        stolen_admin_token,
                                                        method, path, kwargs):
    """**铸造/改写凭据**（OCR 的第三方 token）与**放松安全限制**（解锁别人的
    账号）都改写了「别人能不能进来」这件事 ⇒ 与 §3.2 同档。

    闸门在依赖里解析，所以它**先于**路由体执行 —— 不管参数对不对、
    目标存不存在，回的都是同一个 403。
    """
    path = path.replace("/1/unlock", f"/{admin_user.id}/unlock")
    r = getattr(client, method)(path, headers=stolen_admin_token, **kwargs)
    assert r.status_code == 403, (path, r.status_code, r.text)
    assert "二次验证" in r.text, (path, r.text)


def test_插件定时任务的开关要近期二次验证(client, db, plugin_task,
                                        stolen_admin_token):
    """**插件任务跑在宿主进程里** —— 开关它 = 让一段外部代码开始/停止执行。"""
    plugin_id, task_name = plugin_task
    r = client.put(f"/api/system/scheduler/tasks/{plugin_id}/{task_name}",
                   json={"enabled": False}, headers=stolen_admin_token)
    assert r.status_code == 403, r.text
    assert "二次验证" in r.text, r.text

    db.expire_all()
    row = db.get(ScheduledTask, scheduler_service.task_key(plugin_id, task_name))
    assert row.enabled is True, "被拒的请求居然把插件任务的开关动了"


def test_带近期二次验证可以改OCR配置(client, db, fresh_admin_token):
    """**放行的那一半也要钉。**"""
    r = client.put("/api/system/ocr",
                   json={"api_url": "https://ocr.example.com",
                         "api_token": "third-party-secret"},
                   headers=fresh_admin_token)
    assert r.status_code == 200, r.text
    db.expire_all()
    from app.core import system_settings as ss
    assert ss.get_value(db, "ocr_api_token") == "third-party-secret"


def test_带近期二次验证可以解锁用户(client, db, admin_user, fresh_admin_token):
    """**放行的那一半也要钉。**"""
    db.expire_all()
    target = db.get(SysUser, admin_user.id)
    target.locked_until = None
    target.failed_logins = 3
    target.lock_level = 1
    db.commit()

    r = client.post(f"/api/system/users/{admin_user.id}/unlock",
                    headers=fresh_admin_token)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(SysUser, admin_user.id).failed_logins == 0


def test_带近期二次验证可以开关插件定时任务(client, db, plugin_task,
                                           fresh_admin_token):
    """**放行的那一半也要钉。**"""
    plugin_id, task_name = plugin_task
    r = client.put(f"/api/system/scheduler/tasks/{plugin_id}/{task_name}",
                   json={"enabled": False}, headers=fresh_admin_token)
    assert r.status_code == 200, r.text
    db.expire_all()
    row = db.get(ScheduledTask, scheduler_service.task_key(plugin_id, task_name))
    assert row.enabled is False


@pytest.mark.parametrize("method,path,kwargs", [
    ("delete", "/api/system/scheduler/secret/previous", {}),
])
def test_止损类路由不挂二次验证(client, admin_user, stolen_admin_token,
                               method, path, kwargs):
    """**停用旧的调度触发密钥 = 杀掉一个凭据 = 收紧。** 不挂。

    挂上去的后果很具体：轮换流程里「换完之后把旧密钥作废」这一步需要
    管理员**恰好**刚做完二次验证，否则外部调度服务会在换密钥的过程中
    突然调不动。
    """
    r = getattr(client, method)(path, headers=stolen_admin_token, **kwargs)
    assert r.status_code == 200, (path, r.status_code, r.text)
    assert "二次验证" not in r.text, (path, r.text)


@pytest.mark.parametrize("method,path,kwargs", [
    ("post", "/api/system/backup/test", {}),
    ("post", "/api/system/backup/run", {}),
])
def test_诊断与跑任务类路由不挂二次验证(client, admin_user, stolen_admin_token,
                                     method, path, kwargs):
    """**诊断**（只测连通、不回显凭据）与**跑任务**（往 OSS 上传**密文**
    制品，密文闸还在）都不铸造凭据、不改写权限面 ⇒ 不挂。

    这里断言的是「**不是**被二次验证挡下的」：没配 OSS 时它们会回 400
    （配置不全），但那与二次验证无关。
    """
    r = getattr(client, method)(path, headers=stolen_admin_token, **kwargs)
    assert r.status_code != 403, (path, r.status_code, r.text)
    assert "二次验证" not in r.text, (path, r.text)
