"""口令散列与 JWT。

演示版使用 PBKDF2-HMAC-SHA256（标准库实现，无三方依赖）。
生产环境可整体替换为 argon2 / bcrypt，只需替换 verify_password / hash_password。
"""
import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from app.core.config import settings

_ITERATIONS = 240_000

# --------------------------------------------------------------------- 密码策略
#
# **口径只收在这里**（审计 P2-1）。建号、重置与**本人自助改密**三条路径
# 共用下面这几个值 —— 过去是三套不同的下限（建号 6 位、重置**没有**下限、
# 自助注册 8 位），而最危险的那条（管理员重置）恰恰是唯一一条没有下限的：
# 把账号密码重置成 1 个字符，系统照收，而且那个密码**立即可用**。
#
# 放在 core 而不是 api 里，是因为它**不是某个接口的规矩，是这套系统对
# 「一个可用的密码」的定义**：管理员建号、自助注册、本人改密问的是同一个问题。
# 各写一份的话，改下限那天一定会漏掉一处 —— 而漏掉的那一处恰好是最危险
# 的那条路径。
#
# **下限 6 位**（用户 2026-10-22 定的口径）：自助注册与管理员建号**用同一把
# 尺**，不再「注册比建号更严」。8 位那条是 2026-10-20 审计后加的，理由是
# 「离线爆破一个 6 位密码是常规操作」—— 那条理由仍然成立，所以真正兜底的
# 是 **PBKDF2 的成本与登录失败锁定**，不是复杂度规则。
#
# **复杂度下限已撤（用户 2026-10-23 定的口径）：全部数字也认。**
# 原来还要求「至少 2 类字符」（大写 / 小写 / 数字 / 符号），理由是
# 「单类字符看着长，字典与规则化爆破一撞就开」。现在撤掉，理由是：
#   * 真正兜底的是**登录失败锁定**与 PBKDF2 的成本，不是字符的花样；
#   * 要求「两类」拦下的是**随手敲出来的数字串**（生日、手机号后六位），
#     而这类数字串本来就该被允许 —— 逼人为了凑花样写一个自己记不住的密码，
#     净损失（记不住的密码最后都写在便利贴上）。
# **下限（位数）留着**：它才是那条真正在拦「1 个字符」的规则。
#
# **存量用户不受影响**：密码策略**只在真的要设新密码时**校验。改姓名、改
# 状态、走一遍「改别的字段」都**不**触发它 —— 否则历史密码只有 6 位的用户
# 在管理员想给他改个电话时会被卡住，而「改别的字段也要重设密码」正是把
# 存量用户逼着改密码的最坏理由。
PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 128


def password_policy_error(password: str) -> Optional[str]:
    """密码不合策略时返回一句**给用户看**的话；合规返回 ``None``。

    建号、重置与本人自助改密共用这一个判断 —— 别两处各写一遍（写两遍就会
    改一处漏一处，而「最危险的那条没有下限」正是这么来的）。

    过去的文案是「给管理员看」的（只有管理员能设别人的密码）；现在本人也能
    改自己的密码了，所以这句话是给**使用者**看的 —— 同一个判断，两种读者。
    """
    if password is None:
        return None
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"密码至少 {PASSWORD_MIN_LENGTH} 位"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"密码最长 {PASSWORD_MAX_LENGTH} 位"
    return None


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
