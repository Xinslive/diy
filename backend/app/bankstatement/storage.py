"""对账单原件的本地留存。

**原件必须留。** 解析器会改、别名表会补、格式画像会更新 —— 这些都意味着
「同一份文件，早期的解析结果和后来的不一样」。没有原件就没有重来的余地：
用户抱怨「这笔钱认错了」时，我们能做的只有让他重新去网银导一遍。

落盘规则：

- 目录 ``data/statements/{账套id}/``，文件名 ``{流水id}.{扩展名}``；
- **扩展名按容器类型定，不按上传时的文件名定** —— 库里那条
  ``container_format`` 才是真相，文件名只是给人看的；
- 路径只存**相对**根目录的部分，数据库里不存绝对路径：换部署目录、
  换机器、挂载到别处时不必改数据。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from app.core.config import settings

#: 容器类型 → 落盘扩展名。认的是**文件头**（见 :mod:`app.bankstatement.formats`），
#: 不是上传时的文件名 —— 那一批样例里有三份扩展名是假的。
_EXTENSIONS = {
    "XLSX": ".xlsx",
    "XLS": ".xls",
    "HTML": ".html",
    "CSV": ".csv",
    "PDF": ".pdf",
}


def root() -> Path:
    path = Path(settings.statement_storage_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _book_dir(book_id: int) -> Path:
    path = root() / str(int(book_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def extension_for(container: str) -> str:
    return _EXTENSIONS.get(container, ".bin")


def store(book_id: int, container: str, data: bytes) -> str:
    """把原件落盘，返回**相对**路径。

    先写临时文件再 ``os.replace``：同目录内改名是原子的，不会出现
    「读到半个文件」的情况。
    """
    directory = _book_dir(book_id)
    suffix = extension_for(container)
    fd, temp_name = tempfile.mkstemp(prefix=".upload-", suffix=suffix, dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    final_name = f"{Path(temp_name).stem.lstrip('.upload-')}{suffix}"
    os.replace(temp_name, directory / final_name)
    return f"{int(book_id)}/{final_name}"


def rename_to(book_id: int, relative: str, container: str) -> str:
    """落盘时还不知道流水 id，先用临时名；入库拿到 id 后改成正式名。

    对账单 id 是入库后才有的，而文件名里带 id 便于人工在磁盘上定位
    「这份原件对应库里第几份」。改名失败不算致命 —— 名字只是给人看的。
    """
    source = path_for(relative)
    if not source.exists():
        return relative
    target = source.with_name(f"{Path(source).stem}{extension_for(container)}")
    if target == source:
        return relative
    try:
        os.replace(source, target)
    except OSError:
        return relative
    return f"{int(book_id)}/{target.name}"


def path_for(relative_path: str) -> Path:
    base = root().resolve()
    path = (base / relative_path).resolve()
    # **不许跳出根目录**：相对路径是从库里读出来的，理论上干净，
    # 但多一道 ``startswith`` 就多一道防线（``../`` 那种路径一旦进来，
    # 删除接口就能把根目录外的文件删掉）。
    if base != path and base not in path.parents:
        raise ValueError("对账单原件路径非法")
    return path


def read(relative_path: str) -> bytes:
    return path_for(relative_path).read_bytes()


def exists(relative_path: str) -> bool:
    try:
        return path_for(relative_path).exists()
    except ValueError:
        return False


def delete(relative_path: str) -> None:
    try:
        path = path_for(relative_path)
    except ValueError:
        return
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass
