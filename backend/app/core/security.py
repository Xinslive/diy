"""口令散列与 JWT。

演示版使用 PBKDF2-HMAC-SHA256（标准库实现，无三方依赖）。
生产环境可整体替换为 argon2 / bcrypt，只需替换 verify_password / hash_password。

**口令散列的成本是「可升级」的**：散列串里记着算出它时用的迭代数，
验证一律按**库里记着的那个数**算（所以老口令永远登得进去），而当库里
那个数低于当前值时，验证成功之后会用当前值重算一遍并写回
（见 :func:`password_needs_rehash`）。这样提高成本不需要停机、不需要
强制所有人改密码，也不会把谁锁在门外。
"""
import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import jwt

from app.core.config import settings

#: PBKDF2-HMAC-SHA256 的迭代轮数。
#:
#: **600,000** 是 OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议值（原来的 240,000
#: 低于这条线，差额 2.5 倍）。本机实测：240k 约 22ms、600k 约 52ms ——
#: 一次登录多花 30ms，换来的是离线爆破成本翻 2.5 倍。
#:
#: **提高它不会把存量用户挡在门外**：散列串里带着算出它时的轮数
#: （``pbkdf2_sha256$<iters>$<salt>$<dk>``），验证时按库里那个数算；
#: 老散列登录成功之后会被 :func:`password_needs_rehash` 判为「该升级」，
#: 由登录路径重算并写回（见 ``api/auth.py``）。于是成本是**随登录自动爬升**的，
#: 而不是一次性的「全部作废重设」。
_ITERATIONS = 600_000

#: 散列串里允许出现的最大轮数。
#:
#: 库里的散列是**我们自己写的**，但万一这一列被改坏（手工改库、导入脏数据），
#: 一个离谱大的轮数会让每一次验证都变成一次几十分钟的 CPU 占用 ——
#: 那是一个不需要任何凭据就能触发的 DoS。所以解析时给它一个上限，
#: 越界的一律当「散列不合法」（验证直接失败，不去算）。
_MAX_ITERATIONS = 10_000_000

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


def _parse_stored(stored: str) -> Optional[tuple[int, bytes, bytes]]:
    """拆开库里那串散列：``(轮数, 盐, 摘要)``。**不合法时返回 None。**

    轮数必须落在 ``1.._MAX_ITERATIONS`` 之间：散列是我们自己写的，正常情况下
    必然满足，但**万一这一列被改坏**（手工改库、导入了脏数据），一个离谱大的
    轮数会让每一次验证都变成一次几十分钟的 CPU 占用 —— 那是一个不需要任何
    凭据就能触发的拒绝服务。越界的一律当「散列不合法」，验证直接失败。
    """
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return None
        count = int(iters)
        if count < 1 or count > _MAX_ITERATIONS:
            return None
        salt = base64.b64decode(salt_b64)
        dk = base64.b64decode(dk_b64)
        if not salt or not dk:
            return None
        return count, salt, dk
    except Exception:
        return None


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """验证口令。

    **按库里记着的轮数算**，不是按当前值 —— 于是老散列（24 万轮）依然登得进去，
    升级轮数不会把任何存量用户挡在门外。轮数升级由
    :func:`password_needs_rehash` + 登录路径上的重算写回来完成。
    """
    parsed = _parse_stored(stored or "")
    if parsed is None:
        return False
    iters, salt, dk = parsed
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
    return hmac.compare_digest(calc, dk)


def password_needs_rehash(stored: str) -> bool:
    """这个散列是不是该用**当前**轮数重算一遍。

    只在**口令已经验证通过**之后才该调用它 —— 判据是「轮数低于当前值」，
    不是「口令对不对」。散列本身不合法时返回 False（那种散列验证阶段就已经
    失败了，重算没有意义）。
    """
    parsed = _parse_stored(stored or "")
    if parsed is None:
        return False
    return parsed[0] < _ITERATIONS


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

    **另外三个字段每个令牌都带**（审查 M-11）：

    * ``iat`` —— 签发时刻（epoch 秒）。之前载荷里只有 ``exp``，于是「这个令牌
      是什么时候签的」只能靠 ``exp`` 倒推。
    * ``jti`` —— 这一枚令牌的唯一标识（``uuid4().hex``）。它是「**只吊销这一枚
      令牌**」的粒度：同一个账号的两台设备拿到的令牌 jti 不同，将来要做
      单枚吊销（登出某一台、改密后只踢掉泄露的那一枚）时就有地方可指了。
      **它不替代 ``tv``**：``tv`` 是账号级的、立刻生效的全量撤销（改密/停用），
      仍然是主机制，``jti`` 是它之上的**更细的粒度**，两者都在。
    * ``iss`` —— 签发方（``settings.jwt_issuer``）。**解码时必须校验**：没有它，
      同一套 ``JWT_SECRET`` 为别的服务签发的令牌也能被本服务接受（只要 ``sub``
      指向某个存在的用户）—— 跨系统复用密钥是常见部署形态，那会变成一条越权路径。
    """
    now = int(time.time())
    expires = datetime.now(timezone.utc) + timedelta(minutes=token_minutes_for(is_admin))
    payload = {"sub": str(user_id), "username": username,
               "iat": now, "jti": uuid4().hex, "iss": settings.jwt_issuer,
               "exp": expires, "tv": int(token_version or 0)}
    if mfa_at is not None:
        payload["amr"] = "pwd+mfa"
        payload["mfa_at"] = int(mfa_at)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), int(
        expires.timestamp()
    )


def decode_access_token(token: str) -> dict:
    """解码并校验访问令牌。**``iss`` 必须对得上。**

    **存量令牌（没有 ``iss`` 的那些）一律拒绝。** 这是刻意的 fail-closed：
    拒绝的代价是「升级后最长 12 小时的窗口内，已签发的令牌要重新登录一次」；
    放行的代价是「同一个密钥下别的服务签的令牌在本服务照样能用」。前者是
    一次可预期的重新登录，后者是一条**跨系统的越权路径** —— 后者更贵。

    而且这个窗口本来就不长：令牌最长 12 小时（管理员 4 小时），
    而**改口令 / 停用账号会立刻作废它**（``tv`` 与库里的 ``token_version``
    比对），也就是说「有人拿着旧令牌」这件事最多持续到下一次改密为止。

    **``aud`` 故意不加**（暂时也不校验）：本服务只有一个受众 —— 它自己的 API，
    令牌从来不会发给第三方。加一个永远等于同一个值的字段只会多一处要同步的
    口径。**将来真的有了第二个受众（网关、第三方集成）再加，那时必须
    同时改签发与校验两侧**：PyJWT 的默认行为是「载荷里有 ``aud`` 而解码时
    没给 ``audience`` ⇒ 直接判非法」，只改签发侧会把自己刚签的令牌也拒掉。
    """
    return jwt.decode(token, settings.jwt_secret,
                      algorithms=[settings.jwt_algorithm],
                      issuer=settings.jwt_issuer)
