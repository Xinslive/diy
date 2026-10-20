"""邮件通知子系统（审计 P2-7 的整改）。

审计 P2-7 的原话：**「安全事件无告警通道，全部只落审计表」**。
账号锁定、IP 在扫、定时任务失败、备份失败 —— 这些最该被**主动发现**的
事件，发现延迟等于人的轮询频率。审计表是证据通道（要人主动去翻），
邮件是检测通道（当时就到）。两个通道各司其职。

**这个文件最重要的一条断言是「发不出去也不影响主流程」。**

通知是**旁路**：没有 SMTP 服务器、口令过期、对方拒收 —— 这些都不该让
一个用户登不进去，或让一个定时任务被判失败。所以每一条失败路径都要
能被证明「主流程毫发无损」。

**为什么用「替换 dispatch_async」而不是等线程。**

``events.dispatch_async`` 起的是守护线程，等它跑完是件 flaky 事
（快慢取决于机器负载）。这里把 ``dispatch_async`` 整个换成一个**同步
记录器**：被测的逻辑（什么时候该发、发的是什么）因此是确定的，
而线程机制本身另有用例单独钉。
"""
from __future__ import annotations

import time

import pytest

from app.notify import config as notify_config
from app.notify import events as notify_events
from app.notify import mailer


@pytest.fixture
def creds(admin_user):
    """admin_user 的登录凭据。

    与 ``test_login_lockout.py`` 里那个同名夹具同一套 —— 那个是本文件
    局部定义的，conftest 里没有，所以这里也要自己带一个。
    """
    return {"username": admin_user.username, "password": "admin123"}


def _reset_notify_config(client, auth) -> None:
    """把通知配置清回「什么都没配」。

    **为什么需要它**：``sys_setting`` 里的行是**跨用例存活**的 ——
    接口里 ``db.commit()`` 提交了，测试夹具的 rollback 只回滚它自己
    那个会话里未提交的改动。所以一个用例配了收件人，下一个用例看到的
    仍然是它 —— 「没配收件人时应当报错」这条就会莫名其妙地不成立。

    这不是测试洁癖：它正是「配置是全局的」这件事在测试里的表现。
    """
    client.put("/api/system/notify", headers=auth, json={
        "host": "", "username": "", "from_address": "",
        "recipients": "", "enabled": True, "clear_password": True,
    })


# ------------------------------------------------------------------ 配置解析


def test_收件人按逗号拆分并去重():
    got = notify_config.parse_recipients("a@x.com, b@x.com ,a@x.com")
    assert got == ["a@x.com", "b@x.com"], "重复的地址应当去重且保持原顺序"


def test_不合法的收件人被跳过而不是整份拒绝():
    """一个地址写错，不该让另外几个也配不上。

    严格校验在别处是对的（参数错就该拒），但收件人是一**串**地址，
    「一个人填错，全组人收不到」是最坏的结果形态。
    """
    got = notify_config.parse_recipients("good@x.com, 不是地址, @bad.com, other@x.com")
    assert got == ["good@x.com", "other@x.com"]


def test_收件人里的换行被拒():
    """换行 = 邮件头注入（一封信发到别处去）。

    这一条不能靠「管理员不会这么填」来防。
    """
    assert not notify_config.valid_recipient("a@x.com\r\nBcc: evil@x.com")
    assert not notify_config.valid_recipient("a@x.com\nevil@x.com")


def test_收件人列表去掉空项():
    assert notify_config.parse_recipients("  ,  ,a@x.com, ") == ["a@x.com"]


# ------------------------------------------------------------------ 配置接口


def test_通知配置接口非管理员进不去(client, plain_auth):
    r = client.get("/api/system/notify", headers=plain_auth)
    assert r.status_code == 403, f"普通用户读到了通知配置：{r.status_code}"


