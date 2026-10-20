"""口令散列与 JWT。

演示版使用 PBKDF2-HMAC-SHA256（标准库实现，无三方依赖）。
生产环境可整体替换为 argon2 / bcrypt，只需替换 verify_password / hash_password。
"""
import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings

_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def token_minutes_for(is_admin: bool) -> int:
    """这个身份该发多长的令牌。

    管理员令牌更短（审计 P3-04 建议 3）：管理员能开号、能看全量操作日志，
    令牌一旦泄露，暴露的权限面比普通会计大得多，所以窗口就该更短。

    取**较小者**而不是直接用管理员值 —— 有人把全局配成 30 分钟时，
    管理员这一档不该反过来把有效期拉长。
    """
    if not is_admin:
        return settings.access_token_minutes
    return min(settings.access_token_minutes, settings.admin_token_minutes)


def create_access_token(user_id: int, username: str, is_admin: bool = False,
                        token_version: int = 0,
                        mfa_at: int | None = None) -> tuple[str, int]:
    """签发访问令牌。

    ``token_version`` 写进载荷（``tv``），由 :func:`app.core.deps.get_current_user`
    与库里的当前值比对。这是「重置口令 / 停用账号之后旧令牌立刻失效」的全部机制
    （审计 P2-01）—— 没有它，口令改了也已经发出去的令牌照样能用满有效期。

    默认值 0 是为了让不关心这件事的调用方不用改签名；真正的签发入口
    （``/api/auth/login``）一定会把库里的值传进来。

    ``mfa_at``：**只在真的过了邮箱二次验证时传**（epoch 秒）。

    传了就多签两个字段：

    * ``amr`` —— 认证方法（``"pwd+mfa"``）。``amr`` 是 OAuth 语义里的
      「这次会话是怎么来的」，用它就能一眼看出「这个会话过了二次验证」。
    * ``mfa_at`` —— 二次验证通过的时刻。高危动作的「近期重新认证」
      （:func:`app.core.deps.require_recent_admin_mfa`）就是拿它跟
      ``admin_mfa_grace_minutes`` 比。

    **不传时载荷与改动前逐字一致**（一个字段都不多不少）—— 这样普通用户
    的登录路径、已签发的老令牌、以及所有断言载荷形状的回归测试都不受影响。
    """
    expires = datetime.now(timezone.utc) + timedelta(minutes=token_minutes_for(is_admin))
    payload = {"sub": str(user_id), "username": username,
               "exp": expires, "tv": int(token_version or 0)}
    if mfa_at is not None:
        payload["amr"] = "pwd+mfa"
        payload["mfa_at"] = int(mfa_at)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), int(
        expires.timestamp()
    )


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
