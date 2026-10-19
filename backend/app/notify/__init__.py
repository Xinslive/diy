"""邮件通知子系统。

**它解决什么**：审计 P2-7 —— 「安全事件无告警通道，全部只落审计表」。
账号被锁定、IP 在扫、定时任务失败、备份失败，这些**最该被主动发现**的
事件，发现延迟等于人的轮询频率：审计表是**证据**通道，而它要人主动去翻。

**它不解决什么**：它不替代审计。审计表与业务变更同事务提交、绝不丢；
邮件是尽力而为（发不出去就发不出去，不影响主流程）。两个通道各司其职：
**审计负责「事后能查」，邮件负责「当时就知道」**。

模块划分：

* :mod:`app.notify.config` —— SMTP 配置的读写（与 OCR / OSS 同一套约定）
* :mod:`app.notify.mailer` —— 纯标准库的发信实现
* :mod:`app.notify.events` —— 事件到邮件的翻译，以及「发失败不影响主流程」
  这条铁律的落点
"""

from app.notify.config import (
    notify_enabled,
    parse_recipients,
    recipients,
    set_notify_enabled,
    set_recipients,
    smtp_config,
    smtp_config_public,
    valid_recipient,
)
from app.notify.events import (
    account_locked,
    backup_failed,
    backup_failed_blocking,
    dispatch,
    dispatch_async,
    local_backup_failed_blocking,
    task_failed,
    task_missed,
    task_missed_blocking,
)
from app.notify.mailer import MailError, send_mail, test_connection

__all__ = [
    "MailError",
    "account_locked",
    "backup_failed",
    "backup_failed_blocking",
    "dispatch",
    "dispatch_async",
    "local_backup_failed_blocking",
    "notify_enabled",
    "parse_recipients",
    "recipients",
    "send_mail",
    "set_notify_enabled",
    "set_recipients",
    "smtp_config",
    "smtp_config_public",
    "task_failed",
    "task_missed",
    "task_missed_blocking",
    "test_connection",
    "valid_recipient",
]
