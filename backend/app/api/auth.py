"""认证：注册、登录、登出。

**登录路径上的每一条出口都要留痕**（审计 P1-6）。过去的实现里，
``_record_failure`` 只碰两个字段：失败计数与 ``locked_until``，
于是「有人在爆破管理员账号」这件事在 ``/api/system/audit-logs`` 里
一条痕迹都没有 —— 那张表恰恰是出事之后第一个被翻的地方。

留痕的边界写在这里，因为登录接口有个别的接口没有的特殊风险：
**审计写失败绝不能把登录搞挂**。审计是观测组件，一旦它成了可用性单点，
「审计坏了」就等于「整个系统登不进去」。所以这里的审计一律包在
SAVEPOINT 里（见 :func:`_audit`），失败只打应用日志、不影响本次登录。

**P2-11 的两半，本文件各占一半。**

* 「登录时序侧信道」早就不存在了：未知用户名与错误口令都会走完一次
  PBKDF2（见 :func:`_dummy_hash`），实测 33.4ms vs 32.7ms。**别再重做它。**
* 这一版补的是另一半 —— **按 IP 聚合的判断**。``sys_user`` 上的失败计数
  只回答「这个账号被打了多少次」，而轮换用户名的攻击源压根不碰同一个账号
  两次：它对上万个账号各试 5 次，每次只锁一个账号，锁定几乎不生效，
  审计里留下的也只是一堆互不相干的 ``LOGIN_FAILURE``。

所以下面这一整节的核心取舍是：**加判断，不加锁**。理由见
「IP 维度软节流」那一段的注释 —— 按 IP 硬锁在 NAT 后面是把几百人一起关在
门外，而且这个动作攻击者可以主动触发。
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import NamedTuple, Optional

import html
import logging
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log as write_audit
from app.api.schemas import LoginIn, RegisterIn, TokenOut
from app.core import audit as core_audit
from app.core import system_settings
from app.core import lockout
from app.core.config import is_trusted_proxy, settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.models import AdminMfaChallenge, SysUser, UserBook
# LoginIpThrottle 直接从定义模块导入而没有走 app.models 的导出桶：
# 那个桶不属于本条工作线的改动范围，而漏登记的实际后果只是「别人
# from app.models import * 时拿不到这个类」—— 全仓没有任何地方这么用。
from app.models.system import LoginIpThrottle
from app.notify import mfa as mfa_service

log = logging.getLogger("cwq")

router = APIRouter(prefix="/api/auth", tags=["认证"])


# --------------------------------------------------- 管理员二次验证：响应模型
#
# **这几个模型定义在本文件里，不在 api/schemas.py。**
# schemas.py 那一层是「通用接口的入参/出参」，而二次验证的形状是
# 「一次登录有三种可能的结局」（直接给令牌 / 要二次验证 / 报错），
# 把它混进通用层会让那个文件同时承担两件事。
#
# **回执里没有 access_token —— 这是这套接口存在的全部意义。**
# 邮件链接那串东西绝不能直接当会话令牌（它会被网关扫描、被手机锁屏
# 预览、被 IT 留档），理由见 app/models/mfa.py 的模块注释。

class MfaRequiredOut(BaseModel):
    """「口令对了，但还要再验一次」的回执。"""

    mfa_required: bool = True
    challenge_id: int
    # 只发给**发起登录的那个客户端**，库里只存它的散列。
    client_secret: str
    # 链接有效期（秒）。
    expires_in: int
    # 收件地址的**脱敏**形态（本地部分只留首尾各一位）。
    email_hint: str


class MfaPollOut(BaseModel):
    """轮询结果。待验证时只有 ``status``；换到令牌时多几个字段。"""

    status: str
    access_token: Optional[str] = None
    token_type: str = "bearer"
    expires_at: Optional[int] = None
    user: Optional[dict] = None


class MfaChallengeIn(BaseModel):
    """「用 challenge_id + client_secret 证明我是发起方」的入参。"""

    challenge_id: int = Field(ge=1)
    client_secret: str = Field(min_length=1, max_length=256)
    # 应急口令。**只在进程环境里配了 ADMIN_MFA_EMERGENCY_CODE 时才有用**，
    # 用一次即废。它绝不出现在任何响应里，也绝不写日志。
    emergency_code: Optional[str] = Field(default=None, max_length=256)


class MfaChallengeOut(BaseModel):
    """「重新发送」与「重新认证」的回执。"""

    challenge_id: int
    client_secret: str
    expires_in: int
    email_hint: str = ""
    # 距离下一次可以「重新发送」还有几秒。
    resend_after: int = 0


# --------------------------------------------------------------------- 审计


class _UnknownUser:
    """审计行里的「无此用户」占位。

    ``audit_helper.log`` 用 ``getattr`` 取 id / username，所以只要这两个属性。
    ``id`` 留空表示「这次登录尝试对应的账号不存在」—— 这本身就是要留的线索。
    """

    def __init__(self, username: str) -> None:
        self.id = None
        self.username = username[:64]


def _client_ip(request: Request) -> str:
    """这次登录请求来自哪里。**审计口径，不参与任何判定。**

    nginx 在前面，真正的来源在 ``X-Forwarded-For`` 的**第一个**元素；
    直连部署时这个头没有，落到 ``request.client.host``。

    边界要说明白：这个值**只写进审计** —— 也就是说即便有人伪造这个头，
    代价仅仅是让自己那条日志写着别人的 IP，换不来任何权限、也解不开任何锁定。
    在取证这件事上「宁可记一个可能被伪造的值，也别把真实来源弄丢」是对的：
    登录失败审计的全部价值就在于「谁、从哪、在扫什么」。

    **但判定绝不能用它**，理由见 :func:`_throttle_ip`：这里取的是 XFF 的
    最左元素，而最左元素恰恰是客户端自己写的第一个值，无条件采信它等于
    把限流的钥匙交给攻击者。所以下面另有一个 ``_throttle_ip``，两套口径
    故意分开，一套求「信息尽量多」，一套求「判定一定可靠」。
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "")[:45] or "-"


def _throttle_ip(request: Request) -> str:
    """节流判定用的来源地址。**只有这一个口径参与安全判断。**

    取值顺序：

    1. 对端不在 ``TRUSTED_PROXIES`` 白名单里 → 只认 ``request.client.host``。
       这是 TCP 层的真实对端，**攻击者伪造不了**（除非他控制了这台机器）。
    2. 对端是白名单里的代理 → 才去读代理头，且**先 ``X-Real-IP``**：
       ``deploy/nginx/cwq.conf`` 用的是
       ``proxy_set_header X-Real-IP $remote_addr``，那是**覆盖**而不是追加，
       客户端自己发什么都会被丢弃，所以它是干净的。
    3. 没有 ``X-Real-IP`` 才退回 ``X-Forwarded-For``，而且取**最右**一个元素。
       nginx 的 ``$proxy_add_x_forwarded_for`` 是**追加**语义：客户端发什么，
       nginx 都会在右边补上它自己看到的那个地址。所以最右边是代理亲眼见到的，
       最左边（审计口径取的那个）则是客户端写的，可以随便写。

    **默认（``TRUSTED_PROXIES`` 为空）落在第 1 条**：一个代理都不信，只按对端计。
    这样「伪造 XFF 换一个桶」这条路一开始就不存在 —— 攻击者轮换头部没有任何
    收益，所有请求仍然落在同一个对端上。反过来，配置成「无条件信任 XFF」是
    最容易写出来、也最容易被当成安全的那份代码，而它恰好把这一层防护整个作废：
    每次请求换一个头就能换一个桶，IP 维度限流对攻击者等于不存在。

    代价要说明白：**在反向代理后面且没配 ``TRUSTED_PROXIES`` 时，所有请求的
    对端都是那个代理**（nginx → 127.0.0.1:8000 的部署就是这样），于是全站
    共用一个桶，节流会误伤。这不是 bug 而是「没告诉系统谁是自己人」的必然结果，
    ``config.validate_production_settings`` 会在启动时为此打一条告警。
    """
    peer = (request.client.host if request.client else "") or ""
    if not is_trusted_proxy(peer):
        return peer[:45] or "-"

    real = (request.headers.get("x-real-ip", "") or "").strip()
    if real:
        return real[:45]
    fwd = (request.headers.get("x-forwarded-for", "") or "").strip()
    if fwd:
        # 最右元素才是代理自己看到的那个；最左是客户端写的（见 docstring）。
        return fwd.rsplit(",", 1)[-1].strip()[:45]
    return peer[:45] or "-"



