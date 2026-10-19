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
from typing import Any

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
