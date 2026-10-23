"""事务提交后的同步通知。插件失败不回滚已经完成的核心结账。"""
import logging
from sqlalchemy import event
from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.core.db import SessionLocal
from app.models.system import SysUser
from app.plugins.access import require_plugin_access
from app.plugins import audit
from app.plugins.public_api import current_principal

log = logging.getLogger(__name__)
_runtime = None


def set_runtime(runtime):
    global _runtime
    _runtime = runtime


def period_closed(db, *, user_id, book_id, period_id):
    if _runtime is not None:
        if not db.in_transaction():
            db.begin()
        db.info.setdefault('plugin_events', []).append({'name': 'on_period_close', 'user_id': user_id,
            'book_id': book_id, 'period_id': period_id})


@event.listens_for(Session, 'after_rollback')
def discard_events(db):
    db.info.pop('plugin_events', None)


@event.listens_for(Session, 'after_commit')
def committed(db):
    events = db.info.pop('plugin_events', [])
    runtime = _runtime
    if not runtime:
        return
    for payload in events:
        for pid, hooks in list(runtime.hooks.items()):
            handler = hooks.get(payload['name'])
            if not handler:
                continue
            # user 在鉴权闸里取；闸本身出问题（DB 抖动）时它是未定义的，
            # 下面的 except 分支要能记审计 —— 所以先给个 None。
            user = None
            try:
                with SessionLocal() as gate:
                    user = gate.get(SysUser, payload['user_id'])
                    if not user or not user.active:
                        continue
                    require_plugin_access(gate, pid, user, generation=runtime.generation)
                token = current_principal.set((pid, payload['user_id'], runtime.generation))
                try:
                    handler(dict(payload))
                finally:
                    current_principal.reset(token)
            except HTTPException:
                # 用户未授权、已撤权或 worker barrier 未就绪，均不运行通知。
                continue
            except Exception:
                log.exception('plugin hook failed id=%s name=%s', pid, payload['name'])
                try:
                    audit.failure('hook', pid, user, 'HOOK_EXECUTION_FAILED')
                except Exception:
                    log.exception('plugin hook audit failed id=%s', pid)
            except BaseException:
                # **顺序不能反：Exception 在前、BaseException 在后。**
                #
                # SystemExit / KeyboardInterrupt / GeneratorExit 派生自
                # BaseException，**不被 except Exception 接住** —— 它们会一路
                # 穿出 ``Session.commit()``。后果不是"插件出错"而是：结账
                # 事务**已经提交**（原子性没破），调用方却拿到 500 ——
                # 用户看到「结账失败」，实际账已经锁了，重试还会撞「已结账」。
                #
                # 所以插件的异常**任何形态都不许穿出宿主边界**。
                log.exception('plugin hook aborted id=%s name=%s', pid, payload['name'])
                try:
                    audit.failure('hook', pid, user, 'HOOK_EXECUTION_FAILED')
                except BaseException:
                    log.exception('plugin hook audit failed id=%s', pid)