def _audit(db: Session, **kw) -> None:
    """写一条审计；**写失败绝不能影响登录本身**。

    用 SAVEPOINT 收敛失败范围：审计插入出错（表缺失、字段超长、连接抖动）
    只回滚到保存点，外层事务里的计数器变更照常提交。反过来（不隔离）
    的话，一个纯观测组件就会成为登录的可用性单点 —— 而这是登录路径上
    唯一的「审计失败 ≠ 安全」的理由，不能省。

    同时也要说清楚**没有**放弃审计：
    * 写失败会 ``log.exception`` 进应用日志，运维能发现审计在漏；
    * 写成功时它与业务变更在**同一个事务**里提交，不存在
      「业务成了、审计丢了」的中间态。
    """
    try:
        with db.begin_nested():
            write_audit(db, **kw)
    except Exception:
        log.exception("审计写入失败（已忽略，不影响本次登录）action=%s", kw.get("action"))


def _audit_login(ok: bool, db: Session, ip: str, user: SysUser | None,
                 username: str, reason: str = "", **extra) -> None:
    """登录成功 / 失败的统一审计入口。"""
    _audit(
        db,
        action="LOGIN_SUCCESS" if ok else "LOGIN_FAILURE",
        entity="user",
        entity_id=user.id if user is not None else username[:64],
        user=user if user is not None else _UnknownUser(username),
        detail=f"{'登录成功' if ok else '登录失败'}：{username[:64]}（{ip}）"
               + (f"—— {reason}" if reason else ""),
        after={"username": username[:64], "ip": ip, "reason": reason, **extra},
    )


# --------------------------------------------------------------------- 注册


@router.get("/config")
def public_config(db: Session = Depends(get_db)):
    """登录页在**还没有令牌**的时候要知道「自助注册开没开」。

    匿名可读，而且只回一个布尔。它本来就不是秘密：注册开着的话任何人点一下
    就有分晓，关着的话也只有管理员看这个值有意义。

    这里不给令牌，是因为登录页此刻确实没有令牌 —— 把它做成登录后才能读，
    登录页就只能「先显示、再自己判断要不要藏」，于是注册按钮闪一下才消失，
    或者更糟：一直不消失，因为那条请求根本发不出去。

    **顺带提醒（前端要照着做）**：客户端拿到失败时应当按「关着」处理。
    接口挂了就藏掉注册入口，比把它显示出来再去撞一个 403 好。
    """
    return {"registration_enabled": system_settings.registration_enabled(db)}


#: 自助注册的用户名与口令的长度边界。
#:
#: **这里再写一遍是有意的。** ``RegisterIn`` 的定义在 ``api/schemas.py``，
#: 那份文件正由另一条工作线改动；而这一条边界（用户名 3-32 位、口令
#: 至少 8 位）**不该只由那一处来守** —— 一个「注册接口的口令下限」是
#: 安全边界，不是接口形状。把它就地再校一遍，schemas.py 那边将来放宽
#: 了，这里仍然拦得住。
#:
#: 与 ``RegisterIn`` 当前的口径一致（见 api/schemas.py），所以正常请求
#: 不会因为这段多出来的检查而改变结果。
_REGISTER_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_REGISTER_USERNAME_MIN = 3
_REGISTER_USERNAME_MAX = 32
_REGISTER_PASSWORD_MIN = 8
_REGISTER_PASSWORD_MAX = 128


def _validate_registration(payload: RegisterIn) -> None:
    """自助注册的入参就地校验。**不合规抛 422。**

    与 Pydantic 的 422 同一个状态码 —— 对调用方来说「参数不合法」就是
    参数不合法，不该因为校验发生在哪一层而给出两种形状的失败。
    """
    username = (payload.username or "").strip()
    if not (_REGISTER_USERNAME_MIN <= len(username) <= _REGISTER_USERNAME_MAX):
        raise HTTPException(
            422,
            f"用户名长度必须在 {_REGISTER_USERNAME_MIN} 到 "
            f"{_REGISTER_USERNAME_MAX} 位之间")
    if not _REGISTER_USERNAME_RE.match(username):
        raise HTTPException(422, "用户名只能包含字母、数字与下划线")
    if not (_REGISTER_PASSWORD_MIN <= len(payload.password or "")
            <= _REGISTER_PASSWORD_MAX):
        raise HTTPException(
            422,
            f"密码长度必须在 {_REGISTER_PASSWORD_MIN} 到 "
            f"{_REGISTER_PASSWORD_MAX} 位之间")


@router.post("/register", status_code=201)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    """自助注册：**只建账号，不授予任何账套**。

    刻意不在这里发 token：没有账套授权的用户登录后是一片空白，
    让他自己走一遍登录、看到「请联系管理员分配账套」，比自动进来更清楚。

    登记注册要留痕（P1-6）：自助注册是**唯一一条**不经过管理员就能往
    ``sys_user`` 里加行的路径，自助注册开着时它就是「谁进过这个系统」
    的完整名单。不留痕的话，被拿来自助建号进来的账号无法追认。

    闸门读的是**库里的开关**（库里没有则退回 env），管理员在界面上改完
    立即生效，不需要重启后端。

    **「用户名已被占用」不再是一个可区分的响应**（审查报告安全域 P2-2）。
    原来三种输入给三种可区分的响应：

        已存在用户名 → 400「用户名『victim』已被占用」
        不存在用户名 → 201 {"ok": true, ...}
        注册关闭时    → 403「系统未开放自助注册」

    第一条就是一条**用户名枚举信道** —— 而登录路径本来是靠「用户名不存在
    也走完一次 PBKDF2」堵住枚举的（``_dummy_hash``），注册路径把它敞开了。
    攻击者只要注册一下就能筛出所有有效用户名，再拿去登录爆破。

    现在：**重名与成功返回完全相同的响应**（201 + 同一个 body），只是
    **不建号**。于是 201 与 403 的差别只说明「自助注册开着没有」——
    那是公开信息（``GET /api/auth/config`` 已经暴露它），**不泄漏任何
    关于某个用户名是否存在的信息**。

    重名这件事不是消失，而是**挪到了管理员看得见的地方**：审计表里记一条
    ``REGISTER_DUPLICATE``，管理员看得到「有人在试这个手机号」。
    """
    if not system_settings.registration_enabled(db):
        # 关闭时对**所有人**是同一个响应 —— 这条是公开信息，不构成枚举信道。
        raise HTTPException(403, "系统未开放自助注册，请联系管理员开号")

    src = _throttle_ip(request)
    now = _utcnow()
    # **节流判定放在入参校验之前**：被节流的请求不必付校验的代价。
    _reject_if_register_throttled(db, payload.username, src, now)

    _validate_registration(payload)

    # **IP 桶**记每一次尝试 —— 枚举与批量建号都撞在这里（枚举者试的是
    # **不同的**名字，每次都是「成功」响应，所以这个桶必须统计成功）。
    _bump_register_bucket(db, _register_ip_bucket(src), now)

    if db.scalar(select(SysUser).where(SysUser.username == payload.username)):
        # **用户名桶只在重名时记** —— 「反复占用一个已经存在的账号」才是
        # 该被限流的事。成功注册不记：一次正常的注册不该给用户名攒计数
        # （那个用户名可能是用户自己在用，攒到阈值反而把本人挡在外面）。
        _bump_register_bucket(db, _register_user_bucket(payload.username), now)
        # **重名：不建号，但回成功一样的响应。** 响应里不出现「已被占用」。
        _audit(
            db,
            action="REGISTER_DUPLICATE",
            entity="user",
            entity_id=payload.username[:64],
            user=None,
            detail=f"自助注册命中已存在的用户名（未建号）：{payload.username[:64]}（{src}）",
            after={"username": payload.username[:64], "ip": src},
        )
        db.commit()
        return {"ok": True, "username": payload.username}

    # real_name **不再让自助注册的用户填**（前端那个输入框已去掉），空值兜底成
    # 「会计」。原先的兜底是 ``payload.username``，那是「用户名即身份标识」时代
    # 的做法；注册改成手机号之后，它会把一串号码当人的名字存进库，界面上就出现
    # 「13800138000」这种姓名。strip() 是为了连纯空白也一起兜住 ——
    # ``"   " or x`` 在 Python 里是真值。
    #
    # 注意管理员建号那条路径（api/system.py）**保持** ``or payload.username`` 不动：
    # 那里姓名是管理员显式填的，填了空才退回用户名，语义不同。
    u = SysUser(username=payload.username, password_hash=hash_password(payload.password),
                real_name=(payload.real_name or "").strip() or "会计", active=True)
    db.add(u)
    db.flush()
    # 快照字段与 system.py::_user_snapshot 对齐，**刻意不含 password_hash**：
    # 审计表是全局可读的，散列落进去等于给每个管理员送一份爆破素材。
    # 这里不 import system.py 的那个函数：那是 api→api 的横向依赖，
    # 而且它带的是管理员视角的字段（含 is_admin），自助注册者一律 false。
    _audit(
        db,
        action="REGISTER_USER",
        entity="user",
        entity_id=u.id,
        user=u,
        detail=f"自助注册：{u.username}（{_client_ip(request)}）",
        after={"id": u.id, "username": u.username, "real_name": u.real_name,
               "active": u.active, "is_admin": u.is_admin, "ip": _client_ip(request)},
    )
    db.commit()
    return {"ok": True, "username": u.username}


