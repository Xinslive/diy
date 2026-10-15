"""FastAPI 入口。"""
from __future__ import annotations

import logging
import re
import sys
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.api import auth, books, ledger, printing, reports, subjects, system, vouchers
from app.core.config import detect_env, settings
from app.core.db import get_db
from app.core.schema_version import assert_schema_ready

log = logging.getLogger("cwq")


def configure_logging() -> None:
    """把日志级别钉在配置上，不看进程外面怎么配（审计 P3-14）。

    以前这里只有一句 ``log = logging.getLogger("cwq")``：级别完全由外部
    环境决定，而 root 上没有 handler 时 logging 的兜底是 WARNING ——
    于是 INFO 及以下全部静默丢弃。审计点名的「护栏 4xx 零日志」（P2-24）
    就是这么来的：那条日志不是没写，是写了也被级别过滤掉了。

    ``basicConfig`` 单独用是不够的：**root 上已经有 handler 时它是空操作**
    （gunicorn、uvicorn、systemd 都会先加），此时只有 level 参数被应用上，
    格式不生效、level 也常常不是我们想要的那一档。所以两件事都显式做。
    """
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger().setLevel(settings.log_level)


configure_logging()


def api_docs_urls(env: str | None = None) -> dict[str, str | None]:
    """按环境决定要不要挂接口文档。**生产默认关**（审计 P1-7）。

    ``/openapi.json`` 不是一张无害的静态图片，它是一份完整的路由表 +
    每个接口的请求模型 + 每个字段的约束 —— 等于主动告诉未授权方
    「这个系统有哪些入口、各自要什么、哪些参数能传」。
    配合越权或鉴权漏挂，一份 schema 足以把「哪些接口没挂鉴权依赖」
    变成一条可批量自动化探测的清单。``/docs`` 还额外挂着 CDN 上的
    Swagger UI 资源，等于往外发请求。

    只用 ``detect_env`` 判定，与启动校验 / 健康检查同一个口径 ——
    出现「校验说你是 prod、文档却开着」这种自相矛盾的配置最难排查。
    写成「默认开、显式关」的话，默认值自己给自己开了免检（本项目
    在 JWT_SECRET 上已经吃过一次这个亏，见 app_env 字段注释）。
    """
    if detect_env(settings, env) in ("dev", "test"):
        return {"docs_url": "/docs", "redoc_url": "/redoc",
                "openapi_url": "/openapi.json"}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(
    title="财务记账系统 API",
    description="多账套云财务记账系统。核心原则：唯一事实来源是凭证与分录，"
                "余额 / 账簿 / 报表 / 打印全部由同一套聚合引擎派生。",
    version="0.1.0",
    **api_docs_urls(),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


# 外部请求号原样透传，只认这几种字符、限长 64。
#
# 透传而不是一律自造：前端 / 网关 / 采集链路已经把同一条链路的请求串起来了，
# 后端再另起一个号，前端报障时两边对不上号。过滤是为了不让任意长、任意字符的
# 头进日志 —— 它是**要被人读、被人搜、可能被人抄进工单**的字段。
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _request_id(request: Request) -> str:
    rid = request.headers.get("x-request-id", "")
    if _REQUEST_ID_RE.match(rid):
        return rid
    return uuid.uuid4().hex[:16]


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """给每个请求发一个号，并贯穿到响应头与日志（审计 P2-24）。

    为什么必须有号：一次 500 之后，用户手里只有一句「页面打不开」，
    服务端日志里是几百上千行并发请求 —— 没有号根本对不上是哪一行。
    有了号，用户截图里的那串字符就能在日志里定位到唯一一条。

    ``request.state`` 落在 scope 上，所以中间件里写的号，异常处理器里读得到
    （异常处理器与路由共享同一个 Request scope）。
    """
    rid = _request_id(request)
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response

for r in (auth.router, system.router, books.router, subjects.router, vouchers.router,
          ledger.router, reports.router, printing.router):
    app.include_router(r)


@app.exception_handler(DBAPIError)
async def dbapi_error_handler(request, exc: DBAPIError):
    """把数据库护栏抛出的错误翻译成用户能看懂的中文提示。

    会计护栏全部落在 PostgreSQL 触发器里，psycopg 抛的是通用异常；
    这里取 diag.message_primary —— 那正是 RAISE EXCEPTION 里的原文。

    分两种回法（P2-24）：

    * **约束类 4xx**：把 message_primary 回给客户端。它是 ``db_init.sql`` 里
      人工写的中文文案（"已记账的凭证不允许删除"之类），本来就是要给人看的；
      400 不是「出事了」，是「你这次请求不合法」。
    * **其它（500）**：**不回原文**。``str(orig)`` 里是表名、列名、约束名、
      有时还有数据片段（唯一冲突会把重复键的值带出来）—— 把它回给调用方
      等于把库结构抄送给未授权方。改成一句脱敏提示 + 请求号：
      排查靠服务端日志（那里有完整异常栈），用户那边有可报障的凭据。

    两条路都要**记日志**。护栏被绕过（有人绕过 API 直接改库、或有 bug
    打到了不该打的地方）是最该留痕的路径之一，过去它一条日志都不产生 ——
    唯一能看见的痕迹是用户截图里的那句报错。
    """
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None)
    diag = getattr(orig, "diag", None)
    message = (getattr(diag, "message_primary", None) or str(orig)).strip()
    rid = getattr(request.state, "request_id", "-")
    where = f"rid={rid} {request.method} {request.url.path}"

    if sqlstate in ("23514", "23505", "23503", "23502"):
        if sqlstate == "23505":
            message = f"数据已存在或被引用，违反唯一约束：{message}"
        # 护栏拒绝也要留痕：谁、在什么时候、想干什么被挡下来了。
        # 日志里不记 message 全文（可能含重复键的值），只记状态码与截断摘要。
        log.warning("数据库约束拒绝 %s sqlstate=%s %s", where, sqlstate, message[:200])
        return JSONResponse(status_code=400,
                            content={"detail": message[:500]},
                            headers={"X-Request-ID": rid})

    log.exception("数据库异常 %s sqlstate=%s：%s", where, sqlstate, str(orig)[:500])
    return JSONResponse(
        status_code=500,
        content={"detail": f"数据库内部错误（请求号 {rid}），请联系管理员并提供该请求号"},
        headers={"X-Request-ID": rid},
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
    #
    # **这是存活探针，不是就绪探针** —— 它故意不碰数据库。
    # 混用会有两个后果：数据库抖动一下，所有实例的 health 一起变红，
    # 编排器把**全部**实例摘掉再拉起（重启风暴）；而真正的就绪信息
    # 一点也没多。要查库的那个是 `/api/ready`。
    return {"ok": True, "service": "cwq-accounting", "env": detect_env(settings)}


@app.get("/api/ready")
def ready(request: Request, db: Session = Depends(get_db)):
    """就绪探针：**真的去查库**，并核对 schema 版本。

    为什么必须和 `/api/health` 分开（而不是合并成一个）：

    * 存活 = 进程还在不在。就绪 = 现在能不能接活。两者在
      「数据库连不上」时结论完全不同 —— 进程好好地活着，但一单都接不了。
    * 负载均衡器该用的是就绪。健康检查被数据库抖动带崩会造成全体重启，
      而这恰恰是最不该重启的时候。

    为什么这里只回一句话、不把异常原文吐出去：异常里可能带连接串、表名，
    也就是库结构。排查靠服务端日志（有完整栈），调用方拿到的是可报障的凭据。
    与 `dbapi_error_handler` 的脱敏口径一致（§11.5）。

    **schema 版本也要查**：迁移没跑完时进程是能起来的（`assert_schema_ready`
    只在启动时拦），此时所有写接口都会撞「列不存在」。只看 `SELECT 1` 会把
    这种状态报成健康。
    """
    rid = getattr(request.state, "request_id", "-")
    try:
        db.execute(text("SELECT 1"))
        assert_schema_ready()
    except Exception:
        # 分开记：查库不通与 schema 不符是两回事，处置动作也不同
        # （前者查网络/库进程，后者去跑 alembic upgrade head）。
        log.exception("就绪探针失败 rid=%s：数据库不可达或 schema 版本不符", rid)
        raise HTTPException(
            503, f"服务未就绪（请求号 {rid}），请稍后重试或联系管理员")
    return {"ok": True, "service": "cwq-accounting", "env": detect_env(settings)}
