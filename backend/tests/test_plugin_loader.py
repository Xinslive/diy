import pytest
from sqlalchemy import event, text
from app.core.db import engine
from app.plugins.installer import install
from app.plugins.loader import Registrar
from app.plugins.errors import PluginError
from tests.plugin_fixtures import plugin_env
from tests.plugin_helpers import package


def test_registrar_rejects_host_route_request_dependencies_and_hooks():
    from fastapi import Depends, Request
    _, manifest, _, _ = package()
    r = Registrar(manifest)
    def good():
        return {'ok': True}
    def bad(request: Request):
        return {}
    def dependency(value=Depends(good)):
        return {}
    for path, fn in [('http://foreign', good), ('/../../host', good), ('/a', bad), ('/a', dependency), ('/_host', good), ('/_host/imports', good)]:
        with pytest.raises(PluginError):
            r.get(path, fn)
    r.get('/a', good)
    r.get('/b/a', good)
    with pytest.raises(PluginError):
        r.get('/a', good)
    with pytest.raises(PluginError):
        r.hook('unknown', good)


def test_good_bad_plugin_isolation_startup_has_no_ddl_and_exception_boundary(plugin_env, client, auth, admin_user):
    build, restart = plugin_env
    bad, bad_m, _, _ = build(files={'backend/main.py': b'def register(registrar, ctx):\n    raise ValueError("private source error")\n', 'frontend/index.js': b'export default host => ({render: () => host.h("div")});'})
    good, good_m, _, _ = build(files={'backend/main.py': b'def register(registrar, ctx):\n    def broken():\n        raise ValueError("secret database details")\n    registrar.get("/broken", broken)\n', 'frontend/index.js': b'export default host => ({render: () => host.h("div")});'})
    install(bad, admin_user); install(good, admin_user)
    statements = []
    def capture(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().split(' ', 1)[0].upper() in {'CREATE', 'ALTER', 'DROP'}:
            statements.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        restart()
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert statements == []
    for pid in (bad_m['id'], good_m['id']):
        client.post(f'/api/system/plugins/{pid}/users/{admin_user.id}', headers=auth)
    assert client.get(f'/api/plugins/{bad_m["id"]}/missing', headers=auth).status_code == 503
    r = client.get(f'/api/plugins/{good_m["id"]}/broken', headers=auth)
    assert r.status_code == 500
    assert 'secret' not in r.text
    assert r.json()['detail']['request_id'] == r.headers['x-request-id']
    assert r.headers['cache-control'] == 'private, no-store'
    assert client.get('/api/health').status_code == 200
