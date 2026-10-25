"""管理员二次验证（邮箱链接）的服务层。

审查报告 P0-1：「管理员账号无任何二次验证」。这一层是那条整改的实现核心。

**形态是邮箱链接，复用项目已有的 SMTP 通道**（:mod:`app.notify.mailer`，纯标准库）
—— 零新增依赖、零新增运维面。状态机与「为什么是邮箱链接而不是 TOTP」的
理由写在 :mod:`app.models.mfa` 的模块注释里，这里不重复。

**为什么单独成文件。** 三件事必须放在一起才说得清楚：

#. 「二次验证此刻生效吗」（开关 vs 通道）；
#. 挑战的状态机（造 / 点链接 / 换令牌）；
#. 旁路发信。

散进 ``api/auth.py`` 的话，「开关开着但 SMTP 没配好」这个最要紧的分支会被
接口代码淹没 —— 而那恰恰是最容易在改版时被顺手「降级成放行」的地方。

**三个判定口径，不要混用。**

* :func:`mfa_enabled` —— **开关**。库里优先、env 兜底（``sys_setting`` 的
  ``admin_mfa_enabled`` 键，键名与 env 的 ``ADMIN_MFA_ENABLED`` 一致）。
  管理员在界面上改完立即生效，不必重启后端。
* :func:`smtp_ready` —— **通道**。SMTP 有主机、且有收件人。
* :func:`mfa_active` —— 开关**且**通道。

**「开关开着但通道没配好」绝不能降级成放行。** 那等于把一个安全开关变成
一个假开关：界面上明明写着「已开启二次验证」，实际却一个字节都没生效。
本层的做法是**明确报错**（见 :func:`channel_error`），让运维去把 SMTP 配好；
真要临时关，走界面上的开关或 ``scripts/mfa_admin.py --disable`` ——
**明确关掉**与**假装开着**是两件完全不同的事。

**发信是旁路。** 发不出去不该让登录失败（那是一次可用性事故），但也不能
假装发过了：失败会记一条 warning 日志，而登录接口回的仍然是「已发送」。

为什么不用 :func:`app.notify.events.dispatch_async`：那条路会先过**通知总开关**
（``notify_enabled``）。总开关是给「告警」用的（锁号、备份失败），而二次
验证邮件是**认证**的一部分 —— 总开关一关，管理员就再也登不进系统，而且症状
是「一直收不到邮件」，极难自查。所以这里自己发（复用同一个
``mailer.send_mail`` 与同一套并发上限），把「告警」与「认证」两件事的语义
分开。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional
from urllib.parse import quote

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import detect_env, settings
from app.core.system_settings import get_value, set_value
from app.models import AdminMfaChallenge, SysUser
from app.models.mfa import (STATUS_CONSUMED, STATUS_EXPIRED, STATUS_PENDING,
                            STATUS_VERIFIED)

log = logging.getLogger("cwq.notify.mfa")

#: 开关在 ``sys_setting`` 里的 key。与 env 的 ``ADMIN_MFA_ENABLED`` 同名，
#: 省掉一次映射（与 ``registration_enabled`` 同一套约定）。
MFA_ENABLED_KEY = "admin_mfa_enabled"

_TRUTHY = ("1", "true", "yes", "on")

#: 两串密钥的长度（``secrets.token_urlsafe``，约 43 个字符）。
_SECRET_BYTES = 32

#: 同时最多几封二次验证邮件在发。与 :mod:`app.notify.events` 的口径一致：
#: 发信的并发成本是真的（每封一个 SMTP 连接），而登录路径上的每一次尝试
#: 都在攻击者的放大器射程之内。
_MAX_CONCURRENT = 4
_slots = threading.BoundedSemaphore(_MAX_CONCURRENT)

#: 轮询的最短间隔（秒）。**别调小** —— 轮询接口是「每隔几秒问一次有没有
#: 验证」的模型，间隔太小只会把数据库变成放大器。
POLL_MIN_INTERVAL_SECONDS = 2.0

#: 同一个挑战最多轮询多少次。``POLL_MIN_INTERVAL_SECONDS`` 乘以它 =
#: 这个挑战能被问的最长时间（默认 2×300 = 600 秒，与链接有效期同量级）。
POLL_MAX_ATTEMPTS = 300

#: 轮询限流的状态表。**进程级** —— 它要挡的是「把轮询接口当放大器」，
#: 而这个目的用进程级状态就够了；跨进程限流需要额外的表，超出本条范围。
#:
#: **键是 ``(challenge_id, client_secret 的摘要)``，不是单独的
#: ``challenge_id``**（审查 §3.4）。原来按 id 存，理由写的是「攻击者拿
#: 随机 id 来问也进不来（查不到就 401）」—— 而那个假设正是被打破的地方：
#: :func:`poll_guard` 在**校验 ``client_secret`` 之前**就被调用，于是
#: 攻击者拿**任意** id + **错的** secret 请求，拿到的是 401（对），
#: 但**预算已经被扣掉了**。实测：受害者的合法客户端接着就收到 429
#: 「请 2 秒后再试」；累计 300 次之后那个挑战**永远**没法被任何人消费
#: —— 包括持有正确 secret 的那个人。
#:
#: 键里加上 secret 的摘要之后，攻击者动的是**另一个桶**：受害者的
#: 轮询预算只有持有正确 secret 的人能碰。
_poll_state: dict[str, tuple[float, int]] = {}
_poll_lock = threading.Lock()

#: 进程级状态表的条数上限。超过就丢掉最老的一批 —— 防止「用随机 id 刷
#: 限流表」把内存吃光（那等于换了个形式的放大器）。
_POLL_STATE_MAX = 2048

#: 应急口令：**用一次即废**。它只活在进程环境里（``ADMIN_MFA_EMERGENCY_CODE``），
#: 从不写库、从不进任何响应、从不写日志。
_emergency_lock = threading.Lock()
_emergency_used = False


# --------------------------------------------------------------------- 时间


def now_utc() -> datetime:
    """当前 UTC 时间，**aware**。

    ``admin_mfa_challenge`` 的时间列都是 ``DateTime(timezone=True)``，
    与 ``api/auth.py`` 里那些 naive UTC 的登录时间列**不是同一套**。
    混用会在东八区部署时凭空差 8 小时 —— 表现是「链接永远不过期」或
    「链接一出生就过期」，两种都很难在事后看出来。
    """
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """把可能 naive 的时间补成 aware UTC。

    库里读出来的时间列理论上带时区，但驱动/连接的时区设置一旦被改动，
    就可能交出 naive 的 datetime。比较前统一补齐，避免 ``TypeError``
    变成一次 500。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _expired(row: AdminMfaChallenge, now: datetime) -> bool:
    exp = _aware(row.expires_at)
    return exp is None or now >= exp