def test_通知配置不回显口令(client, db, admin_user, auth):
    """**口令只写不读** —— 与 OCR token / OSS AccessKey 同一条口径。

    能读出来就意味着它会出现在浏览器历史、网络面板和一次误开的调试
    窗口里。管理员要能回答「到底配没配上」，但不需要把它再读出来。
    """
    r = client.put("/api/system/notify", headers=auth, json={
        "host": "smtp.invalid", "port": 465, "username": "notice@x.com",
        "password": "super-secret-value", "use_ssl": True, "use_tls": False,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["password_set"] is True
    assert body["password_hint"].endswith("alue")
    assert "super-secret-value" not in str(body), "响应里出现了口令明文"

    g = client.get("/api/system/notify", headers=auth)
    assert "super-secret-value" not in g.text, "GET 响应里出现了口令明文"


def test_口令留空表示不修改(client, db, admin_user, auth):
    """界面上那个空的输入框，**不能**等于「清空口令」。

    否则一次普通保存就把凭据悄悄抹掉了，而界面上看不出任何区别 ——
    而抹掉之后通知会静默地发不出去（那正是最该被发现的故障）。
    """
    client.put("/api/system/notify", headers=auth, json={
        "host": "smtp.invalid", "password": "first-secret",
    })
    client.put("/api/system/notify", headers=auth, json={"host": "smtp2.x.com"})
    g = client.get("/api/system/notify", headers=auth)
    assert g.json()["password_set"] is True, "空口令把已配置的口令抹掉了"


def test_清除口令要走显式开关(client, db, admin_user, auth):
    client.put("/api/system/notify", headers=auth, json={
        "host": "smtp.invalid", "password": "first-secret",
    })
    r = client.put("/api/system/notify", headers=auth, json={"clear_password": True})
    assert r.status_code == 200
    assert r.json()["password_set"] is False


def test_收件人里的换行被接口拒掉(client, db, admin_user, auth):
    r = client.put("/api/system/notify", headers=auth, json={
        "recipients": "a@x.com\r\nBcc: evil@x.com",
    })
    assert r.status_code == 422, f"含换行的收件人被接受了：{r.status_code}"


def test_改通知配置要留审计(client, db, admin_user, auth):
    from app.models import AuditLog

    client.put("/api/system/notify", headers=auth, json={
        "host": "smtp.invalid", "password": "secret-value",
        "recipients": "ops@x.com",
    })
    entry = (db.query(AuditLog)
             .filter(AuditLog.action == "SYSTEM_NOTIFY_CONFIG_UPDATE")
             .order_by(AuditLog.id.desc()).first())
    assert entry is not None, "改通知配置没有留痕"
    # 审计里**不能有口令明文** —— 审计表是全局可读的。
    assert "secret-value" not in str(entry.after)
    assert "secret-value" not in str(entry.before)


# ------------------------------------------------------------------ 测试发送


def test_没配收件人时测试发送明确报错(client, db, admin_user, auth):
    _reset_notify_config(client, auth)
    r = client.post("/api/system/notify/test", headers=auth)
    assert r.status_code == 400
    assert "收件人" in r.json()["detail"]


def test_没配服务器时测试发送明确报错(client, db, admin_user, auth):
    client.put("/api/system/notify", headers=auth, json={"recipients": "ops@x.com"})
    r = client.post("/api/system/notify/test", headers=auth)
    assert r.status_code == 400
    assert "SMTP" in r.json()["detail"]


def test_测试发送失败时把原因透出来(client, db, admin_user, auth, monkeypatch):
    """失败原因要可读 —— 「发送失败」四个字对排障没有任何帮助。"""
    client.put("/api/system/notify", headers=auth, json={
        "host": "smtp.invalid", "recipients": "ops@x.com",
    })

    def _boom(config, to_addresses, subject, body):
        raise mailer.MailError("连不上 SMTP 服务器 smtp.invalid:25：Connection refused")

    monkeypatch.setattr(mailer, "send_mail", _boom)
    r = client.post("/api/system/notify/test", headers=auth)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Connection refused" in detail, f"失败原因没有透出来：{detail}"
    # **口令不能出现在错误里**。
    assert "password" not in detail.lower()


# ------------------------------------------------------------------ 事件触发


def test_账号锁定会发通知(client, db, admin_user, creds, monkeypatch):
    sent = []
    monkeypatch.setattr(notify_events, "dispatch_async",
                        lambda event, subject, body: sent.append((event, subject, body)))

    from tests.auth_helpers import 打到硬锁

    # **只有硬锁才发通知**（审查报告安全域 P2-4）：递增延迟只有几秒，
    # 每次撞库口令都发一封的话，收件箱会被刷爆 —— 而收件箱是**通知通道本身**，
    # 真告警会淹没在里面。
    打到硬锁(client, db, admin_user.username, password="错的")

    assert len(sent) == 1, f"账号被硬锁了却没有发通知：{sent}"
    event, subject, body = sent[0]
    assert event == "ACCOUNT_LOCKED"
    assert admin_user.username in subject
    # 正文里要有处置需要的信息：来源 IP、锁多久、第几轮
    assert "来源 IP" in body
    assert "轮" in body


def test_通知发不出去不影响锁定(client, db, admin_user, creds, monkeypatch):
    """**这是本文件最重要的一条。**

    通知是旁路。SMTP 挂了、口令过期了、对方拒收了 —— 这些都不该让
    「账号被锁定」这件事本身不发生，更不该让登录接口抛 500。
    """
    def _boom(event, subject, body):
        raise RuntimeError("SMTP 炸了")

    monkeypatch.setattr(notify_events, "dispatch_async", _boom)

    from tests.auth_helpers import 打到递增延迟

    打到递增延迟(client, admin_user.username, password="错的")
    r = client.post("/api/auth/login", json={**creds, "password": "错的"})
    assert r.status_code in (401, 429), (
        f"通知炸了导致登录接口行为变了：{r.status_code}")

    db.refresh(admin_user)
    assert admin_user.locked_until is not None, "通知发不出去时账号竟然没限流"


def test_dispatch_永不抛异常(db, monkeypatch):
    """``dispatch`` 存在的全部理由就是这一条：它不把异常抛给调用方。"""
    def _boom(config, to_addresses, subject, body):
        raise mailer.MailError("boom")

    monkeypatch.setattr(mailer, "send_mail", _boom)
    # 库里没有 SMTP 配置时它直接跳过，返回 False 而不是抛
    assert notify_events.dispatch("TEST", "标题", "正文") is False


def test_定时任务失败会发通知(db, monkeypatch):
    sent = []
    monkeypatch.setattr(notify_events, "dispatch_async",
                        lambda event, subject, body: sent.append((event, subject, body)))
    notify_events.task_failed(task_key="plugin:demo:sync", error_code="ValueError",
                               message="字段对不上")
    assert len(sent) == 1
    event, subject, body = sent[0]
    assert event == "TASK_FAILED"
    assert "plugin:demo:sync" in subject
    assert "ValueError" in body


def test_备份失败会发通知(db, monkeypatch):
    sent = []
    monkeypatch.setattr(notify_events, "dispatch_async",
                        lambda event, subject, body: sent.append((event, subject, body)))
    notify_events.backup_failed(scope="账套备份（每日 OSS）", message="AccessKey 无效")
    assert len(sent) == 1
    event, subject, body = sent[0]
    assert event == "BACKUP_FAILED"
    # 备份是出事后的退路，这句话值得出现在正文里 —— 它改变的是处置动作
    assert "退路" in body


def test_通知正文不含任何凭据(db, admin_user, client, creds, auth, monkeypatch):
    """通知正文会进管理员的邮箱，**任何凭据都不该出现在里面**。

    这条钉的是一个真实的不变量：通知事件将来可能被扩展（多带几个字段
    进来），而「不小心把某个配置值带进正文」是那种**加字段时顺手就发生**
    的事 —— 所以用「已知的凭据值一个都不许出现」来盯，而不是盯关键词。
    """
    secret = "smtp-secret-9f3a2b"
    client.put("/api/system/notify", headers=auth, json={
        "host": "smtp.invalid", "username": "notice@x.com", "password": secret,
        "recipients": "ops@x.com",
    })

    sent = []
    monkeypatch.setattr(notify_events, "dispatch_async",
                        lambda event, subject, body: sent.append((event, subject, body)))

    from tests.auth_helpers import 打到硬锁

    # **只有硬锁才发通知**（审查报告安全域 P2-4）：递增延迟只有几秒，
    # 每次撞库口令都发一封的话，收件箱会被刷爆 —— 而收件箱是**通知通道本身**，
    # 真告警会淹没在里面。
    打到硬锁(client, db, admin_user.username, password="错的")

    assert len(sent) == 1, f"账号被硬锁了却没有发通知：{sent}"
    _, subject, body = sent[0]

    # SMTP 口令、口令散列、令牌 —— 一个都不许出现
    assert secret not in body and secret not in subject
    assert admin_user.password_hash not in body
    assert "pbkdf2_sha256" not in body


# ------------------------------------------------------------------ 真发一封
#
# 上面那些用例都在验证「配置」与「该不该发」。这一组验证的是**真的发出去
# 了吗、发出去的东西对不对** —— 用一个假 SMTP 客户端接住 send_message，
# 不碰真网络，但走的是 mailer 的完整路径（建连接 → 认证 → 构造 MIME → 投递）。

class _FakeSMTP:
    """只实现 mailer 用到的那几个方法。"""

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.sent = []
        self.logged_in = None
        self.quit_called = False
        # **服务器声明的认证方式**：mailer 在调 login 之前会先看一眼这个
        # （见 mailer._login 的注释）—— 没有它的话，每个用认证的用例都会
        # 被误报成"服务器不提供任何认证方式"。
        self.esmtp_features = {"auth": ["PLAIN"]}

    def ehlo_or_helo_if_needed(self):
        """真客户端在这里会发 EHLO。假客户端什么都不用做。"""

    def starttls(self, context=None):
        self.starttls_called = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message, to_addrs=None):
        self.sent.append((message, to_addrs))

    def quit(self):
        self.quit_called = True

    def close(self):
        pass


def test_真的把信发出去且内容对(monkeypatch):
    import smtplib

    captured = {}

    def _fake_smtp(host, port, timeout=None, context=None):
        client = _FakeSMTP(host, port, timeout, context)
        captured["client"] = client
        return client

    monkeypatch.setattr(smtplib, "SMTP", _fake_smtp)

    config = {"host": "smtp.invalid", "port": 587, "username": "notice@x.com",
              "password": "pw", "from_address": "notice@x.com",
              "use_ssl": False, "use_tls": True, "timeout_seconds": 5}
    count = mailer.send_mail(config, ["ops@x.com", "boss@x.com"],
                             "账号锁定：alice", "正文一行\n正文两行\n")

    client = captured["client"]
    assert count == 2
    assert client.logged_in == ("notice@x.com", "pw"), "没有用配置的账号认证"
    assert client.starttls_called, "use_tls=true 却没有发起 STARTTLS"
    assert client.quit_called, "没有正常关闭连接"

    message, to_addrs = client.sent[0]
    assert to_addrs == ["ops@x.com", "boss@x.com"], "信封里的收件人必须完整"
    assert message["Subject"] == "账号锁定：alice"
    assert message["To"] == "ops@x.com", "第一个收件人落在 To 头上"
    assert "notice@x.com" in message["From"], "发件人地址不对"
    body = message.get_content()
    assert "正文一行" in body


def test_多个收件人不会炸且地址互相不可见(monkeypatch):
    """这条钉的是一个**已经发生过的线上级 bug**。

    原实现想「一个收件人一个 To 头」，但 ``email.policy.default`` 限制
    一封邮件至多一个 To 头 —— 第二个收件人直接 ``ValueError``。因为
    ``events.dispatch`` 承诺永不抛异常，这个异常被吞成一条 warning，
    表现是**配了多个收件人之后，一封告警都发不出去**，而且日志里只有
    一句不起眼的 warning。

    后果的严重性取决于配了几个地址：单收件人时完全正常，多收件人时
    整条告警通道静默失效 —— 而告警通道静默失效恰恰是最不该发生的失效
    方式。所以这里同时钉住两件事：

    1) 多收件人时**不抛异常**；
    2) 收件人之间**互相看不见地址**（Bcc 头必须在投递前删掉，
       否则等于把名单公布出去）。
    """
    import smtplib

    captured = {}

    def _fake_smtp(host, port, timeout=None, context=None):
        client = _FakeSMTP(host, port, timeout, context)
        captured["client"] = client
        return client

    monkeypatch.setattr(smtplib, "SMTP", _fake_smtp)

    config = {"host": "smtp.invalid", "port": 25, "username": "", "password": "",
              "from_address": "noreply@x.com", "use_ssl": False, "use_tls": False,
              "timeout_seconds": 5}
    count = mailer.send_mail(config, ["a@x.com", "b@x.com", "c@x.com"],
                             "标题", "正文")

    assert count == 3
    message, to_addrs = captured["client"].sent[0]
    assert to_addrs == ["a@x.com", "b@x.com", "c@x.com"]
    assert message.get("Bcc") is None, "Bcc 头没删掉，等于把收件人名单公布出去"
    for hidden in ("b@x.com", "c@x.com"):
        assert hidden not in message.as_string(), f"{hidden} 不该出现在报文里"
    assert "a@x.com" in message.as_string()


def test_隐式_TLS_走_SSL_SMTP(monkeypatch):
    """use_ssl=true 时要用 SMTP_SSL，而不是 SMTP + STARTTLS。

    两者搞混的报错出现在**最远的一次连接**上，且报文完全不同 ——
    现场极难复现，所以这里直接盯住「用哪个类」。
    """
    import smtplib

    used = {}

    class _FakeSSL(_FakeSMTP):
        def __init__(self, host, port, timeout=None, context=None):
            super().__init__(host, port, timeout, context)
            used["ssl"] = True

    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSSL)
    monkeypatch.setattr(smtplib, "SMTP",
                        lambda *a, **k: pytest.fail("use_ssl=true 时不该走普通 SMTP"))

    config = {"host": "smtp.invalid", "port": 465, "username": "", "password": "",
              "from_address": "noreply@x.com", "use_ssl": True, "use_tls": False,
              "timeout_seconds": 5}
    mailer.send_mail(config, ["ops@x.com"], "标题", "正文")
    assert used.get("ssl") is True


