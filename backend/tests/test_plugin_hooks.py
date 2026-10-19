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