# --------------------------------------------------------------------- 开关


def mfa_enabled(db: Session) -> bool:
    """二次验证的**开关**。**库里优先，env 兜底。**

    与 :func:`app.core.system_settings.registration_enabled` 同一套约定：
    库里没有这一行时读 ``settings.admin_mfa_enabled``（默认 False），
    所以升级到这一版之后，服务器上原有的 ``ADMIN_MFA_ENABLED=false``
    立刻继续生效 —— 不需要谁去库里补一行。

    **默认 False 是刻意的**：强制开二次验证的第一前提是「通知邮箱通道
    已经配好且发得出信」。默认开着而 SMTP 没配，等于把所有管理员一次性
    锁在门外 —— 那是把一个安全能力变成一次可用性事故。
    """
    raw = get_value(db, MFA_ENABLED_KEY, "")
    if not raw:
        return bool(getattr(settings, "admin_mfa_enabled", False))
    return raw.strip().lower() in _TRUTHY


def set_mfa_enabled(db: Session, value: bool, *,
                    updated_by: str | None = None) -> None:
    """写这个开关。只存文本，与同表其它配置一致。

    **父会话接线点**：`api/system.py` 的管理界面用这个函数落库。
    """
    set_value(db, MFA_ENABLED_KEY, "1" if value else "0", updated_by=updated_by)


def smtp_ready(db: Session) -> bool:
    """二次验证的**通道**：SMTP 有主机、且有收件人。

    **刻意不看通知总开关**（``notify_enabled``）：那是「告警要不要发」的
    开关，而二次验证邮件是认证的一部分。总开关一关，管理员就再也登不进
    系统 —— 一个「关告警」的动作把「能登录」一起关掉了，那是两件事。
    """
    from app.notify.config import recipients, smtp_config

    config = smtp_config(db)
    if not (config.get("host") or "").strip():
        return False
    return bool(recipients(db))


def mfa_active(db: Session) -> bool:
    """二次验证此刻**真的生效**：开关开着**且**通道就绪。"""
    return mfa_enabled(db) and smtp_ready(db)


def channel_error(db: Session) -> str:
    """「开关开着但通道没配好」时给人看的错误文案；一切正常时返回空串。

    这段文案是**给人看的**，不是给日志看的：它会被原样放进接口的
    ``detail`` 里，让管理员一眼看出「是运维没配 SMTP」，而不是
    「系统坏了」。所以里面不出现任何内部细节（表名、主机名、口令）。
    """
    if not mfa_enabled(db):
        return ""
    if smtp_ready(db):
        return ""

    from app.notify.config import recipients, smtp_config

    config = smtp_config(db)
    missing: list[str] = []
    if not (config.get("host") or "").strip():
        missing.append("未配置 SMTP 服务器")
    if not recipients(db):
        missing.append("未配置通知收件人")
    detail = "；".join(missing) or "SMTP 通道不可用"
    return ("二次验证通道未配置，请联系运维：ADMIN_MFA_ENABLED 已开但没有可用的 "
            f"SMTP 收件人（{detail}）")


def email_hint(db: Session) -> str:
    """给界面看的收件地址（**脱敏**）。没有收件人时返回空串。"""
    from app.notify.config import recipients

    addrs = recipients(db)
    if not addrs:
        return ""
    return mask_email(addrs[0])


def mask_email(address: str) -> str:
    """脱敏一个邮箱地址：本地部分只留首尾各一位，域名保留。

    「首尾各一位」不是随手取的：只留首位的 ``a***@x.com`` 挡不住
    「本地部分只有两三个字符」的小号，而只留首位的反面（全部打码）
    又会让收件人自己都认不出那是哪个邮箱、进而以为信发错了人。
    """
    value = (address or "").strip()
    if "@" not in value:
        return "***"
    local, _, domain = value.partition("@")
    if not local:
        return f"***@{domain}"
    if len(local) <= 2:
        return f"{'*' * len(local)}@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


# --------------------------------------------------------------------- 密钥


