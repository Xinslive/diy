"""审计查询（便于在业务层直接调用）。"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog

log = logging.getLogger("cwq")

#: 管理员二次验证的动作名。与 ``app/notify/mfa.py`` 的状态机一一对应。
#:
#: * ``ADMIN_MFA_SENT`` —— 验证邮件已发出（含「重新发送」）；
#: * ``ADMIN_MFA_VERIFIED`` —— 邮件里的链接被点开；
#: * ``ADMIN_MFA_CONSUMED`` —— 客户端换到了访问令牌（**会话真正建立**）；
#: * ``ADMIN_MFA_FAILED`` —— 出了岔子（通道没配好、token 不对、过期…）。
MFA_ACTION_SENT = "ADMIN_MFA_SENT"
MFA_ACTION_VERIFIED = "ADMIN_MFA_VERIFIED"
MFA_ACTION_CONSUMED = "ADMIN_MFA_CONSUMED"
MFA_ACTION_FAILED = "ADMIN_MFA_FAILED"


def recent(db: Session, book_id: int, limit: int = 50) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(AuditLog.book_id == book_id)
            .order_by(AuditLog.id.desc()).limit(limit)
        ).all()
    )


def log_admin_mfa(db: Session, *, action: str, user, detail: str = "",
                  **extra) -> None:
    """写一条二次验证的审计。**写失败绝不能影响主流程。**

    与 ``api/auth.py:: _audit`` 同一个立场：审计是**观测组件**，一旦它
    成了可用性单点，「审计坏了」就等于「管理员登不进系统」。所以用
    SAVEPOINT 把失败范围收敛在审计这一段里，失败只打应用日志。

    **这里绝不能出现 token / secret 原文。** 二次验证的两串密钥是
    「拿到就能用」的东西 —— 散列进审计表等于给每个能看操作日志的人发
    一把万能钥匙（审计表是全局可读的，见
    ``app/core/system_settings.py::mask_secret`` 的注释）。调用方只传
    分类信息（``challenge_id``、``reason``、``ip``），**不传密钥**。
    """
    from app.accounting.audit_helper import log as write_audit

    try:
        with db.begin_nested():
            write_audit(
                db,
                action=action,
                entity="user",
                entity_id=getattr(user, "id", "") or "",
                user=user,
                detail=detail,
                after=extra or None,
            )
    except Exception:  # noqa: BLE001
        log.exception("二次验证审计写入失败（已忽略，不影响主流程）action=%s", action)

