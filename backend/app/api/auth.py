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
from typing import NamedTuple

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log as write_audit
from app.api.schemas import LoginIn, RegisterIn, TokenOut
from app.core import system_settings
from app.core.config import is_trusted_proxy, settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.models import SysUser, UserBook
# LoginIpThrottle 直接从定义模块导入而没有走 app.models 的导出桶：
# 那个桶不属于本条工作线的改动范围，而漏登记的实际后果只是「别人
# from app.models import * 时拿不到这个类」—— 全仓没有任何地方这么用。
from app.models.system import LoginIpThrottle

log = logging.getLogger("cwq")

router = APIRouter(prefix="/api/auth", tags=["认证"])


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
    """
    if not system_settings.registration_enabled(db):
        raise HTTPException(403, "系统未开放自助注册，请联系管理员开号")
    if db.scalar(select(SysUser).where(SysUser.username == payload.username)):
        raise HTTPException(400, f"用户名「{payload.username}」已被占用")
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


def _utcnow() -> datetime:
    """当前 UTC 时间，naive。

    库里 ``locked_until`` 是 naive 的 DateTime，比较时两边必须同口径。
    这里显式转成 UTC 再去掉 tzinfo，而不是用 ``datetime.utcnow()`` ——
    后者在新版 Python 已废弃，而它被悄悄换成「本地时间」的那天，
    东八区部署的锁定时长会凭空变成 8 小时。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _lock_left(user: SysUser) -> int:
    """还锁着几秒。没锁返回 0。

    拿服务器当前时间去比，而不是靠「锁定时记一个时间戳」之外的东西 ——
    库里的 ``locked_until`` 是 UTC naive 时间，比较时必须同样按 UTC 处理，
    否则部署在东八区时锁定会**变成 8 小时**（把本地时间当 UTC 比，偏了 8 小时）。
    """
    if user.locked_until is None:
        return 0
    return max(0, int((user.locked_until - _utcnow()).total_seconds()))


def _record_failure(db: Session, user: SysUser) -> bool:
    """记一次失败，达阈值就锁。**返回这次是否触发了锁定**。

    计数与锁定时间在同一个 flush 里写、分开写的话，中途断电会留下
    「计数加了但没锁」的账，攻击者正好卡在这个窗口里无限试。

    这里**不再自己提交**：提交权交给调用方，让「失败计数 / 锁定」与
    「这一次的审计」进同一个事务 —— 分两次提交就会出现「锁上了但
    审计没写」这种最坏的中间态（正是 P1-8 那类缺口的同构形态）。
    """
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= settings.login_max_failures:
        user.locked_until = _utcnow() + timedelta(minutes=settings.login_lock_minutes)
        user.failed_logins = 0
        return True
    return False


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


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
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
            # 锁定期间也**不**去校验口令：校验了就等于告诉攻击者
            # 「密码猜对了，只是被锁着」，把口令空间又缩掉一层。
            #
            # 这是唯一保留的时序/状态差异，它换来的是「锁定期内不泄漏口令」，
            # 两者不能兼得，取更严的一侧（423 只在**已经锁定过的账号**上出现，
            # 攻击者仍然可以靠它确认「这个账号存在且被锁过」，但拿不到任何
            # 口令信息）。锁定本身留痕，见下。
            _audit_login(False, db, ip, user, payload.username,
                         f"账号锁定中，剩余 {left // 60 + 1} 分钟",
                         locked_until=user.locked_until.isoformat() if user.locked_until else None)
            # 锁定中的尝试照样计入来源计数：它同样是一次未经认证的尝试，
            # 而「有人在猛敲一个已锁定的账号」正是这一层要看的东西。
            # （不计入的话，攻击者只要等账号被锁住就自动获得无限额度。）
            _note_source_failure(db, payload, src, now)
            db.commit()
            raise HTTPException(423, f"账号已锁定，请 {left // 60 + 1} 分钟后再试")

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
        if locked:
            # 锁定单独记一条：它是「这台系统在主动拒绝这个账号」的信号，
            # 事后第一眼要看的也是它（谁被锁了、锁到什么时候）。
            _audit(
                db,
                action="ACCOUNT_LOCKED",
                entity="user",
                entity_id=user.id,
                user=user,
                detail=f"连续登录失败达到阈值，账号被锁定 {settings.login_lock_minutes} 分钟："
                       f"{user.username}（{ip}）",
                after={"username": user.username, "ip": ip,
                       "lock_minutes": settings.login_lock_minutes,
                       "locked_until": user.locked_until.isoformat()},
            )
        db.commit()
        raise HTTPException(401, "用户名或密码错误")

    # 登录成功即清零：否则一次误输留下的计数会跟后面几次正常登录叠起来，
    # 最终把一个从没被人猜过的账号锁住。
    user.failed_logins = 0
    user.locked_until = None
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
