"""MCP 令牌鉴权：签发、校验、吊销、轮换，以及账套可见性的解析。

**两条已拍板的语义**（用户 2026-10-21 定）：

1. **令牌绑定一个真实用户；该用户能访问哪些账套，用这个令牌的 agent 就
   只能访问哪些账套。** 令牌**不携带自己的账套范围** —— 可见性 100%
   来自 ``sys_user_book``。
2. **令牌永不过期。** 没有到期时间，不会因为时间自动失效。

合起来的效果是：**令牌 = 绑定用户权限的只读镜像**。用户侧的任何权限变化
（改角色、撤账套授权、停用账号），agent 侧**立刻同步** —— 因为每次调用
都重新查 ``UserBook`` 与 ``sys_user``，**不做跨请求缓存**（与
``core/deps.py`` 的 ``_link_or_403`` 同一个原则）。

**凭据形态。**
明文 ``mcp_<token_urlsafe(32)>``，库里只存 ``sha256(明文)``。前缀只用于
识别与防呆（避免把登录 JWT 误当令牌），**摘要对完整字符串计算**。

用 SHA-256 而不是 PBKDF2：口令是用户自选的、低熵，必须用慢 KDF 拖慢离线
爆破；令牌是 256 位随机生成的，没有字典可撞，快散列正是正确选择。本仓对
这两类凭据已经区分处理（``core/security.py`` vs ``scheduler/service.py``），
MCP 令牌归入后者。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Book, McpToken, SysUser, UserBook

from app.mcp.errors import McpAuthError, McpForbiddenError, McpRateLimited

#: 明文令牌前缀。只用于识别与防呆 —— 避免把登录 JWT 当成 MCP 令牌传进来
#: （那种情况会一路查库查不到，然后回一个「令牌无效」，排查方向全错）。
TOKEN_PREFIX = "mcp_"

#: 未授权时统一回这句。**不区分「不存在」「已吊销」「已过宽限期」「用户被
#: 停用」** —— 能区分的接口就是令牌状态的探测器。
UNAUTHORIZED_MESSAGE = "MCP 令牌无效或已失效"


def generate_token() -> str:
    """生成一个新的明文令牌。"""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_digest(value: str) -> str:
    """明文 → 库里存的那一串（``sha256`` 十六进制）。

    与 ``scheduler/service.py::secret_digest`` **同一套做法**。
    """
    return hashlib.sha256(value.encode()).hexdigest()


def digest_matches(value: str, expected: str) -> bool:
    """常数时间比对。"""
    return hmac.compare_digest(token_digest(value), expected or "")


def now_utc() -> datetime:
    """当前时间（带时区）。

    与库里的 ``DateTime(timezone=True)`` 对齐 —— 本项目的连接池把会话
    时区钉死成 UTC，所以从库里读回来的时间戳都是 UTC 的 aware 值。
    """
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- 限流

#: 每小时调用次数的计数状态。键是**用户 id**，值是 ``(窗口起点, 次数)``。
#:
#: **为什么用内存而不是数据库。**
#: 令牌**永不过期** ⇒ 泄漏窗口是无限的。吊销是唯一的事后手段，而限流是
#: 唯一能压住「被拿去批量导出数据」这一形态的**事前**手段 —— 它要在每次
#: 调用前给出判断，所以必须便宜。
#:
#: **多进程下它是每进程一份**（生产 2 worker），所以实际生效的上限是
#: ``mcp_max_calls_per_hour × worker 数``。这是有意的取舍：为了一个近似
#: 精确的计数而给每次调用加一次写放大（数据库计数要么加一列要么插一行），
#: 代价更大；而限流本身是**兜底**，不是精确配额 —— 真要看「谁在用、查了
#: 什么」，靠的是**读取审计**（``app/mcp/audit.py``），那是落库的、跨进程
#: 完整的。
#:
#: **后续项（DB 版）**：把这个字典换成一张计数表（或 Redis），就能把
#: 「× worker 数」这个放大系数去掉，也能让「换一枚令牌」不再换桶。
#: 那是较大的改动（每次调用一次写放大），这里先做前一半。
#:
#: **为什么键是用户而不是令牌（审查 L-26）。** 以前键是 ``token_id``，
#: 于是「换一枚令牌」就换一个桶：同一个用户发 10 枚令牌就能拿到 10 倍的
#: 配额，而令牌**永不过期**、泄漏窗口无限 —— 那等于限流形同虚设。键改成
#: 用户之后，**换令牌不换桶**：一个人一共只能用一个桶。
_RATE_STATE: dict[tuple[str, int], tuple[datetime, int]] = {}
_RATE_LOCK = threading.Lock()


def _token_owner_id(token_id: int) -> Optional[int]:
    """令牌 → 所属用户。计数按用户走，所以这里要把令牌换成用户。

    **一次主键查询**（限流本来就要每次调用前判一次，多这一次索引查找
    不改变量级）。查不到时返回 None —— 调用方退回按令牌计数，不因为
    「查不到归属」就放行。
    """
    from app.core.db import SessionLocal
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with SessionLocal() as db:
            return db.scalar(select(McpToken.user_id).where(McpToken.id == token_id))
    except SQLAlchemyError:
        return None


def check_rate_limit(token_id: int, limit: Optional[int] = None) -> None:
    """每小时调用次数上限。超了抛 ``McpRateLimited``。

    **计数键是用户**（同一个用户的所有令牌共用一个桶），见 ``_RATE_STATE``
    上面的说明。拿不到令牌的归属时退回按令牌计数（更保守，不会误杀）。

    窗口按**自然小时**对齐（``now`` 截断到整点）。固定窗口比滑动窗口便宜
    得多，而这里要的只是「一小时里别超过 N 次」这个量级的判断。
    """
    limit = settings.mcp_max_calls_per_hour if limit is None else limit
    if limit <= 0:
        return
    now = now_utc()
    window = now.replace(minute=0, second=0, microsecond=0)
    owner = _token_owner_id(token_id)
    key = ("user", owner) if owner is not None else ("token", token_id)
    with _RATE_LOCK:
        start, count = _RATE_STATE.get(key, (window, 0))
        if start != window:
            start, count = window, 0
        count += 1
        _RATE_STATE[key] = (window, count)
        # 顺手清掉上一小时及更早的条目 —— 不清的话，这个字典会随
        # 「签发过的令牌数」只增不减。
        stale = [k for k, (started, _c) in _RATE_STATE.items()
                 if started < window]
        for stale_key in stale:
            _RATE_STATE.pop(stale_key, None)
    if count > limit:
        raise McpRateLimited(
            f"该令牌本小时的调用次数已超过上限（{limit} 次），请稍后再试")


def reset_rate_limits() -> None:
    """清空计数（测试用）。"""
    with _RATE_LOCK:
        _RATE_STATE.clear()


# --------------------------------------------------------------------- 主体

@dataclass(frozen=True)
class McpPrincipal:
    """一次调用里的「谁」。"""

    user_id: int
    username: str
    token_id: int
    token_name: str

    def describe(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "token_id": self.token_id,
            "token_name": self.token_name,
        }


# --------------------------------------------------------------------- 校验

def extract_bearer(headers) -> Optional[str]:
    """从请求头里取出 ``Authorization: Bearer mcp_...`` 的令牌部分。"""
    raw = ""
    try:
        raw = headers.get("authorization") or headers.get("Authorization") or ""
    except AttributeError:  # pragma: no cover - 防御：传进来的不是映射
        return None
    raw = raw.strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered.startswith("bearer "):
        raw = raw[7:].strip()
    if not raw:
        return None
    return raw


def verify_token(db: Session, raw: str) -> McpPrincipal:
    """校验一个明文令牌，返回调用主体。

    校验链（**每次调用都查库**，不做跨请求缓存）：

    1. 取出明文 → ``sha256`` → 按 ``token_hash`` 查 ``mcp_token``；
    2. 命中且 ``enabled`` 且（``revoked_at IS NULL`` 或 ``grace_until > now``）
       ⇒ 通过；否则 401；
    3. 取 ``user_id`` → 查 ``SysUser``：不存在或 ``active=False`` ⇒ 401。
       **用户停用立即作废其全部令牌** —— 这是「永不过期」前提下最不能少
       的一条；
    4. 顺手更新 ``last_used_at``（管理面靠它算 ``unused_days``）。

    第 3 条是「永不过期」这个产品决定的**必要补偿**：令牌自己没有到期
    时间，那么「人走了、账号停了」这件事就必须由用户状态来表达。
    """
    if not raw or not raw.startswith(TOKEN_PREFIX):
        raise McpAuthError(UNAUTHORIZED_MESSAGE)

    digest = token_digest(raw)
    row = db.scalar(select(McpToken).where(McpToken.token_hash == digest))
    if row is None:
        raise McpAuthError(UNAUTHORIZED_MESSAGE)

    if not row.enabled:
        raise McpAuthError(UNAUTHORIZED_MESSAGE)

    # 吊销后是否还在宽限期内。
    if row.revoked_at is not None:
        if row.grace_until is None or row.grace_until <= now_utc():
            raise McpAuthError(UNAUTHORIZED_MESSAGE)

    user = db.get(SysUser, row.user_id)
    if user is None or not user.active:
        raise McpAuthError(UNAUTHORIZED_MESSAGE)

    row.last_used_at = now_utc()
    db.flush()

    return McpPrincipal(
        user_id=user.id,
        username=user.username,
        token_id=row.id,
        token_name=row.name,
    )


# --------------------------------------------------------------------- 账套

def require_book_access(db: Session, principal: McpPrincipal, book_id: int) -> UserBook:
    """这个主体能不能访问这个账套。

    **与业务 API 完全同一套判定**：查 ``UserBook(user_id, book_id)``，没有
    授权 ⇒ 403，文案与 ``core/deps.py`` 的 ``_link_or_403`` **逐字一致**
    （「没有该账套的访问权限」）。

    为什么可以复用同一套：本仓的权限事实只有一处 —— ``sys_user_book``。
    MCP 不另立一套「令牌账套范围」（那会是同一口径两份实现）。

    **每次调用都查**：不做跨请求缓存 —— 用户被撤授权后，agent 下一次调用
    就必须被挡住，不重启、不换令牌。
    """
    link = db.scalar(
        select(UserBook).where(
            UserBook.user_id == principal.user_id,
            UserBook.book_id == book_id,
        )
    )
    if link is None:
        raise McpForbiddenError("没有该账套的访问权限")
    return link


def require_book(db: Session, principal: McpPrincipal, book_id: int) -> Book:
    """账套授权 + 账套本身存在。顺序与 ``core/deps.get_my_book`` 一致。"""
    require_book_access(db, principal, book_id)
    book = db.get(Book, book_id)
    if book is None:
        from app.mcp.errors import McpNotFoundError

        raise McpNotFoundError("账套不存在")
    return book


def accessible_books(db: Session, principal: McpPrincipal) -> list[tuple[Book, str]]:
    """这个主体能访问的账套 + 各自角色。

    签发响应与管理面列表都回带它 —— 绑定一个 OWNER 的令牌意味着 agent
    能读该用户**所有**账套，签发人必须当场知情。
    """
    return _book_rows(db, principal.user_id)


def _book_rows(db: Session, user_id: int) -> list[tuple[Book, str]]:
    """账套清单的**唯一一条查询**。

    可见性 100% 来自 ``sys_user_book``（见本模块开头的说明），所以
    「这个人能看哪些账套」这个问题只允许有一个查询实现 ——
    令牌签发时回给管理员看的那份、个人资料页回给本人看的那份、
    MCP 工具运行时判权限用的那一份，必须是同一套口径。
    """
    rows = db.execute(
        select(Book, UserBook.role)
        .join(UserBook, UserBook.book_id == Book.id)
        .where(UserBook.user_id == user_id)
        .order_by(UserBook.sort_order.asc().nulls_last(), Book.company_name, Book.name)
    ).all()
    return [(book, role) for book, role in rows]


def books_of_user(db: Session, user_id: int) -> list[dict]:
    """某个用户能访问的账套 + 各自角色（给界面看的形状）。

    与 :func:`accessible_books` **同一条查询**（见 :func:`_book_rows`）——
    同一口径两份实现迟早对不上，而对不上的那一次是「界面说能看、
    接口说不能」这种最难查的形态。
    """
    return [
        {
            "id": book.id,
            "name": book.name,
            "company_name": book.company_name,
            "role": role,
        }
        for book, role in _book_rows(db, user_id)
    ]


def token_state(row: McpToken) -> dict:
    """这个令牌此刻的状态。

    **界面上要能分开看的三件事**，混成一句「已失效」就没法处置了：

    * ``status == "grace"`` —— 轮换后的**宽限期内**：旧令牌**仍然可用**，
      到期自动失效。把它显示成「已吊销」会让人以为连接已经断了；
    * ``status == "revoked"`` —— 已吊销，下一次调用就 401；
    * ``status == "disabled"`` —— 软开关关着（记录与审计都还在）；
    * ``status == "active"`` —— 能用。

    ``unused_days`` 从来没用过是 ``None``（区别于「用过但很久没用」），
    管理面靠它一眼认出僵尸令牌。
    """
    now = now_utc()
    grace_until = row.grace_until
    in_grace = bool(grace_until is not None and row.revoked_at is not None
                    and grace_until > now)
    if in_grace:
        status = "grace"
    elif row.revoked_at is not None:
        status = "revoked"
    elif not row.enabled:
        status = "disabled"
    else:
        status = "active"
    return {
        "enabled": bool(row.enabled),
        "in_grace": in_grace,
        "status": status,
        "revoked_at": row.revoked_at,
        "grace_until": grace_until,
        "last_used_at": row.last_used_at,
        "unused_days": unused_days(row),
    }


def active_token_count(db: Session, user_id: int) -> int:
    """这个人现在有几枚**还能用**的令牌。

    **宽限期内的那枚算「还能用」** —— 它到宽限期结束才失效，在那之前
    agent 照样能用它调接口。把它漏掉，于是「上限 5 枚」会被宽限期里的
    旧令牌绕过。

    用来给自助签发设上限：令牌**永不过期**，于是「能铸出多少枚永久
    凭据」这件事必须有界 —— 否则一枚被偷走的会话令牌能铸出无穷多枚
    永久读凭据，而审计里只会留下一堆看不出所以然的记录。
    """
    from sqlalchemy import func, or_

    now = now_utc()
    return db.scalar(
        select(func.count()).select_from(McpToken).where(
            McpToken.user_id == user_id,
            McpToken.enabled.is_(True),
            or_(McpToken.revoked_at.is_(None), McpToken.grace_until > now),
        )
    ) or 0


# --------------------------------------------------------------------- 生命周期

def issue_token(db: Session, *, user: SysUser, name: str,
                created_by: str) -> tuple[McpToken, str]:
    """签发一个新令牌，返回 ``(行, 明文)``。

    **明文只在这里出现一次** —— 调用方负责把它回给管理员，之后库里只剩
    摘要，谁都拿不回明文。
    """
    plain = generate_token()
    row = McpToken(
        name=name,
        token_hash=token_digest(plain),
        user_id=user.id,
        enabled=True,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return row, plain


def rotate_token(db: Session, row: McpToken, *,
                 created_by: str) -> tuple[McpToken, str]:
    """轮换一个令牌：签发新令牌，旧令牌进入宽限期。

    旧令牌在**宽限期内仍然可用**，到期自动失效（由 ``verify_token`` 比较
    ``grace_until`` 决定，不需要定时任务去清理）。

    这与 ``scheduler/service.py`` 的 ``rotate_trigger_secret`` 是同一个做法：
    保留 previous 密钥一段时间，避免调用方（这里是外部 agent）换密钥时
    断线。
    """
    grace_days = max(0, int(settings.mcp_token_rotate_grace_days))
    row.revoked_at = now_utc()
    row.grace_until = now_utc() + timedelta(days=grace_days)
    row.enabled = True
    db.flush()
    return issue_token(db, user=db.get(SysUser, row.user_id), name=row.name,
                       created_by=created_by)


def revoke_token(db: Session, row: McpToken) -> None:
    """**立即吊销**：下一次调用就失效。

    ``grace_until`` 清空 —— 宽限期是「轮换」的语义，立即吊销不给宽限。
    """
    row.revoked_at = now_utc()
    row.grace_until = None
    row.enabled = False
    db.flush()


def revoke_user_tokens(db: Session, user_id: int) -> int:
    """批量吊销某个用户的全部令牌。返回吊销了几条。

    改密、离职、疑似泄漏时的标准处置。

    **这是管理面那一侧的语义**：吊销 = 标记失效，**记录留着**
    （谁在什么时候吊的，审计里查得到，而且轮换要用到「已吊销但还在宽限期」
    这个状态）。

    本人自助那一侧走的是**物理删除**（:func:`delete_user_tokens`）——
    两个动词、两种语义，不要混用。
    """
    rows = list(db.scalars(
        select(McpToken).where(McpToken.user_id == user_id)
    ).all())
    stamp = now_utc()
    for row in rows:
        row.revoked_at = stamp
        row.grace_until = None
        row.enabled = False
    db.flush()
    return len(rows)


def delete_token(db: Session, row: McpToken) -> None:
    """**物理删除**一枚令牌 —— 这一行从此不存在。

    **与「吊销」的区别**：吊销是**标记失效**（记录留着，审计查得到，
    轮换时旧令牌要靠它进宽限期）；删除是**连记录一起去掉**。

    **删了会怎样**：这枚令牌下一次调用立刻 401（校验链是「按摘要查库」，
    查不到就是查不到）。而**已经发生过的读取痕迹仍在** ——
    ``app/mcp/audit.py`` 的 ``mcp.read`` 审计记的是 ``user_id`` 与
    ``token_id``（字符串列，不随这一行消失），所以「谁读过账」查得到，
    丢的只是「这枚密钥曾经存在、现在被删了」这条管理痕迹。

    **本人自助这一侧就是它**：MCP 密钥是本人的私人凭据，怎么处置是本人的
    事，不留审计痕迹。
    """
    db.delete(row)
    db.flush()


def delete_user_tokens(db: Session, user_id: int) -> int:
    """**物理删除**某个用户的全部令牌。返回删了几条。

    本人自助的「全部删除」走这里。**任何状态的行都删** —— 包括管理员
    吊销过的那几枚：那一行对本人已经没有用处（管理员的吊销动作本身留在
    管理员那条审计里）。
    """
    rows = list(db.scalars(
        select(McpToken).where(McpToken.user_id == user_id)
    ).all())
    for row in rows:
        db.delete(row)
    db.flush()
    return len(rows)


def unused_days(row: McpToken) -> Optional[int]:
    """这个令牌多久没被用了。

    从来没用过 ⇒ ``None``（区别于「用过但很久没用」）。
    """
    if row.last_used_at is None:
        return None
    last = row.last_used_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0, (now_utc() - last).days)


__all__ = [
    "McpPrincipal", "TOKEN_PREFIX", "UNAUTHORIZED_MESSAGE", "accessible_books",
    "active_token_count", "books_of_user", "check_rate_limit", "delete_token",
    "delete_user_tokens", "digest_matches", "extract_bearer", "generate_token",
    "issue_token", "now_utc", "require_book", "require_book_access",
    "reset_rate_limits", "revoke_token", "revoke_user_tokens", "rotate_token",
    "token_digest", "token_state", "unused_days", "verify_token",
]
