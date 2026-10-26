"""事务提交后的同步通知。插件失败不回滚已经完成的核心结账。

**钩子只在「真正的顶层提交」之后跑 —— 保存点释放不算提交。**

SQLAlchemy 2.x 的 ``SessionTransaction.commit()`` 里有这么一句::

    if self._parent is None or self.nested:
        ...
        self.session.dispatch.after_commit(self.session)

``self.nested`` 为真的那一支**也会**派发 ``after_commit``。也就是说
``with db.begin_nested():`` 这个保存点一释放，钩子就被叫醒了 ——
可那不是提交：外层事务还开着，它攥着的行锁（账套行、期间行、凭证行）
一把都没松。

在这个时机跑钩子，插件会去写自己的表；而插件台账（``plugin_*_run``）有
外键指向 ``book``，插入时 PostgreSQL 要给账套行加一把 ``FOR KEY SHARE`` ——
那把锁正被**发起这次操作的那个事务自己**用 ``SELECT ... FOR UPDATE`` 持有。
于是两个连接互相等：插件的插入等本事务提交，本事务等插件返回。

生产实测就是这个死法（2026-10-26，紫欣工贸账套 11，2025 → 2026 年度结转）：
结转请求挂在插件的 ``data_insert`` 上一动不动，客户端 30 秒超时拿到 499，
而服务端那个事务一直不结束；每重试一次再叠一个，前一个把后一个堵死，
最后连 ``systemctl stop`` 都停不下来（uvicorn 要等在途请求，90 秒后 SIGKILL）。

所以这里把「提交」和「保存点释放」分开记账：``after_commit`` / ``after_rollback``
只置标记，真正跑钩子放在 ``after_transaction_end``，且只认**顶层**事务。
"""
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

#: 顶层事务刚提交过的标记。存在 ``db.info`` 里 —— 那是**会话**自己的字典，
#: 跟事务同生共死，不会串到别的会话上去。
_COMMIT_MARK = '_plugin_commit_marked'


def set_runtime(runtime):
    global _runtime
    _runtime = runtime


def period_closed(db, *, user_id, book_id, period_id):
    if _runtime is not None:
        if not db.in_transaction():
            db.begin()
        db.info.setdefault('plugin_events', []).append({'name': 'on_period_close', 'user_id': user_id,
            'book_id': book_id, 'period_id': period_id})


@event.listens_for(Session, 'after_commit')
def mark_committed(db):
    """**只记标记，不跑钩子。**

    保存点释放也会走到这里（见模块 docstring），那时外层事务还没结束，
    跑钩子就是自锁。
    """
    db.info[_COMMIT_MARK] = True


@event.listens_for(Session, 'after_rollback')
def mark_rolled_back(db):
    """回滚：标记作废。

    注意**保存点回滚也会派发 ``after_rollback``**，而保存点回滚之后外层
    事务还活着、后面还会提交 —— 所以这里只清标记，**不能**把已入队的
    事件一起丢掉（丢掉的话，那次结账的插件钩子会被静默跳过）。
    """
    db.info.pop(_COMMIT_MARK, None)


@event.listens_for(Session, 'after_transaction_end')
def transaction_end(db, transaction):
    """顶层事务结束：提交了就跑钩子，回滚了就作废事件。

    ``transaction.parent`` 非空或 ``transaction.nested`` 为真 = 保存点级别，
    顶层事务还在继续，事件先留着，等顶层结束再结算。
    """
    if transaction.parent is not None or transaction.nested:
        return
    was_committed = db.info.pop(_COMMIT_MARK, False)
    events = db.info.pop('plugin_events', [])
    if not was_committed:
        # 顶层是回滚：事件作废（回滚后账也没结成，插件不该收到通知）。
        return
    dispatch(db, events)


def dispatch(db, events):
    """跑插件钩子。每个事件对每个装了同名钩子的插件通知一次。"""
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