def test_没有收件人时明确报错():
    with pytest.raises(mailer.MailError):
        mailer.send_mail({"host": "smtp.invalid"}, [], "标题", "正文")


def test_没有配置服务器时明确报错():
    with pytest.raises(mailer.MailError):
        mailer.send_mail({"host": ""}, ["ops@x.com"], "标题", "正文")


# ------------------------------------------------------------------ 限流
#
# 这一组钉的是邮件通道**自己的**攻击面。
#
# 邮件通道和「操作日志」最本质的区别是：后者只有管理员能写，前者**任何
# 人都能触发**。攻击者对一批账号各试三次错误口令，就能让系统发出成百上千
# 封「账号锁定」告警 —— 管理员的邮箱被灌满（真告警淹没在里面），邮件服务商
# 那边这个发件域名也很快会被判成垃圾邮件源，于是连**真正的**告警都发不出。
#
# 这两件事都是攻击者花几分钱就能买到的效果，所以必须在通道这一侧挡住。

def _配好通知(client, auth, recipients="ops@x.com") -> None:
    """把 SMTP 与收件人配好（走真实接口，配置落在库里的那份）。"""
    r = client.put("/api/system/notify", headers=auth, json={
        "host": "smtp.invalid", "port": 25, "username": "", "password": "",
        "from_address": "noreply@x.com", "recipients": recipients,
        "use_ssl": False, "use_tls": False, "timeout_seconds": 5,
    })
    assert r.status_code == 200, r.text


