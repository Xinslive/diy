"""外部每分钟调用的应用级调度入口。"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.db import SessionLocal
from app.scheduler import service

router = APIRouter()


@router.get("/api/scheduler/tick/", include_in_schema=False)
@router.get("/api/scheduler/tick")
def tick(request: Request, background: BackgroundTasks):
    token = getattr(request.state, "scheduler_token", None)
    with SessionLocal() as db:
        expected = service.trigger_secret_digests(db)
    if not any(expected):
        raise HTTPException(503, "调度触发密钥尚未配置")
    if not service.valid_trigger(token, expected):
        raise HTTPException(401, "调度触发密钥无效")

    runtime = getattr(request.app.state, "plugin_runtime", None)
    claims = service.claim_due(runtime)
    if claims:
        background.add_task(service.execute_claims, claims, runtime)
    return JSONResponse(
        status_code=202,
        content={"ok": True, "accepted": len(claims), "run_ids": [item["run_id"] for item in claims]},
        background=background,
    )