def hash_secret(raw: str) -> str:
    """散列。与 ``sys_user.password_hash`` 同一个口径：库里只存散列。"""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def secret_matches(raw: str, digest: str) -> bool:
    """常量时间地比对一串密钥。"""
    if not digest:
        return False
    try:
        return hmac.compare_digest(hash_secret(raw), digest)
    except (TypeError, ValueError):
        return False


def new_secret() -> str:
    """生成一串 URL-safe 的随机密钥。"""
    return secrets.token_urlsafe(_SECRET_BYTES)


# --------------------------------------------------------------------- 挑战


def ttl_seconds() -> int:
    """链接有效期（秒）。至少 60 秒 —— 配成 0 会让链接一出生就作废。"""
    return max(60, int(settings.admin_mfa_ttl_seconds or 600))


def resend_interval_seconds() -> int:
    """「重新发送」的最短间隔（秒）。至少 1 秒。"""
    return max(1, int(settings.admin_mfa_resend_interval_seconds or 60))


def grace_seconds() -> int:
    """高危动作的「近期重新认证」窗口（秒）。"""
    return max(0, int(settings.admin_mfa_grace_minutes or 0)) * 60


def create_challenge(db: Session, user: SysUser, *, reason: str = "login",
                     ip: str = "", user_agent: str = "") -> tuple[
                         AdminMfaChallenge, str, str]:
    """造一条挑战。返回 ``(挑战, email_token, client_secret)``。

    **两个明文密钥只在这里出现一次**：邮件链接里放 ``email_token``，
    只发给发起登录的客户端的是 ``client_secret``。库里存的全是散列
    （见 :mod:`app.models.mfa` 的模块注释：链接会被网关扫描、被手机
    锁屏预览、被 IT 留档，所以它绝不能直接就是会话令牌）。

    **同一账号此前未完成的挑战一律作废**（置 EXPIRED）：一个人同时只该有
    一个「待验证」的窗口，否则发第二封邮件会让第一封变成一张废纸，
    而用户往往只点最新那封 —— 于是「点错了一封」变成「登不进去」。
    """
    now = now_utc()
    email_token = new_secret()
    client_secret = new_secret()

    db.execute(
        update(AdminMfaChallenge)
        .where(AdminMfaChallenge.user_id == user.id,
               AdminMfaChallenge.status.in_([STATUS_PENDING, STATUS_VERIFIED]))
        .values(status=STATUS_EXPIRED, updated_at=func.now())
    )

    row = AdminMfaChallenge(
        user_id=user.id,
        email_token_hash=hash_secret(email_token),
        client_secret_hash=hash_secret(client_secret),
        status=STATUS_PENDING,
        expires_at=now + timedelta(seconds=ttl_seconds()),
        last_sent_at=now,
        ip=(ip or "")[:45],
        user_agent=(user_agent or "")[:256],
        reason=(reason or "login")[:32],
    )
    db.add(row)
    db.flush()
    return row, email_token, client_secret


def expire_open_challenges(db: Session, *, reason: str = "") -> int:
    """作废**全部**未完成的挑战（``PENDING`` 与 ``VERIFIED``）。返回作废了几条。

    **为什么需要它 —— 「改收件人」就是二次验证本身的那个开关。**

    管理员二次验证邮件**没有 per-user 收件人**：``sys_user`` 表里没有邮箱列，
    全部邮件统一发给全局 ``notify_config.recipients``。于是「谁能改收件人」
    就等于「谁能收到下一次二次验证邮件」。

    复核方（2026-10-25）在实测里跑通的一条完整链条：

        1. 一枚**没有** ``mfa_at`` 的管理员令牌 → ``POST /api/system/users``
           被 403 拦下（「该操作需要近期完成邮箱二次验证…」）；
        2. ``PUT /api/system/notify`` 改收件人为 ``attacker@evil.test`` →
           **200**（这条路由当时没挂 ``require_recent_admin_mfa``）；
        3. ``POST /api/auth/mfa/reauth`` → 邮件真的发到了
           ``attacker@evil.test``；
        4. 点开那封邮件里的链接 → 挑战变 ``VERIFIED``；
        5. ``POST /api/auth/mfa/poll`` → 换到一枚**带** ``mfa_at`` 的令牌；
        6. 用它建号 → **201，账号真的建出来了**。

    整道 §3.2 的闸门被从侧翼击穿：``require_recent_admin_mfa`` 的全部意义是
    「把危险动作与『令牌刚被偷』解耦」，而「改收件人」这个动作**恰恰就是让
    二次验证生效的那个开关**。

    挂上 ``require_recent_admin_mfa`` 挡住了第 2 步，但**还剩一条路径**：
    **在改收件人之前就已经发出去的挑战**。那一封邮件是按**旧**投递目标投出去的
    —— 如果它已经被人截获（或者旧收件人本来就是攻击者），那么「改完收件人」
    之后点开那封旧邮件仍然能换到令牌。所以投递目标一变，就把**所有**未完成
    的挑战作废。

    **为什么连 ``VERIFIED`` 一起作废**：链接已被点开、但还没换令牌的状态，
    同样是一张「还没用掉的凭据」。留着它等于留着一条已经铺好的路。

    **不提交**：提交权交给调用方 —— 作废必须与「改收件人」在**同一个事务**
    里，否则两者之间会有一个「配置已改、挑战还活着」的窗口。

    **轮询预算也一并清掉**：这些挑战已经死了，它们的限流桶留着只是占内存
    （见 :func:`reset_poll_state`）。
    """
    ids = list(db.scalars(
        select(AdminMfaChallenge.id).where(
            AdminMfaChallenge.status.in_([STATUS_PENDING, STATUS_VERIFIED]))
    ).all())
    if not ids:
        return 0
    # 条件写：只有「现在还是未完成」的才作废 —— 并发下另一个请求已经
    # 消费掉的挑战不该被算进这次作废的数量里。
    result = db.execute(
        update(AdminMfaChallenge)
        .where(AdminMfaChallenge.id.in_(ids),
               AdminMfaChallenge.status.in_([STATUS_PENDING, STATUS_VERIFIED]))
        .values(status=STATUS_EXPIRED, updated_at=func.now())
    )
    expired = int(result.rowcount or 0)
    for challenge_id in ids:
        reset_poll_state(challenge_id)
    if expired:
        log.info("已作废 %d 条未完成的二次验证挑战（原因：%s）", expired, reason or "未注明")
    return expired


