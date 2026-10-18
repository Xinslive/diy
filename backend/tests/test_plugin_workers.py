"""P0：两个独立 OS 进程，HTTP 请求不能因 worker 路由有无而间歇成功。"""
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from app.core.config import settings
from app.main import app
from app.plugins.installer import install
from tests.plugin_fixtures import plugin_env


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize('fail_second', [False, True])
def test_two_real_workers_barrier_and_committed_grant_revoke(plugin_env, client, auth, plain_auth, plain_user, admin_user, tmp_path, fail_second):
    build, _ = plugin_env
    raw, manifest, _, _ = build()
    pid = manifest['id']
    install(raw, admin_user)
    app.state.plugin_runtime.close()
    app.state.plugin_runtime = None
    generation, started = uuid.uuid4().hex, str(time.time_ns())
    shared = dict(os.environ, APP_ENV='test', PLUGIN_ENABLED='true', PLUGIN_ROOT=settings.plugin_root,
                  PLUGIN_TRUSTED_KEYS=settings.plugin_trusted_keys, CWQ_PLUGIN_GENERATION=generation,
                  CWQ_PLUGIN_STARTED_AT=started, CWQ_PLUGIN_EXPECTED_WORKERS='2',
                  PLUGIN_HEARTBEAT_SECONDS='1', PLUGIN_LEASE_SECONDS='4')
    processes, logs, urls = [], [], []
    try:
        for number in range(2):
            port = free_port()
            source = ''
            if fail_second and number == 1:
                source = 'import app.plugins.loader as loader\ndef broken(*args):\n    raise ValueError("injected worker initialization failure")\nloader.load_one = broken\n'
            source += f'import uvicorn\nuvicorn.run("app.main:app", host="127.0.0.1", port={port}, log_level="warning")\n'
            handle = (tmp_path / f'worker-{number}.log').open('w+')
            logs.append(handle)
            processes.append(subprocess.Popen([sys.executable, '-c', source], cwd=Path(__file__).resolve().parents[1], env=shared, stdout=handle, stderr=subprocess.STDOUT))
            urls.append(f'http://127.0.0.1:{port}')
        with httpx.Client(timeout=3) as http:
            deadline = time.monotonic() + 25
            for url in urls:
                while True:
                    try:
                        if http.get(url + '/api/ready').status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, 'worker failed to start: ' + '\n'.join(Path(h.name).read_text() for h in logs)
                    time.sleep(.1)
            # 安装者/管理员没有隐式使用授权；缺路由的坏 worker 也统一 404。
            for url in urls:
                r = http.get(url + f'/api/plugins/{pid}/hello', headers=auth)
                assert r.status_code == 404
                assert r.json()['detail']['code'] == 'PLUGIN_NOT_FOUND_OR_UNAVAILABLE'
            assert http.post(urls[0] + f'/api/system/plugins/{pid}/users/{plain_user.id}', headers=auth).status_code == 200
            expected = 503 if fail_second else 200
            for _ in range(3):
                for url in urls:
                    r = http.get(url + f'/api/plugins/{pid}/hello', headers=plain_auth)
                    assert r.status_code == expected, r.text
                    if fail_second:
                        assert r.json()['detail']['code'] == 'PLUGIN_UNAVAILABLE'
                    assert http.get(url + '/api/health').status_code == 200
                    assert http.get(url + '/api/ready').status_code == 200
            assert http.delete(urls[1] + f'/api/system/plugins/{pid}/users/{plain_user.id}', headers=auth).status_code == 200
            for url in urls:
                assert http.get(url + f'/api/plugins/{pid}/hello', headers=plain_auth).status_code == 404
                assert http.get(url + '/api/plugins/manifest', headers=plain_auth).json() == {'plugins': []}
            # 旧代次不能因新 cohort 已 READY 继续路由：独立单进程本地运行时的 generation 已过期。
            from app.core.db import SessionLocal
            from app.plugins.models import PluginRegistry
            from app.plugins.workers import status
            with SessionLocal() as db:
                assert status(db, db.get(PluginRegistry, pid), '0' * 32)[1] == 'STALE_GENERATION'
    finally:
        for proc in processes:
            proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        for handle in logs:
            handle.close()


def test_new_cohort_waits_until_all_old_worker_leases_close(plugin_env, admin_user, monkeypatch):
    from app.core.db import SessionLocal
    from app.plugins.models import PluginRegistry
    from app.plugins.workers import WorkerRuntime, status
    build, _ = plugin_env
    raw, manifest, _, _ = build()
    install(raw, admin_user)
    monkeypatch.setenv("CWQ_PLUGIN_EXPECTED_WORKERS", "2")
    old, new = [], []
    try:
        with SessionLocal() as db:
            plugin = db.get(PluginRegistry, manifest["id"])
            for _ in range(2):
                runtime = WorkerRuntime()
                runtime.join()
                runtime.register(plugin, "LOADED")
                old.append(runtime)
        with SessionLocal() as db:
            assert status(db, db.get(PluginRegistry, manifest["id"]), old[0].generation)[0] == "READY"
        monkeypatch.setenv("CWQ_PLUGIN_GENERATION", uuid.uuid4().hex)
        monkeypatch.setenv("CWQ_PLUGIN_STARTED_AT", str(time.time_ns()))
        with SessionLocal() as db:
            plugin = db.get(PluginRegistry, manifest["id"])
            for _ in range(2):
                runtime = WorkerRuntime()
                runtime.join()
                runtime.register(plugin, "LOADED")
                new.append(runtime)
        for number in range(2):
            with SessionLocal() as db:
                plugin = db.get(PluginRegistry, manifest["id"])
                assert status(db, plugin, old[number].generation) == ("UNAVAILABLE", "STALE_GENERATION")
                assert status(db, plugin, new[number].generation) == ("UNAVAILABLE", "OLD_WORKERS_ACTIVE")
            old[number].close()
        with SessionLocal() as db:
            assert status(db, db.get(PluginRegistry, manifest["id"]), new[0].generation)[0] == "READY"
    finally:
        for runtime in old + new:
            runtime.close()
