"""发票原件的本地文件留存。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.core.config import settings


def root() -> Path:
    path = Path(settings.invoice_storage_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_pdf(batch_id: int, data: bytes) -> str:
    directory = root()
    filename = f"batch_{batch_id}.pdf"
    target = directory / filename
    fd, temp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return filename


def path_for(relative_path: str) -> Path:
    base = root().resolve()
    path = (base / relative_path).resolve()
    if base not in path.parents:
        raise ValueError("发票原件路径非法")
    return path


def delete(relative_path: str) -> None:
    try:
        path_for(relative_path).unlink()
    except FileNotFoundError:
        pass