def build_verify_link(link_base: str, email_token: str) -> str:
    """拼出邮件里的验证链接。"""
    base = (link_base or "").rstrip("/")
    return f"{base}/api/auth/mfa/verify?token={quote(email_token, safe='')}"


#: 没配 ``ADMIN_MFA_LINK_BASE`` 时给运维看的那句话。
#:
#: 说清三件事：**没发信**、**为什么不发**、**怎么改**。
#: 「为什么」必须写明白「不能用请求里的 Host 头」—— 否则运维看到的只是一句
#: 「没配置」，多半会当成配置项填错，然后随手填一个自己记得住的域名。
LINK_BASE_MISSING = (
    "没有配置站点地址（ADMIN_MFA_LINK_BASE），出于安全考虑未发送二次验证邮件。"
    "验证链接不会退回使用请求里的 Host 头（那是客户端可控的，会让二次验证变成"
    "钓鱼投递通道）。请让管理员在系统设置里配置站点地址"
    "（形如 https://acct.example.com），或设置环境变量 ADMIN_MFA_LINK_BASE 后重试。"
)


class MfaLinkBaseNotConfigured(RuntimeError):
    """站点地址没配 ⇒ **不发那封邮件**。

    这是**配置错误**，不是临时故障：不配就每一次都失败。调用方把它翻成
    503（服务不可用），**不是** 500 —— 500 会让人以为是程序坏了。
    """

    def __init__(self, message: str = LINK_BASE_MISSING) -> None:
        super().__init__(message)


def link_base(request) -> str:
    """验证链接的站点前缀。**生产环境没配就不发信**（fail-closed）。

    **只认配置里的** ``admin_mfa_link_base``（管理员显式配置的站点地址）。

    过去这里在留空时退回**请求的 base_url** —— 那是**客户端给的 Host 头**。
    后果是一条完整的账号接管链：攻击者拿到管理员口令后，带
    ``Host: evil.example`` 登录一次，系统就会向管理员邮箱发出一封
    **域名正规、系统自己发出**的邮件，但链接指向攻击者的域名；管理员点开 →
    ``email_token`` 落到对方服务器 → 对方调 ``/api/auth/mfa/poll`` 换到管理员
    访问令牌。**二次验证不但没挡住，反而成了投递通道。**

    所以：

    * **生产（``APP_ENV=prod``）没配 ⇒ 抛 :class:`MfaLinkBaseNotConfigured`**，
      由调用方翻成 503。**不发一封错的信，胜过发一封指向别人域名的信。**
    * 开发/测试环境退回 ``request.base_url`` 并记一条 **warning** ——
      本地跑不必先配一遍域名（那里没有「别人点你的链接」这件事）。

    环境判定用 :func:`app.core.config.detect_env` —— 与启动校验、``/health``
    是**同一个口径**（显式没配 ``APP_ENV`` 时按 prod 处理）。
    """
    configured = (settings.admin_mfa_link_base or "").strip()
    if configured:
        return configured.rstrip("/")
    if detect_env(settings) == "prod":
        log.error("ADMIN_MFA_LINK_BASE 未配置，拒绝发送二次验证邮件"
                  "（退回 Host 头会让二次验证变成钓鱼投递通道）")
        raise MfaLinkBaseNotConfigured()
    base = str(request.base_url).rstrip("/")
    log.warning("ADMIN_MFA_LINK_BASE 未配置，验证链接退回请求的 base_url：%s", base)
    return base