def _假SMTP(记录):
    """造一个假 SMTP 工厂，把每次用到的客户端收进 ``记录``。"""
    def _factory(host, port, timeout=None, context=None):
        client = _FakeSMTP(host, port, timeout, context)
        记录.append(client)
        return client

    return _factory


def test_同类告警超过每窗口上限后不再发信(client, auth, monkeypatch):
    """额度用完就不发了 —— 但**不抛异常**，主流程毫发无损。"""
    import smtplib

    from app.core.config import settings

    _配好通知(client, auth)
    记录 = []
    monkeypatch.setattr(smtplib, "SMTP", _假SMTP(记录))
    monkeypatch.setattr(settings, "notify_rate_limit", 3)
    monkeypatch.setattr(settings, "notify_rate_window_seconds", 600)

    for i in range(10):
        # dispatch 永不抛异常：主流程（登录/定时任务）不能被邮件通道带崩
        assert notify_events.dispatch("ACCOUNT_LOCKED", f"标题{i}", "正文") in (True, False)

    assert len(记录) == 3, f"额度是 3 封，却发了 {len(记录)} 封"


def test_被抑制的条数会补报在下一封信里(client, auth, monkeypatch):
    """**抑制不等于静默丢。**

    被压掉的告警要留下一条计数，由下一封真能发出去的信替它说一声 ——
    「还有 N 条同类告警没单独通知」。否则管理员看到的是"突然安静了"，
    而安静既可能是"没事"也可能是"全被压了"，两者的处置完全不同。
    """
    import smtplib

    from app.core.config import settings

    _配好通知(client, auth)
    记录 = []
    monkeypatch.setattr(smtplib, "SMTP", _假SMTP(记录))
    monkeypatch.setattr(settings, "notify_rate_limit", 2)
    monkeypatch.setattr(settings, "notify_rate_window_seconds", 1)

    # 用光额度，再多打两发（这两发被抑制）
    for i in range(4):
        notify_events.dispatch("ACCOUNT_LOCKED", f"标题{i}", "正文")
    assert len(记录) == 2

    # 窗口过去，额度恢复
    time.sleep(1.1)
    notify_events.dispatch("ACCOUNT_LOCKED", "恢复后的第一封", "正文")
    assert len(记录) == 3, "窗口过去后应当又能发了"

    正文 = 记录[-1].sent[0][0].get_content()
    assert "2 条同类告警未单独通知" in 正文, f"被抑制的条数没有补报：{正文!r}"


