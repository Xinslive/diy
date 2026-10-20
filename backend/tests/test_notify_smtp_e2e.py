"""邮件通道的**真收发**测试：自己当一次 SMTP 服务器。

**为什么要有这个文件。** 前面的用例把 ``smtplib.SMTP`` 换成了假客户端，于是
**邮件的 MIME 序列化与 SMTP 会话从未真的跑过**。而那恰恰是最容易出问题
的一段：

* 报文头的中文怎么编码（主题行是中文）；
* From 头在真实序列化下长什么样（显示名是中文，地址是 ASCII）；
* Bcc 有没有真的没上线（收件人之间应当互相看不见地址）；
* AUTH 有没有真的按配置走。

假客户端一概验不到：它只接住一个 ``Message`` 对象，**序列化那一步根本没
执行**。所以这里起一个**真的 socket 上的最小 SMTP 服务器**，让应用通过它
把信发出去，然后检查**服务端收到的那串字节**。

服务器只说这套系统用到的那几个动词（EHLO / AUTH / MAIL / RCPT / DATA /
QUIT），不实现 RFC 5321 的其余部分 —— ���验的是**我们的报文对不对**，不是
它是不是一个完整的邮件服务器。
"""
from __future__ import annotations

import base64
import socket
import threading
import time
from email.parser import BytesParser
from email import policy

import pytest

from app.core.config import settings
from app.core.db import SessionLocal
from app.notify import config as notify_config
from app.notify import events as notify_events
from app.notify import mailer