def verify_email_token(db: Session, token: str) -> tuple[bool, str, Optional[AdminMfaChallenge]]:
    """点邮件链接。**只置状态，绝不签发令牌。**

    返回 ``(成功, 原因, 挑战行)``。原因取值：``ok`` / ``invalid`` / ``used`` /
    ``expired`` —— 全部是**给人看的分类**，不含任何库里的细节。

    挑战行只在「这串 token 确实对得上库里某条挑战」时才有值。**对不上的
    随机 token 返回 None** —— 那个链接是公共的，任何人都能拿随机串来点，
    每次都留一条审计等于给审计表灌水（而真正要紧的是「一个真实挑战被
    点了两次」）。

    **为什么点链接不能直接拿到令牌**：邮件链接会被网关扫描、被手机锁屏
    预览、被 IT 留档。让它直接等于会话令牌，等于「谁截获了那封信谁就是
    管理员」。点链接只把挑战标成 VERIFIED，真正的令牌要由**发起登录的
    那个客户端**拿着 ``challenge_id + client_secret`` 来换。
    """
    raw = (token or "").strip()
    if not raw or len(raw) > 512:
        return False, "invalid", None

    row = db.scalar(select(AdminMfaChallenge).where(
        AdminMfaChallenge.email_token_hash == hash_secret(raw)))
    if row is None:
        return False, "invalid", None

    now = now_utc()
    if row.status == STATUS_EXPIRED:
        return False, "expired", row
    if row.status != STATUS_PENDING:
        # 已被用过（VERIFIED / CONSUMED）—— 再点一次必须失败。
        return False, "used", row
    if _expired(row, now):
        db.execute(
            update(AdminMfaChallenge)
            .where(AdminMfaChallenge.id == row.id,
                   AdminMfaChallenge.status == STATUS_PENDING)
            .values(status=STATUS_EXPIRED, updated_at=func.now())
        )
        db.commit()
        return False, "expired", row

    # 条件写：并发点两次只有一个能成功。
    result = db.execute(
        update(AdminMfaChallenge)
        .where(AdminMfaChallenge.id == row.id,
               AdminMfaChallenge.status == STATUS_PENDING)
        .values(status=STATUS_VERIFIED, verified_at=now, updated_at=func.now())
    )
    db.commit()
    if (result.rowcount or 0) != 1:
        return False, "used", row
    return True, "ok", row


class ConsumeResult(NamedTuple):
    """一次「换令牌」的结果。"""

    row: Optional[AdminMfaChallenge]
    state: str          # pending / consumed / expired / invalid / used
    emergency: bool = False


def consume_challenge(db: Session, challenge_id: int, client_secret: str,
                      emergency_code: str | None = None) -> ConsumeResult:
    """把挑战换成令牌。

    **只有 VERIFIED 的挑战能换到令牌**，而且换的动作是**原子**的
    （条件写 + 查 rowcount）—— 「先查后写」在两个并发请求下会让两个
    都以为自己拿到了这个挑战，于是同一个二次验证换出两个会话。

    ``invalid`` 与 ``used``/``expired`` 在接口层回**同一句文案**：
    「这个 id 存在吗」本身也是一条信息（可以用来数库里有多少挑战），
    而对使用者来说这两种情况的处置完全一样 —— 重新登录。
    """
    row = db.get(AdminMfaChallenge, challenge_id)
    if row is None or not secret_matches(client_secret, row.client_secret_hash):
        return ConsumeResult(None, "invalid")

    now = now_utc()

    if row.status == STATUS_PENDING:
        if _expired(row, now):
            db.execute(
                update(AdminMfaChallenge)
                .where(AdminMfaChallenge.id == row.id,
                       AdminMfaChallenge.status == STATUS_PENDING)
                .values(status=STATUS_EXPIRED, updated_at=func.now())
            )
            db.commit()
            return ConsumeResult(None, "expired")
        if emergency_code and consume_emergency_code(emergency_code):
            # 应急通道：邮件通道坏了时的唯一出路。它**用一次即废**。
            ok = db.execute(
                update(AdminMfaChallenge)
                .where(AdminMfaChallenge.id == row.id,
                       AdminMfaChallenge.status == STATUS_PENDING)
                .values(status=STATUS_VERIFIED, verified_at=now,
                        updated_at=func.now())
            )
            if (ok.rowcount or 0) != 1:
                db.commit()
                return ConsumeResult(None, "used")
            db.execute(
                update(AdminMfaChallenge)
                .where(AdminMfaChallenge.id == row.id,
                       AdminMfaChallenge.status == STATUS_VERIFIED)
                .values(status=STATUS_CONSUMED, consumed_at=now,
                        updated_at=func.now())
            )
            db.commit()
            db.refresh(row)
            return ConsumeResult(row, "consumed", emergency=True)
        return ConsumeResult(row, "pending")

    if row.status == STATUS_VERIFIED:
        result = db.execute(
            update(AdminMfaChallenge)
            .where(AdminMfaChallenge.id == row.id,
                   AdminMfaChallenge.status == STATUS_VERIFIED)
            .values(status=STATUS_CONSUMED, consumed_at=now,
                    updated_at=func.now())
        )
        db.commit()
        if (result.rowcount or 0) != 1:
            return ConsumeResult(None, "used")
        db.refresh(row)
        return ConsumeResult(row, "consumed")

    if row.status == STATUS_CONSUMED:
        return ConsumeResult(None, "used")
    return ConsumeResult(None, "expired")


def reset_poll_state(challenge_id: int) -> None:
    """清掉某个挑战的轮询计数（挑战作废时调用）。

    **键里含 secret 摘要，所以要按前缀清** —— 只删一个精确键会漏掉
    「用错 secret 试过」留下的那些桶。
    """
    prefix = f"{challenge_id}:"
    with _poll_lock:
        for key in [k for k in _poll_state if k.startswith(prefix)]:
            _poll_state.pop(key, None)


