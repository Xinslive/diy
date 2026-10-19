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


def stage_pdf(batch_id: int, index: int, data: bytes) -> str:
    """把一份源文件先落到暂存名，返回相对路径。

    批量上传时每个源文件都先落到 ``batch_{id}_src{n}.pdf``，合并成
    ``batch_{id}.pdf`` 之后再删掉它们。分两步的理由：解析器读的是**合并后**
    的那份（页码连续，一页一张发票），而原件字节流不因为合并而改写 ——
    只合并了一份时（单文件上传）根本不走合并，落盘的就是用户传上来的原文件。
    """
    filename = f"batch_{batch_id}_src{index}.pdf"
    target = root() / filename
    fd, temp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=root())
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


def merge_pdfs(sources: list[tuple[str, str]], batch_id: int) -> str:
    """按顺序把多份源 PDF 合成一份，页码连续，返回相对路径。

    为什么合并而不是把每个文件的页码错开记进批次：解析器按**页**切发票
    （``extract_pages_text`` → 每页一条记录），合并后这条链路一个字都不用改，
    页码天然连续，也就是「一份合并 PDF = 一个批次 N 张发票」的老行为。

    ``sources`` 是 ``(暂存相对路径, 用户上传时的文件名)``，后者只用于报错时点名。

    一份源文件都读不出来（加密、损坏、根本不是 PDF）时**整批失败**并点名是谁：
    这类文件属于「本该解析却解析不了」，静默跳过等于用户以为传进去了。
    非 PDF 文件的跳过在接口层就完成了，压根不会走到这里。
    """
    import pypdfium2 as pdfium

    directory = root()
    filename = f"batch_{batch_id}.pdf"
    target = directory / filename
    fd, temp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=directory)
    os.close(fd)
    document = None
    try:
        document = pdfium.PdfDocument.new()
        for relative, source_name in sources:
            try:
                source = pdfium.PdfDocument(path_for(relative))
            except Exception as exc:  # noqa: BLE001 —— pdfium 的异常类型随版本变
                raise ValueError(f"源文件无法读取（可能已加密或已损坏）：{source_name}") from exc
            try:
                document.import_pages(source)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"源文件无法读取（可能已加密或已损坏）：{source_name}") from exc
            finally:
                source.close()
        document.save(temp_name)
        document.close()
        document = None
        os.replace(temp_name, target)
    except Exception:
        if document is not None:
            document.close()
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
