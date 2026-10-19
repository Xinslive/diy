"""使用真实签名安装、HTTP 网关、PostgreSQL 事务验证插件导入闭环。"""
import copy
import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import delete, func, select, update
from app.core.config import settings
from app.core.db import SessionLocal
from app.models import AuditLog, Book, Subject, UserBook, Voucher
from app.models.plugin import PluginRegistry
from app.models.plugin_import import PluginImportBatch as Batch, PluginImportFile as Source, PluginVoucherOrigin as Origin
from app.plugins.imports import now, purge_expired
from app.plugins.public_api import context_for
from tests.plugin_fixtures import plugin_env

CODE = b'''def register(registrar, ctx):
    def parse(ident: str, payload: dict):
        return ctx.imports.preview(ident, payload["vouchers"], revision=payload["revision"], configuration=payload.get("configuration"))
    registrar.post("/parse/{ident}", parse)
'''


@pytest.fixture
def importer(plugin_env, client, auth, plain_auth, admin_user, plain_user, book, db):
    build, restart = plugin_env
    pid = 'com.test.importer'
    raw, _, _, _ = build(plugin_id=pid, files={'backend/main.py': CODE, 'frontend/index.js': b'export default host => ({render: () => host.h("div")});'},
                          edit=lambda m: m.update(permissions=['book:read', 'import:rw', 'voucher:write']))
    r = client.post('/api/system/plugins/install', files={'file': ('importer.cwp', raw)}, headers=auth)
    assert r.status_code == 200, r.text
    restart()
    assert client.put(f'/api/system/plugins/{pid}/users', headers=auth, json={'user_ids': [plain_user.id, admin_user.id]}).status_code == 200
    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role='ACCOUNTANT'))
    db.commit()
    path = f'/api/plugins/{pid}'
    base = path + '/_host/imports'
    leaves = db.scalars(select(Subject).where(Subject.book_id == book.id, Subject.active.is_(True), Subject.is_leaf.is_(True)).order_by(Subject.code).limit(3)).all()
    def draft(amount='12.50', date='2026-03-02', debit=None):
        return {'voucher_date': date, 'remark': '文件转凭证', 'entries': [
            {'subject_id': debit or leaves[0].id, 'direction': 'DEBIT', 'amount': amount, 'summary': '来源第2行'},
            {'subject_id': leaves[1].id, 'direction': 'CREDIT', 'amount': amount, 'summary': '来源第2行'}]}
    def upload(content=b'account,amount\n1001,12.50', headers=None, target=None, extra=None):
        r = client.post(base, params={'book_id': target or book.id}, headers=headers or plain_auth,
                        files=[('files', ('../交易.csv', content, 'text/csv'))] + (extra or []))
        return r
    def parse(batch, vouchers=None):
        r = client.post(path + '/parse/' + batch['id'], headers=plain_auth, json={
            'revision': batch['revision'], 'vouchers': vouchers or [draft()], 'configuration': {'version': 3, 'account': '1001'}})
        assert r.status_code == 200, r.text
        return r.json()
    def confirm(batch):
        r = client.post(base + '/' + batch['id'] + '/confirm', headers=plain_auth, json={'revision': batch['revision']})
        assert r.status_code == 200, r.text
        return r.json()
    return dict(pid=pid, path=path, base=base, draft=draft, leaves=leaves, upload=upload, parse=parse, confirm=confirm)


def count(db, model, book):
    return db.scalar(select(func.count()).select_from(model).where(model.book_id == book.id))