def poll_guard(challenge_id: int, client_secret: str = "") -> tuple[bool, int]:
    """轮询限流。返回 ``(放行吗, 还要等几秒)``。

    **最短间隔**挡住「一秒问 20 次」，**总次数上限**挡住「每隔 2 秒问
    一整天」。两者都只影响**轮询**这一个接口，不影响验证本身。

    **键含 ``client_secret`` 的摘要**（审查 §3.4）：预算按
    ``(challenge_id, secret 摘要)`` 记，而不是按 ``challenge_id`` 记。
    理由见上面 ``_poll_state`` 的注释 —— 按 id 记的话，攻击者用错的
    secret 就能替受害者扣掉轮询额度（401 发生在扣额度**之后**），
    而持有正确 secret 的合法客户端反而被挡在外面。

    ``client_secret`` 传空串时（调用方拿不到 secret 的场景）退化成
    「按 challenge_id 记」—— 那一档只挡住「同一个 id 的高频轮询」。
    """
    now = time.monotonic()
    key = f"{challenge_id}:{hash_secret(client_secret or '')}"
    with _poll_lock:
        last, count = _poll_state.get(key, (0.0, 0))
        elapsed = now - last
        if elapsed < POLL_MIN_INTERVAL_SECONDS:
            wait = int(POLL_MIN_INTERVAL_SECONDS - elapsed) + 1
            return False, wait
        if count >= POLL_MAX_ATTEMPTS:
            return False, 60
        if len(_poll_state) >= _POLL_STATE_MAX:
            # 丢掉最老的一批，防止这张表被随机 id 撑爆。
            for old in sorted(_poll_state, key=lambda k: _poll_state[k][0])[:256]:
                _poll_state.pop(old, None)
        _poll_state[key] = (now, count + 1)
        return True, 0


# ---------------------------------------------------------------------------
# 点链接接口的失败计数与退避（审查 L-02 / §3.3）
# ---------------------------------------------------------------------------
#
# ``/api/auth/mfa/verify`` **不需要鉴权** —— 它是一个公开的链接：邮件网关会
# 预取它、手机会在锁屏上预览它、IT 会留档它。所以它必然是未认证可达的。
#
# 令牌本身是 256 位随机，猜不中，**所以它不是认证绕过**。但它每次请求都要
# 打一次库（按 token 的散列查挑战行），于是「一个未认证的请求 = 一次数据库
# 查询」—— 而这个端点是全站唯一一个「谁都能打」的数据库入口。
#
# 这一层挡的就是这件事：**按来源计数 + 退避**。超过阈值就暂时不让这个来源
# 再打这个端点（连查询都不发生）。
#
# **只数「token 对不上任何挑战」的失败**（``invalid``）：那才是「在猜」的形态。
# 「已用过」「已过期」是**点错了链接的真人**（用户点两次旧邮件很常见），
# 把它算成失败会误伤正常使用，所以不计。
#
# 粒度是**进程级**，与 :func:`poll_guard` 同一套理由：它要挡的是「把点链接
# 当放大器」，进程级状态已经够用；跨进程限流需要额外的表，而这一点收益
# 换不来一张新表。
#
# ---------------------------------------------------------------------------
# **键改了：主键是「这串 token 自己的摘要」，来源 IP 退成二级维度**
# （审查 §3.3）
# ---------------------------------------------------------------------------
#
# 原来按**来源 IP** 记失败次数。这在「nginx 后面且 ``TRUSTED_PROXIES``
# 没配」的部署里是一个**全站共用的桶** —— 于是：
#
#     攻击者点 10 次垃圾链接  →  桶被封锁 60 秒
#     → 管理员点**真**链接      →  429「请 60 秒后再试」
#
# 而 ``verify`` 是管理员完成二次验证的**唯一**路径（``poll`` 依赖已创建的
# 挑战），所以这不是「这次登录被拒」，是**管理员再也登不进系统**。
# 攻击者只要 ~0.17 req/s 就能无限续期这个封锁。
#
# **为什么「按来源」这件事本身在这条路径上不成立**，值得单独说清楚 ——
# 同样的 429 在两条路径上的可用性含义**完全不同**：
#
# * **登录节流的 429** = 「这一次尝试被拒了」。客户端可以换个账号、
#   换个来源再来 —— 代价落在攻击者自己身上，**可用性没有损失**。
# * **MFA 验证的 429** = 「管理员**永远**完不成二次验证」。这里没有
#   fallback（除了进程环境里的 ``ADMIN_MFA_EMERGENCY_CODE``），被拒的
#   恰恰是**唯一能走通的那条路**。这时候 429 不是「拒绝一次尝试」，
#   是「把某个人彻底关在门外」—— 而触发它的动作**不需要任何凭据**。
#
# 所以这条路径上的限流**必须**按「攻击者伪造不出来、又不会全局共享」的
# 量来分桶。``email_token`` 的 sha256 摘要正好是这么一个量：它本身
# 就是 256 位随机密钥，攻击者**只能算自己发出去的那几串**的摘要，
# **算不出受害者那串**的摘要。
#
# 于是：
#
# * **主键 = token 摘要**：受害者那串 token 的桶，只有**拿那串 token
#   去点、而且点失败**才会被记 —— 而它一旦点对就成功，**永远不会进
#   失败分支**。所以受害者的桶永远是干净的。
# * **二级维度 = 来源 IP**：仍然记（要知道「这个来源在扫」），但它
#   **不再单独拦人** —— 它只用来**加长**同一个 token 的封锁窗口。
#
# **代价要说清楚（别把它说成「更严了」）**：来源维度的「聚合拦截」
# 没有了，于是「用一堆**各不相同**的垃圾 token 狂刷」这个形态，
# 每个请求仍然会打一次库（那是一次**唯一索引**上的点查，
# ``uq_admin_mfa_challenge_email_token``，不是全表扫）。
# 换来的是：那条路径上**再也不会出现「管理员被别人的失败挡住」**
# —— 而这正是这条限制最初要挡的东西。
# 换句话说，这里是拿「对公开端点的放大倍数」换「管理员不会被锁在门外」，
# 方向是刻意选的：被锁在门外的代价比多几次索引点查贵得多。