def test_发不出去时抑制计数不丢(client, auth, monkeypatch):
    """计数是在投递**前**读进正文的 —— 那封信没送到，计数就得留着。

    否则「还有 N 条没单独通知」这句话随信一起消失了：那 N 条既没单独
    通知、也没被补报，就此无人提起。**静默**是这个通道最坏的失效方式 ——
    比"多发一封"糟得多。
    """
    import smtplib

    from app.core.config import settings

    _配好通知(client, auth)
    记录 = []
    状态 = {"失败": False}

    def _factory(host, port, timeout=None, context=None):
        if 状态["失败"]:
            raise OSError("连不上")
        client = _FakeSMTP(host, port, timeout, context)
        记录.append(client)
        return client

    monkeypatch.setattr(smtplib, "SMTP", _factory)
    monkeypatch.setattr(settings, "notify_rate_limit", 2)
    monkeypatch.setattr(settings, "notify_rate_window_seconds", 1)

    # 用光额度，再多打两发（这两发被抑制）
    for i in range(4):
        notify_events.dispatch("ACCOUNT_LOCKED", f"标题{i}", "正文")
    assert len(记录) == 2

    # 窗口过去、额度恢复，但这封发不出去
    time.sleep(1.1)
    状态["失败"] = True
    assert notify_events.dispatch("ACCOUNT_LOCKED", "发不出去", "正文") is False

    # 再来一封发得出去的：计数必须还在
    状态["失败"] = False
    notify_events.dispatch("ACCOUNT_LOCKED", "发出去了", "正文")
    assert len(记录) == 3
    正文 = 记录[-1].sent[0][0].get_content()
    assert "2 条同类告警未单独通知" in 正文, f"计数随那封失败的信一起丢了：{正文!r}"


