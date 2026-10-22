"""应用入口的对外暴露面与错误回法（审计 P1-7 / P2-24 / P3-14）。

三件事都属于「入口」这一层，一起钉：

* **P1-7** 生产暴露 Swagger UI 与 OpenAPI。``/openapi.json`` 是一份完整的
  路由表 + 请求模型 + 字段约束，等于把「有哪些入口、各自要什么」交给未授权方。
  而 ``app_env`` 默认就是 ``prod`` —— 默认值必须自己站在关闭那一侧。
* **P2-24** 护栏 4xx 零日志、``str(orig)[:300]`` 回给客户端、无 request id。
  护栏被绕过是最该留痕的路径之一，而它当时一条日志都不产生。
* **P3-14** 日志级别依赖外部环境。root 上没有 handler 时 logging 兜底是
  WARNING，于是 INFO 级日志被整个丢掉 —— 「护栏没打日志」有一半是这个原因。
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, detect_env


def _app_for(env: str) -> FastAPI:
    """按某个环境的口径造一个**真的**应用出来，而不是只测判定函数。"""
    from app.main import api_docs_urls

    return FastAPI(title="t", **api_docs_urls(env))


# --------------------------------------------------------------------- P1-7


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_生产环境不暴露接口文档(path):
    """HTTP 层验证，不是只看变量为 None。"""
    r = TestClient(_app_for("prod")).get(path)
    assert r.status_code == 404, f"生产环境 {path} 返回 {r.status_code}，等于对外公开"


@pytest.mark.parametrize("env", ["dev", "test"])
def test_开发与测试环境保留接口文档(env):
    """开发要留着 Swagger，否则每次改路由都要手写 curl。"""
    c = TestClient(_app_for(env))
    assert c.get("/docs").status_code == 200
    assert c.get("/openapi.json").status_code == 200


def test_默认判定就是关():
    """没显式声明环境时按 prod 处理 —— 与 app_env 的 fail-closed 口径一致。"""
    from app.main import api_docs_urls

    s = Settings(jwt_secret="a" * 48, app_env="")
    assert detect_env(s) == "prod"
    assert api_docs_urls("prod") == {"docs_url": None, "redoc_url": None, "openapi_url": None}


def test_实际跑着的app口径一致(client):
    """运行中的应用与判定函数必须给同一个答案，否则文档会「自己在某个环境下开着」。"""
    from app.main import api_docs_urls, app

    expected = api_docs_urls()
    assert app.docs_url == expected["docs_url"]
    assert app.openapi_url == expected["openapi_url"]


# --------------------------------------------------------------------- P2-24


def test_每个响应都带请求号(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    rid = r.headers.get("X-Request-ID")
    assert rid, "响应里没有请求号 —— 用户报错时对不上日志是哪一行"
    assert 8 <= len(rid) <= 64


def test_外部请求号原样透传(client):
    """前端 / 网关 / 采集链路已经把同一条链路串起来了，后端另起一个号就对不上。"""
    r = client.get("/api/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["X-Request-ID"] == "trace-abc-123"


@pytest.mark.parametrize("bad", ["a" * 200, "bad id", "trace\r\nX-Injected: 1"])
def test_乱来的请求号不被照抄(client, bad):
    """这个头是给人读、给人搜、可能被人抄进工单的，不能变成日志注入面。"""
    r = client.get("/api/health", headers={"X-Request-ID": bad})
    assert r.headers["X-Request-ID"] != bad


def _dbapi_error(sqlstate: str, message: str):
    """造一个 psycopg 在护栏触发时会抛的同形状异常。

    真实护栏（``db_init.sql`` 里的 ``RAISE EXCEPTION ... USING ERRCODE``）
    在应用层被预校验挡住的地方不会走到 handler，所以这里注入同形状的异常
    来验证 handler 本身：sqlstate、diag.message_primary、回给客户端的内容、
    有没有写日志 —— 这四件事全在 handler 里。
    """
    import types

    from sqlalchemy.exc import DBAPIError

    orig = type("Orig", (Exception,), {})()
    orig.sqlstate = sqlstate
    orig.diag = types.SimpleNamespace(message_primary=message)
    return DBAPIError("SELECT 1", {}, orig)


def test_护栏拒绝要写日志(client, auth, monkeypatch, caplog):
    """**护栏被绕过是最该留痕的路径**：过去它一条日志都不产生，
    唯一能看见的痕迹是用户截图里的那句报错。"""
    import app.api.system as system_mod

    monkeypatch.setattr(system_mod, "_links_by_user", lambda db: (_ for _ in ()).throw(
        _dbapi_error("23514", "凭证 记001 已记账，不能删除。请先反记账再删")))

    with caplog.at_level(logging.WARNING, logger="cwq"):
        r = client.get("/api/system/users", headers=auth)

    assert r.status_code == 400, r.text
    texts = " ".join(x.getMessage() for x in caplog.records)
    assert "约束拒绝" in texts, f"护栏拒绝没有写日志：{caplog.records}"
    assert "sqlstate=23514" in texts
    assert "/api/system/users" in texts, "日志里没有请求路径"
    assert r.headers.get("X-Request-ID")


def test_护栏4xx仍然回中文提示(client, auth, monkeypatch):
    """脱敏不能把「你这次请求不合法」的提示也一起脱掉 —— 那是给人看的文案
    （``RAISE EXCEPTION`` 里的原文），不是数据库内部结构。"""
    import app.api.system as system_mod

    monkeypatch.setattr(system_mod, "_links_by_user", lambda db: (_ for _ in ()).throw(
        _dbapi_error("23514", "凭证 记001 已记账，不能删除。请先反记账再删")))

    r = client.get("/api/system/users", headers=auth)
    assert r.status_code == 400
    assert "反记账" in r.json()["detail"], "400 的中文提示没了，用户只能看到一个空 detail"


def test_400响应也带请求号(client, book):
    """应用层的拒绝（鉴权、校验、护栏）同样要能被报障定位。"""
    r = client.get(f"/api/books/{book.id}/subjects", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert r.headers.get("X-Request-ID")



def test_500不回数据库原始文本(client, book, auth, plain_user, monkeypatch):
    """``str(orig)`` 里是表名、列名、约束名，有时还带数据片段 ——
    回给调用方等于把库结构抄送给未授权方。"""
    from sqlalchemy.exc import DBAPIError

    class _Orig(Exception):
        sqlstate = "58030"

    def boom(*a, **kw):
        raise DBAPIError("UPDATE sys_user_book", {}, _Orig("表 sys_user_book 的键值重复: 张三, secret-hash-abc"))

    monkeypatch.setattr("app.api.system._apply_links", boom)
    # 目标是**真实存在**的用户：接口里先 ``db.get(SysUser, user_id)``，
    # 人不存在就 404 返回了，压根走不到 _apply_links，测的就不是「500 之后
    # 怎么回包」而是「404 怎么回包」了。
    r = client.put(f"/api/system/users/{plain_user.id}/books", headers=auth, json=[
        {"book_id": book.id, "role": "OWNER"}])

    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    for leak in ("sys_user_book", "58030", "secret-hash-abc"):
        assert leak not in detail, f"内部结构/数据片段漏给了客户端：{detail}"
    assert r.headers.get("X-Request-ID"), "500 的回包里没有请求号，用户无法报障"


# --------------------------------------------------------------------- P3-14


def test_日志级别有配置项():
    """级别必须能被问出来，而不是取决于 systemd 怎么配。"""
    assert Settings(jwt_secret="a" * 48, app_env="").log_level == "INFO"
    assert Settings(jwt_secret="a" * 48, app_env="", log_level="warning").log_level == "WARNING"


def test_非法日志级别拒绝启动():
    """静默回退到 INFO 会把「我以为在打 DEBUG」藏起来 —— 没人会去看少了什么。"""
    with pytest.raises(Exception) as e:
        Settings(jwt_secret="a" * 48, app_env="", log_level="TRACE")
    assert "LOG_LEVEL" in str(e.value)


def test_生产环境不允许DEBUG():
    """DEBUG + DB_ECHO = 把带绑定参数的 SQL 连同账套数据打进日志。"""
    from app.core.config import validate_production_settings

    s = Settings(jwt_secret="a" * 48, app_env="prod", log_level="DEBUG",
                 database_url="postgresql+psycopg://cwq:Str0ngP%40ss@db.internal:5432/cwq")
    with pytest.raises(ValueError) as e:
        validate_production_settings(s, env="prod")
    assert "LOG_LEVEL" in str(e.value)


def test_已配置basicConfig():
    """以前只有 ``log = logging.getLogger("cwq")``，级别完全由外部决定。"""
    from app import main

    main.configure_logging()
    assert logging.getLogger().level == logging.INFO