#: 同一串 token 在窗口内允许的「无效」次数。超过就封锁**它自己**。
#:
#: **比原来严（原来 10）**：这个端点必然是未认证可达的，阈值越低，
#: 「拿同一串错 token 反复试」这个形态越早就被掐掉。
VERIFY_FAIL_LIMIT = 5

#: 失败计数的有效窗口（秒）。超过这个时间的失败不累计。
VERIFY_FAIL_WINDOW_SECONDS = 900

#: 超过阈值后的退避时长（秒）。
#:
#: **刻意很短（20 秒，原来 60）**：退避期间这串 token 一个请求都打不动，
#: 而窗口越长，「误伤」的代价越大（点错链接的真人要等更久）。
#: 现在这条封锁**只作用于出错的那串 token**，不再牵连同一个来源的
#: 其他人 —— 连带影响已经没有了，窗口自然可以收短。
VERIFY_BLOCK_SECONDS = 20

#: 同一个来源在窗口内的失败次数上限。
#:
#: **它不拦人**，只决定「这个来源出错的 token 被封锁多久」：
#: 超过这个数就把封锁窗口乘三（见 :func:`note_verify_failure`）。
#: 把它单列成一个常量而不是塞进 IP 桶，是为了让人一眼看出
#: 「来源维度是二级维度」这件事。
VERIFY_SOURCE_FAIL_LIMIT = 30

#: 状态表的条数上限。超过就丢掉最老的一批 —— 防止「用随机来源刷限流表」
#: 把内存吃光（那等于换了个形式的放大器）。
_VERIFY_STATE_MAX = 2048

#: token 摘要 -> (窗口起点, 失败次数, 退避到什么时候)
_verify_state: dict[str, tuple[float, int, float]] = {}
#: 来源 -> (窗口起点, 失败次数)   **二级维度**
_verify_source_state: dict[str, tuple[float, int]] = {}
_verify_lock = threading.Lock()


def verify_key(token: str) -> str:
    """点链接限流的**主键**：这串 token 自己的 sha256 摘要。

    为什么是它：``email_token`` 本身就是 256 位随机密钥（见
    :func:`new_secret`），攻击者**算不出受害者那串**的摘要，只能算自己
    发出去的那几串 —— 所以按它分桶时，攻击者的失败**永远落不到**受害者
    的桶上。

    加 ``v:`` 前缀是为了不和来源键（``s:``）混在一张表里，也为了让人
    一眼看得出这是**限流桶的键**、不是凭证本身。
    """
    return "v:" + hash_secret(token or "")


def verify_source_key(source: str) -> str:
    """点链接限流的**二级维度**：来源地址。"""
    return "s:" + ((source or "-").strip() or "-")


def verify_guard(token_key: str) -> tuple[bool, int]:
    """点链接接口的准入。返回 ``(放行吗, 还要等几秒)``。

    **在查库之前问**：被退避的 token 连那条查询都不会发生 —— 那正是这一层
    要省掉的东西。

    **只按 token 摘要判**（审查 §3.3）：来源 IP 不参与拦截，理由见上面
    那一大段（「登录节流的 429」与「MFA 验证的 429」可用性含义不同）。
    """
    now = time.monotonic()
    with _verify_lock:
        started, count, blocked_until = _verify_state.get(token_key, (0.0, 0, 0.0))
        if blocked_until > now:
            return False, int(blocked_until - now) + 1
        # 退避期过了就重新计数 —— 不是「解封后接着上一次算」。
        if started and now - started > VERIFY_FAIL_WINDOW_SECONDS:
            _verify_state.pop(token_key, None)
        return True, 0


def note_verify_failure(token_key: str, source: str = "") -> None:
    """记一次「token 对不上任何挑战」。超过阈值就封锁**这串 token**。

    ``source`` 是**二级维度**：它只用来判断「这个来源是不是在狂刷」，
    命中时把同一串 token 的封锁窗口拉长 —— 它**不拦人**。
    """
    now = time.monotonic()
    source_bucket = verify_source_key(source)
    with _verify_lock:
        # ---- 二级维度：来源的失败计数 ----
        src_started, src_count = _verify_source_state.get(source_bucket, (0.0, 0))
        if not src_started or now - src_started > VERIFY_FAIL_WINDOW_SECONDS:
            src_started, src_count = now, 0
        src_count += 1
        _verify_source_state[source_bucket] = (src_started, src_count)

        # ---- 主键：这串 token 的失败计数 ----
        started, count, blocked_until = _verify_state.get(token_key, (0.0, 0, 0.0))
        if not started or now - started > VERIFY_FAIL_WINDOW_SECONDS:
            started, count = now, 0
        count += 1
        if count >= VERIFY_FAIL_LIMIT:
            window = VERIFY_BLOCK_SECONDS
            if src_count >= VERIFY_SOURCE_FAIL_LIMIT:
                # 这个来源在狂刷（同一个来源已经错了这么多次）——
                # 同一串错 token 的封锁窗口拉长。**仍然只作用于这串 token**，
                # 不牵连这个来源的其他人（这正是审查 §3.3 要修的那件事）。
                window *= 3
            blocked_until = now + window
            count = 0
        if len(_verify_state) >= _VERIFY_STATE_MAX:
            for old in sorted(_verify_state, key=lambda k: _verify_state[k][0])[:256]:
                _verify_state.pop(old, None)
        _verify_state[token_key] = (started, count, blocked_until)


