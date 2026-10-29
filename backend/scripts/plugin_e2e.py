#!/usr/bin/env python3
"""隔离数据库+临时测试密钥的真实双 worker / 浏览器演练。不会连接应用数据库。"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    database = make_url(os.environ.get('CWQ_PLUGIN_E2E_DATABASE_URL', 'postgresql+psycopg://cwq:cwq123@127.0.0.1:15432/cwq_plugin_browser_test'))
    if database.host not in {'127.0.0.1', 'localhost'} or not re.fullmatch(r'[a-z][a-z0-9_]*_plugin_browser_test', database.database or ''):
        raise SystemExit('只允许本机且名称以 _plugin_browser_test 结尾的隔离数据库')
    admin_engine = create_engine(database.set(database='postgres'), isolation_level='AUTOCOMMIT')
    with admin_engine.connect() as conn:
        if not conn.scalar(text('SELECT 1 FROM pg_database WHERE datname=:name'), {'name': database.database}):
            conn.execute(text('CREATE DATABASE "'+database.database+'"'))
    admin_engine.dispose()
    os.environ.update(APP_ENV='test', DATABASE_URL=database.render_as_string(hide_password=False),
                      DB_ECHO='false', JWT_SECRET='plugin-browser-isolated-secret-at-least-32-characters', PLUGIN_ENABLED='true')
    from app.core.db import engine, SessionLocal
    from app.core.schema_version import upgrade_to_head
    from app.core.security import hash_password
    from app.models import SysUser, UserBook, Book
    from app.accounting.periods import create_periods
    from app.accounting.years import ensure_book_year
    from app.seed.chart_of_accounts import install_chart
    from datetime import datetime, timezone
    from scripts.plugin_package import generate_key, build
    with engine.begin() as conn:
        conn.execute(text('DROP SCHEMA public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))
    upgrade_to_head(engine)
    password = 'Isolated-browser-test-2026'
    with SessionLocal.begin() as db:
        users = [SysUser(username=name, password_hash=hash_password(password), active=True, is_admin=name=='plugin-admin', real_name=name) for name in ['plugin-admin','plugin-staff','plugin-other']]
        db.add_all(users); db.flush()

        def make_book(name, sort_order):
            """造一本**空**账套（12 个期间 + 标准科目表）并授权给三个账号。

            共享卡那两本是**单独造的**：共享卡要断言「各账套加起来恰好等于
            这张卡」，账上多一分钱（别的场景留下的凭证）这条断言就不成立了 ——
            那种耦合会让演练变成「两个场景互相影响」的玄学。
            """
            b = Book(company_name='浏览器测试公司', name=name, start_year=2026, start_month=1)
            db.add(b); db.flush(); create_periods(db, b)
            install_chart(db, b.id)
            # 初始化状态挂在**年度**上（book_year，迁移 0032）
            by = ensure_book_year(db, b, 2026)
            by.init_subjects_at = datetime.now(timezone.utc); by.init_subjects_source = 'CHART'
            db.add_all([UserBook(user_id=user.id, book_id=b.id,
                                 role='OWNER' if user.is_admin else 'ACCOUNTANT',
                                 sort_order=sort_order) for user in users])
            return b.id

        book_id = make_book('插件隔离演练账套', 0)
        other_id = make_book('切换演练账套', 1)
        sharedcard_book_id = make_book('共享卡演练账套甲', 2)
        sharedcard_other_id = make_book('共享卡演练账套乙', 3)
    processes, handles = [], []
    with tempfile.TemporaryDirectory(prefix='cwq-plugin-browser-') as tmp:
        temporary = Path(tmp)
        keys = generate_key(temporary/'key.pem', 'browser-test')
        package_path = temporary/'example.cwp'
        normal = build(ROOT/'docs/plugin-example', temporary/'key.pem', 'browser-test', package_path)
        failed_source = temporary/'failed'
        shutil.copytree(ROOT/'docs/plugin-example', failed_source)
        manifest = json.loads((failed_source/'plugin.json').read_text())
        manifest.update(id='com.cwq.frontendfailure', name='前端降级演练', permissions=[], hooks=[], tables=[])
        manifest['contributes']['menu'].update(route='/plugin/com.cwq.frontendfailure', title='前端降级演练')
        (failed_source/'plugin.json').write_text(json.dumps(manifest))
        shutil.rmtree(failed_source/'migrations')
        (failed_source/'backend/main.py').write_text('def register(registrar, ctx):\n    def hello():\n        return {"ok": True}\n    registrar.get("/hello", hello)\n')
        (failed_source/'frontend/index.js').write_text('export default () => { throw new Error("expected frontend failure") };')
        failed_path = temporary/'failed.cwp'
        failed = build(failed_source, temporary/'key.pem', 'browser-test', failed_path)
        csv_path = temporary/'csv.cwp'
        csv_plugin = build(ROOT/'docs/plugin-import-example', temporary/'key.pem', 'browser-test', csv_path)
        # 共享卡对账：**真的那个插件包**（仓库 plugins/ 下的磁盘目录），
        # 不是另写一份演练用的替身 —— 浏览器里跑的那份必须是将来装到用户
        # 机器上的那一份。
        #
        # **插件源码不在 git 里**（/plugins/ 是 gitignore 的，见 .gitignore
        # 第 104 行）：干净 checkout 里没有它。那就**跳过这一段**，而不是让
        # 整条演练失败 —— 但要**大声**跳过：悄悄少跑一段场景比红着更坏
        # （红着会有人来看，悄悄少跑没人知道）。
        sharedcard_source = ROOT/'plugins/com.cwq.sharedcard'
        sharedcard_path = temporary/'sharedcard.cwp'
        sharedcard = None
        if sharedcard_source.is_dir():
            sharedcard = build(sharedcard_source, temporary/'key.pem', 'browser-test', sharedcard_path)
        else:
            print('· 跳过共享卡场景：干净 checkout 里没有 plugins/com.cwq.sharedcard'
                  '（插件不入库，见 .gitignore）—— 这段场景**没有跑**')
        api_port, web_port = free_port(), free_port()
        env = dict(os.environ, PLUGIN_ROOT=str(temporary/'plugins'), PLUGIN_TRUSTED_KEYS=json.dumps(keys), CWQ_API_WORKERS='2')
        state_file = temporary/'state.json'
        state_file.write_text(json.dumps({'url': f'http://127.0.0.1:{web_port}', 'password': password, 'bookId': book_id, 'otherBookId': other_id,
            # 共享卡**自己的**两本账：不与别的场景共用，余额断言才成立得了。
            'sharedcardBookId': sharedcard_book_id, 'sharedcardOtherBookId': sharedcard_other_id,
            # 对账单样例：真实的民生银行 Excel（5 行，收入支出都有）。
            'statementFixture': str(ROOT/'backend/tests/fixtures/statements/cmbc_xlsx.xlsx'),
            'packages': [{'id':normal['id'], 'path':str(package_path)}, {'id':failed['id'], 'path':str(failed_path)},
                         {'id':csv_plugin['id'], 'path':str(csv_path)}]
                         + ([{'id':sharedcard['id'], 'path':str(sharedcard_path)}] if sharedcard else [])}))
        def start_backend():
            log = (temporary/('backend-'+str(len(handles))+'.log')).open('w')
            handles.append(log)
            process = subprocess.Popen([sys.executable, 'scripts/start_server.py', '--port', str(api_port), '--workers','2'], cwd=ROOT/'backend', env=env, stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            wait_http(f'http://127.0.0.1:{api_port}/api/ready', process)
            return process
        try:
            api = start_backend()
            web = subprocess.Popen(['node', 'tools/uitest/plugin_server.mjs', str(web_port), f'http://127.0.0.1:{api_port}', str(ROOT/'frontend/dist')], cwd=ROOT, stdout=subprocess.DEVNULL)
            processes.append(web)
            wait_http(f'http://127.0.0.1:{web_port}/', web)
            subprocess.run(['node','tools/uitest/plugin_e2e.mjs','install',str(state_file)], cwd=ROOT, check=True)
            api.terminate(); api.wait(timeout=15)
            api = start_backend()
            subprocess.run([sys.executable,'scripts/plugin_status.py','--wait','20'], cwd=ROOT/'backend', env=env, check=True)
            subprocess.run(['node','tools/uitest/plugin_e2e.mjs','verify',str(state_file)], cwd=ROOT, check=True)
            verify_data_and_retained_uninstall(api, start_backend, api_port, env, temporary, book_id, password,
                                               normal['id'], failed['id'], csv_plugin['id'],
                                               sharedcard['id'] if sharedcard else None)
            print('PASS isolated signed-plugin browser E2E with production bundle/CSP and supervisor workers=2')
        except BaseException:
            for log in handles:
                log.flush()
                print(Path(log.name).read_text()[-7000:], file=sys.stderr)
            raise
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
            for handle in handles: handle.close()
            with engine.begin() as conn:
                conn.execute(text('DROP SCHEMA public CASCADE')); conn.execute(text('CREATE SCHEMA public'))
            engine.dispose()



def verify_data_and_retained_uninstall(api, start_backend, port, env, temporary, book_id, password, normal_id, failed_id, csv_id, sharedcard_id):
    """真实示例的恢复演练和保留表卸载；所有生命周期修改经过管理 API。"""
    import httpx
    url = f'http://127.0.0.1:{port}'
    with httpx.Client(base_url=url, timeout=30) as http:
        def login(name):
            response = http.post('/api/auth/login', json={'username': name, 'password': password})
            response.raise_for_status()
            return {'Authorization': 'Bearer '+response.json()['access_token']}
        admin, staff = login('plugin-admin'), login('plugin-staff')
        before = http.get(f'/api/plugins/{normal_id}/notes', params={'book_id': book_id}, headers=staff)
        before.raise_for_status()
        assert len(before.json()) == 1
        # 与正式恢复用同一导出、预检、FK 拓扑恢复和序列重置代码；只清除此隔离账套快照的行。
        restore_source = """
