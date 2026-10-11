"""FastAPI 入口。"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from app.api import auth, books, ledger, printing, reports, subjects, system, vouchers
from app.core.config import detect_env, settings

log = logging.getLogger("cwq")

app = FastAPI(
    title="财务记账系统 API",
    description="多账套云财务记账系统。核心原则：唯一事实来源是凭证与分录，"
                "余额 / 账簿 / 报表 / 打印全部由同一套聚合引擎派生。",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (auth.router, system.router, books.router, subjects.router, vouchers.router,
          ledger.router, reports.router, printing.router):
    app.include_router(r)


@app.exception_handler(DBAPIError)
async def dbapi_error_handler(request, exc: DBAPIError):
    """把数据库护栏抛出的错误翻译成用户能看懂的中文提示。

    会计护栏全部落在 PostgreSQL 触发器里，psycopg 抛的是通用异常；
    这里取 diag.message_primary —— 那正是 RAISE EXCEPTION 里的原文。
    """
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None)
    diag = getattr(orig, "diag", None)
    message = (getattr(diag, "message_primary", None) or str(orig)).strip()

    if sqlstate in ("23514", "23505", "23503", "23502"):
        if sqlstate == "23505":
            message = f"数据已存在或被引用，违反唯一约束：{message}"
        return JSONResponse(status_code=400, content={"detail": message})

    log.exception("数据库异常: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"数据库错误 [{sqlstate}]：{message[:300]}"},
    )


@app.on_event("startup")
def startup() -> None:
    from app.core.config import validate_production_settings
    from app.core.schema_version import assert_schema_ready

    # P3-04：配置校验必须在碰库**之前**。
    # 反过来的话，进程会先把 schema 建好、种子数据灌进去，再因为配置
    # 不合格退出 —— 库已经被改动过了，而运维看到的是「启动失败」。
    validate_production_settings(settings)

    # P3-01：这里**只校验版本，不执行任何 DDL**。
    # 原来这里是 ``init_database()`` —— 每次启动都 create_all + 重放 db_init.sql。
    # 而 create_all 只建缺失的表、不改已存在的表，于是「给已有表加 NOT NULL 列」
    # 这类变更在生产上会静默不生效：服务照常启动，问题到运行时才炸。
    # 建表、改表的职责已经移交 Alembic（backend/migrations/），
    # 版本对不上就拒绝启动，并打印该执行哪条命令。
    assert_schema_ready()


@app.get("/api/health")
def health():
    # 与启动校验共用 detect_env，避免出现「校验说 prod、健康检查说 dev」。
    return {"ok": True, "service": "cwq-accounting", "env": detect_env(settings)}
