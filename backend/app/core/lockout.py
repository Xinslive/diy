"""登录锁定的时长口径。

**为什么单独一个模块。**

锁定时长这个数要被**两处**用到：登录路径（``api/auth.py``）决定这次锁
多久，管理员后台（``api/system.py``）决定「这个账号现在锁着没有、还剩
多久、还能不解封」。两处各算一次，迟早会对不上 —— 而对不上的后果是
「界面说没锁，登录却回 423」或者反过来，两头都在说谎。

它也不能放在 ``api/auth.py`` 里让 ``api/system.py`` 去 import：那是一条
**api→api 的横向依赖**（``auth.py`` 自己的注释里就写过为什么不这么干
——「横向依赖会让两个模块绑在一起，改一个就得看另一个」）。所以口径
放在 core 这一层，两边都往下依赖它，依赖方向是单向的。

**时间的口径必须处处一致。**

库里 ``locked_until`` 是 **naive UTC**（与 ``sys_user`` 这一列的类型
一致），而「现在」若用本地时间去比，东八区部署时锁定会凭空差 8 小时
（把本地时间当 UTC 比，偏 8 小时；反向偏回来则是 -8）。所以这里统一
走 :func:`_utcnow`，与 ``auth.py`` 一直用的那一个同源。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.config import settings


def utcnow() -> datetime:
    """当前 UTC 时间，naive。

    与库里 ``locked_until`` 同一口径（naive UTC）。混用 aware/naive 会
    让比较直接抛 TypeError，而那正是「东八区部署时锁定时长变成 8 小时」
    这类问题的温床。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def lock_duration_minutes(level: int) -> int:
    """第 ``level`` 轮连续锁定要锁多久（分钟）。

    口径：**第 N 轮 = 基准时长 × N**，上限 ``login_lock_max_minutes``。
    默认基准 10 分钟、上限一天，于是 10 → 20 → 30 → … → 1440。

    为什么是「按轮次」而不是「按失败次数」：失败次数是 ``failed_logins``，
    它在触发锁定时就被清零了（那是「离下次锁定还差几次」的语义），拿它
    算时长会在最需要知道的那刻失真。轮次是 ``lock_level``，只增不减，
    只在登录成功或管理员解封时清零。

    **上限不是可选项。** 没有上限的递进就是「永久锁定」：攻击者只要持续
    触发锁定，就能让一个账号再也登不进去，而没有人能证明是谁干的。
    上限让它停在「很久但有限」，管理员始终有介入的窗口。
    """
    level = max(1, int(level or 1))
    base = max(1, int(settings.login_lock_minutes or 1))
    cap = max(base, int(settings.login_lock_max_minutes or base))
    return min(base * level, cap)


def lock_left_seconds(user: Any) -> int:
    """还锁着几秒。没锁返回 0。

    ``user`` 只需要有 ``locked_until`` 与 ``lock_level`` 两个属性 ——
    这里刻意不 import 模型，免得 core 反向依赖 models。
    """
    locked_until = getattr(user, "locked_until", None)
    if locked_until is None:
        return 0
    return max(0, int((locked_until - utcnow()).total_seconds()))


def is_locked(user: Any) -> bool:
    """这个账号现在是不是锁着。"""
    return lock_left_seconds(user) > 0


def lock_summary(user: Any) -> dict:
    """给管理界面看的锁定状态。

    一次算全，界面直接用 —— 免得前端自己拿 ``locked_until`` 去和浏览器
    时钟比（那又是一次口径分裂，而且用的是**浏览器**时区）。
    """
    left = lock_left_seconds(user)
    return {
        "locked": left > 0,
        "locked_left_seconds": left,
        "locked_until": (getattr(user, "locked_until", None).isoformat()
                         if getattr(user, "locked_until", None) else None),
        "lock_level": int(getattr(user, "lock_level", 0) or 0),
        "failed_logins": int(getattr(user, "failed_logins", 0) or 0),
    }


