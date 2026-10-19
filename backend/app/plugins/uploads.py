"""插件上传的容量、文件名和内容摘要契约。"""
import hashlib
from fastapi import HTTPException
from app.core.uploads import UploadTooLarge, check_archive_size, read_upload_sync, upload_limit


def read_file(file, *, limit=None):
    if not hasattr(file, "file") or not hasattr(file, "filename"):
        raise HTTPException(400, "插件上传对象无效")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise HTTPException(400, "插件文件读取上限无效")
    cap = upload_limit() if limit is None else min(upload_limit(), limit)
    try:
        raw = read_upload_sync(file, limit=cap, label="插件文件")
        check_archive_size(raw, label="插件文件")
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    if not raw:
        raise HTTPException(400, "插件文件不能为空")
    name = str(file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if ord(char) >= 32 and ord(char) != 127)[:256]
    if not name or name in {".", ".."}:
        raise HTTPException(400, "插件文件名无效")
    content_type = str(getattr(file, "content_type", "") or "")[:128]
    return {"filename": name, "content_type": content_type, "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "content": raw}
