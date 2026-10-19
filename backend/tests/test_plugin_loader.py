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
    assert len(post_route.dependant.body_params) == 1
    assert post_route.dependant.query_params == []
    # 标量参数在 GET 上仍然是查询参数：GET 接口靠它带 book_id，不能顺手也改成 body
    assert [p.name for p in get_route.dependant.query_params] == ['book_id']
    assert get_route.dependant.body_params == []


def test_registrar_takes_scalar_post_params_from_body_too():
    """插件实际写法是 ``def save_rule(book_id: int, payload: dict)``。

    ``book_id`` 这种没写 ``Body()`` 的标量参数会被 FastAPI 判成**必填查询参数**，
    于是前端 JSON body 里的 book_id 永远收不到，POST 一律回 422 ``Field required``。
    只修 dict 参数不够：整个签名都得改判为请求体。
    """
    _, manifest, _, _ = package()
    r = Registrar(manifest)

    def save_rule(book_id: int, payload: dict):
        return {'book_id': book_id, 'payload': payload}

    r.post('/rules', save_rule)
    route = next(x for x in r._router.routes if 'POST' in x.methods)
    # book_id 仍在查询串上，但已是**可省**的：从前端 JSON body 提交时不该再 422
    assert [p.name for p in route.dependant.query_params] == ['book_id']
    # 整个请求体由 payload 接住
    assert [p.name for p in route.dependant.body_params] == ['payload']

    # 分发：book_id 从 body 取同名键，payload 收整个 body
    wrapped = route.endpoint
    assert wrapped(payload={'book_id': 3, 'name': '房租'}, book_id=None) == {
        'book_id': 3, 'payload': {'book_id': 3, 'name': '房租'}}


def test_registrar_keeps_path_params_and_reports_missing_body_field():
    """删除类路由形如 ``/rules/{rule_id}/delete``：rule_id 仍从路径取。

    包装不能把路径参数也改成 body；同时 body 里真的没带的必填字段要给出中文
    提示，而不是把 None 塞进插件函数、让它在半路炸成 500。
    """
    from fastapi import HTTPException
    from app.plugins.loader import _BODY_ANCHOR, _with_body_payload
    _, manifest, _, _ = package()
    r = Registrar(manifest)

    def delete_rule(book_id: int, rule_id: int):
        return {'book_id': book_id, 'rule_id': rule_id}

    r.post('/rules/{rule_id}/delete', delete_rule)
    route = next(x for x in r._router.routes if 'POST' in x.methods)
    assert [p.name for p in route.dependant.path_params] == ['rule_id']
    assert [p.name for p in route.dependant.query_params] == ['book_id']
    assert [p.name for p in route.dependant.body_params] == [_BODY_ANCHOR]

    # FastAPI 只会把路径参数、查询串和整体请求体传进来，分发要在这三者之间做对
    wrapped = _with_body_payload(delete_rule, '/rules/{rule_id}/delete', ['POST'])
    assert wrapped(rule_id=7, **{_BODY_ANCHOR: {'book_id': 3}}) == {'book_id': 3, 'rule_id': 7}
    # 旧写法：book_id 走查询串也照样有效
    assert wrapped(rule_id=7, book_id=3, **{_BODY_ANCHOR: {'book_id': 999}}) == {
        'book_id': 3, 'rule_id': 7}
    # body 里没有 book_id → 中文 422，且不进入插件函数
    with pytest.raises(HTTPException) as excinfo:
        wrapped(rule_id=7, **{_BODY_ANCHOR: {}})
    assert excinfo.value.status_code == 422
    assert 'book_id' in str(excinfo.value.detail)

    # GET 路由的标量参数仍是查询参数，不进 body
    assert _with_body_payload(delete_rule, '/rules', ['GET']) is delete_rule


def test_body_payload_wrapper_keeps_original_handler_intact():
    """套壳只改签名，不改也不替换插件自己的函数对象。"""
    from app.plugins.loader import _with_body_payload

    def save_note(payload: dict):
        return {'got': payload}

    wrapper = _with_body_payload(save_note, '/notes', ['POST'])
    assert wrapper is not save_note
    # 文档写法里 payload 就是整个请求体
    assert wrapper(payload={'a': 1}) == {'got': {'a': 1}}
    # 原函数的签名一个都没动，否则 _check 验过的约束就被绕过去了
    import inspect as pyinspect
    params = pyinspect.signature(save_note).parameters
    assert params['payload'].default is pyinspect.Parameter.empty

    def list_notes(book_id: int):
        return book_id

    assert _with_body_payload(list_notes, '/charts', ['GET']) is list_notes


def test_post_route_receives_json_body_end_to_end(plugin_env, client, auth, admin_user):
    """端到端：插件前端 JSON body 里的字段必须真的到达插件处理函数。

    生产上「添加规则」一律 422 ``Field required``，根因就在路由装配这一步只看
    注解、不看插件客户端怎么提交。签名层断言证明不了 FastAPI 真的会这样解析，
    所以这里真发一个 JSON 请求，看插件函数收到什么。
    """
    build, restart = plugin_env
    backend = b'''def register(registrar, ctx):
    def save_rule(book_id: int, payload: dict):
        return {"book_id": book_id, "name": payload.get("name"), "fields": sorted(payload)}

    def delete_rule(book_id: int, rule_id: int):
        return {"book_id": book_id, "rule_id": rule_id}

    registrar.post("/rules", save_rule)
    registrar.post("/rules/{rule_id}/delete", delete_rule)
'''
    raw, manifest, _, _ = build(files={
        "backend/main.py": backend,
        "frontend/index.js": b'export default (host) => ({render: () => host.h("div")});\n'})
    install(raw, admin_user)
    restart()
    pid = manifest['id']
    client.post(f'/api/system/plugins/{pid}/users/{admin_user.id}', headers=auth)

    saved = client.post(f"/api/plugins/{pid}/rules",
                        json={"book_id": 1, "name": "房租"}, headers=auth)
    assert saved.status_code == 200, saved.text
    assert saved.json()["book_id"] == 1
    assert saved.json()["name"] == "房租"
    assert saved.json()["fields"] == ["book_id", "name"]

    deleted = client.post(f"/api/plugins/{pid}/rules/7/delete",
                          json={"book_id": 1}, headers=auth)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"book_id": 1, "rule_id": 7}


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