# --------------------------------------------------------------------- 注册节流
#
# **为什么注册路径要单开一套计数。**
#
# 登录的 IP 桶与注册共用一个键会互相污染阈值：登录失败会涨计数、注册成功
# 也涨计数，两种流量混在一个数里，谁也调不准那个阈值（「用自己日志污染的
# 阈值没有人调得准」是这条桶从一开始就写下的告诫）。
#
# 所以这里用**同一个表**（``login_ip_throttle``）、**不同的桶键前缀**：
#     ri:<ip>   这个来源在批量试
#     ru:<name> 这个用户名被反复试
# 前缀带在键里，于是两套计数在物理上分开，判定与计数仍共用同一份实现
# （一条 upsert，原子）。
#
# **键长。** ``login_ip_throttle.ip`` 是 ``VARCHAR(45)``，而 IPv6 最长 39
# 个字符 —— 所以前缀只能有 6 个字符以内。``ri:`` / ``ru:`` 都是 3 个，
# 加 39 = 42，留够。

def _register_ip_bucket(src: str) -> str:
    return f"ri:{src}"[:45]


def _register_user_bucket(username: str) -> str:
    return f"ru:{(username or '').strip()}"[:45]


def _register_window() -> timedelta:
    return timedelta(seconds=max(60, int(settings.register_window_seconds)))


def _bump_register_bucket(db: Session, bucket: str, now: datetime) -> int:
    """给注册桶记一次，返回窗口内的计数。**一条语句、原子。**

    与 :func:`_bump_ip_failure` 同一个形状（``ON CONFLICT DO UPDATE``），
    只是阈值与窗口不同、且不做硬锁 —— 注册路径只需要**软节流**：
    拒绝一段时间就行，不该有「这个 IP 被锁死」这种可被主动触发的状态。
    """
    stale = LoginIpThrottle.last_failed_at <= (now - _register_window())
    new_count = case((stale, 1), else_=LoginIpThrottle.failures + 1)
    stmt = (
        pg_insert(LoginIpThrottle)
        .values(ip=bucket, failures=1, window_started_at=now, last_failed_at=now,
                blocked_until=None, created_at=now, updated_at=now)
        .on_conflict_do_update(
            index_elements=[LoginIpThrottle.ip],
            set_={
                "failures": new_count,
                "window_started_at": case((stale, now),
                                          else_=LoginIpThrottle.window_started_at),
                "last_failed_at": now,
                "updated_at": func.now(),
            },
        )
    )
    db.execute(stmt)
    db.flush()
    row = db.scalar(select(LoginIpThrottle).where(LoginIpThrottle.ip == bucket))
    if row is None:
        return 0
    if row.last_failed_at is None or (now - row.last_failed_at) >= _register_window():
        return 0
    return int(row.failures or 0)


def _reject_if_register_throttled(db: Session, username: str, src: str,
                                   now: datetime) -> None:
    """两个桶都判一次；超阈值就 429。**一定抛异常。**

    先留痕再拒绝：证据比响应时间重要。
    """
    checks = (
        (_register_ip_bucket(src), int(settings.register_ip_max_attempts), "来源"),
        (_register_user_bucket(username), int(settings.register_username_max_attempts),
         "用户名"),
    )
    for bucket, threshold, kind in checks:
        row = db.scalar(select(LoginIpThrottle).where(LoginIpThrottle.ip == bucket))
        if row is None or row.last_failed_at is None:
            continue
        if (now - row.last_failed_at) >= _register_window():
            continue
        failures = int(row.failures or 0)
        if failures < threshold:
            continue
        retry = max(1, int(_register_window().total_seconds()
                           - (now - row.last_failed_at).total_seconds()))
        _audit(
            db,
            action="REGISTER_THROTTLED",
            entity="user",
            entity_id=(username or "")[:64],
            user=None,
            detail=f"自助注册被节流（{kind}维度）：用户名「{(username or '')[:64]}」"
                   f"窗口内 {failures} 次，阈值 {threshold}，请 {retry // 60 + 1} 分钟后再试",
            after={"username": (username or "")[:64], "ip": src,
                   "failures": failures, "threshold": threshold},
        )
        db.commit()
        raise HTTPException(
            429,
            f"请求过于频繁，请 {retry // 60 + 1} 分钟后再试",
            headers={"Retry-After": str(retry)},
        )


def _utcnow() -> datetime:
    """当前 UTC 时间，naive。

    库里 ``locked_until`` 是 naive 的 DateTime，比较时两边必须同口径。
    这里显式转成 UTC 再去掉 tzinfo，而不是用 ``datetime.utcnow()`` ——
    后者在新版 Python 已废弃，而它被悄悄换成「本地时间」的那天，
    东八区部署的锁定时长会凭空变成 8 小时。

    **实现在 core/lockout.py**：管理员后台也要用同一口径算「还剩多久」，
    两处各算一次迟早对不上（界面说没锁、登录却回 423，两头都在说谎）。
    """
    return lockout.utcnow()


def _lock_left(user: SysUser) -> int:
    """还锁着几秒。没锁返回 0。

    拿服务器当前时间去比，而不是靠「锁定时记一个时间戳」之外的东西 ——
    库里的 ``locked_until`` 是 UTC naive 时间，比较时必须同样按 UTC 处理，
    否则部署在东八区时锁定会**变成 8 小时**（把本地时间当 UTC 比，偏了 8 小时）。
    """
    return lockout.lock_left_seconds(user)


def lock_duration_minutes(level: int) -> int:
    """第 ``level`` 轮连续锁定要锁多久（分钟）。实现在 core/lockout.py。

    这里保留一个同名薄封装，是为了让本模块的读者不必跳到 core 才知道
    「锁多久」这件事的全部口径 —— 口径本身只有一份实现。
    """
    return lockout.lock_duration_minutes(level)


