"""通知事件：把「发生了什么」翻译成一封告警邮件。

**这个模块最重要的性质是：发送失败绝不抛到调用方。**

审计 P2-7 的原话是「告警发送失败**绝不能**影响主流程」。理由很直接：
通知是**旁路**，主流程（登录、定时任务）才是业务。没有 SMTP 服务器、
口令过期、对方拒收 —— 这些都不该让一个用户登不进去，或让一个定时任务
被判失败。所以这里每一个出口都包着 try/except，失败只记日志。

**为什么是异步线程。**

账号锁定发生在登录接口上。那条路径每多花 1ms，就是每个攻击者都能免费
放大的 1ms —— 攻击者可以对着一个不存在的账号反复触发锁定，把 SMTP 的
往返时间变成自己的放大器。所以锁定通知走**守护线程**，请求不等它。

代价是「发了但进程正好退出」会丢一封信。这是可以接受的：通知是尽力而为，
而**审计表里的那条记录才是可靠的那一份**（它与业务变更同事务提交）。
两者分工是刻意的 —— 审计负责「事后能查」，邮件负责「当时就知道」。

**并发上限。** 同时最多 4 封在发。超出的直接丢弃并记日志：SMTP 连接是
有成本的，而「一个攻击者锁 1000 个账号」不该变成「1000 条 SMTP 连接」。
丢弃的通知会留下一条 warning 日志，不留则等于静默。

**限流。** 见 :class:`_EventBudget`。这一条不是"锦上添花"：邮件通道是
未认证可达的，攻击者能拿它当放大器，把管理员的邮箱灌满 —— 而被淹没的
邮箱里没有一条是真告警，那是告警通道最坏的失效方式。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app.notify import mailer
from app.notify.config import notify_enabled, recipients, smtp_config

log = logging.getLogger("cwq.notify")

#: 同时最多几封在发。理由见模块头。
_MAX_CONCURRENT = 4
_slots = threading.BoundedSemaphore(_MAX_CONCURRENT)


class _BudgetState:
    """单个事件类型的预算状态。**只在锁内读写。**"""

    __slots__ = ("tokens", "updated_at", "suppressed")

    def __init__(self, capacity: int, now: float):
        self.tokens = float(capacity)
        self.updated_at = now
        self.suppressed = 0


class _EventBudget:
    """按事件类型的发信预算（令牌桶）。

    **为什么需要它。** 邮件通道和「操作日志」最大的区别是：后者只有
    管理员能写，前者**任何人都能触发**。攻击者对一批账号各试三次错误
    口令，就能让系统发出成百上千封「账号锁定」告警 ——

    * 管理员的邮箱被灌满，真告警淹没在里面看不见；
    * 邮件服务商那边，这个发件域名很快会被判成垃圾邮件源，
      于是连**真正的**告警也发不出去了。

    两件事都是**攻击者花几分钱就能买到的**效果，所以必须在通道这一侧
    就挡住，而不是指望"攻击者没那么坏"。

    **按事件类型分桶。** 锁定告警刷屏不该把「备份失败」一起压掉 ——
    后者恰恰是最该被看到的那一条。共用一个桶的话，攻击者锁 1000 个账号
    就能把当天的备份告警一并压掉 —— 那等于给了攻击者一个"关掉备份告警"
    的开关。

    **被抑制的不静默丢。** 超出配额的告警不发信，但**计数留着**；下一封
    真能发出去时，正文里补一句"另有 N 条同类告警未单独通知"。于是
    「不淹没邮箱」与「不静默丢事件」两件事同时成立 —— 全量明细始终在
    「操作日志」里，邮件只是把"发生过很多次"这件事说出来。

    **失败的发送要退回额度。** 额度代表的是「管理员邮箱还能承受多少封
    信」，而一封没送出去的信根本没进邮箱。SMTP 宕机时如果照扣不误，
    等它恢复后第一批真告警会被当成超限丢掉 —— 那时系统已经在出事了，
    恰恰是最不该丢告警的时候。
    """

    def __init__(self):
        self._states: dict[str, _BudgetState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _limits() -> tuple[int, int]:
        """配额与窗口。**每次都现读配置**，不缓存 —— 改配置即刻生效，
        测试也不必去戳模块内部状态。"""
        from app.core.config import settings

        capacity = max(1, int(settings.notify_rate_limit or 1))
        window = max(1, int(settings.notify_rate_window_seconds or 1))
        return capacity, window

    def _state(self, event: str) -> _BudgetState:
        """取（必要时建立）某事件类型的状态，并按经过的时间补充令牌。

        **调用方持锁。**
        """
        capacity, window = self._limits()
        now = time.monotonic()
        state = self._states.get(event)
        if state is None:
            state = _BudgetState(capacity, now)
            self._states[event] = state
            return state
        elapsed = now - state.updated_at
        if elapsed > 0:
            state.tokens = min(float(capacity),
                               state.tokens + elapsed * capacity / window)
            state.updated_at = now
        return state

    def take(self, event: str) -> bool:
        """取一个发送名额。取不到返回 False —— 这一封就不发了。"""
        with self._lock:
            state = self._state(event)
            if state.tokens >= 1:
                state.tokens -= 1
                return True
            return False

    def refund(self, event: str) -> None:
        """发送失败时把名额还回去。理由见类注释。"""
        with self._lock:
            state = self._state(event)
            state.tokens = min(float(self._limits()[0]), state.tokens + 1)

    def suppress(self, event: str) -> int:
        """记一次「没发出去」，返回累计被抑制的条数。"""
        with self._lock:
            state = self._state(event)
            state.suppressed += 1
            return state.suppressed

    def take_suppressed(self, event: str) -> int:
        """当前累计被抑制的条数（**不清零**）。

        计数是在**投递前**读进正文的，而投递可能失败 —— 失败时这封信根本
        没到管理员邮箱，所以计数必须留着给下一封。于是这里只读，清零由
        :meth:`clear_suppressed` 在**真的发出去之后**做。
        """
        with self._lock:
            return self._state(event).suppressed

    def clear_suppressed(self, event: str, count: int) -> None:
        """扣掉已经**真的发出去了**的那部分计数。

        只扣 ``count`` 而不是清零：发送期间新来的抑制计数属于下一封，
        一起清掉的话那几条就永远没人提了。
        """
        with self._lock:
            state = self._state(event)
            state.suppressed = max(0, state.suppressed - count)

    def reset(self) -> None:
        """清空全部状态。

        给测试用：预算是**进程级**的（它得跨请求生效），而测试共享进程 ——
        上一条用例触发的几十封通知会把配额吃光，让后面的用例随机失败。
        那不是"偶发抖动"，是**顺序依赖**：换台机器跑就红了。
        """
        with self._lock:
            self._states.clear()


_budget = _EventBudget()


def _now_text() -> str:
    """给邮件正文用的时间戳。

    用**本地时间**而不是 UTC：收件人是人，看到的应该是他自己时钟上的
    时刻，便于和「当时我看到什么」对上。
    """
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _load(db=None):
    """取生效配置。返回 ``(config, to_addresses, enabled)``。

    ``db`` 为 None 时**自己开一个会话** —— 跨线程不能共用别人的 Session
    （SQLAlchemy 的 Session 不是线程安全的，用了会串数据）。
    """
    if db is not None:
        return smtp_config(db), recipients(db), notify_enabled(db)
    from app.core.db import SessionLocal

    with SessionLocal() as session:
        return smtp_config(session), recipients(session), notify_enabled(session)


def _sendable(config, to_addresses, enabled, event: str) -> bool:
    """现在能不能发？不能的话把原因记下来。

    这不是失败，是**配置还没配完**（通知总开关关着、没填收件人、没配
    SMTP）—— 所以只记 info，不记 warning：否则每次登录失败都刷一条
    warning，运维会学会忽略它。

    放在**取额度之前**：配都没配好的时候不该消耗额度，否则真发不出信
    的时候额度已经被空转掉了。
    """
    if not enabled:
        log.info("通知已关闭，跳过 event=%s", event)
        return False
    if not to_addresses:
        log.info("未配置收件人，跳过 event=%s", event)
        return False
    if not (config.get("host") or "").strip():
        log.info("未配置 SMTP 服务器，跳过 event=%s", event)
        return False
    return True


def _send(config, to_addresses, event: str, subject: str, body: str) -> bool:
    """真发。失败抛 :class:`mailer.MailError`，由 :func:`dispatch` 兜住。"""
    count = mailer.send_mail(config, to_addresses, subject, body)
    log.info("通知已发送 event=%s 收件人=%d 主题=%s", event, count, subject)
    return True


def dispatch(event: str, subject: str, body: str, *, db=None) -> bool:
    """发一封通知。**永不抛异常**，返回是否真的发出去了。

    ``db`` 给了就用调用方那个会话读配置（同一个线程内），没给就自己开一个
    —— 后者是跨线程调用时唯一安全的做法。
    """
    try:
        config, to_addresses, enabled = _load(db)
        if not _sendable(config, to_addresses, enabled, event):
            return False
        if not _slots.acquire(blocking=False):
            # 并发满了直接丢：这封通知的发出不能拖住主流程，而堆积下去
            # 只会让 SMTP 那边更难恢复。
            total = _budget.suppress(event)
            log.warning("通知并发已满，丢弃一封 event=%s 主题=%s 累计抑制=%d",
                        event, subject, total)
            return False
        try:
            if not _budget.take(event):
                # 额度用完了。这一封**不发** —— 见 _EventBudget 的类注释：
                # 邮件通道是未认证可达的，不限流就等于把它交给攻击者。
                # 但计数留着，下一封真能发出去时会补报总数。
                total = _budget.suppress(event)
                log.warning("同类告警超过每窗口上限，已抑制 event=%s 主题=%s 累计抑制=%d",
                            event, subject, total)
                return False
            suppressed = _budget.take_suppressed(event)
            if suppressed:
                body = body + (
                    f"\n（另有 {suppressed} 条同类告警未单独通知，"
                    f"完整记录见「操作日志」）\n"
                )
            try:
                sent = _send(config, to_addresses, event, subject, body)
            except Exception:
                # 没送出去就不占额度：额度代表「管理员邮箱还能承受多少封信」，
                # 而失败的信根本没进邮箱。
                _budget.refund(event)
                raise
            # 计数只在**真的发出去了**之后才清：这封信要是没送到，那句
            # 「另有 N 条未单独通知」也跟着没送到 —— 计数必须留着给下一封，
            # 否则那 N 条就此无人提起。
            if suppressed:
                _budget.clear_suppressed(event, suppressed)
            return sent
        finally:
            _slots.release()
    except Exception as exc:  # noqa: BLE001
        # 这一行是整个模块存在的理由：**发不出去不是主流程的问题**。
        log.warning("通知发送失败（已忽略，不影响主流程）event=%s 错误=%s", event, exc)
        return False


def dispatch_async(event: str, subject: str, body: str) -> None:
    """在守护线程里发一封。**调用方不等它。**

    用于请求路径（登录锁定）—— 那条路径上多等一毫秒都是放大器。
    """
    def _run() -> None:
        try:
            dispatch(event, subject, body)
        except Exception:  # noqa: BLE001
            # dispatch 本身不抛，这层是「万一」的双保险 —— 守护线程里的
            # 未捕获异常会打到 stderr，污染应用日志的格式。
            log.exception("异步通知线程异常 event=%s", event)

    try:
        thread = threading.Thread(target=_run, name=f"notify-{event}", daemon=True)
        thread.start()
    except Exception:  # noqa: BLE001
        log.warning("无法启动通知线程（已忽略）event=%s", event)


def _safe_dispatch_async(event: str, subject: str, body: str) -> None:
    """发通知，**永不抛异常**。

    这一层存在的理由是**调用点在主流程上**：登录接口、定时任务执行
    循环里调的是 :func:`account_locked` / :func:`task_failed` 这些事件
    函数，而不是 :func:`dispatch`。所以「发不出去不影响主流程」这条
    承诺必须在**事件函数这一层**就成立 —— 哪怕将来有人把
    :func:`dispatch_async` 改成会抛的（比如忘了那个 try/except），
    主流程也不该跟着炸。

    事件函数自己的正文拼装同样包在这里：正文里有 ``str()`` 调用，
    理论上能碰到一个 ``__str__`` 会炸的对象 —— 而那不该让一次登录失败。
    """
    try:
        dispatch_async(event, subject, body)
    except Exception as exc:  # noqa: BLE001
        log.warning("通知派发失败（已忽略，不影响主流程）event=%s 错误=%s", event, exc)


# ------------------------------------------------------------------ 事件


def account_locked(username: str, real_name: str, ip: str,
                   minutes: int, level: int, threshold: int) -> None:
    """账号被锁定。

    正文里给的是**处置需要的信息**：是谁、从哪、锁多久、第几轮。
    没有口令，也没有任何账套数据 —— 这封信会进管理员的邮箱。
    """
    who = f"{username}" + (f"（{real_name}）" if real_name else "")
    subject = f"账号锁定：{who}"
    body = (
        f"账号被连续登录失败触发锁定。\n\n"
        f"账号：{who}\n"
        f"来源 IP：{ip or '未知'}\n"
        f"连续失败阈值：{threshold} 次\n"
        f"这是第 {level} 轮连续锁定，锁定 {minutes} 分钟\n"
        f"发生时间：{_now_text()}\n\n"
        f"如果确认是本人误操作，请联系管理员在「用户与授权」页提前解除锁定；\n"
        f"如果不是本人操作，建议同时重置该账号的登录密码。\n"
    )
    _safe_dispatch_async("ACCOUNT_LOCKED", subject, body)


def task_failed(task_key: str, error_code: str, message: str) -> None:
    """定时任务失败。"""
    subject = f"定时任务失败：{task_key}"
    body = (
        f"计划任务运行失败。\n\n"
        f"任务：{task_key}\n"
        f"错误码：{error_code or '未知'}\n"
        f"错误信息：{(message or '（无）')[:1000]}\n"
        f"发生时间：{_now_text()}\n\n"
        f"任务在「计划任务」页可以看到最近运行记录与完整错误信息。\n"
    )
    _safe_dispatch_async("TASK_FAILED", subject, body)


def _backup_content(scope: str, message: str) -> tuple[str, str]:
    """账套备份失败的标题与正文。

    **为什么抽出来**：同一件事有两个投递方式 —— 应用内调度器走异步
    （守护线程），命令行脚本走同步（进程马上就要退出）。内容只能有一份，
    否则两条路径的措辞迟早对不上，而**对不上就意味着有一处漏了关键信息**
    （比如备份脚本那条忘了说"退路"）。
    """
    subject = f"备份失败：{scope}"
    body = (
        f"账套备份运行失败。\n\n"
        f"范围：{scope}\n"
        f"错误信息：{(message or '（无）')[:1000]}\n"
        f"发生时间：{_now_text()}\n\n"
        f"备份是出事后的退路，请尽快在「数据安全」页查看运行记录并手动重试。\n"
    )
    return subject, body


def _local_backup_content(scope: str, message: str) -> tuple[str, str]:
    """本机 .sql 备份失败的标题与正文。"""
    subject = f"本机数据库备份失败：{scope}"
    body = (
        f"本机数据库备份运行失败。\n\n"
        f"范围：{scope}\n"
        f"错误信息：{(message or '（无）')[:1000]}\n"
        f"发生时间：{_now_text()}\n\n"
        f"本机 .sql 备份与 OSS 备份是**两条独立的退路**（一台机器坏了，另一台\n"
        f"还在）。这条断了，退路就只剩一条了。\n"
        f"请检查备份目录、磁盘空间与 pg_dump 客户端，并手动补跑一次。\n"
    )
    return subject, body


def backup_failed(scope: str, message: str) -> None:
    """备份失败。

    这条尤其要紧：备份是**出事后唯一的退路**，而它失败时通常没有人在看
    —— 备份跑在凌晨，失败要到第二天才有人发现（审计 P2-7 点了这一条）。

    **异步**投递：供应用内调用（定时任务线程里），不等 SMTP 往返。
    """
    subject, body = _backup_content(scope, message)
    _safe_dispatch_async("BACKUP_FAILED", subject, body)


def backup_failed_blocking(scope: str, message: str) -> bool:
    """备份失败，**同步**投递。**给命令行脚本用。**

    **为什么 CLI 要同步而应用内要异步**：守护线程是**随进程一起死的**。
    脚本发完邮件就要退出，异步线程还没把信交出去就被连带杀掉 ——
    表现是「脚本说备份成功了、通知却从来没到过」，而那恰恰是最坏的一种
    失效：看起来一切正常。

    命令行进程马上就要结束，等一次 SMTP 往返完全可接受（备份本来就是
    批处理，没有请求在等它）。
    """
    subject, body = _backup_content(scope, message)
    return dispatch("BACKUP_FAILED", subject, body)


def local_backup_failed_blocking(scope: str, message: str) -> bool:
    """本机 .sql 备份失败，**同步**投递。给 ``scripts/db_backup.py`` 用。

    单独一个事件类型（而不是复用 ``BACKUP_FAILED``）是为了**不共用限流桶**：
    两条退路各断各的，不该因为一边刷屏就把另一边一起压掉。
    """
    subject, body = _local_backup_content(scope, message)
    return dispatch("LOCAL_BACKUP_FAILED", subject, body)


def _task_missed_content(task_key: str, task_name: str, owner_type: str,
                         due_at: str, overdue: str,
                         last_run_at: str) -> tuple[str, str]:
    """任务「到点没跑」告警的标题与正文。"""
    who = f"{task_key}（{task_name}）"
    subject = f"计划任务没有运行：{who}"
    body = (
        f"计划任务到点没有运行。\n\n"
        f"任务：{who}\n"
        f"类型：{'系统内置任务' if owner_type == 'SYSTEM' else '插件任务'}\n"
        f"本该运行时间：{due_at}\n"
        f"已逾期：{overdue}\n"
        f"最近一次运行：{last_run_at}\n"
        f"检查时间：{_now_text()}\n\n"
        f"这通常意味着**外部调度服务没有在调用 tick 接口** —— 宿主的计划任务\n"
        f"由外部每分钟触发一次 tick 驱动，tick 不来，任务就不会被领取。\n"
        f"请检查：外部调度服务的配置与触发密钥、应用的可达性、以及该任务\n"
        f"是否仍然启用。\n\n"
        f"在「系统管理 → 计划任务」页可以看到每个任务的最近运行记录。\n"
    )
    return subject, body


def task_missed(task_key: str, task_name: str, owner_type: str,
                due_at: str, overdue: str, last_run_at: str) -> None:
    """任务到点没跑。**异步**投递（供应用内调用）。"""
    subject, body = _task_missed_content(task_key, task_name, owner_type,
                                          due_at, overdue, last_run_at)
    _safe_dispatch_async("TASK_MISSED", subject, body)


def task_missed_blocking(task_key: str, task_name: str, owner_type: str,
                         due_at: str, overdue: str, last_run_at: str) -> bool:
    """任务到点没跑，**同步**投递。**给看门狗脚本用**。

    看门狗是**独立于应用**跑的（它要发现的正是"应用没被 tick 到"这件事，
    所以它不能住在应用里）—— 进程跑完就退出，异步线程会随进程一起死。
    """
    subject, body = _task_missed_content(task_key, task_name, owner_type,
                                          due_at, overdue, last_run_at)
    return dispatch("TASK_MISSED", subject, body)