# ---------------------------------------------------------------------------
# 来源分桶（审计 P2-3）
# ---------------------------------------------------------------------------
#
# **这一层只回答一个问题：一次登录失败算在哪个桶里。**
#
# 现状（``api/auth.py::_throttle_ip``）：对端不在 ``TRUSTED_PROXIES`` 白名单
# 里就只认 TCP 对端（``request.client.host``）；在白名单里才读代理头
# （``X-Real-IP`` 优先，其次 ``X-Forwarded-For`` 的**最右**一个）。
#
# ## 「拿不到真实客户端 IP」时怎么办 —— 这是本模块要显式化的那件事
#
# 代码是 **fail-closed** 的：配了白名单才信代理头，没配就一个代理都不信。
# 于是「nginx → 127.0.0.1:8000」这种部署形态下**全站共用一个桶** ——
# 任何人连续输错 30 次，全公司一起进 429 窗口（``login_ip_max_failures``）。
#
# **别把这件事「优化」掉。** 改成「无条件信任 X-Forwarded-For」看着更准，
# 实际上是把限流的钥匙交给攻击者（每次请求换一个头就能换一个桶，IP 维度
# 限流等于不存在）。**拿不到真实 IP 时保守地视为「不可区分」是对的** ——
# 那是现状，也是现状之所以没被改掉的原因。
#
# 真正要修的不是这个判断，而是**它不可见**：没人知道当前部署里「所有请求
# 都算作同一个来源」。所以下面把分桶口径做成**一个可查询的状态**
# （:func:`source_bucketing`），由 ``GET /api/system/login-protection``
# 显式地告诉管理员，并给出改法。
#
# **分桶键的算法收在这里**（:func:`source_bucket_key`）——「哪个来源算同一
# 个桶」只有一份实现，别在别处再写一遍。

#: 分桶键的前缀。带前缀是为了让「按 IP 分的桶」与「按对端分的桶」在
#: 存储层里**永远不会撞上** —— 它们的粒度不同，混在一张表里会让「某个
#: 键的失败数」含义不明。
BUCKET_PREFIX_IP = "ip"
BUCKET_PREFIX_PEER = "peer"


def source_bucket_key(peer: str, resolved: str | None = None, *,
                     trusted: bool = False) -> str:
    """这次请求算在哪个桶里。

    * ``resolved`` 有值**且**对端是可信代理 ⇒ 按**客户端 IP** 分桶
      （``ip:<地址>``）：这才是「哪个客户端在扫」。
    * 其余情况 ⇒ 按 **TCP 对端** 分桶（``peer:<地址>``）：对端是攻击者
      唯一伪造不了的东西。**这包括「对端是可信代理、但代理没带
      X-Real-IP / X-Forwarded-For」** —— 那种情况下没有任何可证明的
      客户端地址，退回对端是**保守**的选择（可证明的粒度就是最粗的粒度）。

    **「可证明的粒度」是这个函数唯一的原则。** 宁可粗一点（多误伤），
    不可细一点（被伪造）。

    ⚠️ **这个键是给人看的诊断形态，不是 ``login_ip_throttle.ip`` 那一列的
    取值**：那一列是 ``String(45)``，存的是**原始地址**（最长 45 字符的
    IPv6），而这里的键带了前缀，最长会到 48 字符 —— 直接写进去会截断。
    真要把它接进 ``auth.py``，要么那一列换成 ``String(64)``，要么这里
    改成对超长地址取散列。**别把两处的长度口径搞混。**
    """
    peer = (peer or "").strip() or "-"
    if trusted:
        client = (resolved or "").strip()
        if client:
            return f"{BUCKET_PREFIX_IP}:{client[:45]}"
    return f"{BUCKET_PREFIX_PEER}:{peer[:45]}"


def source_bucketing() -> dict:
    """当前**按来源聚合**的分桶口径（可观测，不含任何凭据）。

    回答的是「现在防得住什么」：白名单为空 ⇒ 只按 TCP 对端分桶 ⇒ 反向代理
    后面全站共用一个桶（一次小规模爆破就会让所有人一起进 429 窗口）。
    """
    from app.core.config import trusted_proxy_networks

    raw = (getattr(settings, "trusted_proxies", "") or "").strip()
    try:
        networks = trusted_proxy_networks(raw)
    except Exception:       # noqa: BLE001 - 配置有问题不该让这个接口 500
        networks = ()
    shared = not networks
    return {
        "mode": "peer-only" if shared else "proxy-aware",
        "trusted_proxies": raw,
        "trusted_proxy_count": len(networks),
        # 「所有请求算作同一个来源」——这就是审计 P2-3 说的那个退化形态。
        "shared_bucket": shared,
        "shared_bucket_reason": (
            "TRUSTED_PROXIES 为空：一个代理都不信，于是所有请求都按 TCP 对端"
            "分桶。反向代理后面（nginx → 127.0.0.1:8000）全站共用一个桶 —— "
            "一次小规模爆破就会让所有人一起进 429 窗口。"
            if shared else ""
        ),
        "fix_hint": ("把 TRUSTED_PROXIES 设成反向代理的地址，例如 "
                     "TRUSTED_PROXIES=127.0.0.1（改完要重启后端）"
                     if shared else ""),
        # 白名单里配了、但代理没带来源头的请求会**退回按对端分桶** ——
        # 那是「可证明的粒度」原则的体现，不是漏配。
        "fallback": "peer-only",
    }


