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


def test_registrar_treats_bare_dict_payload_as_request_body():
    """SDK 约定 ``def save_note(payload: dict)`` 里的 payload 是**请求体**。

    FastAPI 只按注解决定参数去处：裸 ``dict`` 又没写 ``Body()`` 的参数会被当成
    查询参数，于是插件前端用 JSON body 提交的数据收不到，接口一律回 422
    ``Field required``。文档、插件客户端、插件三方约定一致，路由装配不能掉链子。
    """
    _, manifest, _, _ = package()
    r = Registrar(manifest)

    def save_note(payload: dict):
        return payload

    def list_notes(book_id: int):
        return {'book_id': book_id}

    r.post('/notes', save_note)
    r.get('/charts', list_notes)
    post_route = next(x for x in r._router.routes if 'POST' in x.methods)
    get_route = next(x for x in r._router.routes if 'GET' in x.methods)
    assert [p.name for p in post_route.dependant.body_params] == ['payload']
    assert post_route.dependant.query_params == []
    # 标量参数仍然是查询参数：GET 接口靠它带 book_id，不能顺手也改成 body
    assert [p.name for p in get_route.dependant.query_params] == ['book_id']
    assert get_route.dependant.body_params == []


def test_body_payload_wrapper_keeps_original_handler_intact():
    """套壳只改签名，不改也不替换插件自己的函数对象。"""
    from app.plugins.loader import _with_body_payload

    def save_note(payload: dict):
        return {'got': payload}

    wrapper = _with_body_payload(save_note)
    assert wrapper is not save_note
    assert wrapper({'a': 1}) == {'got': {'a': 1}}
    # 原函数的签名一个都没动，否则 _check 验过的约束就被绕过去了
    import inspect as pyinspect
    params = pyinspect.signature(save_note).parameters
    assert params['payload'].default is pyinspect.Parameter.empty

    def list_notes(book_id: int):
        return book_id

    assert _with_body_payload(list_notes) is list_notes


def test_registrar_accepts_only_declared_sync_task_handler():
    manifest = {"id": "com.example.tasks", "tasks": [
        {"name": "hourly", "schedule": {"type": "interval", "minutes": 60}}
    ]}
    registrar = Registrar(manifest)

    def handler(context):
        return context

    async def async_handler(context):
        return context

    def two_args(context, other):
        return context, other

    with pytest.raises(PluginError):
        registrar.task("missing", handler)
    with pytest.raises(PluginError):
        registrar.task("hourly", async_handler)
    with pytest.raises(PluginError):
        registrar.task("hourly", two_args)
    registrar.task("hourly", handler)
    with pytest.raises(PluginError):
        registrar.task("hourly", handler)


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