def test_不同事件类型的额度互不相干(client, auth, monkeypatch):
    """锁定告警刷屏**不该**把「备份失败」一起压掉。

    共用一个桶的话，攻击者锁 1000 个账号就能顺手把当天的备份告警压掉 ——
    那等于给了他一个"关掉备份告警"的开关。而备份失败恰恰是最该被看到的
    那一封。
    """
    import smtplib

    from app.core.config import settings

    _配好通知(client, auth)
    记录 = []
    monkeypatch.setattr(smtplib, "SMTP", _假SMTP(记录))
    monkeypatch.setattr(settings, "notify_rate_limit", 2)
    monkeypatch.setattr(settings, "notify_rate_window_seconds", 600)

    for i in range(10):
        notify_events.dispatch("ACCOUNT_LOCKED", f"锁定{i}", "正文")
    assert len(记录) == 2

    notify_events.dispatch("BACKUP_FAILED", "备份失败", "正文")
    assert len(记录) == 3, "备份失败被锁定告警的额度连带压掉了"


def test_发不出去的邮件不占额度(client, auth, monkeypatch):
    """SMTP 宕机时不能把额度白花掉。

    额度代表的是「管理员邮箱还能承受多少封信」，而**没送出去的信根本没进
    邮箱**。照扣不误的话，等 SMTP 恢复后第一批真告警会被当成超限丢掉 ——
    那时系统已经在出事了，恰恰是最不该丢告警的时候。
    """
    import smtplib

    from app.core.config import settings

    _配好通知(client, auth)
    monkeypatch.setattr(settings, "notify_rate_limit", 2)
    monkeypatch.setattr(settings, "notify_rate_window_seconds", 600)

    def _boom(*args, **kwargs):
        raise OSError("连不上")

    monkeypatch.setattr(smtplib, "SMTP", _boom)
    for i in range(10):
        assert notify_events.dispatch("ACCOUNT_LOCKED", f"标题{i}", "正文") is False

    # 换成能连的：额度应当还在
    记录 = []
    monkeypatch.setattr(smtplib, "SMTP", _假SMTP(记录))
    for i in range(5):
        notify_events.dispatch("ACCOUNT_LOCKED", f"恢复{i}", "正文")
    assert len(记录) == 2, "失败的发送把额度吃掉了"


def test_没配SMTP时不消耗额度(client, auth, monkeypatch):
    """配置还没配好的时候不该空转额度。

    否则真发不出信的时候额度已经被空转掉了 —— 那正是最不该发生的时刻。
    """
    import smtplib

    from app.core.config import settings

    _reset_notify_config(client, auth)
    monkeypatch.setattr(settings, "notify_rate_limit", 2)
    monkeypatch.setattr(settings, "notify_rate_window_seconds", 600)

    记录 = []
    monkeypatch.setattr(smtplib, "SMTP", _假SMTP(记录))
    for i in range(10):
        assert notify_events.dispatch("ACCOUNT_LOCKED", f"标题{i}", "正文") is False
    assert not 记录

    _配好通知(client, auth)
    for i in range(5):
        notify_events.dispatch("ACCOUNT_LOCKED", f"配好之后{i}", "正文")
    assert len(记录) == 2, "没配 SMTP 时的空转把额度吃掉了"
