"""FastAPI 依赖：当前用户、账套授权，以及**口令复核**。

**这一层为什么同时管「账套授权」与「口令复核」。**

两者回答的是同一个问题的两半：**「现在这个请求，是谁在做？」**
账套授权回答「他能不能动这本书」，口令复核回答「他是不是真的他」——
而后者是**不能从令牌里读出来的**：令牌可以被 XSS 拿走，光有令牌不足以
证明「就是他本人按的这个按钮」。这两半都住在同一层，是因为它们都依赖
同一件事：``sys_user`` 这一行 + ``sys_user_book`` 那张授权表。
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import lockout
from app.core.config import settings
from app.core.db import get_db
from app.core.security import decode_access_token, verify_password
from app.models import Book, SysTokenRevocation, SysUser, UserBook

bearer = HTTPBearer(auto_error=False)

# 账套角色的**唯一**合法取值。
#
# 这三处必须一致，改一处就得改另外两处：
#   core/deps.py（这里，判定）→ models/system.py（CHECK 约束）→ api/system.py::ROLES（入口校验）
# 少一处的后果都是「值能进库但判不出来」，进而退化成放行。
WRITE_ROLES = frozenset({"OWNER", "ACCOUNTANT"})
READ_ONLY_ROLES = frozenset({"VIEWER"})

# 高危动作撞上「近期二次验证」闸门时，回给客户端的**机器可读**错误码。
#
# **为什么不能只靠那句中文文案**：前端要判断「这个 403 是不是『该去重新
# 验证』」，才能给出「去重新验证」的引导 —— 没有引导的话，管理员在令牌超过
# 宽限期之后碰高危动作，只会看到一句 403，**界面上没有下一步**（审计 §3.2
# 闸门扩到 13 条受管控写路由之后，这是管理员唯一会撞上的墙）。
#
# 拿中文文案去匹配（detail.includes("二次验证")）是**拿协议去猜协议**：
# 文案一改就静默失效 —— 403 还是那个 403，只是没人弹引导了，管理员撞死路，
# 而且**没有任何报错**（最难查的那一类）。
#
# 有稳定错误码之后，文案可以随便改；前端认的是 code，不认文案。
# 前端那边认的就是这个字符串（frontend/src/api/client.js）——
# 两边必须一起改，后端那条用例（test_audit_fix_admin_write_mfa_gates.py）
# 会跨语言核对一遍。
MFA_REAUTH_REQUIRED = "MFA_REAUTH_REQUIRED"


def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
) -> SysUser:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少访问令牌")
    try:
        payload = decode_access_token(creds.credentials)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "访问令牌无效或已过期")
    # 把载荷挂到 request.state 上，供下游依赖读取（见
    # require_recent_admin_mfa）。**不要重新解一遍 JWT** —— 那既慢，
    # 又会出现「解两次得到两份载荷」的不一致窗口。
    if request is not None:
        request.state.auth_payload = payload
    # **``sub`` 缺失或不是数字 ⇒ 401，不是 500**（审查 M-12）。
    #
    # 签名合法但载荷里没有 ``sub`` 的令牌（老版本签发的、别的服务用同一把
    # 密钥签的、手工构造的）会**解码成功** —— 错误只会在下面取 ``sub`` 时
    # 才炸，于是 KeyError/ValueError 变成一次 500，并把堆栈写进日志。
    # 一个「令牌不对」的请求必须回 401：它是**客户端**的问题，不是服务端的。
    sub = payload.get("sub")
    if sub is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "访问令牌无效或已过期")
    try:
        user_id = int(sub)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "访问令牌无效或已过期")
    user = db.get(SysUser, user_id)
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已停用")
    # 令牌版本核对（审计 P2-01）。JWT 自签名、服务端不存会话，所以「谁签发的、
    # 什么时候签的」之外，服务端唯一能查到的状态就是库里这一列。
    # 管理员重置口令或停用账号时把它加一，这个账号此前签发的所有令牌同时作废 ——
    # 口令泄露后的标准处置就是「改口令」，过去改完旧令牌还能用满 12 小时，
    # 等于没改。
    #
    # **这一条在「令牌不限时」之后更关键了**（见 §9.9）：令牌不再有到期时间，
    # 于是「改口令 / 停用」是唯一能立刻作废一整批令牌的手段。
    #
    # 载荷里没有 ``tv`` 的老令牌按 0 处理：升级前签发的令牌在存量库里
    # 也是 tv=0，仍然可用（改口令/停用会立刻作废它们）。
    if int(payload.get("tv", 0)) != (user.token_version or 0):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "访问令牌已失效（密码或账号状态已变更），请重新登录")
    # 单枚吊销（登出）。**登录令牌不设时间上限**（载荷里没有 ``exp``），
    # 所以它的终点只有三个：这里、上一行的 ``tv``、以及签名对不上。
    #
    # **查库，不做跨请求缓存。** 缓存会让「A 机器登出、B 机器上的同一枚令牌
    # 还能用」—— 生产是两个 uvicorn worker（``deploy/systemd/cwq-api.service``），
    # 进程内缓存只看得见自己那一份。吊销必须**对下一次请求立刻生效**，
    # 这与「改密 / 停用立刻生效」是同一条纪律。
    #
    # 代价：每个请求多一次主键点查（``jti`` 上的唯一约束自带索引），
    # 约零点几毫秒。换成「过期就好」的成本是「登出之后那枚令牌永远有效」——
    # 后者贵得多。
    jti = payload.get("jti")
    if jti:
        revoked = db.scalar(select(SysTokenRevocation.id)
                             .where(SysTokenRevocation.jti == jti))
        if revoked is not None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "访问令牌已失效（该会话已登出），请重新登录")
    return user


def get_my_book(book_id: int, db: Session = Depends(get_db),
                user: SysUser = Depends(get_current_user),
                request: Request = None) -> Book:  # type: ignore[assignment]
    _link_or_403(request, db, user.id, book_id)
    book = db.get(Book, book_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "账套不存在")
    return book


def require_admin(user: SysUser = Depends(get_current_user)) -> SysUser:
    """系统管理员专用依赖。

    操作日志里存着每次删除/修改的完整凭证快照 —— 等同于全量凭证副本，
    普通会计不该看到别人的操作痕迹。
    """
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有系统管理员可以访问")
    return user


def require_recent_admin_mfa(
    user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
) -> SysUser:
    """高危动作的「近期重新认证」依赖。

    **语义**：当前用户必须是管理员，**且**令牌的 ``mfa_at`` 落在
    ``admin_mfa_grace_minutes`` 分钟内 —— 否则 403，并明确告诉用户
    「需要在 X 分钟内完成邮箱二次验证」以及去哪儿做（``POST
    /api/auth/mfa/reauth``）。

    **为什么令牌里没有 ``mfa_at`` 就一律拒绝**（fail-closed）：那意味着
    这个会话是「没经过二次验证」来的 —— 老令牌、或者二次验证开着但
    这个会话是关着的时候签发的。两种都不该被当成「刚验过」。

    **开关关着时这个依赖不拦人**。理由是可用性：二次验证是**可选的
    组织决策**，默认关闭（见 ``admin_mfa_enabled`` 的注释）—— 若「没开
    二次验证」就等于「所有管理员都做不了高危动作」，那么这个功能的
    默认状态会变成一次全站事故。

    但**开关开着而通道坏了的时候一律拒绝**（fail-closed）—— 那时管理员
    本来就换不到新令牌（连登录都过不去），高危动作同样不该放行。
    真正的逃生口是 ``scripts/mfa_admin.py --disable``（明确关掉）与
    进程环境里的 ``ADMIN_MFA_EMERGENCY_CODE``。

    **父会话接线点**：``api/system.py`` 的高危子集挂这个依赖即可，
    不必再判断开关 —— 判定在这里。
    """
    from app.notify import mfa as mfa_service

    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只有系统管理员可以访问")
    if not mfa_service.mfa_enabled(db):
        return user

    grace = mfa_service.grace_seconds()
    payload = None
    if request is not None:
        payload = getattr(request.state, "auth_payload", None)
    mfa_at = (payload or {}).get("mfa_at")

    def _need_reauth() -> HTTPException:
        """这个 403 的**唯一**出处。

        detail 是**结构化**的（``code`` + ``message``）而不是一句纯文案 ——
        前端要靠 ``code`` 判断「这个 403 该给『去重新验证』的引导」，
        靠文案去猜是拿协议去猜协议（见文件头 MFA_REAUTH_REQUIRED 的说明）。

        两处拒（没有 ``mfa_at`` / ``mfa_at`` 太旧）走同一个函数 ——
        同一句话有两个出处时，改了一处就可能出现「两种 403 前端认得不一样」。
        """
        return HTTPException(
            status.HTTP_403_FORBIDDEN,
            {
                "code": MFA_REAUTH_REQUIRED,
                "message": f"该操作需要近期完成邮箱二次验证：请在 {grace // 60} 分钟内通过 "
                           "POST /api/auth/mfa/reauth 重新验证后再试",
            },
        )

    if not mfa_at:
        raise _need_reauth()
    try:
        age = time.time() - float(mfa_at)
    except (TypeError, ValueError):
        age = None
    if age is None or age > grace:
        raise _need_reauth()
    return user


def normalize_role(role: Optional[str]) -> str:
    """把库里存的 role 归一化成判定用的形式。

    大小写与首尾空白都吃掉。**权限判定不能因为「写库的人手滑打了个小写」
    就换一个含义** —— 过去的写法是 ``if link.role == "VIEWER"``，
    于是一个字面量比不中就放行。
    """
    return (role or "").strip().upper()


def is_read_only(role: Optional[str]) -> bool:
    """这个角色能不能写。**白名单语义**（审计 P2-12）。

    黑名单（只挡 "VIEWER"）与白名单（只放行 OWNER/ACCOUNTANT）的差别，
    全部落在「不认识的值」上：'viewer'、'read_only'、''、None、
    'Owner'（没被归一化到的那层）—— 黑名单一律**放行**，于是绕过 API
    直接改库的写入者（psql、脚本、误连的 ETL）拿到完整写权限，
    这与项目通篇 fail-closed 的原则正好相反。

    白名单一律**按只读处理**：配置写错的后果变成「这个人进不去」，
    而不是「这个人拿到全部权限」，并且真出问题时管理员能在审计里
    看到那个非法角色值，改回来即可。
    """
    return normalize_role(role) not in WRITE_ROLES


def require_write(book: Book, user: SysUser, link: UserBook) -> None:
    """只读账号一律拒写，**含一切不认识的角色值**。

    区分两种拒绝文案不是讲究：VIEWER 是正常业务状态（用户知道自己是只读），
    而非法的角色值是**配置事故**，需要有人去修 —— 文案一样的话，
    管理员看到满屏「当前账套为只读权限」会以为是自己配的只读。
    """
    role = (link.role or "")[:32]
    if normalize_role(role) in READ_ONLY_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "当前账套为只读权限")
    if normalize_role(role) not in WRITE_ROLES:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"账套角色「{role}」不在系统允许的取值内（{'、'.join(sorted(WRITE_ROLES | READ_ONLY_ROLES))}），"
            "已按只读处理，请联系管理员修正")


def get_book_link(book_id: int, db: Session = Depends(get_db),
                  user: SysUser = Depends(get_current_user),
                  request: Request = None) -> UserBook:  # type: ignore[assignment]
    return _link_or_403(request, db, user.id, book_id)


def require_book_write(book_id: int, db: Session = Depends(get_db),
                       user: SysUser = Depends(get_current_user),
                       request: Request = None) -> UserBook:  # type: ignore[assignment]
    """**账套写操作统一依赖**：既要归属这个账套，又要能写。

    为什么抽成依赖：权限校验散在各路由里，历史上就这么漏过。
    ``close_period`` / ``unclose_period`` / ``year_end`` / ``set_openings``
    当时只挂了 ``get_my_book``（它只查「有没有授权」，不查角色），而同一个文件里的
    ``carry_forward`` 查了 —— 于是 VIEWER 能结账、反结账、年度结转、篡改期初。

    **规则：新增写接口一律挂这个依赖**，不要手写 ``if link.role == "VIEWER"``。
    漏挂时 ``tests/test_auth_permissions.py`` 的全量矩阵用例会红 ——
    用测试盯住「每个写接口都挡住了 VIEWER」，而不是靠人记得。

    返回 ``UserBook``：调用方通常已经通过 ``get_my_book`` 拿到账套了，
    两个依赖可以并排挂，不冲突。

    这里**不再自己查一遍** ``UserBook``（审计中等 4）：``get_my_book`` /
    ``get_book_link`` / ``require_book_write`` 发的是同一条查询，而几乎所有写接口
    把它们并排挂着（``vouchers.py:119``、``printing.py:236``），于是每个写请求在
    业务逻辑开始前就有 4 次串行往返。现在统一走 :func:`_link_or_403`，
    同一个请求内只发一条。
    """
    # 直接调函数体而不是再挂一次 Depends：这里三个参数都由外层依赖注入了，
    # 走 get_book_link() 等于再查一次 UserBook。
    link = _link_or_403(request, db, user.id, book_id)
    require_write(None, user, link)  # type: ignore[arg-type]
    return link


def _link_or_403(request: Optional[Request], db: Session,
                 user_id: int, book_id: int) -> UserBook:
    """查「这个用户在这个账套里的授权」，没有就 403。**同一个请求内只查一次。**

    复用是**请求内**的，不是跨请求缓存（审计中等 4 明确要求不做跨请求缓存：
    改密、停用、换角色必须立刻生效）。载体是 ``request.state`` —— 它随这一个请求
    的生命周期存在，下一个请求重新查。
    查询的键是 ``(user_id, book_id)`` 而不是只按账套，所以一个请求里碰多个账套
    也不会串。

    没有 ``request``（比如被当成普通函数直接调用）时退化成每次都查：
    少一次优化，但不会因为拿不到 request 就崩。
    """
    cache = None
    if request is not None:
        cache = getattr(request.state, "_cwq_book_links", None)
        if cache is None:
            cache = {}
            request.state._cwq_book_links = cache
    key = (user_id, book_id)
    if cache is not None and key in cache:
        link = cache[key]
    else:
        link = db.scalar(select(UserBook).where(
            UserBook.user_id == user_id, UserBook.book_id == book_id))
        if cache is not None:
            # None 也记下来：「没有授权」同样是这个请求里已经查明的事实，
            # 第二个依赖再问一遍不会得到别的答案。
            cache[key] = link
    if link is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "没有该账套的访问权限")
    return link


# ---------------------------------------------------------------------------
# 口令复核：失败记账与「当前密码」闸门
# ---------------------------------------------------------------------------
#
# **为什么这两块住在 core，而不是 ``api/auth.py``。**
#
# 「一次口令算错就记一次、连续算错到阈值就按阶梯延迟、再错就硬锁」这条规则
# 现在有**三个执行点**：登录（``api/auth.py::login``）、自助改密码
# （``api/auth.py::change_own_password``）、签发 MCP 密钥
# （``api/mcp_tokens.py``）。三处各写一份，迟早会对不上 —— 而对不上的那处
# 几乎总是**更松**的那侧（少记一次失败 = 白送一个不限速的密码猜测机）。
#
# 所以规则与算术都住在 core：时长与阶梯的口径在 ``core/lockout.py``，
# 执行（那把行锁 + 计数 + 锁定）在下面这个模块。两个 api 模块都往下依赖它，
# 依赖方向是单向的 —— 不存在 api→api 的横向依赖。


def record_password_failure(db: Session, user: SysUser) -> Optional[dict]:
    """记一次口令校验失败，达阈值就锁。

    返回**这次锁定的详情**（时长、轮次、截止时刻），没触发锁定时返回
    ``None``。调用方用它写审计并发通知 —— 这两件事都需要「锁了多久、
    第几轮」这两个数，而它们只在触发的那一刻才知道。

    计数与锁定时间在同一个 flush 里写、分开写的话，中途断电会留下
    「计数加了但没锁」的账，攻击者正好卡在这个窗口里无限试。

    这里**不再自己提交**：提交权交给调用方，让「失败计数 / 锁定」与
    「这一次的审计」进同一个事务 —— 分两次提交就会出现「锁上了但
    审计没写」这种最坏的中间态（正是 P1-8 那类缺口的同构形态）。

    **并发下不能丢更新**（审查 §3.7）。原来这里是纯 Python 侧的
    读改写：``user.failed_logins = (user.failed_logins or 0) + 1``，
    而这行 ``user`` 是在 **PBKDF2（约 50ms）之前**用普通 SELECT 加载的 ——
    于是 N 个并发请求各自读到**同一个旧值**、各自写 ``+1``，
    **只有最后一次写生效**。

    实测（阈值 3）：20 次**并发**错误口令之后，计数只到 2、锁根本没上
    （20 次真实口令校验全部执行）；而 20 次**顺序**尝试之后是
    ``failed_logins=0, lock_level=1``（锁上了）。**阈值 3 实际放过了 33 次。**
    代码在每个延迟档位都把计数清零（下面两处 ``failed_logins = 0``），
    于是丢更新的影响被进一步放大。

    而 IP 维度的节流是**并发安全**的（一条原子 upsert），所以账号计数器
    是「分布式喷洒」的**唯一**防线 —— 偏偏它就是失效的那个。

    **怎么修的**：把这一行**锁住再读** —— ``SELECT ... FOR UPDATE``。
    它把「读-改-写」整段变成临界区，并发请求在这里排队，于是每个人
    读到的都是**上一个请求已经提交**的值，计数不会丢。

    ``populate_existing=True`` 不是可选项：这一行在**同一个 Session**
    里已经有一个实例了（登录路径前面用普通 SELECT 读过），而 ORM 默认
    **不会**用查询结果覆盖已加载实例的属性 —— 不加这个参数，
    ``FOR UPDATE`` 只是把行锁住、拿到的却仍是那个**陈旧的旧值**，
    丢更新照旧。
    """
    # 先把这一行锁住再读。``populate_existing`` 让 ORM 用锁住的行
    # **刷新**已加载的实例（见上面的说明）。
    #
    # ``locked`` 与 ``user`` 是**同一个对象**（同一个 Session 的同一行），
    # 所以下面继续写 ``user`` 即可 —— 调用方随后读到的
    # ``user.failed_logins`` / ``user.locked_until`` 就是刷新后的值。
    locked = db.scalar(
        select(SysUser).where(SysUser.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        # 用户在这次请求期间被删掉了：没有行可锁，也就没有计数可加。
        return None
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= settings.login_max_failures:
        # 轮次 +1，**不重置**：这正是递进式的「连续」所在。
        #
        # **这一行不能少**：少了它，轮次永远停在 0，于是
        # ``should_hard_lock`` 永远为假 —— 递增延迟的阶梯也就永远不会
        # 往上走，「第 3 轮起分钟级硬锁」那半段**根本到不了**。
        # 搬这段代码时漏过它一次（症状是「第 3 次算错之后 lock_level 仍是 0，
        # 延迟永远是 5 秒，硬锁那一档形同虚设」），所以在这里写明它。
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
            minutes = lockout.lock_duration_minutes(user.lock_level)
            user.locked_until = lockout.utcnow() + timedelta(minutes=minutes)
            # 硬锁这一轮内重新计数：从现在起又要错够阈值才会再次触发。
            user.failed_logins = 0
            return {"minutes": minutes, "level": user.lock_level,
                    "locked_until": user.locked_until, "hard": True}
        seconds = lockout.account_delay_seconds(user.lock_level)
        user.locked_until = lockout.utcnow() + timedelta(seconds=seconds)
        # **这一轮内重新计数**：从现在起又要错够阈值才会再次触发。
        #
        # 这一条对**正常用户**很重要：不清零的话，延迟期过后**再错一次**
        # 就直接进下一轮、再错一次就硬锁 —— 一个手滑连错四次的人会被锁
        # 半小时。清零之后每一轮都要错够 ``login_max_failures`` 次，
        # 而延迟本身也把人劝退了。
        user.failed_logins = 0
        return {"minutes": 0, "seconds": seconds, "level": user.lock_level,
                "locked_until": user.locked_until, "hard": False}
    return None


def verify_current_password(db: Session, user: SysUser, password: str) -> dict:
    """复核「当前密码」—— 一次口令校验的**完整闸门**。

    ### 为什么这道关不能省

    会话令牌是**可以被拿走的**：XSS、共享电脑、令牌落在别人手里。令牌在手
    就能调那些「铸造长期凭据」的接口 —— 而 MCP 密钥**永不过期**，铸出来
    是一枚能读你全部账套的**永久**读凭据。没有这道关，拿到令牌的人顺手
    就替本人铸了一枚，而且那枚密钥不会随登出或改密失效。

    这道关把「有令牌」降级成「有令牌**且**知道口令」—— 而知道口令的人
    本来就已经是账号的主人了。

    ### 三步，顺序不能换

    1. **先看这个账号是不是正处在延迟 / 硬锁中**。锁定中的账号**不许再试**
       —— 否则「正在被撞」这件事就有了第二条不受限速的路。
    2. **再验口令**。
    3. **验错了就记一次**（:func:`record_password_failure`），由那一套计数
       决定要不要进入延迟 / 硬锁。

    ### 为什么失败也计入登录那套锁定

    这个闸门验证的也是口令，而它是一个**持着有效令牌**就能调的接口 ——
    登录那边的账号锁定与 IP 节流**都不覆盖它**。不另设一套，就是白送一个
    不限速的口令猜测机：拿着（偷来的）令牌每秒几十次地猜当前密码，猜中即
    铸出一枚永久密钥。PBKDF2 的 33ms 只把**单次**成本抬高，抬高不出「限速」。

    所以口径与登录**完全一致**：同一套计数（``failed_logins``）、同一套
    递进延迟、同一套硬锁（``lock_level`` / ``locked_until``）。

    ### 返回值（**不抛 HTTPException**）

    审计动作与文案由调用方决定 —— 登录、自助改密、签发 MCP 密钥三处的
    审计动作名不同，但「这条闸门怎么判」只有这一份。

    * 成功：``{"ok": True}``
    * 失败：``{"ok": False, "reason": ..., "left_seconds": int, "locked": dict|None}``

      ``reason`` 取三个值之一：

      * ``"hard_locked"`` —— 账号正处在硬锁中（423）；
      * ``"delayed"`` —— 账号正处在递增延迟中（429，等几秒就能继续）；
      * ``"mismatch"`` —— 口令不对（400；若这一次恰好触发锁定，
        ``locked`` 里有详情，调用方据此回 429 / 423）。

    **失败时失败计数已经加过了** —— 调用方要么把这次请求拒掉，要么
    （不应该）当它没发生过，但计数是加了的。
    """
    # ---- 1. 此刻是不是正处在延迟 / 硬锁中 -----------------------------
    state = lockout.delay_state(user)
    if state["hard_locked"]:
        return {"ok": False, "reason": "hard_locked",
                "left_seconds": state["left_seconds"], "locked": None}
    if state["delayed"]:
        return {"ok": False, "reason": "delayed",
                "left_seconds": state["left_seconds"], "locked": None}

    # ---- 2. 验口令 ------------------------------------------------------
    if verify_password(password or "", user.password_hash):
        return {"ok": True}

    # ---- 3. 算错：记一次，由计数决定要不要锁 --------------------------
    locked = record_password_failure(db, user)
    return {"ok": False, "reason": "mismatch",
            "left_seconds": lockout.lock_left_seconds(user),
            "locked": locked}
