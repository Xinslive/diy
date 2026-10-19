import json
from pathlib import Path
from sqlalchemy import select, text, inspect
from app.core.db import SessionLocal, engine
from app.plugins.models import PluginRegistry, PluginInstallAttempt, PluginUserAccess
from tests.plugin_fixtures import plugin_env


def test_upgrade_requires_restart_preserves_grants_and_verified_backup(plugin_env, client, auth, plain_user):
    build, restart = plugin_env
    raw, manifest, _, _ = build(tables=True)
    pid = manifest['id']
    post = lambda suffix, **kw: client.post('/api/system/plugins/' + suffix, headers=auth, **kw)
    assert post('install', files={'file': ('x.cwp', raw)}).status_code == 200
    assert client.get('/api/system/plugins', headers=auth).json()['plugins'][0]['runtime_status'] == 'PENDING_RESTART'
    assert post(f'{pid}/users/{plain_user.id}').status_code == 200
    new, _, _, _ = build(plugin_id=pid, version='1.1.0', tables=True)
    assert post(f'{pid}/upgrade', files={'file': ('x.cwp', new)}).status_code == 409
    assert post(f'{pid}/disable').status_code == 200
    assert post(f'{pid}/upgrade', files={'file': ('x.cwp', new)}).status_code == 409
    restart()
    r = post(f'{pid}/upgrade', files={'file': ('x.cwp', new)})
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'disabled'
    listed = client.get('/api/system/plugins', headers=auth).json()['plugins'][0]
    assert listed['runtime_status'] == 'DISABLED'
    assert listed['user_count'] == 1
    index = json.loads((Path(listed['backup_path']) / 'index.json').read_text())
    assert index['snapshots'] and index['snapshots'][0]['sha256']
    assert post(f'{pid}/upgrade', files={'file': ('x.cwp', new)}).status_code == 409
    old, _, _, _ = build(plugin_id=pid, version='0.9.0', tables=True)
    assert post(f'{pid}/upgrade', files={'file': ('x.cwp', old)}).status_code == 409
    r = client.delete(f'/api/system/plugins/{pid}', headers=auth)
    assert r.status_code == 200, r.text
    table = r.json()['retained_tables'][0]
    with engine.connect() as conn:
        assert inspect(conn).has_table(table)
    assert client.get(f'/api/system/plugins/{pid}/tables', headers=auth).json()['tables'][0]['name'] == table
    assert post(f'{pid}/tables/cleanup', json={'confirmation': 'wrong'}).status_code == 409
    r = post(f'{pid}/tables/cleanup', json={'confirmation': pid})
    assert r.status_code == 200, r.text
    assert client.get(f'/api/system/plugins/{pid}/tables', headers=auth).json()['tables'] == []
    with SessionLocal() as db:
        assert db.scalar(select(PluginUserAccess).where(PluginUserAccess.plugin_id == pid)).revoked_at
        assert db.get(PluginRegistry, pid).error_code == 'TABLES_CLEANED'
    with engine.connect() as conn:
        assert not inspect(conn).has_table(table)


def test_upgrade_runs_only_new_revision_and_retains_old_data(plugin_env, client, auth, admin_user, book):
    from app.plugins.security import verify_archive
    from app.plugins.manifest import table_prefix, version_table
    build, restart = plugin_env
    raw, manifest, _, _ = build(tables=True)
    pid, name = manifest['id'], table_prefix(manifest['id']) + 'items'
    assert client.post('/api/system/plugins/install', headers=auth, files={'file': ('old.cwp', raw)}).status_code == 200
    with engine.begin() as conn:
        main_head = conn.scalar(text('SELECT version_num FROM alembic_version'))
        conn.execute(text(f'INSERT INTO "{name}" (book_id,label) VALUES (:id, :label)'), {'id': book.id, 'label': '升级前数据'})
    assert client.post(f'/api/system/plugins/{pid}/disable', headers=auth).status_code == 200
    restart()
    files = dict(verify_archive(raw).files)
    files['migrations/versions/0002_extra.py'] = f'''from alembic import op
import sqlalchemy as sa
revision = "0002_extra"
down_revision = "0001_items"
def upgrade():
    op.add_column("{name}", sa.Column("extra", sa.String(32), nullable=False, server_default="preserved"))
'''.encode()
    def metadata(doc):
        doc['permissions'] = manifest['permissions']
        doc['tables'] = manifest['tables']
    upgraded, _, _, _ = build(plugin_id=pid, version='1.1.0', files=files, edit=metadata)
    result = client.post(f'/api/system/plugins/{pid}/upgrade', headers=auth, files={'file': ('new.cwp', upgraded)})
    assert result.status_code == 200, result.text
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT version_num FROM alembic_version')) == main_head
        assert conn.scalar(text(f'SELECT version_num FROM "{version_table(pid)}"')) == '0002_extra'
        assert conn.execute(text(f'SELECT label, extra FROM "{name}"')).one() == ('升级前数据', 'preserved')
    assert client.get('/api/system/plugins', headers=auth).json()['plugins'][0]['runtime_status'] == 'DISABLED'
