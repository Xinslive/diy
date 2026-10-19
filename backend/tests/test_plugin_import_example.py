"""真实 CSV 示例包与配置 CAS、配置快照、独立账套和来源查询的集成验证。"""
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from app.models import Book, Subject, UserBook
from tests.plugin_fixtures import plugin_env

SOURCE = Path(__file__).resolve().parents[2] / 'docs/plugin-import-example'


@pytest.fixture
def example(plugin_env, client, auth, plain_auth, plain_user, book, db):
    build, restart = plugin_env
    manifest = json.loads((SOURCE / 'plugin.json').read_text())
    files = {p.relative_to(SOURCE).as_posix(): p.read_bytes() for p in SOURCE.rglob('*') if p.is_file() and p.name != 'plugin.json'}
    raw, _, _, _ = build(plugin_id=manifest['id'], files=files, edit=lambda m: m.update(manifest))
    installed = client.post('/api/system/plugins/install', headers=auth, files={'file': ('csv.cwp', raw)})
    assert installed.status_code == 200, installed.text
    restart()
    pid = manifest['id']
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth, json={'user_ids': [plain_user.id]}).status_code == 200
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role='ACCOUNTANT')); db.commit()
    leaves = db.scalars(select(Subject).where(Subject.book_id == book.id, Subject.active.is_(True), Subject.is_leaf.is_(True)).order_by(Subject.code).limit(2)).all()
    base = f'/api/plugins/{pid}'
    config_url = base + '/_host/config/' + manifest['tables'][0]['name'] + '/mapping'
    def put(version, value=None, target=None):
        return client.put(config_url, headers=plain_auth, params={'book_id': target or book.id}, json={'expected_version': version,
            'value': value or {'debit_code': leaves[0].code, 'credit_code': leaves[1].code}})
    def upload(content=b'date,amount,summary\n2026-03-02,12.50,CSV row\n2026-03-03,7.25,CSV row2\n'):
        response = client.post(base + '/_host/imports', params={'book_id': book.id}, headers=plain_auth,
                               files=[('files', ('example.csv', content, 'text/csv'))])
        assert response.status_code == 200, response.text
        return response.json()
    def parse(batch):
        return client.post(base + '/parse/' + batch['id'], headers=plain_auth, json={'revision': batch['revision']})
    return dict(base=base, config=config_url, put=put, upload=upload, parse=parse, pid=pid)


def test_example_configuration_cas_snapshot_and_atomic_commit(example, client, plain_auth, book, db):
    e = example
    initial = client.get(e['config'], params={'book_id': book.id}, headers=plain_auth).json()
    assert initial['version'] == 0 and initial['value'] == {}
    uploaded = e['upload']()
    assert e['parse'](uploaded).status_code == 422
    saved = e['put'](0)
    assert saved.status_code == 200, saved.text
    assert saved.json()['version'] == 1
    assert e['put'](0).status_code == 409
    invalid = client.put(e['config'], params={'book_id': book.id}, headers=plain_auth, json={'expected_version': True, 'value': {}})
    assert invalid.status_code == 422
    batch = e['parse'](uploaded).json()
    assert batch['status'] == 'PREVIEW_READY', batch
    assert batch['configuration']['refs'][0] == saved.json()
    assert batch['configuration']['locations'][1]['line'] == 3
    endpoint = e['base'] + '/_host/imports/' + batch['id']
    assert e['put'](1).status_code == 200
    stale = client.post(endpoint + '/confirm', headers=plain_auth, json={'revision': batch['revision']})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'IMPORT_CONFIGURATION_CHANGED'
    batch = e['parse'](batch).json()
    batch = client.post(endpoint + '/confirm', headers=plain_auth, json={'revision': batch['revision']}).json()
    assert e['put'](2).status_code == 200
    stale = client.post(endpoint + '/submit', headers=plain_auth, json={'revision': batch['revision']})
    assert stale.status_code == 409
    batch = e['parse'](batch).json()
    batch = client.post(endpoint + '/confirm', headers=plain_auth, json={'revision': batch['revision']}).json()
    response = client.post(endpoint + '/submit', headers=plain_auth, json={'revision': batch['revision']})
    assert response.status_code == 200, response.text
    batch = response.json()
    assert batch['status'] == 'SUBMITTED' and len(batch['result']) == 2
    origin = client.get(e['base'] + f"/_host/vouchers/{batch['result'][0]['id']}/origin", headers=plain_auth)
    assert origin.status_code == 200
    assert origin.json()['batch']['id'] == batch['id']
    assert origin.json()['batch']['files'][0]['sha256'] == batch['files'][0]['sha256']
    limits = client.get(e['base'] + '/_host/import-limits', headers=plain_auth).json()
    assert limits['max_files'] > 0 and limits['max_rows'] > 0


def test_configuration_is_book_scoped_and_cas_serializes_initial_insert(example, client, plain_auth, plain_user, book, db):
    e = example
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: e['put'](0), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    other = Book(company_name='其他公司', name='配置隔离账套', start_year=2026, start_month=1)
    db.add(other); db.flush(); db.add(UserBook(user_id=plain_user.id, book_id=other.id, role='VIEWER')); db.commit()
    config = client.get(e['config'], params={'book_id': other.id}, headers=plain_auth)
    assert config.status_code == 200 and config.json()['version'] == 0
    assert e['put'](0, target=other.id).status_code == 403
    denied = client.get(e['config'].replace('plugin_25ac53a1cf75_config', 'plugin_unknown_config'), params={'book_id': book.id}, headers=plain_auth)
    assert denied.status_code == 403
    link = db.scalar(select(UserBook).where(UserBook.user_id == plain_user.id, UserBook.book_id == book.id))
    link.role = 'VIEWER'; db.commit()
    assert e['put'](1).status_code == 403


@pytest.mark.parametrize('content,line', [
    (b'date,amount,summary\n2026-03-02,1.005,bad\n', 2),
    (b'date,amount,summary\n2026-02-30,1.00,bad\n', 2),
    (b'date,amount,summary\n2026-03-02,NaN,bad\n', 2),
    (b'date,amount\n2026-03-02,1.00\n', 1),
])
def test_csv_errors_include_file_and_row(example, content, line):
    assert example['put'](0).status_code == 200
    result = example['parse'](example['upload'](content))
    assert result.status_code == 422, result.text
    assert result.json()['detail']['file'] == 'example.csv'
    assert result.json()['detail']['line'] == line