# ---------------------------------------------------------------------------
# 账号维度的**递增延迟**（审计 P2-4）
# ---------------------------------------------------------------------------
#
# ## 问题：账号硬锁可以被人当成**定向拒绝服务**用
#
# 知道用户名的人连错几次就把**别人的**账号挡在门外，而且时长随轮次递增
# （10 → 20 → … → 1440 分钟）。攻击成本极低（3 次错误口令），收益是让
# 特定人在整段时间里进不去 —— 月结期尤其致命。
#
# ## 正解：把「锁定」与「失败计数」**解耦**
#
# * 失败计数只回答「离下一次触发还差几次」；
# * 达到阈值时**不立刻锁死**，而是进入**递增延迟** —— 后续尝试要等 N 秒
#   （N 随轮次递增、封顶），而**正确口令仍然登得进去**；
# * 只有**延迟也扛不住**时才升级成硬锁（423），或者由管理员一键解封。
#
# ## 权衡（两个方向都要说清）
#
# * **降低 DoS 风险** ⇒ 受害者被挡的时间变短（甚至只是等几秒）；
# * **提高撞库成本** ⇒ 攻击者每轮能试的次数变少、速率变慢。
#
# 这两件事**天然对立**：同一个旋钮（延迟长度）往一边拧，另一边就松。
# 这里的取法是「**先短后长**」：第一轮只延迟几秒（**一次性锁人几乎不成立**
# —— 受害者等几秒就能继续干活），而**持续攻击**会逐轮把延迟拉长，
# 撞库的速率随之塌下去。换句话说：**一次性 DoS 变得不划算，持续撞库变得
# 越来越慢**。
#
# ## 为什么「先短后长」而不是「一直短」
#
# 一直短（比如永远 5 秒）的话，攻击者的猜测速率是 12 次/分钟 —— 那比
# 现在的「10 分钟一次」宽松得多，撞库成本反而**降**了。先短后长把
# 「一次性锁人」和「持续撞库」这两种形态**分别**压住：前者不成立，
# 后者的速率逐轮塌下去。
#
# ## 执行点在哪（重要）
#
# **本模块只提供口径与算术，执行点在 ``api/auth.py``** —— 那里才是
# 「一次登录尝试」的入口（延迟要在那里等/拒）。把「等几秒再验口令」写成
# 一句口号是没有用的，得真的在登录路径上等。
#
# ⚠️ **当前默认行为未改**（首轮仍是 10 分钟硬锁）。原因见本模块末尾
# 「为什么现在没接上」。

#: 第一轮的延迟（秒）。**刻意很短**：第一轮是「连错三次」就能触发的，
#: 那正是最容易被拿来当一次性 DoS 工具的形态。
ACCOUNT_DELAY_BASE_SECONDS = 5

#: 延迟的封顶（秒）。5 分钟 ≈ 与「现在的首轮硬锁」同量级 ——
#: 也就是说**持续攻击**最终仍然会被挡住，只是它得先付出一串连续的失败。
ACCOUNT_DELAY_MAX_SECONDS = 300

#: 超过这个轮次就**升级成硬锁**（423），不再只是延迟。
#:
#: 硬锁是留给「同一个账号被从多个来源同时打」的：延迟挡不住分布式攻击
#: （每个来源各自只失败几次，账号的累计失败在涨，但谁都没到阈值）。
#: 到第 3 轮还继续 ⇒ 那不是误输。
ACCOUNT_HARD_LOCK_AFTER_ROUNDS = 3


def account_delay_seconds(level: int) -> int:
    """第 ``level`` 轮连续失败之后，一次登录尝试要等多久（秒）。

    阶梯：5 → 10 → 20 → 40 → 80 …，封顶 :data:`ACCOUNT_DELAY_MAX_SECONDS`。

    ``level`` 是**轮次**（``lock_level``），不是失败次数 —— 失败次数在触发
    时就被清零了（那是「离下次触发还差几次」的语义），拿它算延迟会在
    最需要知道的那刻失真（理由同 :func:`lock_duration_minutes`）。
    """
    level = max(1, int(level or 1))
    delay = ACCOUNT_DELAY_BASE_SECONDS * (2 ** (level - 1))
    return int(min(delay, ACCOUNT_DELAY_MAX_SECONDS))


def should_hard_lock(level: int) -> bool:
    """这一轮要不要**硬锁**（423）而不是「递增延迟」。

    见 :data:`ACCOUNT_HARD_LOCK_AFTER_ROUNDS` 的理由：延迟挡不住分布式
    攻击，而持续到第 3 轮还继续的已经不是误输。
    """
    return int(level or 0) >= ACCOUNT_HARD_LOCK_AFTER_ROUNDS