import sys
from pathlib import Path
from sqlalchemy import tuple_
from app.core.db import SessionLocal
from app.models.base import Base
from app.backup.export import export_scope, read_snapshot, restore, SCOPE_BOOK
from app.plugins.backup import validate_restore
path, book_id = Path(sys.argv[1]), int(sys.argv[2])
with SessionLocal() as db:
    export_scope(db, scope=SCOPE_BOOK, book_id=book_id, book_label='签名示例往返演练', out_path=path)
data = read_snapshot(path)
assert data['header']['version'] == 2
with SessionLocal.begin() as db:
    counts = restore(db, path, dry_run=True)
    plugin_tables = validate_restore(db.connection(), data, SCOPE_BOOK)
    ordered = list(Base.metadata.sorted_tables) + plugin_tables
    for table in reversed(ordered):
        payload = data['tables'].get(table.name)
        if not payload or not payload['rows']:
            continue
        pk = list(table.primary_key.columns)
        values = [tuple(r[c.name] for c in pk) for r in payload['rows']]
        db.execute(table.delete().where(tuple_(*pk).in_(values)))
    assert restore(db, path) == counts
print('PASS signed example backup v2 dry-run, FK restore and sequence reset')
"""
        subprocess.run([sys.executable, '-c', restore_source, str(temporary/'book.json.gz'), str(book_id)], cwd=ROOT/'backend', env=env, check=True)
        after = http.get(f'/api/plugins/{normal_id}/notes', params={'book_id': book_id}, headers=staff)
        assert after.status_code == 200 and after.json() == before.json(), after.text
        inserted = http.post(f'/api/plugins/{normal_id}/notes', json={'book_id':book_id,'label':'恢复后序列验证'}, headers=staff)
        assert inserted.status_code == 200, inserted.text
        assert inserted.json()['id'] > before.json()[0]['id']
        for pid in (normal_id, failed_id, csv_id, sharedcard_id):
            if not pid:
                continue
            response = http.post(f'/api/system/plugins/{pid}/disable', headers=admin)
            assert response.status_code == 200 and response.json()['status'] == 'pending_restart', response.text
        assert http.delete(f'/api/system/plugins/{normal_id}', headers=admin).status_code == 409
        assert http.get(f'/api/plugins/{normal_id}/notes', params={'book_id':book_id}, headers=staff).status_code == 503
    api.terminate()
    api.wait(timeout=15)
    start_backend()
    subprocess.run([sys.executable, 'scripts/plugin_status.py', '--wait', '20'], cwd=ROOT/'backend', env=env, check=True)
    with httpx.Client(base_url=url, headers=admin, timeout=30) as http:
        listed = http.get('/api/system/plugins').json()['plugins']
        assert all(p['runtime_status'] == 'DISABLED' for p in listed), listed
        deleted = http.delete(f'/api/system/plugins/{normal_id}')
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()['retained_tables'] and deleted.json()['backup_path']
        retained = http.get(f'/api/system/plugins/{normal_id}/tables')
        assert retained.status_code == 200 and retained.json()['tables'][0]['row_count'] == 2, retained.text
        tombstone = next(p for p in http.get('/api/system/plugins').json()['plugins'] if p['id']==normal_id)
        assert tombstone['deleted'] and tombstone['user_count']==0
        assert http.get(f'/api/plugins/{normal_id}/notes', params={'book_id':book_id}, headers=staff).status_code == 404
        reconciled = http.post('/api/system/plugins/reconcile')
        assert reconciled.status_code == 200, reconciled.text
    print('PASS disable requires full restart, old-token refusal, stopped barrier, verified backup, uninstall retains two rows and revokes users, reconcile')


def wait_http(url, process):
    import httpx
    deadline = time.monotonic()+30
    while time.monotonic()<deadline:
        if process.poll() is not None: raise RuntimeError('E2E subprocess exited')
        try:
            if httpx.get(url, timeout=2).status_code == 200: return
        except httpx.TransportError: pass
        time.sleep(.2)
    raise RuntimeError('E2E server readiness timeout')


if __name__ == '__main__':
    main()
