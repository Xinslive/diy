"""SMTP 发信。

只用标准库 ``smtplib`` + ``email.message``，**不引第三方依赖**。

理由与项目里其它「不加依赖」的选择一致：邮件发送是**旁路**，不是业务主
路径。为它引入一个第三方 SMTP 客户端，换来的是供应链上多一个需要盯的包
（而审计 P2-9 刚指出后端锁文件连哈希都没有），换不来任何本项目用得上的
能力。``smtplib`` 覆盖了 SMTP + STARTTLS + SMTPS 这三种部署形态，而实际
的邮件服务器（企业微信、QQ、163、Exchange、自建 Postfix）全都在这三类里。

**正文一律纯文本。** 不拼 HTML —— 通知的收件人是管理员，HTML 邮件意味着
「把系统里的数据塞进邮件正文」这件事要额外处理转义，而纯文本零成本。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

log = logging.getLogger("cwq.notify")


class MailError(Exception):
    """发信失败。消息里带**人可读**的原因，不带口令。"""


def _timeout(config: dict) -> int:
    try:
        value = int(config.get("timeout_seconds") or 10)
    except (TypeError, ValueError):
        value = 10
    # 下限 1 秒：0 或负数会让 socket 立刻超时，发出去的成功率是 0。
    # 上限 60 秒：通知是旁路，等一分钟还没有结果就是出事了，不该继续等。
    return max(1, min(value, 60))


def _from_address(config: dict) -> str:
    """发件人地址。**没有**配置就用用户名，再没有就用用户名@主机名。"""
    explicit = (config.get("from_address") or "").strip()
    if explicit:
        return explicit
    username = (config.get("username") or "").strip()
    if username:
        # 用户名本身可能已经是一个完整地址（`a@b.com`），那就不再拼域名。
        return username if "@" in username else f"{username}@localhost"
    return "noreply@localhost"


def _connect(config: dict) -> smtplib.SMTP:
    """按配置建一条 SMTP 连接。

    三种形态：

    * ``use_ssl``（通常 465）：``SMTP_SSL``，握手就是加密的；
    * ``use_tls``（通常 587）：先明文握手，再用 ``STARTTLS`` 升级；
    * 都不开：纯明文。**允许但要在界面上说清楚** —— 有些内网邮件网关
      就是明文的，禁掉它等于让这类部署发不出信。
    """
    host = (config.get("host") or "").strip()
    if not host:
        raise MailError("未配置 SMTP 服务器地址")
    try:
        port = int(config.get("port") or 25)
    except (TypeError, ValueError):
        port = 25
    timeout = _timeout(config)

    try:
        if config.get("use_ssl"):
            return smtplib.SMTP_SSL(host, port, timeout=timeout,
                                    context=ssl.create_default_context())
        client = smtplib.SMTP(host, port, timeout=timeout)
        if config.get("use_tls"):
            # 证书校验**开着**：create_default_context() 会验证书链与主机名。
            # 关掉它等于给中间人开门，而通知里走的是账号名、来源 IP、失败
            # 原因这类不该被第三方改写的内容。
            try:
                client.starttls(context=ssl.create_default_context())
            except smtplib.SMTPNotSupportedError as exc:
                # **最常见的误配**：端口选成 587（STARTTLS），而服务器要的是
                # 465（隐式 SSL）或干脆只收明文。服务器不报错，它只是说
                # 「没有这个扩展」—— 而这句话被笼统地报成「拒绝连接」的话，
                # 管理员会去查防火墙和口令，方向全错。
                raise MailError(
                    f"服务器不支持 STARTTLS（{exc}）。"
                    f"多半是端口与加密方式配错了："
                    f"465 请选「SSL」、587 请选「STARTTLS」、25 请选「不加密」。"
                ) from exc
        return client
    except ssl.SSLError as exc:
        raise MailError(
            f"TLS 握手失败：{exc}。"
            f"若是证书不受信（自签证书），请改用该服务器官方提供的证书链，"
            f"或改用「不加密」由内网邮件网关中转。"
        ) from exc
    except OSError as exc:
        raise MailError(f"连不上 SMTP 服务器 {host}:{port}：{exc}") from exc
    except smtplib.SMTPException as exc:
        raise MailError(f"SMTP 服务器拒绝连接：{exc}") from exc


def _login(client: smtplib.SMTP, config: dict) -> None:
    username = (config.get("username") or "").strip()
    if not username:
        return
    password = config.get("password") or ""

    # **先自己看一遍服务器声明了什么**，再决定要不要调 login。
    #
    # 为什么不能靠 smtplib 的异常类型来分：它在「没有可用方式」时抛的是
    # 一个**笼统的** ``SMTPException``（"No suitable authentication method
    # found."），只有「服务器完全不支持 AUTH」时才抛 ``SMTPNotSupportedError``。
    # 而这两件事的处置方向正好相反：前者要改服务器端或换中继，后者要动
    # 登录账号。笼统成一句"认证失败"的话，管理员会去反复改口令（而口令
    # 本来就是对的），白折腾一轮。
    #
    # **必须先确保 EHLO 已经发生过**：``connect()`` 只读欢迎语，EHLO 是
    # ``login()`` 内部才发出去的 —— 不补这一步的话这里读到的是空的
    # ``esmtp_features``，于是"服务器不提供任何认证方式"会被误报给
    # **每一个**用认证的服务器。
    client.ehlo_or_helo_if_needed()
    声明 = client.esmtp_features.get("auth", "") or ""
    # **``auth`` 的形态在 Python 版本之间变过**：3.11 的 smtplib 存的是
    # 空格分隔的**字符串**（"PLAIN LOGIN"），而更早/别的实现存的是**列表**。
    # 两种都认 —— 不认的话，按字符排序出来的 "、A、I、L、N、P" 会让
    # 每一个正常的服务器都被报成"不兼容"。
    服务器支持 = sorted(声明.split() if isinstance(声明, str) else 声明)
    本客户端支持 = ("PLAIN", "LOGIN", "CRAM-MD5")
    if not 服务器支持:
        raise MailError(
            "服务器不提供任何认证方式 —— 要么登录账号不该填"
            "（有些内网邮件网关不要求认证），要么端口/协议选错了。")
    if not set(服务器支持) & set(本客户端支持):
        raise MailError(
            f"服务器只支持 {'、'.join(服务器支持)}，"
            f"本客户端只支持 {'、'.join(本客户端支持)} —— 两者没有交集。"
            f"请换一个中继/客户端，或让服务器开启其中一种方式。")

    try:
        client.login(username, password)
    except smtplib.SMTPAuthenticationError as exc:
        # 报错里**不回显口令**。smtplib 的异常本身也不含口令，但账号名
        # 会出现在日志里 —— 那是运维自己配的，可接受。
        raise MailError(f"SMTP 认证失败（账号 {username}）：{exc.smtp_code} {exc.smtp_error!r}") from exc
    except smtplib.SMTPException as exc:
        raise MailError(f"SMTP 认证失败：{exc}") from exc


def send_mail(config: dict, to_addresses: list[str], subject: str, body: str) -> int:
    """发一封纯文本邮件，返回成功投递的收件人数。

    失败一律抛 :class:`MailError`（消息里没有口令）。**调用方要自己兜住**
    —— 见 ``events.py::dispatch``：通知发不出去绝不能影响主流程。
    """
    if not to_addresses:
        raise MailError("没有收件人")
    subject = (subject or "").strip() or "系统通知"
    body = body or ""

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("财务记账系统", _from_address(config)))
    # 收件人之间互相看不见地址：第一个进 To 头，其余走 Bcc。
    #
    # 这里有两个坑：
    #
    # 1) **不能**「一个收件人一个 To 头」。``email.policy.default`` 明确
    #    限制「一封邮件至多一个 To 头」，多写会直接抛 ValueError；而且
    #    就算写得进去，所有地址最终还是会显示在每个人的邮件里 —— 跟
    #    「互相看不见」的初衷正好相反。
    # 2) Bcc 头必须在**投递前删掉**。``smtplib`` 自己也会删（它做的是浅
    #    拷贝后 ``del`` 掉 Bcc），但我们提前删：一来不依赖那个行为在每个
    #    Python 版本上都一样，二来我们自己手上的这个 ``Message`` 对象就
    #    不会带着 Bcc 到处走（比如被别处读去写日志）。
    visible = to_addresses[0]
    hidden = list(to_addresses[1:])
    message["To"] = visible
    if hidden:
        message["Bcc"] = ", ".join(hidden)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="cwq-accounting")
    # **正文用 quoted-printable，不让它以 8bit 上线。**
    #
    # 默认的 8bit 传输意味着「正文里有裸的非 ASCII 字节」，而这要求直接
    # 收信的那台服务器支持 8BITMIME 扩展。不支持的老网关/企业邮件网关会
    # 把它转成乱码 —— 而这封信的内容是一句告警，收到的是「?????」，
    # 等于**告警到了、但没人读得懂**。
    #
    # quoted-printable 把非 ASCII 编码成 ``=XX``，整封信于是纯 ASCII，
    # 任何服务器都能原样传输；收信端按声明的 utf-8 解回来，文字是好的。
    # 选它而不是 base64：base64 更保险但正文在原始报文里完全不可读，
    # 而排障时能直接看原文是有用的。
    message.set_content(body, cte="quoted-printable")

    client = _connect(config)
    try:
        _login(client, config)
        if hidden:
            del message["Bcc"]
        client.send_message(message, to_addrs=list(to_addresses))
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailError(f"收件人被拒：{list(exc.recipients)}") from exc
    except smtplib.SMTPException as exc:
        raise MailError(f"发送失败：{exc}") from exc
    except OSError as exc:
        raise MailError(f"发送时网络中断：{exc}") from exc
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            # quit 只是发 QUIT 命令等对方回应，失败无所谓 —— 邮件已经出去了。
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    return len(to_addresses)


def test_connection(config: dict, to_addresses: list[str]) -> int:
    """发一封测试信。**收件人就是真实的收件人** —— 收不到信的地址就是配错了，
    而「配错了却显示测试成功」是最没用的测试结果。
    """
    return send_mail(
        config, to_addresses,
        subject="财务记账系统 · SMTP 配置测试",
        body=(
            "这是一封来自财务记账系统的测试邮件。\n\n"
            "如果你收到了它，说明 SMTP 服务器、账号、口令与加密方式都配对了，"
            "系统可以正常发送告警通知。\n\n"
            "如果这封信是管理员发起的，说明该地址已被设为告警通知的收件人。\n"
        ),
    )