def delay_state(user: Any) -> dict:
    """这个账号此刻处于「延迟」还是「硬锁」。**只读，不改任何状态。**

    判据只用现有两列（``lock_level`` 与 ``locked_until``），**不需要新增
    数据库列** —— 这一点很重要：新增列要动模型与迁移，而模型不是这次
    整改能碰的。

    * 还没到 ``locked_until`` ⇒ 没在延迟。
    * 到了且轮次 < :data:`ACCOUNT_HARD_LOCK_AFTER_ROUNDS` ⇒ **延迟**
      （等完还能用正确口令登进去）。
    * 到了且轮次 ≥ 那个门槛 ⇒ **硬锁**（423，只能等或让管理员解封）。
    """
    level = int(getattr(user, "lock_level", 0) or 0)
    left = lock_left_seconds(user)
    hard = should_hard_lock(level)
    return {
        "delayed": left > 0 and not hard,
        "hard_locked": left > 0 and hard,
        "left_seconds": left,
        "delay_seconds": account_delay_seconds(level) if left > 0 and not hard else 0,
        "lock_level": level,
    }


def protection_summary() -> dict:
    """登录防护的当前形态（给管理界面看，不含任何凭据）。

    回答的是「这套系统现在防得住什么、防不住什么」：

    * **来源维度**：按什么分桶？是不是「全站一个桶」（审计 P2-3）。
    * **账号维度**：阈值多少、锁多久、阶梯长什么样（审计 P2-4）。
    * **IP 维度**：阈值与窗口，硬锁开没开。
    """
    return {
        "source_bucketing": source_bucketing(),
        "account": {
            "max_failures": int(getattr(settings, "login_max_failures", 3) or 3),
            "base_minutes": int(getattr(settings, "login_lock_minutes", 10) or 10),
            "max_minutes": int(getattr(settings, "login_lock_max_minutes", 1440) or 1440),
            "ladder": [lock_duration_minutes(i) for i in range(1, 6)],
            # 递增延迟阶梯（审计 P2-4）。**当前尚未在登录路径上生效** ——
            # 执行点要落在 api/auth.py（见本模块「为什么现在没接上」）。
            "delay_ladder_seconds": [account_delay_seconds(i) for i in range(1, 6)],
            "delay_max_seconds": ACCOUNT_DELAY_MAX_SECONDS,
            "hard_lock_after_rounds": ACCOUNT_HARD_LOCK_AFTER_ROUNDS,
        },
        "ip": {
            "max_failures": int(getattr(settings, "login_ip_max_failures", 30) or 30),
            "window_seconds": int(getattr(settings, "login_ip_window_seconds", 900) or 900),
            "hard_lock": bool(getattr(settings, "login_ip_hard_lock", False)),
        },
    }


# ---------------------------------------------------------------------------
# 为什么现在没接上（审计 P2-4 的现状说明）
# ---------------------------------------------------------------------------
#
# 上面这套「递增延迟」**当前没有在登录路径上生效**，原因有三，都不是「忘了」：
#
# 1. **执行点在 ``api/auth.py``**：延迟要在登录尝试里真的等/拒，而登录
#    路径是那个文件（不在这次整改的文件所有权范围内）。
# 2. **现有行为被测试钉死**：``tests/test_login_lockout.py`` 断言「首轮锁
#    ``settings.login_lock_minutes``（10 分钟）」「第二轮是第一轮的两倍」。
#    把首轮改成 5 秒延迟会让那两条变红 —— 而那个测试文件也不在所有权
#    范围内。**改行为必须连着改那个测试文件一起改**，不能只改一半。
# 3. **数据库没有「延迟截止时间」这一列**：现在只有 ``locked_until``，
#    而它同时被 423 的判定读。要让「延迟」与「硬锁」共存而不新增列，
#    判据只能是轮次（:func:`delay_state`）—— 这正是上面那套算法的由来。
#
# **接上它需要改的三处**（供后续整改直接照做）：
#
# * ``api/auth.py::_record_failure``：把 ``locked_until`` 写成
#   ``now + timedelta(seconds=lockout.account_delay_seconds(level))``
#   （轮次 ≥ ``ACCOUNT_HARD_LOCK_AFTER_ROUNDS`` 时仍用
#   ``lock_duration_minutes`` 走硬锁）；
# * ``api/auth.py::login`` 的锁定分支：``lockout.delay_state(user)`` 判出
#   是「延迟」就**等完继续验口令**（不要 423），是「硬锁」才 423；
# * ``tests/test_login_lockout.py``：把「首轮 10 分钟」改成「首轮
#   5 秒延迟、正确口令等完仍可登录」。