def test_import_preview_confirmation_atomic_result_and_download(importer, client, plain_auth, book, db):
    imp = importer
    uploaded = imp['upload']()
    assert uploaded.status_code == 200, uploaded.text
    batch = uploaded.json()
    assert batch['status'] == 'UPLOADED'
    assert batch['book_id'] == book.id
    file = batch['files'][0]
    assert file['filename'] == '交易.csv'
    assert file['sha256'] == hashlib.sha256(b'account,amount\n1001,12.50').hexdigest()
    downloaded = client.get(imp['base'] + f"/{batch['id']}/files/{file['id']}", headers=plain_auth)
    assert downloaded.content == b'account,amount\n1001,12.50'
    assert 'attachment;' in downloaded.headers['content-disposition']
    assert downloaded.headers['x-content-type-options'] == 'nosniff'
    assert downloaded.headers['cache-control'] == 'private, no-store'
    # 不能绕过解析和人工确认直接提交。
    submit = imp['base'] + '/' + batch['id'] + '/submit'
    assert client.post(submit, headers=plain_auth, json={'revision': 1}).status_code == 409
    assert client.put(imp['base'] + '/' + batch['id'] + '/preview', headers=plain_auth,
                      json={'revision': 1, 'vouchers': [imp['draft']()]}).status_code == 409
    batch = imp['parse'](batch, [imp['draft'](), imp['draft']('7.25')])
    assert batch['status'] == 'PREVIEW_READY' and not batch['validation_errors']
    assert count(db, Voucher, book) == 0
    changed = copy.deepcopy(batch['preview'])
    changed[0]['remark'] = '人工调整摘要'
    edited = client.put(imp['base'] + '/' + batch['id'] + '/preview', headers=plain_auth,
                         json={'revision': batch['revision'], 'vouchers': changed})
    assert edited.status_code == 200, edited.text
    batch = edited.json()
    assert batch['status'] == 'EDITING'
    assert batch['original_preview'][0]['remark'] == '文件转凭证'
    assert batch['preview'][0]['remark'] == '人工调整摘要'
    # 页面刷新、重新请求后仍有同一份草稿与配置快照。
    assert client.get(imp['base'] + '/' + batch['id'], headers=plain_auth).json()['preview'] == batch['preview']
    batch = imp['confirm'](batch)
    r = client.post(submit, headers=plain_auth, json={'revision': batch['revision']})
    assert r.status_code == 200, r.text
    done = r.json()
    assert done['status'] == 'SUBMITTED'
    assert len(done['result']) == 2 and len({v['id'] for v in done['result']}) == 2
    assert count(db, Voucher, book) == 2
    assert count(db, Origin, book) == 2
    # 网络超时重发旧 revision，仍返回第一次的结果。
    assert client.post(submit, headers=plain_auth, json={'revision': batch['revision']}).json()['result'] == done['result']
    assert client.put(imp['base'] + '/' + batch['id'] + '/preview', headers=plain_auth,
                      json={'revision': done['revision'], 'vouchers': changed}).status_code == 409
    assert imp['upload']().json()['id'] == done['id']
    rows = db.scalars(select(AuditLog).where(AuditLog.book_id == book.id, AuditLog.action == 'plugin.import.submitted')).all()
    assert len(rows) == 1
    assert rows[0].after['configuration_digest'] == done['configuration_digest']
    assert rows[0].after['plugin_digest'] == done['plugin_digest']
    assert rows[0].after['vouchers'] == done['result']


def test_concurrent_submit_creates_exactly_one_batch(importer, client, plain_auth, book, db):
    imp = importer
    batch = imp['confirm'](imp['parse'](imp['upload']().json()))
    url = imp['base'] + '/' + batch['id'] + '/submit'
    def submit(_):
        return client.post(url, headers=plain_auth, json={'revision': batch['revision']})
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(submit, range(4)))
    assert [r.status_code for r in responses] == [200] * 4, [r.text for r in responses]
    assert all(r.json()['result'] == responses[0].json()['result'] for r in responses)
    assert count(db, Voucher, book) == count(db, Origin, book) == 1


