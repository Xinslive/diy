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


def create_access_token(user_id: int, username: str, is_admin: bool = False) -> tuple[str, int]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=token_minutes_for(is_admin))
    payload = {"sub": str(user_id), "username": username, "exp": expires}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), int(
        expires.timestamp()
    )


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