class _SMTP服务器:
    """一个只够用的 SMTP 服务器，跑在 127.0.0.1 的随机端口上。

    ``认证声明`` 用来模拟"服务器只提供本客户端不支持的认证方式" ——
    那与"口令错"是两回事，却很容易被笼统地报成同一个错。
    """

    def __init__(self, 认证声明: list[str] | None = None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        # **accept 必须带超时。**
        #
        # 理由是"把这个服务器掐掉"这件事在两个平台上的行为不一样：
        # Linux 上另一个线程正阻塞在 accept() 时，close(fd) **不会**让那个
        # accept 返回 —— 内核仍然持有 listening socket 的引用，那个 socket
        # 还在 LISTEN、照样接受连接。于是"服务器已经关了"成了一句假话：
        # 应用真把信送到了一个"死人"服务器，断言反而挂掉。
        #
        # macOS 上 close 会真的拆掉 socket 并唤醒 accept，所以本地一直绿、
        # Linux CI 一直红 —— **这不是 flaky，是平台差异**。
        #
        # 改成超时轮询 + 停止标志之后，关停就是真的：accept 定期醒来看到
        # 标志就退出，端口随之释放，连接被拒。
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.认证声明 = ["PLAIN"] if 认证声明 is None else 认证声明

        self.mail_from = ""
        self.rcpts: list[str] = []
        self.auth: tuple[str, str] | None = None
        self.data = b""
        self.收到 = threading.Event()

        self._停 = threading.Event()
        self._连接: socket.socket | None = None
        self._线程 = threading.Thread(target=self._跑, daemon=True)
        self._线程.start()

    # ------------------------------------------------------------- 关停
    def 关闭(self) -> None:
        """真把这个服务器关掉：端口不再接受连接。

        **不能只 close(fd)。** 见 ``__init__`` 里关于 accept 超时的说明 ——
        Linux 上 close 一个正被 accept 阻塞的 listening socket 并不会让那个
        accept 返回，socket 还在 LISTEN，"关了"是假的。所以这里先把停止
        标志立起来（accept 循环下一轮就退出），再关 fd，顺手把已建立的
        连接也关掉，让服务线程不必等它的 30 秒读超时。
        """
        self._停.set()
        try:
            self.sock.close()
        except OSError:
            pass
        连接 = self._连接
        if 连接 is not None:
            try:
                连接.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                连接.close()
            except OSError:
                pass

    def 等端口关死(self, 超时=5.0) -> None:
        """等到这个端口**真的**不再接受连接为止。

        close(fd) 之后内核可能还短暂地留着那个 socket（正在退出的 accept
        还持有引用），那一小段时间里连接仍会被接受。直接接着发信就会撞上
        那个窗口，于是"服务器已经关了"又是假的。这里主动连一下，
        **明确被拒**才算真关。
        """
        截止 = time.monotonic() + 超时
        while time.monotonic() < 截止:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    pass
            except ConnectionRefusedError:
                return                       # 明确拒了 —— 端口真的空了
            except OSError:
                pass                         # 连不上但不是"拒"，继续等
            time.sleep(0.05)
        raise AssertionError(f"端口 {self.port} 还在接受连接，服务器没关掉")

    # ------------------------------------------------------------- 协议
    def _跑(self):
        conn = None
        while not self._停.is_set():
            try:
                conn, _ = self.sock.accept()
                break
            except socket.timeout:
                continue
            except OSError:
                return
        if conn is None:
            return
        self._连接 = conn
        with conn:
            try:
                self._会话(conn)
            except OSError:
                # 客户端挂断（或者关停时我们自己掐断了这条连接）都会走到这里。
                # 这不是错误，是一次对话正常结束 —— 但它不能以「未处理的线程异常」
                # 冒出去：pytest 会把它报成 PytestUnhandledThreadExceptionWarning，
                # 而哪天有人给 pytest 加了 -W error，它就变成一条与被测逻辑毫无
                # 关系的红灯。
                return

    def _会话(self, conn):
        conn.settimeout(30)
        f = conn.makefile("rb")
        w = conn.makefile("wb")
        w.write(b"220 cwq-test ESMTP\r\n")
        w.flush()
        while True:
            line = f.readline()
            if not line:
                return
            命令 = line.decode("utf-8", "replace").rstrip("\r\n")
            大写 = 命令.upper()
            if 大写.startswith("EHLO") or 大写.startswith("HELO"):
                # **不** advertise SIZE：smtplib 看到它就会在 MAIL FROM
                # 上带 size= 参数，那与本文件要验的东西无关。
                #
                # **也不** advertise STARTTLS：于是配成「587 + STARTTLS」
                # 的管理员会真的撞上 smtplib 的 SMTPNotSupportedError
                # —— 那是最常见的误配，报错必须指向那个方向。
                w.write(b"250-cwq-test\r\n")
                if self.认证声明:
                    w.write(f"250 AUTH {' '.join(self.认证声明)}\r\n".encode())
                else:
                    w.write(b"250 HELP\r\n")
            elif 大写.startswith("AUTH PLAIN"):
                片段 = 命令.split(" ", 2)
                负载 = 片段[2] if len(片段) > 2 else ""
                if not 负载:
                    w.write(b"334 \r\n")
                    负载 = f.readline().decode("ascii", "replace").strip()
                try:
                    解 = base64.b64decode(负载).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    w.write(b"535 5.7.8 bad base64\r\n")
                    w.flush()
                    continue
                # PLAIN 的载荷是 \0user\0password
                段 = 解.split("\x00")
                self.auth = (段[1] if len(段) > 1 else "", 段[2] if len(段) > 2 else "")
                w.write(b"235 2.7.0 Authentication successful\r\n")
            elif 大写.startswith("MAIL FROM"):
                self.mail_from = 命令.split(":", 1)[-1].strip().strip("<>")
                w.write(b"250 2.1.0 Ok\r\n")
            elif 大写.startswith("RCPT TO"):
                self.rcpts.append(命令.split(":", 1)[-1].strip().strip("<>"))
                w.write(b"250 2.1.5 Ok\r\n")
            elif 大写.startswith("DATA"):
                w.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                w.flush()
                块 = []
                while True:
                    数据行 = f.readline()
                    if not 数据行:
                        break
                    if 数据行 == b".\r\n":
                        break
                    if 数据行.startswith(b".."):
                        数据行 = 数据行[1:]
                    块.append(数据行)
                self.data = b"".join(块)
                w.write(b"250 2.0.0 Ok: queued\r\n")
                self.收到.set()
            elif 大写.startswith("RSET") or 大写.startswith("NOOP"):
                w.write(b"250 2.0.0 Ok\r\n")
            elif 大写.startswith("QUIT"):
                w.write(b"221 2.0.0 Bye\r\n")
                w.flush()
                return
            else:
                w.write(b"502 5.5.1 Command not implemented\r\n")
            w.flush()

    def 等一封(self, 超时=15.0) -> bytes:
        assert self.收到.wait(超时), "超时：服务端没有收到任何邮件"
        return self.data

    def 解析(self) -> "email.message.Message":
        return BytesParser(policy=policy.default).parsebytes(self.data)


@pytest.fixture
def smtp服务器():
    服务器 = _SMTP服务器()
    yield 服务器
    服务器.关闭()


@pytest.fixture(autouse=True)
def _收尾清掉通知配置(client, auth):
    """每个用例结束后把通知配置清回「什么都没配」。

    **为什么这个文件需要它。** ``sys_setting`` 里的行是**跨用例存活**的：
    接口里 ``db.commit()`` 提交了，测试夹具的 rollback 只回滚它自己那个
    会话里未提交的改动。所以本文件里配过的 SMTP（连着一个用完就掐掉的
    端口）会留给后面跑的用例 —— 而那些用例看到的配置指向一个死端口，
    「发不出去」就成了它们的常态，测出来的东西不对。

    这不是洁癖：它正是「配置是全局的」这件事在测试里的表现。
    """
    yield
    client.put("/api/system/notify", headers=auth, json={
        "host": "", "username": "", "from_address": "",
        "recipients": "", "enabled": True, "clear_password": True,
    })


def _配好通知(client, auth, 端口: int, 收件人="ops@x.com,boss@x.com"):
    r = client.put("/api/system/notify", headers=auth, json={
        "host": "127.0.0.1", "port": 端口, "username": "notice@x.com",
        "password": "smtp-secret", "from_address": "notice@x.com",
        "recipients": 收件人, "use_ssl": False, "use_tls": False,
        "timeout_seconds": 10,
    })
    assert r.status_code == 200, r.text


def test_真的把信发出去而且服务端收到的是合规报文(smtp服务器, client, admin_user, auth, db):
    """**整条链路**：登录失败 → 触发锁定 → 真的 SMTP 会话 → 服务端收到字节。

    这条用例的每一步都是真的：真的 socket、真的 AUTH、真的 MIME 序列化。
    假客户端验不到的那些（中文主题的编码、From 头的形态）在这里全都验得到。
    """
    _配好通知(client, auth, smtp服务器.port)

    from tests.auth_helpers import 打到硬锁

    # **只有硬锁才发通知**（审查报告安全域 P2-4）：递增延迟只有几秒，
    # 每次撞库口令都发一封的话，收件箱会被刷爆 —— 而收件箱是**通知通道本身**。
    打到硬锁(client, db, admin_user.username, password="错三次")

    原始 = smtp服务器.等一封()
    assert 原始, "服务端一字节都没收到"

    信 = smtp服务器.解析()
    assert 信["Subject"].startswith("账号锁定"), f"主题不对：{信['Subject']!r}"
    assert admin_user.username in 信["Subject"]

    # From：显示名是中文，地址是 ASCII。**这是 compat32 序列化最容易搞坏
    # 的一处** —— 把整个 "名字 <地址>" 塞进一个编码字里，地址就看不见了，
    # 而不少 MTA 会因此拒收（"invalid sender"）。
    发件人 = str(信["From"])
    assert "notice@x.com" in 发件人, f"From 里读不出地址：{发件人!r}"
    assert 发件人.rstrip().endswith(">"), f"From 的地址必须裸着出现：{发件人!r}"

    # AUTH 真的按配置走了
    assert smtp服务器.auth == ("notice@x.com", "smtp-secret"), \
        f"认证用的不是配置的账号：{smtp服务器.auth}"

    # 正文：中文没乱码
    正文 = 信.get_content()
    assert "锁定" in 正文 and admin_user.username in 正文
    # 正文里不该有凭据
    assert "smtp-secret" not in 正文


def test_整封信必须是7位的(smtp服务器, client, admin_user, auth, db):
    """**整封报文不能含裸的非 ASCII 字节。**

    主题行与 From 的显示名是 RFC 2047 编码字（那是 7 位的），正文也一样 ——
    所以整封信应当是纯 ASCII。这样它才能被**任何**服务器原样传输。

    为什么要在意：默认的 8bit 传输要求收信那端支持 8BITMIME 扩展。不支持的
    老网关会把它转成乱码 —— 而这封信的内容是一句告警，收到的是「?????」，
    等于**告警到了、但没人读得懂**。那种失效比"没收到"还难查：信确实躺在
    收件箱里。
    """
    _配好通知(client, auth, smtp服务器.port)

    from tests.auth_helpers import 打到硬锁

    # **只有硬锁才发通知**（审查报告安全域 P2-4）：递增延迟只有几秒，
    # 每次撞库口令都发一封的话，收件箱会被刷爆 —— 而收件箱是**通知通道本身**。
    打到硬锁(client, db, admin_user.username, password="错三次")

    原始 = smtp服务器.等一封()
    非ASCII = [b for b in 原始 if b >= 128]
    assert not 非ASCII, f"报文里有 {len(非ASCII)} 个裸的非 ASCII 字节"
    assert b"Content-Transfer-Encoding: quoted-printable" in 原始 or \
        b"Content-Transfer-Encoding: 7bit" in 原始, \
        "报文的传输编码不是 7 位安全的"


def test_多个收件人时信封有全部地址而报文里没有别人的地址(smtp服务器, client, admin_user, auth, db):
    """**收件人之间互相看不见地址** —— 这条在真实报文上验。

    信封（RCPT TO）里要有全部收件人，否则第二个人收不到；报文里（头与正文）
    不能出现别人的地址，否则就是隐私泄露。
    """
    _配好通知(client, auth, smtp服务器.port, 收件人="ops@x.com,boss@x.com")

    from tests.auth_helpers import 打到硬锁

    # **只有硬锁才发通知**（审查报告安全域 P2-4）：递增延迟只有几秒，
    # 每次撞库口令都发一封的话，收件箱会被刷爆 —— 而收件箱是**通知通道本身**。
    打到硬锁(client, db, admin_user.username, password="错三次")

    原始 = smtp服务器.等一封()
    信 = smtp服务器.解析()

    assert sorted(smtp服务器.rcpts) == ["boss@x.com", "ops@x.com"], \
        f"信封里的收件人不全：{smtp服务器.rcpts}"
    assert "boss@x.com" not in str(信["To"]), \
        f"To 头里露出了另一个收件人：{信['To']!r}"
    assert "boss@x.com" not in 原始.decode("utf-8", "replace"), \
        "报文里出现了别的收件人地址"
    assert "Bcc" not in 信, "Bcc 头上了线 —— 收件人名单等于公布出去了"


def test_发不出去时登录流程照常_不抛异常也不卡住(smtp服务器, client, admin_user, auth):
    """SMTP 服务器不在的时候，**登录必须照常失败、且是那个失败**。

    这条是"通知是旁路"的最后一层验证：前面几次都是在真服务器活着的时候
    验的；现在把服务器掐了，登录路径不能变成 500、也不能卡住。

    **「掐掉」必须是真的掐掉。** 这里踩过一个平台差异的坑：原来只
    close 了监听 socket 的 fd，而 Linux 上另一个线程正阻塞在 accept()
    时，close 不会让那个 accept 返回 —— 内核仍持有 listening socket 的
    引用，socket 还在 LISTEN、照样收连接。于是"服务器已经关了"是句假话：
    应用真把信送到了这个"死人"服务器，断言反而挂掉；macOS 上 close 会
    拆掉 socket，所以本地一直绿、Linux CI 一直红。**这不是 flaky。**
    """
    端口 = smtp服务器.port
    smtp服务器.关闭()
    smtp服务器.等端口关死()      # 端口真的空了才算数

    _配好通知(client, auth, 端口)

    凭据 = {"username": admin_user.username, "password": "错三次"}
    for _ in range(settings.login_max_failures):
        响应 = client.post("/api/auth/login", json=凭据)
        # 阈值内是 401，达阈值那一次起进入**递增延迟**（429）——
        # 两种都是「这次登录没成」，SMTP 挂不挂都该是这两种之一。
        assert 响应.status_code in (401, 429), (
            f"登录的失败形态变了：{响应.status_code}")

    # **正面断言「确实试了、也确实失败了」。**
    # 只断言"没收到信"是不够的 —— 投递压根没触发过时它同样成立，
    # 等于什么都没验。所以这里先证明连不上（SMTP 服务器真的不在了），
    # 再证明投递被尝试过且失败被吞掉。
    with SessionLocal() as s:
        配置 = notify_config.smtp_config(s)
    with pytest.raises(mailer.MailError) as 失败:
        mailer.send_mail(配置, ["ops@x.com"], "标题", "正文")
    assert "连不上" in str(失败.value), f"失败原因不是连不上：{失败.value}"
    assert notify_events.dispatch("ACCOUNT_LOCKED", "标题", "正文") is False, \
        "服务器都不在了，投递居然报成功"

    # 后台线程那次投递也不能把主流程带崩，更不能有信落地
    time.sleep(0.5)
    assert smtp服务器.收到.is_set() is False, "服务器都掐了还收到了信？"


def test_服务器不支持STARTTLS时给出可操作的提示(smtp服务器, client, auth):
    """**最常见的误配**：端口选 587、加密选 STARTTLS，而服务器要的是 465。

    服务器不会报错，它只是说"没有这个扩展" —— 而这句话被笼统地报成
    "拒绝连接"的话，管理员会去查防火墙与口令，方向全错。所以报错必须
    直接指向「端口 ↔ 加密方式」的对应关系。
    """
    r = client.put("/api/system/notify", headers=auth, json={
        "host": "127.0.0.1", "port": smtp服务器.port, "username": "", "password": "",
        "from_address": "noreply@x.com", "recipients": "ops@x.com",
        "use_ssl": False, "use_tls": True, "timeout_seconds": 5,
    })
    assert r.status_code == 200, r.text

    响应 = client.post("/api/system/notify/test", headers=auth)
    assert 响应.status_code == 400, 响应.text
    文案 = 响应.json()["detail"]
    assert "STARTTLS" in 文案, f"没点出是 STARTTLS 的问题：{文案}"
    assert "465" in 文案 and "587" in 文案 and "25" in 文案, \
        f"没给出端口与加密方式的对应关系：{文案}"


def test_认证方式不兼容时报错要说清方向(client, auth):
    """服务器只提供本客户端不支持的认证方式 —— 与「口令错」是两回事。

    两者都被笼统地报成"认证失败"的话，管理员会去反复改口令（而口令是对的），
    真正要改的是服务器端或换客户端。报错里必须带上服务器声明了哪些方式。
    """
    服务器 = _SMTP服务器(认证声明=["GSSAPI"])
    try:
        r = client.put("/api/system/notify", headers=auth, json={
            "host": "127.0.0.1", "port": 服务器.port,
            "username": "notice@x.com", "password": "pw",
            "from_address": "notice@x.com", "recipients": "ops@x.com",
            "use_ssl": False, "use_tls": False, "timeout_seconds": 5,
        })
        assert r.status_code == 200, r.text

        响应 = client.post("/api/system/notify/test", headers=auth)
        assert 响应.status_code == 400, 响应.text
        文案 = 响应.json()["detail"]
        assert "GSSAPI" in 文案, f"报错里没有服务器声明的认证方式：{文案}"
        assert "PLAIN" in 文案, f"报错里没有本客户端支持的方式：{文案}"
    finally:
        服务器.关闭()
