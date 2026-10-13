"""认证：注册、登录、登出。

**登录路径上的每一条出口都要留痕**（审计 P1-6）。过去的实现里，
``_record_failure`` 只碰两个字段：失败计数与 ``locked_until``，
于是「有人在爆破管理员账号」这件事在 ``/api/system/audit-logs`` 里
一条痕迹都没有 —— 那张表恰恰是出事之后第一个被翻的地方。

留痕的边界写在这里，因为登录接口有个别的接口没有的特殊风险：
**审计写失败绝不能把登录搞挂**。审计是观测组件，一旦它成了可用性单点，
「审计坏了」就等于「整个系统登不进去」。所以这里的审计一律包在
SAVEPOINT 里（见 :func:`_audit`），失败只打应用日志、不影响本次登录。
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log as write_audit
from app.api.schemas import LoginIn, RegisterIn, TokenOut
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.models import SysUser, UserBook

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
    """这次登录请求来自哪里。

    nginx 在前面，真正的来源在 ``X-Forwarded-For`` 的**第一个**元素；
    直连部署时这个头没有，落到 ``request.client.host``。

    边界要说明白：这个值**只写进审计，不参与任何鉴权或锁定判断** ——
    也就是说即便有人伪造这个头，代价仅仅是让自己那条日志写着别人的 IP，
    换不来任何权限、也解不开任何锁定。所以「可伪造」在这里是可接受的；
    反过来「为了防伪造干脆不记」会让真实来源彻底丢失，那更糟 ——
    登录失败审计的全部价值就在于「谁、从哪、在扫什么」。
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "")[:45] or "-"


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


@router.post("/register", status_code=201)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    """自助注册：**只建账号，不授予任何账套**。

    刻意不在这里发 token：没有账套授权的用户登录后是一片空白，
    让他自己走一遍登录、看到「请联系管理员分配账套」，比自动进来更清楚。

    登记注册要留痕（P1-6）：自助注册是**唯一一条**不经过管理员就能往
    ``sys_user`` 里加行的路径，自助注册开着时它就是「谁进过这个系统」
    的完整名单。不留痕的话，被拿来自助建号进来的账号无法追认。
    """
    if not settings.registration_enabled:
        raise HTTPException(403, "系统未开放自助注册，请联系管理员开号")
    if db.scalar(select(SysUser).where(SysUser.username == payload.username)):
        raise HTTPException(400, f"用户名「{payload.username}」已被占用")
    u = SysUser(username=payload.username, password_hash=hash_password(payload.password),
                real_name=payload.real_name or payload.username, active=True)
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
    ip = _client_ip(request)
    user = db.scalar(select(SysUser).where(SysUser.username == payload.username))

    if user is not None and not user.active:
        # 停用账号同样要花掉一次口令校验：跳过它就把「这个用户存在但已停用」
        # 变成了一个比 401 快几十倍的旁路，跟未知用户名是同一种枚举信道。
        verify_password(payload.password, user.password_hash)
        _audit_login(False, db, ip, user, payload.username, "账号已停用")
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
            db.commit()
            raise HTTPException(423, f"账号已锁定，请 {left // 60 + 1} 分钟后再试")

    if user is None:
        # 等量计算：不存在的用户名也要走完一次口令校验（见 _dummy_hash）。
        verify_password(payload.password, _dummy_hash())
        _audit_login(False, db, ip, None, payload.username, "用户名不存在",
                     user_exists=False)
        db.commit()
        raise HTTPException(401, "用户名或密码错误")

    if not verify_password(payload.password, user.password_hash):
        _audit_login(False, db, ip, user, payload.username, "口令错误",
                     user_exists=True, failed_logins=user.failed_logins)
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
