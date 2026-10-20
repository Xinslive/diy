"""登录路径的测试辅助。

**为什么要有这个文件。**

登录失败之后账号进入什么状态，有**两种**形态（审查报告安全域 P2-4 改的）：

* **递增延迟**（第 1-2 轮）：429 + Retry-After，等几秒就能用正确口令继续登录。
  一次性把别人挡在门外的 DoS 因此不成立。
* **硬锁**（第 3 轮起）：423，要等或让管理员解封。延迟挡不住分布式攻击，
  所以留一道真正的锁。

**只有硬锁才发告警邮件** —— 递增延迟只有几秒，每次撞库口令都发一封的话，
收件箱会被刷爆，而收件箱是**通知通道本身**（真告警淹没在里面）。

所以「造一个被锁的账号」这件事需要跨过延迟期，而延迟期要等好几秒 ——
测试里当然不能真等。于是这里提供 :func:`打到硬锁`：把账号一路打到硬锁，
并把中间每一轮的延迟**拨到过去**（等价于「等了几秒」）。

**别在别处再写一份** —— 口径变了要连着改这个文件和所有调用点一起改。
"""
from __future__ import annotations


def 打到硬锁(client, db, username: str, password: str = "错的") -> None:
    """把 ``username`` 这个账号一路打到**硬锁**（第 3 轮起）。

    **只花一次失败尝试**：先把状态拨到临界点（轮次差一轮、失败计数差一次），
    再用一次失败跨过硬锁门槛。

    为什么不用「老老实实错三次、再等三轮」：那要 9 次尝试，而这些用例
    **都来自同一个 IP**（127.0.0.1）—— 9 次会把 **IP 维度**的节流桶打爆
    （阈值 30 次/15 分钟），于是同文件的下一条用例连登录都进不去，
    而且表现是「莫名 429」，极难查。

    把状态拨到临界点不改变被验证的行为：**跨门槛的那一次失败**仍然是
    真实的口令校验失败，仍然走同一条 ``_record_failure`` 路径。
    """
    from app.core import lockout
    from app.core.config import settings
    from app.models import SysUser

    db.query(SysUser).filter(SysUser.username == username).update(
        {
            SysUser.lock_level: lockout.ACCOUNT_HARD_LOCK_AFTER_ROUNDS - 1,
            SysUser.failed_logins: max(0, settings.login_max_failures - 1),
            SysUser.locked_until: None,
        },
        synchronize_session=False)
    db.commit()
    client.post("/api/auth/login",
                json={"username": username, "password": password})


def 打到递增延迟(client, username: str, password: str = "错的") -> None:
    """只打到**第一轮递增延迟**（不硬锁、不发通知）。"""
    from app.core.config import settings

    for _ in range(settings.login_max_failures):
        client.post("/api/auth/login",
                    json={"username": username, "password": password})