def _record_failure(db: Session, user: SysUser) -> dict | None:
    """记一次失败，达阈值就锁。

    返回**这次锁定的详情**（时长、轮次、截止时刻），没触发锁定时返回
    ``None``。调用方用它写审计并发通知 —— 这两件事都需要「锁了多久、
    第几轮」这两个数，而它们只在触发的那一刻才知道。

    计数与锁定时间在同一个 flush 里写、分开写的话，中途断电会留下
    「计数加了但没锁」的账，攻击者正好卡在这个窗口里无限试。

    这里**不再自己提交**：提交权交给调用方，让「失败计数 / 锁定」与
    「这一次的审计」进同一个事务 —— 分两次提交就会出现「锁上了但
    审计没写」这种最坏的中间态（正是 P1-8 那类缺口的同构形态）。
    """
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= settings.login_max_failures:
        # 轮次 +1，**不重置**：这正是递进式的「连续」所在。
        user.lock_level = (user.lock_level or 0) + 1
        # **阈值达成不等于「锁死」**（审查报告安全域 P2-4）。
        #
        # 原来这里直接写 ``locked_until = now + 10 分钟``，于是「知道用户名
        # 的人连错 3 次」就能把**别人的**账号挡在门外 10 分钟，时长还随轮次
        # 递增（10 → 20 → … → 1 天）—— 攻击成本极低，收益是让特定人在整段
        # 时间里进不去。月结期尤其致命。
        #
        # 现在：**前两轮是「递增延迟」，第三轮起才升级成硬锁**。
        #   轮次 1 → 延迟 5 秒；轮次 2 → 延迟 10 秒；轮次 ≥3 → 硬锁（423）。
        # 一次性 DoS 因此**不成立**（受害者等几秒就能继续干活），而持续撞库
        # 的速率逐轮塌下去。算法与权衡见 ``app/core/lockout.py``。
        if lockout.should_hard_lock(user.lock_level):
            minutes = lock_duration_minutes(user.lock_level)
            user.locked_until = _utcnow() + timedelta(minutes=minutes)
            # 硬锁这一轮内重新计数：从现在起又要错够阈值才会再次触发。
            user.failed_logins = 0
            return {"minutes": minutes, "level": user.lock_level,
                    "locked_until": user.locked_until, "hard": True}
        seconds = lockout.account_delay_seconds(user.lock_level)
        user.locked_until = _utcnow() + timedelta(seconds=seconds)
        # **这一轮内重新计数**：从现在起又要错够阈值才会再次触发。
        #
        # 这一条对**正常用户**很重要：不清零的话，延迟期过后**再错一次**
        # 就直接进下一轮、再错一次就硬锁 —— 一个手滑连错四次的人会被锁
        # 半小时。清零之后每一轮都要错够 ``login_max_failures`` 次，
        # 而延迟本身又把人劝退了。
        user.failed_logins = 0
        return {"minutes": 0, "seconds": seconds, "level": user.lock_level,
                "locked_until": user.locked_until, "hard": False}
    return None


# ------------------------------------------------------------- IP 维度软节流
#
# P2-11 的另一半。上面那一整套（failed_logins / locked_until）只认账号：
# 攻击源轮换用户名就能把它摊到上万个账号上，每次只锁一个，等于没锁。
# 下面按「来源 IP」把同一件事聚起来看。
#
# ## 为什么不按 IP 硬锁（下一个维护者最可能踩的坑，写在最前面）
#
# 看上去「IP 锁死」比 429 强得多，但在这套系统的真实部署里它是个
# **DoS 放大器**，理由有两条，缺一条都不成立：
#
# 1. **共享出口 IP 是常态，不是例外。** 办公网、家庭宽带、大客户出口、
#    运营商 NAT 后面可能是几百上千人共用一个 IP。按 IP 锁死等于把这几百人
#    一起关在门外 —— 防护直接变成了故障，而且故障面比攻击面大得多。
# 2. **这个动作可以被攻击者主动触发。** 拿到一个属于己方公司的 IP
#    （从公司网络里发请求即可），反复失败几次，这个 IP 在锁定期内对
#    全公司不可用。也就是说硬锁并不是「防住攻击者」，而是把攻击者手里的
#    工具从爆破换成了拒人于门外。
#
# 所以默认只有**软节流**：超阈值回 429 + 可读文案 + 留痕，**不锁账号、
# 不锁 IP**。真正的硬锁做成一个默认关闭的独立开关（``LOGIN_IP_HARD_LOCK``），
# 它适合「登录页完全对外暴露、且一人一户」的场景；默认关着，是因为在
# NAT 后面它两项收益都很有限、而可用性代价是确定的。
#
# ## 参数为什么这么取
#
# 阈值 30 次 / 15 分钟，比账号维度的 5 次高一个数量级。理由只有一条：
# 一个共享出口上，一个月结期几十个人输错口令是**正常业务噪声**；而
# 攻击者的每一次尝试都要付 PBKDF2 的 20ms，本来就不便宜。宁可让他多试
# 几次，也不要把一整个办公室挡在门外 —— 这条参数的取舍方向是单边的。
#
# 惩罚用「递增延迟 + Retry-After」而不是一把锁：超阈值后每多失败一次多睡
# 200ms（封顶 1 秒），同时给一个随超阈值次数增长的 ``Retry-After``。
# 更大的那部分惩罚交给客户端自己去退避，是因为服务端 sleep 要占住一个
# worker —— 那正是上面刚否掉 DoS 放大器的同一种资源消耗。


class _Verdict(NamedTuple):
    """一次登录尝试的节流判定结果。"""

    throttled: bool
    failures: int          # 窗口内该 IP 的失败总数
    retry_after: int       # 建议客户端等几秒（Retry-After 头）
    delay_ms: int          # 服务端本次要睡几毫秒
    locked_left: int = 0   # 硬锁剩余秒数（仅 LOGIN_IP_HARD_LOCK=true）


def _ip_window() -> timedelta:
    return timedelta(seconds=max(1, settings.login_ip_window_seconds))


def _ip_threshold() -> int:
    """超阈值判定的下限保护：配成 0 会让「任何一次失败都被节流」。"""
    return max(1, settings.login_ip_max_failures)


def _read_ip_state(db: Session, ip: str, now: datetime) -> LoginIpThrottle | None:
    """读该 IP 的节流状态；窗口已滑走就当作**没有这行**。

    窗口是滑动的：只有在连续一个窗口内没有失败时才整体清零
    （见 :func:`_bump_ip_failure`）。所以判定时也用同一条口径 ——
    判定和计数如果用两套窗口，就会出现「限流器认为超阈值、计数却不涨」
    这种自相矛盾的状态，而那种状态只能靠人调参才能解开。
    """
    row = db.scalar(select(LoginIpThrottle).where(LoginIpThrottle.ip == ip))
    if row is None:
        return None
    if row.last_failed_at is None or (now - row.last_failed_at) >= _ip_window():
        return None
    return row


def _ip_verdict(db: Session, ip: str, now: datetime) -> _Verdict:
    """这次尝试要不要被节流。不写库 —— 判定与计数是两件事。

    **被节流的尝试不会延长窗口。** 这条决定是「限流承诺」本身：
    每个 IP 每 ``login_ip_window_seconds`` 秒最多能验证
    ``login_ip_max_failures`` 次口令。若节流中的请求还去刷新
    ``last_failed_at``，那么一个照着 Retry-After 每隔一分钟重试一次的
    用户（或攻击者）就会把窗口无限续上，永远解不开 —— 手册上的
    「N 分钟后再试」变成一句空话。
    """
    if not settings.login_ip_throttle_enabled:
        return _Verdict(False, 0, 0, 0)
    row = _read_ip_state(db, ip, now)
    if row is None:
        return _Verdict(False, 0, 0, 0)

    failures = int(row.failures or 0)
    if settings.login_ip_hard_lock and row.blocked_until:
        left = int((row.blocked_until - now).total_seconds())
        if left > 0:
            return _Verdict(True, failures, left, 0, locked_left=left)

    threshold = _ip_threshold()
    if failures < threshold:
        return _Verdict(False, failures, 0, 0)

    # Retry-After：窗口滑完就该重新给额度，所以给「到窗口结束还剩多久」。
    elapsed = (now - row.last_failed_at).total_seconds()
    retry = max(1, int(_ip_window().total_seconds() - elapsed))
    retry = min(retry, max(1, settings.login_ip_retry_after_max_seconds))
    # 递增延迟：超阈值后每多一次失败多睡一档，封顶在 delay_max。
    step = max(0, settings.login_ip_delay_step_ms)
    delay = min(max(0, settings.login_ip_delay_max_ms), step * (failures - threshold + 1))
    return _Verdict(True, failures, retry, delay)


