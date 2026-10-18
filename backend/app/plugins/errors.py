"""插件边界的稳定错误码；不向调用者回传执行异常或服务器路径。"""
from fastapi import HTTPException


class PluginError(Exception):
    def __init__(self, code: str, message: str = "插件操作失败", status: int = 400, *, findings=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.findings = findings or []

    def http(self):
        return HTTPException(self.status, {"code": self.code, "message": self.message})


def unavailable():
    return HTTPException(503, {"code": "PLUGIN_UNAVAILABLE", "message": "插件尚未就绪，请联系管理员"},
                         headers={"Cache-Control": "private, no-store"})


def not_found():
    return HTTPException(404, {"code": "PLUGIN_NOT_FOUND_OR_UNAVAILABLE", "message": "插件不存在或不可用"},
                         headers={"Cache-Control": "private, no-store"})