def note_verify_success(token_key: str, source: str = "") -> None:
    """点对了就把计数清零 —— 正常使用不该背着旧账。

    顺带把**来源**的计数也清掉：这个来源刚刚证明了自己是**拿着真链接来
    点的人**（一次成功 = 一次口令级验证），它不该继续背着「这个来源在
    狂刷」的旧账。
    """
    with _verify_lock:
        _verify_state.pop(token_key, None)
        if source:
            _verify_source_state.pop(verify_source_key(source), None)


def reset_verify_state() -> None:
    """清掉点链接的失败计数。**只给测试用。**"""
    with _verify_lock:
        _verify_state.clear()
        _verify_source_state.clear()


# ----------------------------------------------------------------- 应急口令


def emergency_code_configured() -> bool:
    """进程环境里是否配了应急口令。"""
    return bool((settings.admin_mfa_emergency_code or "").strip())


def emergency_code_available() -> bool:
    """应急口令是否**此刻还能用**（配了且没用过）。"""
    with _emergency_lock:
        return emergency_code_configured() and not _emergency_used


def consume_emergency_code(raw: str) -> bool:
    """用一次即废。**绝不回显、绝不写日志。**

    比对用常量时间；口令只在**进程环境**里，因此「用一次即废」是进程级
    的 —— 重启进程会重新变成可用（运维重启服务本来就是一次明确的操作）。
    """
    configured = (settings.admin_mfa_emergency_code or "").strip()
    if not configured:
        return False
    try:
        if not hmac.compare_digest(configured, (raw or "").strip()):
            return False
    except (TypeError, ValueError):
        return False
    global _emergency_used
    with _emergency_lock:
        if _emergency_used:
            return False
        _emergency_used = True
        return True


def reset_emergency_code() -> None:
    """把「用过」这个状态清掉。**只给测试用。**"""
    global _emergency_used
    with _emergency_lock:
        _emergency_used = False


# --------------------------------------------------------------------- 发信


def _mail_text(username: str, link: str, ttl_minutes: int, reason: str) -> tuple[str, str]:
    """邮件的标题与正文。

    正文里**只有**验证链接 —— 没有 ``client_secret``、没有令牌、没有
    任何能直接当会话用的东西。收件人点链接能做的唯一一件事是「确认是我
    自己在登录」。
    """
    subject = "财务记账系统：管理员二次验证"
    what = ("有人在登录你的管理员账号" if reason == "login"
            else "有管理员操作需要重新确认身份")
    # 正文是**纯文本**：没有 markdown，也就没有 ``**加粗**`` 这种标记 ——
    # 纯文本邮件里那两个星号会原样印在收件人眼前（网页模板同理，
    # 那是同一个毛病）。强调靠「」和句子本身。
    body = (
        f"{what}。\n\n"
        f"账号：{username}\n"
        f"打开下面这个链接即完成二次验证（{ttl_minutes} 分钟内有效）：\n\n"
        f"{link}\n\n"
        "这个链接只能确认「是我在操作」，它本身不是登录凭证。\n"
        "确认之后请回到发起登录的那个浏览器窗口，点击「我已验证」继续。\n\n"
        "如果这不是你本人的操作，请立刻到「用户与授权」页重置管理员密码，"
        "并联系运维核查操作日志。\n"
    )
    return subject, body


def send_challenge_email(*, user: SysUser, challenge: AdminMfaChallenge,
                         email_token: str, link_base: str,
                         reason: str = "login") -> None:
    """旁路发信。**永不抛异常、永不阻塞调用方。**

    守护线程 —— 登录路径上多等一毫秒，都是攻击者能免费放大的。
    """
    def _run() -> None:
        try:
            _send_now(user=user, challenge=challenge, email_token=email_token,
                      link_base=link_base, reason=reason)
        except Exception:  # noqa: BLE001
            # 守护线程里的未捕获异常会打到 stderr，污染应用日志的格式。
            log.exception("二次验证邮件发送异常（已忽略，不影响登录流程）")

    try:
        thread = threading.Thread(target=_run, name="notify-admin-mfa", daemon=True)
        thread.start()
    except Exception:  # noqa: BLE001
        log.warning("无法启动二次验证邮件线程（已忽略）")


def _send_now(*, user: SysUser, challenge: AdminMfaChallenge,
              email_token: str, link_base: str, reason: str) -> None:
    """真发。失败抛 :class:`app.notify.mailer.MailError`，由上层兜住。"""
    from app.core.db import SessionLocal
    from app.notify import mailer
    from app.notify.config import recipients, smtp_config

    with SessionLocal() as session:
        config = smtp_config(session)
        addresses = recipients(session)

    if not (config.get("host") or "").strip() or not addresses:
        log.warning("二次验证邮件未发送：SMTP 未配置或没有收件人 challenge=%s",
                    challenge.id)
        return
    if not _slots.acquire(blocking=False):
        log.warning("二次验证邮件并发已满，丢弃一封 challenge=%s", challenge.id)
        return
    try:
        link = build_verify_link(link_base, email_token)
        subject, body = _mail_text(user.username, link,
                                    max(1, ttl_seconds() // 60), reason)
        count = mailer.send_mail(config, addresses, subject, body)
        log.info("二次验证邮件已发送 challenge=%s 收件人=%d", challenge.id, count)
    finally:
        _slots.release()
