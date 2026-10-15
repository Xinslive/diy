"""就绪探针 `/api/ready`（2026-10 上线安全收尾）。

**为什么要有第二个探针。** 原来只有 `/api/health`，而它恒返回
`{"ok": True}` —— **从不碰数据库**。于是：

* 编排器 / 负载均衡看它说 ok，就把流量打进来；
* 而数据库连不上、schema 版本对不上时，进来的人拿到的是 500。

「有健康检查」这件事本身成了假安慰：它证明的只是「这个进程还活着」，
但所有人拿它当「能干活」用。

更要命的是反向的那一头：如果把查库塞进 `/api/health`，数据库抖一下，
**全部实例**的 health 一起变红，编排器把全体摘掉再拉起 —— 数据库正在恢复，
服务却在集体重启。所以必须分开：**存活归 health，就绪归 ready**。

这组用例钉的是那个「分开」：

1. `/api/health` 在**查库失败时仍然 200**（它不是就绪信号）；
2. `/api/ready` 在查库失败时 503；
3. 就绪失败**不回异常原文**（里面可能带连接串与表名）；
4. schema 版本不符也算未就绪 —— 只查 ``SELECT 1`` 会把
   「迁移没跑完」报成健康，而那是写接口全线 500 的状态。
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# --------------------------------------------------------------------- 正常态


def test_健康与就绪在一切正常时都是200(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/ready").status_code == 200


def test_就绪回的内容里也有环境(client):
    """与 health 同一口径 —— 否则会出现「就绪说 prod、健康说 dev」的分裂。"""
    r = client.get("/api/ready")
    assert r.status_code == 200
    assert r.json()["env"] in ("dev", "test", "prod")
    assert r.json()["service"] == "cwq-accounting"


def test_就绪也带请求号(client):
    """未就绪时返回的那句「请提供请求号」必须真的对得上日志，否则等于没给。"""
    rid = client.get("/api/ready").headers.get("X-Request-ID")
    assert rid and 8 <= len(rid) <= 64


# --------------------------------------------------------------------- 关键分离


def test_数据库不通时健康检查仍然200(client):
    """这条是本文件存在的理由。

    health 是**存活**探针。数据库挂了而进程好好活着，
    此时把 health 打成 503 会让编排器把全部实例摘掉重启 ——
    数据库正在恢复，服务却在集体重启，那是比不检查更糟的结果。
    """
    boom = mock.Mock(side_effect=RuntimeError("connection refused: postgresql://cwq:pw@db:5432/cwq"))
    with mock.patch("app.main.get_db", side_effect=boom):
        # 注意：patch 的是依赖注入，/api/health 根本不声明 db，
        # 所以它压根不该受影响 —— 这正说明它不查库。
        assert client.get("/api/health").status_code == 200


def test_查库失败时就绪探针503(client):
    import sqlalchemy

    err = sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("db down"))
    with mock.patch("sqlalchemy.orm.Session.execute", side_effect=err):
        r = client.get("/api/ready")
    assert r.status_code == 503, "数据库连不上却报就绪 —— 流量会被打进来吃 500"


def test_schema版本不符也算未就绪(client):
    """迁移没跑完时，进程是能起来的（启动闸门只拦那一刻）。

    那种状态下所有写接口都撞「列不存在」，而只查 ``SELECT 1`` 的探针
    会把它报成健康 —— 于是就绪形同虚设，503 该来的时候不来。
    """
    with mock.patch("app.main.assert_schema_ready",
                    side_effect=RuntimeError("schema 版本对不上，请执行 alembic upgrade head")):
        r = client.get("/api/ready")
    assert r.status_code == 503


# --------------------------------------------------------------------- 脱敏


def test_未就绪时不回异常原文(client):
    """异常里可能带连接串、表名 —— 那等于把库结构抄送给未授权方。

    与 `dbapi_error_handler` 的脱敏口径一致（ARCHITECTURE §11.5）：
    排查靠服务端日志，用户那边有可报障的凭据（请求号）。
    """
    secret = "postgresql+psycopg://cwq:hunter2@10.0.0.9:5432/cwq"
    with mock.patch("app.main.assert_schema_ready",
                    side_effect=RuntimeError(f"连不上 {secret}")):
        r = client.get("/api/ready")
    assert r.status_code == 503
    body = r.text
    assert "hunter2" not in body, "把数据库口令回给了调用方"
    assert "10.0.0.9" not in body, "把库地址回给了调用方"
    assert "alembic" not in body, "把处置命令回给了调用方（那是运维内部信息）"


def test_未就绪时给出可报障的请求号(client):
    with mock.patch("app.main.assert_schema_ready", side_effect=RuntimeError("x")):
        r = client.get("/api/ready")
    assert "请求号" in r.text, "报错没给请求号，用户无从对日志"