def _bump_ip_failure(db: Session, ip: str, now: datetime) -> int:
    """记一次该来源的失败，返回窗口内的失败总数。**一条语句、原子。**

    为什么必须是 upsert 而不是「SELECT 完再 UPDATE」：多进程/多连接下，
    两个并发请求会读到同一个旧值、各自加一写回同一个数 —— 失败数只涨 1，
    而攻击者只要并发打就把这一层计数废掉了。这与 ``failed_logins``
    当初必须放库里（而不是放内存）同一类错误，只是更隐蔽。

    ``ON CONFLICT ... DO UPDATE`` 里所有赋值读到的都是**更新前**的行值，
    所以「窗口已滑走就清零」这个判断不会被同一句里的
    ``last_failed_at = :now`` 污染 —— 这一点依赖 PG 的求值顺序，改写时别动。
    """
    stale = LoginIpThrottle.last_failed_at <= (now - _ip_window())
    new_count = case((stale, 1), else_=LoginIpThrottle.failures + 1)

    values: dict = {
        "failures": new_count,
        "window_started_at": case((stale, now),
                                  else_=LoginIpThrottle.window_started_at),
        "last_failed_at": now,
        # 这条语句是裸 Core SQL，绕过了 ORM 的 onupdate，所以 updated_at
        # 必须自己写（P2-09 那条「触发器改写行也要刷新时间戳」的同一类问题）。
        "updated_at": func.now(),
    }
    if settings.login_ip_hard_lock:
        # 默认关闭的分支。只在显式打开时写 blocked_until，且**只延长不缩短**：
        # func.greatest 让连续失败把锁期推到更远，而不是各自算出各自的时刻。
        until = now + timedelta(minutes=settings.login_ip_hard_lock_minutes)
        values["blocked_until"] = case(
            (stale, None),
            (new_count >= _ip_threshold(),
             case((LoginIpThrottle.blocked_until.is_(None), until),
                  else_=func.greatest(LoginIpThrottle.blocked_until, until))),
            else_=LoginIpThrottle.blocked_until,
        )

    stmt = pg_insert(LoginIpThrottle).values(
        ip=ip, failures=1, window_started_at=now, last_failed_at=now,
        blocked_until=None,
        created_at=func.now(), updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(index_elements=[LoginIpThrottle.ip],
                                      set_=values).returning(LoginIpThrottle.failures)
    return int(db.execute(stmt).scalar() or 0)


# 清理的节流：每个进程最多这么久才扫一次 login_ip_throttle。
#
# 这只是**省资源**的优化，不是正确性的一部分 —— 多进程下各扫各的，
# DELETE 本身幂等，谁先扫到谁省事。所以状态可以安全地放在进程内。
_PURGE_INTERVAL_S = 60.0
_last_purge_at: float = 0.0


def purge_expired_login_ip_state(db: Session, ttl_hours: int | None = None) -> int:
    """删掉久未活动的节流状态行，返回条数。**运维/定时任务入口。**

    这张表是按 IP 一行、只增不减的，写入方是每一次登录失败 —— 没有人
    会来手工清它。P2-23 批的正是那种「定义了却没人回收」的状态：配置项在、
    数据在涨、没人管。所以清理入口必须**可测**：这里就是被测试直接调用的那一个。

    保留期取 ``login_ip_state_ttl_hours``（默认 24h = 窗口的 9 倍）。
    取窗口的倍数而不是「刚好等于窗口」是有意的：刚出窗口的那几行正是
    还要用来判定「这个源是不是刚被放过」的状态，删早了等于让一次节流
    凭空消失。

    已过期的 ``blocked_until`` 不单独处理：判定读的是 ``last_failed_at``，
    行被删掉与锁已过期在效果上完全一致（都不再拦），不需要第二种写法。
    """
    global _last_purge_at
    ttl = settings.login_ip_state_ttl_hours if ttl_hours is None else ttl_hours
    if ttl <= 0:
        return 0
    cutoff = _utcnow() - timedelta(hours=ttl)
    result = db.execute(delete(LoginIpThrottle).where(LoginIpThrottle.last_failed_at < cutoff))
    _last_purge_at = time.monotonic()
    return int(result.rowcount or 0)


def _note_ip_failure(db: Session, ip: str, now: datetime) -> int:
    """失败路径上**唯一**的 IP 维度入口：顺带清理 + 记一次失败。

    清理挂在失败路径上（而不是成功路径）是因为这张表**只由失败写入** ——
    没有失败就没有行，没有行就不需要清理。挂在成功路径上会漏掉
    「系统被扫了一轮之后所有人都不再出错」的这段尾巴，那正是最容易攒下
    大量死行的时刻。
    """
    global _last_purge_at
    now_mono = time.monotonic()
    if now_mono - _last_purge_at >= _PURGE_INTERVAL_S:
        # try/except：清理是维护动作，不该因为它把一次登录搞挂
        # （与 _audit 同一个立场 —— 观测/维护组件不做可用性单点）。
        try:
            purge_expired_login_ip_state(db)
        except Exception:  # noqa: BLE001
            log.exception("IP 节流状态清理失败（已忽略）")
        else:
            _last_purge_at = now_mono
    # 关掉是真的**不写**：写一堆没人读的行，等于把「关掉」变成了
    # 「把清理开关也关掉了」—— 表只增不减，正是 P2-23 点名的那种状态。
    # 所以这里要判在写之前，不是只让判定函数返回 False。
    if not settings.login_ip_throttle_enabled:
        return 0
    return _bump_ip_failure(db, ip, now)


def _note_source_failure(db: Session, payload: LoginIn, src: str,
                         now: datetime) -> int:
    """失败路径的统一收尾：记来源计数，跨过阈值就留一条「扫描」事件。

    **四条失败路径（口令错 / 用户名不存在 / 账号停用 / 锁定中）都要走这里**。
    少一条就等于给攻击者留了一条免费的路：最常见的爆破形态恰恰是「用户名
    根本不存在」的探测（扫用户名不需要知道任何口令），而早期版本只在
    「口令错」那条路上留扫描事件 —— 扫用户名的人于是完全没有留下信号。

    返回窗口内的失败数，调用方有时要用。
    """
    failures = _note_ip_failure(db, src, now)
    if failures >= _ip_threshold():
        _audit_ip_scan(db, payload, src, failures)
    return failures


def _audit_ip_scan(db: Session, payload: LoginIn, src: str, failures: int) -> None:
    """「这个来源刚开始扫」的跨线事件。与节流拒绝分开记，两者处置不同。"""
    _audit(
        db,
        action="LOGIN_IP_SCAN",
        entity="user",
        entity_id=payload.username[:64],
        user=_UnknownUser(payload.username),
        detail=f"来源 {src} 在 {settings.login_ip_window_seconds} 秒内登录失败 "
               f"{failures} 次（阈值 {settings.login_ip_max_failures}），"
               f"下一次起按软节流拒绝：{payload.username[:64]}",
        after={"username": payload.username[:64], "ip": src, "failures": failures,
               "window_seconds": settings.login_ip_window_seconds,
               "max_failures": settings.login_ip_max_failures},
    )


def _reject_throttled(db: Session, payload: LoginIn, src: str,
                      verdict: _Verdict) -> None:
    """节流拒绝：**只拒绝这一次请求**，不锁账号、不锁 IP。**一定抛异常。**

    先留痕再延迟再拒绝：证据比响应时间重要，而且审计与「判定」在同一个
    事务里，不会出现「拒绝了但查不到是谁被拒」的中间态。

    这里**不写 LOGIN_FAILURE**，只写 ``LOGIN_IP_THROTTLED``。同一次请求
    写两行会让「这个源失败了几次」在任何统计里都翻倍，而翻倍后的数字
    恰恰是用来调这个阈值的 —— 被自己的日志污染的阈值没有人调得准。
    """
    hard = verdict.locked_left > 0
    detail = (f"来源 IP 被锁定（硬锁开关已开启），剩余 "
              f"{verdict.locked_left // 60 + 1} 分钟" if hard else
              f"来源 {src} 在 {settings.login_ip_window_seconds} 秒内失败 "
              f"{verdict.failures} 次，已达阈值，请 {verdict.retry_after // 60 + 1} 分钟后再试")
    _audit(
        db,
        action="LOGIN_IP_THROTTLED",
        entity="user",
        entity_id=payload.username[:64],
        user=_UnknownUser(payload.username),
        detail=f"登录被节流（{'IP 硬锁' if hard else '软节流，不锁账号'}）：{detail}",
        after={"username": payload.username[:64], "ip": src,
               "failures": verdict.failures, "hard_lock": hard,
               "window_seconds": settings.login_ip_window_seconds,
               "max_failures": settings.login_ip_max_failures,
               "retry_after": verdict.retry_after},
    )
    db.commit()

    if verdict.delay_ms:
        # 递增惩罚里服务端承担的那一小段（默认封顶 1 秒）。
        # 更大的惩罚在 Retry-After 里 —— 让客户端退避，而不是占住 worker。
        time.sleep(verdict.delay_ms / 1000.0)

    headers = {"Retry-After": str(verdict.retry_after)} if verdict.retry_after else None
    if hard:
        raise HTTPException(
            423, f"登录来源已被暂时锁定，请 {verdict.locked_left // 60 + 1} 分钟后再试",
            headers=headers)
    raise HTTPException(
        429,
        f"登录尝试过于频繁，请 {verdict.retry_after // 60 + 1} 分钟后再试。"
        f"如非本人操作请联系管理员。",
        headers=headers)



# --------------------------------------------------------------------- 时序等价


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """一个「谁的密码都不匹配」的合法散列，用来给未知用户名做等量计算。

    为什么需要：原来的写法是「用户不存在就直接 401」，跳过口令校验。
    PBKDF2 是刻意做慢的（这里 24 万次迭代，约 20ms），于是**扫用户名的
    成本几乎为零**，而**猜密码**一次要 20ms —— 响应时间差一个数量级，
    攻击者按响应快慢就能把用户名挨个筛出来，再拿筛选结果去打口令。

    所以用户名不存在时也要把同样的活干完。这一条同时把「爆破成本」
    从「扫免费、猜 20ms」拉平成「全是 20ms」，不引入任何新的 CPU 放大：
    每次请求至多**一次**口令校验，与一次正常登录完全同量。

    懒加载 + lru_cache：算一次 20ms，如果放在 import 期算就是让每次
    启动都白白付这 20ms（哪怕这个进程一整天没人登录）。
    """
    return hash_password("cwq-dummy-password-for-constant-time-login")


@router.post("/login", response_model=TokenOut | MfaRequiredOut)
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    """登录。

    **口令校验这一段一个字都没改** —— 时序等价、账号锁定、IP 软节流、
    审计留痕，全部保持原样（审计 P2-11 / P1-6 的那些结论不许被这次
    整改顺手弄丢）。

    变的只有**口令正确之后**：管理员且二次验证生效时**不发令牌**，改为
    走一次邮箱确认（见 :func:`_start_admin_mfa`）。二次验证没生效时，
    行为与改动前**完全一致** —— 回归测试（``tests/test_auth_permissions.py``
    / ``tests/test_auth_audit.py``）必须全绿。
    """
    ip = _client_ip(request)          # 审计口径（见 _client_ip 的 docstring）
    src = _throttle_ip(request)       # 判定口径，两者故意不同
    now = _utcnow()

    # 判定放在**口令校验之前**：被节流的来源每一次都不必付那 20ms 的 PBKDF2。
    # 这不是为了省 CPU —— 它同时是「节流期间不验证口令」这条原则的延伸：
    # 既然不验，正确口令与错误口令在节流期里拿到的回答完全相同（都是 429），
    # 不存在任何用响应差异去猜口令的余地。
    verdict = _ip_verdict(db, src, now)
    if verdict.throttled:
        _reject_throttled(db, payload, src, verdict)

    user = db.scalar(select(SysUser).where(SysUser.username == payload.username))

    if user is not None and not user.active:
        # 停用账号同样要花掉一次口令校验：跳过它就把「这个用户存在但已停用」
        # 变成了一个比 401 快几十倍的旁路，跟未知用户名是同一种枚举信道。
        verify_password(payload.password, user.password_hash)
        _audit_login(False, db, ip, user, payload.username, "账号已停用")
        _note_source_failure(db, payload, src, now)
        db.commit()
        raise HTTPException(401, "用户名或密码错误")

    if user is not None:
        left = _lock_left(user)
        if left > 0:
            # **「延迟」与「硬锁」是两件事**（审查报告安全域 P2-4）。
            #
            # * 硬锁（第 3 轮起）：423，锁几分钟到一天，等或让管理员解封。
            # * 延迟（第 1-2 轮）：429 + Retry-After，**等几秒就能继续用
            #   正确口令登录** —— 一次性把别人挡在门外的 DoS 因此不成立。
            #
            # 两种状态下都**不**校验口令：校验了就等于告诉攻击者
            # 「密码猜对了，只是被限着」，把口令空间又缩掉一层。
            #
            # 这是唯一保留的时序/状态差异，它换来的是「限定期内不泄漏口令」，
            # 两者不能兼得，取更严的一侧（423 只在**已经硬锁过的账号**上出现，
            # 攻击者仍然可以靠它确认「这个账号存在且被锁过」，但拿不到任何
            # 口令信息）。限流本身留痕，见下。
            state = lockout.delay_state(user)
            if state["hard_locked"]:
                _audit_login(False, db, ip, user, payload.username,
                             f"账号锁定中，剩余 {left // 60 + 1} 分钟",
                             locked_until=user.locked_until.isoformat() if user.locked_until else None,
                             lock_level=user.lock_level or 0)
                # 锁定中的尝试照样计入来源计数：它同样是一次未经认证的尝试，
                # 而「有人在猛敲一个已锁定的账号」正是这一层要看的东西。
                # （不计入的话，攻击者只要等账号被锁住就自动获得无限额度。）
                _note_source_failure(db, payload, src, now)
                db.commit()
                raise HTTPException(423, f"账号已锁定，请 {left // 60 + 1} 分钟后再试")

            # **递增延迟**：回 429 + Retry-After，让客户端等一会儿自己重试。
            # **不在服务端 sleep** —— 占住一个 worker 线程正是这个项目在
            # IP 维度节流上刻意避开的东西（那里也是把惩罚交给 Retry-After）。
            _audit_login(False, db, ip, user, payload.username,
                         f"连续失败过多，暂时限流 {left} 秒",
                         locked_until=user.locked_until.isoformat() if user.locked_until else None,
                         lock_level=user.lock_level or 0)
            _note_source_failure(db, payload, src, now)
            db.commit()
            raise HTTPException(
                429,
                f"连续登录失败次数过多，请 {left} 秒后重试",
                headers={"Retry-After": str(left)},
            )

    if user is None:
        # 等量计算：不存在的用户名也要走完一次口令校验（见 _dummy_hash）。
        verify_password(payload.password, _dummy_hash())
        _audit_login(False, db, ip, None, payload.username, "用户名不存在",
                     user_exists=False)
        _note_source_failure(db, payload, src, now)
        db.commit()
        raise HTTPException(401, "用户名或密码错误")

    if not verify_password(payload.password, user.password_hash):
        _audit_login(False, db, ip, user, payload.username, "口令错误",
                     user_exists=True, failed_logins=user.failed_logins)
        # 「这个来源在扫」的跨线事件由 _note_source_failure 统一留 ——
        # 四条失败路径共用一个入口，少一条就等于给攻击者留了一条免费的路。
        _note_source_failure(db, payload, src, now)
        locked = _record_failure(db, user)
        if locked and locked.get("hard"):
            # 锁定单独记一条：它是「这台系统在主动拒绝这个账号」的信号，
            # 事后第一眼要看的也是它（谁被锁了、锁到什么时候）。
            #
            # **只有硬锁才记**（审查报告安全域 P2-4）：递增延迟只有几秒，
            # 每次都记一条 ACCOUNT_LOCKED + 发一封告警邮件的话，
            # 一次撞库口令就能把收件箱刷满 —— 而收件箱是**通知通道本身**，
            # 把它刷爆等于把真告警一起刷爆了。
            _audit(
                db,
                action="ACCOUNT_LOCKED",
                entity="user",
                entity_id=user.id,
                user=user,
                detail=f"连续登录失败达到阈值，账号被锁定 {locked['minutes']} 分钟"
                       f"（第 {locked['level']} 轮）：{user.username}（{ip}）",
                after={"username": user.username, "ip": ip,
                       "lock_minutes": locked["minutes"],
                       "lock_level": locked["level"],
                       "locked_until": user.locked_until.isoformat()},
            )
        elif locked:
            # 递增延迟也留痕，但**不**发邮件（见上）。
            _audit(
                db,
                action="ACCOUNT_DELAYED",
                entity="user",
                entity_id=user.id,
                user=user,
                detail=f"连续登录失败达到阈值，暂时限流 {locked['seconds']} 秒"
                       f"（第 {locked['level']} 轮）：{user.username}（{ip}）",
                after={"username": user.username, "ip": ip,
                       "delay_seconds": locked["seconds"],
                       "lock_level": locked["level"],
                       "locked_until": user.locked_until.isoformat()},
            )
        db.commit()
        if locked and locked.get("hard"):
            # **通知在提交之后发**：状态与审计先落库，邮件是旁路。
            # 发不出去也不该有任何后果（notify.events.dispatch 自己兜住）。
            # 异步发：登录路径上多等一毫秒，都是攻击者能免费放大的。
            from app.notify import events as notify_events

            notify_events.account_locked(
                username=user.username, real_name=user.real_name or "",
                ip=ip, minutes=locked["minutes"], level=locked["level"],
                threshold=settings.login_max_failures)
        raise HTTPException(401, "用户名或密码错误")

    # 登录成功即清零：否则一次误输留下的计数会跟后面几次正常登录叠起来，
    # 最终把一个从没被人猜过的账号锁住。
    #
    # **lock_level 也一起清零**：登录成功意味着「这个账号回到了正常状态」，
    # 递进就该从头算 —— 否则一个人只要在解锁窗口里成功登录一次，下一轮
    # 又从 10 分钟起步，那条曲线永远升不上去。
    user.failed_logins = 0
    user.locked_until = None
    user.lock_level = 0

    # ---- 管理员二次验证的分叉 ------------------------------------------
    #
    # 口令到这里已经验过了。**从这里开始，管理员多一道邮箱确认**，
    # 而且**不发访问令牌** —— 令牌要等挑战被验证之后由客户端来换。
    #
    # 判定用 ``mfa_enabled``（**开关**）而不是 ``mfa_active``（开关+通道）：
    # 开关开着而通道没配好时，下面会**明确报错**，而不是悄悄放行 ——
    # 后者等于把一个安全开关变成一个假开关（界面上写着「已开启」，
    # 实际一个字节都没生效）。
    if user.is_admin and mfa_service.mfa_enabled(db):
        return _start_admin_mfa(db, user, request, ip)

    _audit_login(True, db, ip, user, payload.username,
                 is_admin=bool(user.is_admin))
    db.commit()
    token, expires = create_access_token(user.id, user.username, is_admin=bool(user.is_admin),
                                         token_version=user.token_version or 0)
    books = db.scalars(select(UserBook).where(UserBook.user_id == user.id)).all()
    return TokenOut(
        access_token=token,
        expires_at=expires,
        user={
            "id": user.id,
            "username": user.username,
            "real_name": user.real_name,
            "is_admin": user.is_admin,
            "books": [{"id": b.book_id, "role": b.role} for b in books],
        },
    )


# --------------------------------------------------- 管理员二次验证：实现
#
# 形态：**邮箱链接**。复用项目已有的 SMTP 通道（app/notify/mailer.py），
# 不引第三方依赖。整体设计与状态机见 app/notify/mfa.py 与 app/models/mfa.py。


def _mfa_audit(db: Session, action: str, user, detail: str, **extra) -> None:
    """写一条二次验证的审计。**写失败绝不能影响主流程**（见 core/audit.py）。"""
    core_audit.log_admin_mfa(db, action=action, user=user, detail=detail, **extra)


def _mfa_user_payload(db: Session, user: SysUser) -> dict:
    """令牌里带回的用户信息（与 /login 的 TokenOut.user 同构）。"""
    books = db.scalars(select(UserBook).where(UserBook.user_id == user.id)).all()
    return {
        "id": user.id,
        "username": user.username,
        "real_name": user.real_name,
        "is_admin": user.is_admin,
        "books": [{"id": b.book_id, "role": b.role} for b in books],
    }


def _start_admin_mfa(db: Session, user: SysUser, request: Request,
                     ip: str) -> MfaRequiredOut:
    """口令验过之后：**造挑战、发信、但不发令牌**。

    这一步是整条链路的分叉点。它返回的回执里**没有 access_token** ——
    令牌要等邮件里的链接被点开、客户端再来换，才发得出去。
    """
    error = mfa_service.channel_error(db)
    if error:
        # **开关开着但通道没配好 → 明确报错，绝不静默放行。**
        # 放行等于把一个安全开关变成一个假开关：界面上写着「已开启
        # 二次验证」，实际却一个字节都没生效。
        log.error("管理员二次验证通道未配置，登录被拒：user=%s ip=%s 原因=%s",
                  user.username, ip, error)
        _mfa_audit(db, core_audit.MFA_ACTION_FAILED, user,
                   f"二次验证通道未配置，登录被拒：{user.username}（{ip}）",
                   username=user.username[:64], ip=ip,
                   reason="channel_not_configured")
        db.commit()
        raise HTTPException(503, error)

    challenge, email_token, client_secret = mfa_service.create_challenge(
        db, user, reason="login", ip=ip,
        user_agent=request.headers.get("user-agent", ""))
    _mfa_audit(db, core_audit.MFA_ACTION_SENT, user,
               f"口令已通过，已向通知邮箱发送二次验证链接：{user.username}（{ip}）",
               username=user.username[:64], ip=ip, challenge_id=challenge.id,
               reason="login")
    db.commit()
    # 旁路发信：发不出去也不该让登录失败（那是一次可用性事故）。
    mfa_service.send_challenge_email(
        user=user, challenge=challenge, email_token=email_token,
        link_base=mfa_service.link_base(request), reason="login")
    return MfaRequiredOut(
        mfa_required=True,
        challenge_id=challenge.id,
        client_secret=client_secret,
        expires_in=mfa_service.ttl_seconds(),
        email_hint=mfa_service.email_hint(db),
    )


_VERIFY_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; background: #f1f5f9;
         font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         color: #0f172a; }}
  .card {{ background: #fff; padding: 40px 32px; border-radius: 12px;
           max-width: 420px; text-align: center;
           box-shadow: 0 12px 40px rgba(15, 23, 42, .10); }}
  h1 {{ font-size: 20px; margin: 0 0 12px; }}
  p {{ font-size: 14px; line-height: 1.8; margin: 0; color: #334155; }}
  .ok h1 {{ color: #047857; }}
  .bad h1 {{ color: #b91c1c; }}
</style>
</head>
<body>
  <div class="card {cls}">
    <h1>{title}</h1>
    <p>{message}</p>
  </div>
</body>
</html>
"""


def _verify_page(ok: bool) -> str:
    """点链接之后的那一页。

    **这一页里不能出现任何用户输入**，也不能出现访问令牌 —— 页面里
    只有两句固定文案（都要转义，虽然它们是常量）。

    为什么不让点链接直接给令牌：邮件链接会被网关扫描、被手机锁屏预览、
    被 IT 留档。让它直接等于会话令牌，等于「谁截获了那封信谁就是
    管理员」。
    """
    if ok:
        title = "验证成功"
        message = ("二次验证已完成。请回到发起登录的那个浏览器窗口，"
                   "点击「我已验证」继续登录。")
        cls = "ok"
    else:
        title = "验证链接无效"
        message = ("这个链接无效、已经被用过，或者已经过期。"
                   "请回到登录窗口重新登录，或点击「重新发送」获取新链接。")
        cls = "bad"
    return _VERIFY_PAGE.format(
        title=html.escape(title),
        message=html.escape(message),
        cls=html.escape(cls),
    )


@router.get("/mfa/verify")
def mfa_verify(token: str = Query("", max_length=512),
               db: Session = Depends(get_db)):
    """点邮件里的验证链接。**只置状态，不发令牌。**

    返回一个极简页面（人能读懂），而不是 JSON —— 收件人是在浏览器里
    点开的，他要看的是「成没成」，不是一段机器才能解析的东西。

    **失败时也不泄露任何内部细节**：文案里没有表名、没有堆栈、没有
    「token 不存在」这种可供区分的信息（区分了就能拿来数库里有多少
    挑战）。
    """
    ok, reason, row = mfa_service.verify_email_token(db, token)
    if row is not None:
        # 只给「确实对得上某条挑战」的点击留痕。随机 token 的点击**不留**
        # —— 这个链接是公共的，每次都留一条等于给审计表灌水。
        user = db.get(SysUser, row.user_id)
        if ok:
            _mfa_audit(db, core_audit.MFA_ACTION_VERIFIED, user or _UnknownUser(""),
                       f"二次验证链接已点开：{row.reason}",
                       username=(user.username[:64] if user else ""),
                       challenge_id=row.id, reason=row.reason)
        else:
            _mfa_audit(db, core_audit.MFA_ACTION_FAILED, user or _UnknownUser(""),
                       f"二次验证链接无效：{reason}",
                       username=(user.username[:64] if user else ""),
                       challenge_id=row.id, reason=reason)
        db.commit()
    return HTMLResponse(_verify_page(ok))


@router.post("/mfa/poll", response_model=MfaPollOut, response_model_exclude_none=True)
def mfa_poll(payload: MfaChallengeIn, request: Request,
             db: Session = Depends(get_db)):
    """轮询挑战状态；验证通过后**原子地**换到访问令牌。

    **必须同时有 ``challenge_id`` 和 ``client_secret``** —— 少一个都不行：
    只有 challenge_id 的话，任何人猜到/枚举一个 id 就能把别人的挑战
    换走。

    **换令牌是原子的**（条件写 + 查 rowcount）—— 同一个挑战只能换出
    一个令牌，第二次来拿不到。

    ``response_model_exclude_none``：待验证时只回 ``{"status":"PENDING"}``，
    **不要回一个 ``"access_token": null``** —— 那会让「响应里有没有
    令牌」这件事变得没法一眼看清（``"access_token" in body`` 为真，
    而它其实什么都没给）。
    """
    allowed, retry_after = mfa_service.poll_guard(payload.challenge_id)
    if not allowed:
        raise HTTPException(
            429, f"验证状态轮询过于频繁，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)})

    result = mfa_service.consume_challenge(
        db, payload.challenge_id, payload.client_secret,
        emergency_code=payload.emergency_code)

    if result.row is None:
        # 找不到 / secret 不对 / 已用过 / 已过期 —— **同一句文案**。
        # 区分它们等于告诉别人「这个 id 存在」，那是信息泄露。
        reason = "expired" if result.state == "expired" else "invalid"
        user = db.get(SysUser, result.row.user_id) if result.row else None
        _mfa_audit(db, core_audit.MFA_ACTION_FAILED, user or _UnknownUser(""),
                   f"二次验证请求被拒：{reason}",
                   challenge_id=payload.challenge_id, reason=reason,
                   ip=_client_ip(request))
        db.commit()
        if reason == "expired":
            raise HTTPException(410, "验证链接已过期，请重新登录")
        raise HTTPException(401, "验证请求无效，请重新登录")

    user = db.get(SysUser, result.row.user_id)
    if user is None or not user.active:
        raise HTTPException(401, "用户不存在或已停用")

    if result.state == "pending":
        # 还没点链接 —— 这是**正常状态**，不是错误。
        return MfaPollOut(status="PENDING")

    # ---- 换令牌 ------------------------------------------------------
    now = mfa_service.now_utc()
    token, expires = create_access_token(
        user.id, user.username, is_admin=bool(user.is_admin),
        token_version=user.token_version or 0,
        mfa_at=int(now.timestamp()))
    _mfa_audit(db, core_audit.MFA_ACTION_CONSUMED, user,
               f"二次验证通过，已签发访问令牌：{user.username}"
               f"（{_client_ip(request)}）",
               username=user.username[:64], ip=_client_ip(request),
               challenge_id=result.row.id, reason=result.row.reason,
               emergency=bool(result.emergency))
    db.commit()
    return MfaPollOut(status="VERIFIED", access_token=token, expires_at=expires,
                      user=_mfa_user_payload(db, user))


@router.post("/mfa/resend", response_model=MfaChallengeOut)
def mfa_resend(payload: MfaChallengeIn, request: Request,
               db: Session = Depends(get_db)):
    """重新发送验证邮件。

    **受最短间隔约束**（``admin_mfa_resend_interval_seconds``）：超间隔
    回 429 并带 ``Retry-After``。没有它，一次误点「登录」就能把收件箱
    刷满几十封信 —— 而收件箱**就是通知通道本身**，把它刷爆等于把
    告警通道一起刷爆了。

    **重发会换新的挑战**（新的 email_token 与新的 client_secret），
    旧的链接当场作废 —— 一封还在别人收件箱里的旧信不该还能用。
    """
    row = db.get(AdminMfaChallenge, payload.challenge_id)
    if row is None or not mfa_service.secret_matches(
            payload.client_secret, row.client_secret_hash):
        raise HTTPException(401, "验证请求无效，请重新登录")

    now = mfa_service.now_utc()
    interval = mfa_service.resend_interval_seconds()
    last_sent = row.last_sent_at
    if last_sent is not None and last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    if last_sent is not None:
        elapsed = (now - last_sent).total_seconds()
        if elapsed < interval:
            wait = int(interval - elapsed) + 1
            raise HTTPException(
                429, f"发送过于频繁，请 {wait} 秒后再试",
                headers={"Retry-After": str(wait)})

    user = db.get(SysUser, row.user_id)
    if user is None:
        raise HTTPException(401, "验证请求无效，请重新登录")

    new_row, email_token, client_secret = mfa_service.create_challenge(
        db, user, reason=row.reason or "login", ip=row.ip,
        user_agent=row.user_agent)
    _mfa_audit(db, core_audit.MFA_ACTION_SENT, user,
               f"重新发送二次验证链接：{user.username}（{_client_ip(request)}）",
               username=user.username[:64], ip=_client_ip(request),
               challenge_id=new_row.id, reason=new_row.reason, resent=True)
    db.commit()
    mfa_service.send_challenge_email(
        user=user, challenge=new_row, email_token=email_token,
        link_base=mfa_service.link_base(request), reason=new_row.reason)
    return MfaChallengeOut(
        challenge_id=new_row.id,
        client_secret=client_secret,
        expires_in=mfa_service.ttl_seconds(),
        email_hint=mfa_service.email_hint(db),
        resend_after=interval,
    )


@router.post("/mfa/reauth", response_model=MfaChallengeOut)
def mfa_reauth(request: Request, user: SysUser = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """已登录的管理员请求「重新验证」。

    **挑战的 user_id 来自当前登录用户**，不能由请求方指定 —— 否则
    「为另一个账号发起验证」就成了一条越权路径。

    走完这一套之后，客户端用**同一个 poll 接口**换到一个**新的**令牌
    （新令牌带新鲜的 ``mfa_at``），再用新令牌重试那个高危动作。
    """
    if not user.is_admin:
        raise HTTPException(403, "只有系统管理员可以执行该操作")
    if not mfa_service.mfa_enabled(db):
        raise HTTPException(400, "二次验证未启用，无需重新验证")
    error = mfa_service.channel_error(db)
    if error:
        log.error("管理员二次验证通道未配置，无法重新验证：user=%s 原因=%s",
                  user.username, error)
        raise HTTPException(503, error)

    ip = _client_ip(request)
    row, email_token, client_secret = mfa_service.create_challenge(
        db, user, reason="reauth", ip=ip,
        user_agent=request.headers.get("user-agent", ""))
    _mfa_audit(db, core_audit.MFA_ACTION_SENT, user,
               f"已请求重新验证：{user.username}（{ip}）",
               username=user.username[:64], ip=ip, challenge_id=row.id,
               reason="reauth")
    db.commit()
    mfa_service.send_challenge_email(
        user=user, challenge=row, email_token=email_token,
        link_base=mfa_service.link_base(request), reason="reauth")
    return MfaChallengeOut(
        challenge_id=row.id,
        client_secret=client_secret,
        expires_in=mfa_service.ttl_seconds(),
        email_hint=mfa_service.email_hint(db),
        resend_after=mfa_service.resend_interval_seconds(),
    )


@router.post("/logout")
def logout(request: Request, user: SysUser = Depends(get_current_user),
           db: Session = Depends(get_db)):
    """登出：只留痕，**不做任何撤销**。

    令牌是自签名的 JWT、服务端不存会话，所以「登出」在服务端做不了
    撤销动作 —— 客户端丢掉 localStorage 里的令牌就是登出的全部语义。
    要真撤销得走 ``token_version``（那会把这个账号**所有**设备的会话
    一起踢掉，不该由「点一下退出」触发）。

    但留痕做得到，而留痕正是这个接口存在的理由：出事之后
    「最后一个离开的人是谁」往往就是第一条线索（共享账号、离职交接、
    攻击者登进去看了一眼就走）。
    """
    ip = _client_ip(request)
    _audit(
        db,
        action="LOGOUT",
        entity="user",
        entity_id=user.id,
        user=user,
        detail=f"登出：{user.username}（{ip}）",
        after={"username": user.username, "ip": ip},
    )
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(user: SysUser = Depends(get_current_user), db: Session = Depends(get_db)):
    books = db.scalars(select(UserBook).where(UserBook.user_id == user.id)).all()
    return {
        "id": user.id,
        "username": user.username,
        "real_name": user.real_name,
        "is_admin": user.is_admin,
        "books": [{"id": b.book_id, "role": b.role} for b in books],
    }