def test_late_failure_rolls_back_vouchers_audit_and_origins_and_can_retry(importer, client, plain_auth, book, db):
    imp = importer
    batch = imp['parse'](imp['upload']().json(), [imp['draft'](), imp['draft'](debit=imp['leaves'][2].id)])
    batch = imp['confirm'](batch)
    # 预览/确认之后第二张凭证的科目停用，第一张必须跟着回滚。
    third = imp['leaves'][2]
    third.active = False
    db.commit()
    url = imp['base'] + '/' + batch['id']
    r = client.post(url + '/submit', headers=plain_auth, json={'revision': batch['revision']})
    assert r.status_code == 422, r.text
    assert r.json()['detail']['voucher'] == 2
    assert count(db, Voucher, book) == count(db, Origin, book) == 0
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.book_id == book.id, AuditLog.entity == 'voucher')) == 0
    failed = client.get(url, headers=plain_auth).json()
    assert failed['status'] == 'FAILED' and failed['validation_errors']
    assert client.post(url + '/submit', headers=plain_auth, json={'revision': failed['revision']}).status_code == 409
    third.active = True
    db.commit()
    ready = imp['confirm'](imp['parse'](failed))
    assert client.post(url + '/submit', headers=plain_auth, json={'revision': ready['revision']}).status_code == 200
    assert count(db, Voucher, book) == 1


def test_owner_book_permission_and_plugin_authorization_are_live(importer, client, auth, plain_auth, admin_user, plain_user, book, db):
    imp = importer
    batch = imp['parse'](imp['upload']().json())
    url = imp['base'] + '/' + batch['id']
    # 同账套 OWNER 和管理员身份不能读取另一位用户的批次/原文件。
    assert client.get(url, headers=auth).status_code == 404
    assert client.get(url + '/files/' + batch['files'][0]['id'], headers=auth).status_code == 404
    assert imp['upload'](headers=auth).status_code == 409
    assert client.get(imp['base'], params={'book_id': book.id}, headers=auth).json()['items'] == []
    assert imp['upload'](target=book.id + 100000).status_code == 403
    link = db.scalar(select(UserBook).where(UserBook.user_id == plain_user.id, UserBook.book_id == book.id))
    link.role = 'VIEWER'
    db.commit()
    assert client.get(url, headers=plain_auth).status_code == 200
    assert client.post(url + '/confirm', headers=plain_auth, json={'revision': batch['revision']}).status_code == 403
    assert imp['upload']().status_code == 403
    link.role = 'ACCOUNTANT'
    db.commit()
    batch = imp['confirm'](batch)
    # 撤销账套权限后确认记录不会变成永久写权限。
    db.delete(link)
    db.commit()
    assert client.post(url + '/submit', headers=plain_auth, json={'revision': batch['revision']}).status_code == 403
    assert count(db, Voucher, book) == 0
    assert client.put(f"/api/system/plugins/{imp['pid']}/users", headers=auth, json={'user_ids': [admin_user.id]}).status_code == 200
    assert client.get(url, headers=plain_auth).status_code == 404


@pytest.mark.parametrize('problem', ['unbalanced', 'inactive', 'parent', 'period', 'schema', 'number', 'negative_attachments'])
def test_invalid_previews_persist_errors_without_formal_vouchers(importer, client, plain_auth, db, book, problem):
    imp = importer
    draft = imp['draft']()
    if problem == 'unbalanced':
        draft['entries'][1]['amount'] = '1.00'
    elif problem == 'inactive':
        imp['leaves'][0].active = False
        db.commit()
    elif problem == 'parent':
        draft['entries'][0]['subject_id'] = db.scalar(select(Subject.id).where(Subject.book_id == book.id, Subject.is_leaf.is_(False)).limit(1))
    elif problem == 'period':
        draft['voucher_date'] = '1999-01-01'
    elif problem == 'schema':
        draft['entries'][0]['amount'] = 'bad'
    elif problem == 'number':
        draft['voucher_no'] = '记9999'
    else:
        draft['attachments'] = -1
    batch = imp['parse'](imp['upload']().json(), [draft])
    assert batch['status'] == 'EDITING' and batch['validation_errors'][0]['voucher'] == 1
    if problem == 'schema':
        assert batch['validation_errors'][0]['line'] == 1
    assert count(db, Voucher, book) == 0
    assert client.post(imp['base'] + '/' + batch['id'] + '/confirm', headers=plain_auth,
                       json={'revision': batch['revision']}).status_code == 422


