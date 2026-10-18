"""使用者授权与 capability 分离；每个请求读当前 DB 提交状态。"""
from sqlalchemy import select
from app.plugins import audit
from app.plugins.errors import not_found, unavailable
from app.plugins.models import PluginRegistry, PluginUserAccess
from app.plugins.workers import status


def require_plugin_access(db, plugin_id, user, *, generation=None):
    plugin = db.get(PluginRegistry, plugin_id)
    grant = db.get(PluginUserAccess, (plugin_id, user.id)) if plugin and not plugin.deleted_at else None
    if grant is None or grant.revoked_at is not None:
        audit.event(db, "access_denied", plugin_id[:96], user=user, result="denied", code="PLUGIN_NOT_FOUND_OR_UNAVAILABLE")
        db.commit()
        raise not_found()
    if status(db, plugin, generation)[0] != "READY":
        raise unavailable()
    return plugin
