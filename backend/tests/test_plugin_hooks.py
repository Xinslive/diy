import pytest
from sqlalchemy import text
from app.core.db import SessionLocal
from app.plugins.hooks import period_closed
from app.plugins.installer import install
from tests.plugin_fixtures import plugin_env


def test_hooks_only_after_commit_current_grant_and_rollback_isolation(plugin_env, client, auth, admin_user, book):
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    install(raw, admin_user)
    runtime = restart()
    pid = manifest['id']
    seen = []
    runtime.hooks[pid] = {'on_period_close': lambda event: seen.append(event)}
    with SessionLocal.begin() as db:
        period_closed(db, user_id=admin_user.id, book_id=book.id, period_id=1)
    assert seen == []  # installer/admin is still not granted
    client.post(f'/api/system/plugins/{pid}/users/{admin_user.id}', headers=auth)
    with SessionLocal.begin() as db:
        period_closed(db, user_id=admin_user.id, book_id=book.id, period_id=2)
        assert seen == []
    assert len(seen) == 1 and seen[0]['period_id'] == 2
    with SessionLocal() as db:
        period_closed(db, user_id=admin_user.id, book_id=book.id, period_id=3)
        db.rollback()
        db.commit()
    assert len(seen) == 1
    def broken(event):
        raise ValueError('hook failure must not affect host commit')
    runtime.hooks[pid]['on_period_close'] = broken
    with SessionLocal.begin() as db:
        period_closed(db, user_id=admin_user.id, book_id=book.id, period_id=4)
        db.execute(text('SELECT 1'))
    client.delete(f'/api/system/plugins/{pid}/users/{admin_user.id}', headers=auth)
    runtime.hooks[pid]['on_period_close'] = lambda event: seen.append(event)
    with SessionLocal.begin() as db:
        period_closed(db, user_id=admin_user.id, book_id=book.id, period_id=5)
    assert len(seen) == 1


def test_savepoint_release_is_not_a_commit(plugin_env, client, auth, admin_user, book):
    """释放保存点**不是**提交 —— 钩子不能在那时候跑。

    回归 2026-10-26 生产事故：年度结转在 ``ensure_periods`` 里
    ``with db.begin_nested():``，保存点一释放 SQLAlchemy 就派发了
    ``after_commit``（``SessionTransaction.commit()`` 里那句
    ``if self._parent is None or self.nested:``），插件钩子被**提前**叫醒。

    钩子这时去写插件台账（外键指向 ``book``），而 ``book`` 行正被这次结转
    自己的 ``SELECT ... FOR UPDATE`` 锁着 —— 跨连接等自己，请求挂死到
    客户端超时（499），服务端那个事务还留在原地不结束。
    """
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    install(raw, admin_user)
    runtime = restart()
    pid = manifest['id']
    seen = []
    runtime.hooks[pid] = {'on_period_close': lambda event: seen.append(event)}
    client.post(f'/api/system/plugins/{pid}/users/{admin_user.id}', headers=auth)
    with SessionLocal() as db:
        period_closed(db, user_id=admin_user.id, book_id=book.id, period_id=1)
        with db.begin_nested():
            db.execute(text('SELECT 1'))
        assert seen == [], '保存点释放就触发钩子 = 又回到生产那次事故的坑里'
        db.commit()
    assert len(seen) == 1, '顶层提交之后钩子必须跑，且只跑一次'


def test_savepoint_rollback_keeps_the_event(plugin_env, client, auth, admin_user, book):
    """保存点回滚之后外层事务还要提交 —— 事件不能被一起丢掉。

    ``after_rollback`` 对保存点回滚**也会**派发。原来那个监听器一收到就
    把已入队的插件事件清掉，于是「结账中途有个保存点失败」会让这次结账的
    插件钩子被静默跳过 —— 钩子不跑，插件以为没结过账。
    """
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    install(raw, admin_user)
    runtime = restart()
    pid = manifest['id']
    seen = []
    runtime.hooks[pid] = {'on_period_close': lambda event: seen.append(event)}
    client.post(f'/api/system/plugins/{pid}/users/{admin_user.id}', headers=auth)
    with SessionLocal() as db:
        period_closed(db, user_id=admin_user.id, book_id=book.id, period_id=1)
        try:
            with db.begin_nested():
                db.execute(text('SELECT 1'))
                raise ValueError('保存点里的业务失败')
        except ValueError:
            pass
        db.commit()
    assert len(seen) == 1, '保存点回滚不该把顶层事务的事件一起作废'


def test_hook_runs_after_the_host_transaction_committed(plugin_env, client, auth, admin_user, book):
    """钩子必须在外层事务**提交之后**才跑，不能跟发起方自己抢行锁。

    端到端复现生产事故：宿主事务先 ``SELECT book ... FOR UPDATE`` 锁住账套行
    （年度结转的 ``ensure_periods`` 正是这么干的），钩子在**另一条连接**上
    再去拿同一行的锁。没有修复时钩子会在事务中途被叫醒 → 跨连接等自己 →
    挂死。这里给钩子的连接单独设 2 秒等锁上限，让它在旧行为下**快速失败**
    而不是把测试也挂住。
    """
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    install(raw, admin_user)
    runtime = restart()
    pid = manifest['id']
    client.post(f'/api/system/plugins/{pid}/users/{admin_user.id}', headers=auth)
    outcome = {}

    def handler(event):
        try:
            with SessionLocal() as db:
                db.execute(text("SET lock_timeout = '2s'"))
                db.execute(text('SELECT id FROM book WHERE id = :id FOR UPDATE'),
                           {'id': book.id})
            outcome['ok'] = True
        except Exception as exc:  # noqa: BLE001 - 测试里要看到失败原因
            outcome['error'] = repr(exc)

    runtime.hooks[pid] = {'on_period_close': handler}
    with SessionLocal() as db:
        db.execute(text('SELECT id FROM book WHERE id = :id FOR UPDATE'), {'id': book.id})
        period_closed(db, user_id=admin_user.id, book_id=book.id, period_id=1)
        # 生产上的真实顺序：结账入队事件之后，``ensure_periods`` 还会
        # ``with db.begin_nested():`` 一下 —— 旧行为就是在这里把钩子叫醒的。
        with db.begin_nested():
            db.execute(text('SELECT 1'))
        db.commit()
    assert outcome.get('ok'), outcome.get('error')