def test_revision_reconfirmation_and_plugin_upgrade(importer, client, plain_auth, db):
    imp = importer
    batch = imp['confirm'](imp['parse'](imp['upload']().json()))
    url = imp['base'] + '/' + batch['id']
    # 编辑撤销人工确认；陈旧页面不允许覆盖更新。
    edit = client.put(url + '/preview', headers=plain_auth, json={'revision': batch['revision'], 'vouchers': [imp['draft']('4.00')]})
    assert edit.status_code == 200
    assert edit.json()['confirmed_at'] is None
    assert client.put(url + '/preview', headers=plain_auth, json={'revision': batch['revision'], 'vouchers': [imp['draft']()]}).status_code == 409
    assert client.post(url + '/submit', headers=plain_auth, json={'revision': edit.json()['revision']}).status_code == 409
    # 模拟插件升级后的元数据变化，不能把人工编辑谎称为新版本解析。
    # 直接测业务契约，网关的 digest/barrier 升级流程另有生命周期集成测试。
    from app.plugins.imports import check_current_version
    plugin = db.get(PluginRegistry, imp['pid'])
    plugin.plugin_digest = '0' * 64
    db.flush()
    saved = db.get(Batch, batch['id'])
    with pytest.raises(HTTPException) as exc:
        check_current_version(db, saved)
    assert exc.value.status_code == 409
    db.rollback()


def test_multiple_files_capacity_cancel_expiry_and_retained_evidence(importer, client, plain_auth, monkeypatch, db):
    imp = importer
    monkeypatch.setattr(settings, 'import_max_bytes', 32)
    too_big = imp['upload'](content=b'a' * 33)
    assert too_big.status_code == 413
    duplicate = imp['upload'](content=b'a', extra=[('files', ('copy.txt', b'a', 'text/plain'))])
    assert duplicate.status_code == 400
    assert imp['upload'](content=b'a' * 20, extra=[('files', ('other.txt', b'b' * 20, 'text/plain'))]).status_code == 413
    batch = imp['upload'](content=b'a', extra=[('files', ('other.txt', b'b', 'text/plain'))]).json()
    assert len(batch['files']) == 2
    url = imp['base'] + '/' + batch['id']
    cancelled = client.post(url + '/cancel', headers=plain_auth, json={'revision': batch['revision']})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()['status'] == 'CANCELLED'
    assert client.get(url + '/files/' + batch['files'][0]['id'], headers=plain_auth).status_code == 410
    again = imp['upload'](content=b'a', extra=[('files', ('other.txt', b'b', 'text/plain'))]).json()
    assert again['id'] != batch['id']
    saved = db.get(Batch, again['id'])
    saved.expires_at = now() - timedelta(seconds=1)
    db.commit()
    assert client.get(imp['base'] + '/' + again['id'], headers=plain_auth).status_code == 410
    with SessionLocal.begin() as cleanup:
        assert purge_expired(cleanup) == 2
    db.expire_all()
    saved = db.get(Batch, again['id'])
    assert saved.status == 'CANCELLED' and saved.fingerprint
    assert db.get(Source, again['files'][0]['id']).content is None


def test_declared_and_actual_upload_limits_and_no_request_authentication(monkeypatch):
    from app.plugins.uploads import read_file
    from tests.plugin_helpers import package
    _, manifest, _, _ = package(edit=lambda m: m.update(permissions=['upload:read']))
    file = UploadFile(io.BytesIO(b'abcde'), filename='x.txt', size=None)
    with pytest.raises(HTTPException) as denied:
        context_for(manifest).upload_read(file)
    assert denied.value.status_code == 403
    # 不信任 size=None/0，必须真正按 cap+1 读取并发现超限。
    monkeypatch.setattr(settings, 'import_max_bytes', 4)
    for declared in (None, 0, 5):
        with pytest.raises(HTTPException) as large:
            read_file(UploadFile(io.BytesIO(b'abcde'), filename='x.txt', size=declared))
        assert large.value.status_code == 413
    monkeypatch.setattr(settings, 'import_max_bytes', 1024)
    monkeypatch.setattr(settings, 'import_max_unzipped_bytes', 4)
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('a.csv', 'abcde')
    with pytest.raises(HTTPException) as large:
        read_file(UploadFile(io.BytesIO(zipped.getvalue()), filename='x.zip'))
    assert large.value.status_code == 413


