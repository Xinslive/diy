"""插件页面使用的宿主服务路由。与插件代码共享同一个网关和请求身份。"""
from __future__ import annotations

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response
from urllib.parse import quote
from app.plugins.public_api import context_for, current_request_id


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
    vouchers: list


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)


class ConfigurationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)
    value: dict


def router_for(manifest):
    router = APIRouter(prefix="/api/plugins/" + manifest["id"] + "/_host")
    ctx = context_for(manifest)
    if "book:read" in manifest["permissions"]:
        router.add_api_route("/books", ctx.book_list, methods=["GET"])
    if "plugin_data:rw" in manifest["permissions"]:
        @router.get("/config/{table}/{key}")
        def get_config(table: str, key: str, book_id: int):
            return ctx.config.get(table, key, book_id=book_id)

        @router.put("/config/{table}/{key}")
        def put_config(table: str, key: str, book_id: int, payload: ConfigurationInput):
            return ctx.config.put(table, key, payload.value, book_id=book_id, expected_version=payload.expected_version)

    if "import:rw" not in manifest["permissions"]:
        return router
    imports = ctx.imports

    @router.get("/import-limits")
    def limits():
        return imports.limits()

    @router.get("/vouchers/{voucher_id}/origin")
    def origin(voucher_id: int):
        return imports.origin(voucher_id)

    @router.post("/imports")
    def upload(book_id: int, files: list[UploadFile] = File(...)):
        return imports.create(book_id, files)

    @router.get("/imports")
    def batches(book_id: int, before: str | None = None, limit: int = 50):
        return imports.list(book_id, before=before, limit=limit)

    @router.get("/imports/{ident}")
    def read(ident: str):
        return imports.read(ident)

    @router.put("/imports/{ident}/preview")
    def edit(ident: str, payload: PreviewInput):
        return imports.edit(ident, payload.vouchers, revision=payload.revision)

    @router.post("/imports/{ident}/confirm")
    def confirm(ident: str, payload: RevisionInput):
        return imports.confirm(ident, revision=payload.revision)

    @router.post("/imports/{ident}/submit")
    def submit(ident: str, payload: RevisionInput):
        return imports.submit(ident, revision=payload.revision, request_id=current_request_id.get())

    @router.post("/imports/{ident}/cancel")
    def cancel(ident: str, payload: RevisionInput):
        return imports.cancel(ident, revision=payload.revision)

    @router.get("/imports/{ident}/files/{file_id}")
    def download(ident: str, file_id: str):
        value = imports.source(ident, file_id)
        # 用户提供的类型不参与响应执行；所有文件强制下载，并关闭内容嗅探。
        return Response(value["content"], media_type="application/octet-stream", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(value["filename"], safe=""),
            "X-Content-Type-Options": "nosniff"})

    return router
