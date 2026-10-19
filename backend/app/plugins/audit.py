"""审计内容只含标识与结果；不能写源代码、凭据或整份授权名单。"""
from types import SimpleNamespace
from app.accounting.audit_helper import log
from app.core.db import SessionLocal


def event(db, action, plugin_id, *, user=None, result="success", code="", **fields):
    allowed = {k: v for k, v in fields.items() if k in {"version", "digest", "target_user_id", "attempt_id", "key_id", "algo", "archive_digest", "findings"}}
    log(db, action="plugin." + action, entity="plugin", entity_id=plugin_id[:64],
        user=user or SimpleNamespace(id=None, username="system"),
        after={"plugin_id": plugin_id, "result": result, "error_code": code, **allowed})


def failure(action, plugin_id, user, code, **fields):
    # 失败审计用独立提交，不能被失败操作的 rollback 吞掉。
    with SessionLocal.begin() as db:
        event(db, action, plugin_id, user=user, result="failed", code=code, **fields)