def test_source_deduplication_prevents_subset_overlap(importer):
    imp = importer
    assert imp['upload'](content=b'a', extra=[('files', ('b.csv', b'b', 'text/csv'))]).status_code == 200
    response = imp['upload'](content=b'a')
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'IMPORT_FILE_OVERLAP'


def test_unexpected_interruption_rolls_back_and_leaves_confirmed_batch_retryable(importer, client, plain_auth, db, book, monkeypatch):
    from app.accounting import vouchers as svc
    imp = importer
    batch = imp['confirm'](imp['parse'](imp['upload']().json(), [imp['draft'](), imp['draft']()]))
    create = svc.create_voucher
    calls = 0
    def crash(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('simulated worker interruption')
        return create(*args, **kwargs)
    monkeypatch.setattr(svc, 'create_voucher', crash)
    url = imp['base'] + '/' + batch['id']
    response = client.post(url + '/submit', headers=plain_auth, json={'revision': batch['revision']})
    assert response.status_code == 500
    assert count(db, Voucher, book) == count(db, Origin, book) == 0
    current = client.get(url, headers=plain_auth).json()
    assert current['status'] == 'CONFIRMED' and current['revision'] == batch['revision']
    monkeypatch.setattr(svc, 'create_voucher', create)
    assert client.post(url + '/submit', headers=plain_auth, json={'revision': batch['revision']}).status_code == 200
    assert count(db, Voucher, book) == 2


def test_capacity_limits_are_enforced_before_batch_creation(importer, monkeypatch):
    imp = importer
    monkeypatch.setattr(settings, 'plugin_import_max_files', 1)
    assert imp['upload'](content=b'a', extra=[('files', ('b.csv', b'b', 'text/csv'))]).status_code == 400
    monkeypatch.setattr(settings, 'plugin_import_max_pending', 1)
    assert imp['upload'](content=b'a').status_code == 200
    assert imp['upload'](content=b'b').status_code == 409
    monkeypatch.setattr(settings, 'plugin_import_max_pending', 10)
    monkeypatch.setattr(settings, 'plugin_import_storage_max_bytes', 1)
    assert imp['upload'](content=b'b').status_code == 413


def test_source_and_origins_roundtrip_in_book_backup(importer, client, plain_auth, db, book, tmp_path):
    from app.backup.export import SCOPE_BOOK, export_scope, read_snapshot, restore
    from app.models.base import Base
    from sqlalchemy import tuple_
    imp = importer
    batch = imp['confirm'](imp['parse'](imp['upload']().json()))
    result = client.post(imp['base'] + '/' + batch['id'] + '/submit', headers=plain_auth, json={'revision': batch['revision']})
    assert result.status_code == 200, result.text
    out = tmp_path / 'plugin-import.gz'
    # 在独立短会话导出；随后删除归档对应的本账套行并做真实写入恢复。
    with SessionLocal() as snapshot_db:
        export_scope(snapshot_db, scope=SCOPE_BOOK, book_id=book.id, book_label=book.name, out_path=out)
    snapshot = read_snapshot(out)
    assert snapshot['tables']['plugin_import_file']['rows'][0]['content']
    assert snapshot['tables']['plugin_voucher_origin']['rows'][0]['batch_id'] == batch['id']
    with SessionLocal.begin() as remove:
        for table in reversed(Base.metadata.sorted_tables):
            payload = snapshot['tables'].get(table.name)
            if not payload or not payload['rows']:
                continue
            pk = list(table.primary_key.columns)
            values = [tuple(row[column.name] for column in pk) for row in payload['rows']]
            remove.execute(table.delete().where(tuple_(*pk).in_(values)))
    with SessionLocal.begin() as restore_db:
        counts = restore(restore_db, out)
    assert counts['plugin_import_batch'] == counts['plugin_import_file'] == counts['plugin_voucher_origin'] == 1
    url = imp['base'] + '/' + batch['id']
    recovered = client.get(url, headers=plain_auth)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['result'] == result.json()['result']
    downloaded = client.get(url + '/files/' + batch['files'][0]['id'], headers=plain_auth)
    assert downloaded.content == b'account,amount\n1001,12.50'
