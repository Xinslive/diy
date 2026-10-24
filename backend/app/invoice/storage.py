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


def _write_temp(directory: Path, writer) -> str:
    """写一个临时文件，返回它的**相对**文件名。

    ``writer`` 收到一个已打开的二进制句柄，自己往里写。
    写完是 ``os.replace`` 到最终名之前的临时名 —— 同目录，
    所以改名是原子的（不会读到半个文件）。
    """
    fd, temp_name = tempfile.mkstemp(prefix=".upload-", suffix=".pdf", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            writer(handle)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return os.path.basename(temp_name)


def stage_temp_bytes(data: bytes) -> str:
    """把一份源文件（**已经在内存里**）落到临时名，返回相对路径。

    这是给「调用方手里就是字节」的旧接口用的；新上传走
    :func:`app.core.uploads.save_upload_to_temp`，那个才是内存里不放大头的那个。
    """
    directory = root()
    return _write_temp(directory, lambda handle: handle.write(data))


def promote(temp_relative: str, final_relative: str) -> str:
    """把临时文件改名为最终名，返回最终相对路径。

    **改名而不是重写**：内容原样保留（单文件上传时落盘的就是用户传上来的
    原文件，一个字节都不改 —— 这是既有行为，改名天然满足）。
    """
    os.replace(path_for(temp_relative), path_for(final_relative))
    return final_relative


def merge_pdfs(sources: list[tuple[str, str]], batch_id: int) -> str:
    """按顺序把多份源 PDF 合成一份，页码连续，返回相对路径。

    为什么合并而不是把每个文件的页码错开记进批次：解析器按**页**切发票
    （``extract_pages_text`` → 每页一条记录），合并后这条链路一个字都不用改，
    页码天然连续，也就是「一份合并 PDF = 一个批次 N 张发票」的老行为。

    ``sources`` 是 ``(暂存相对路径, 用户上传时的文件名)``，后者只用于报错时点名。

    一份源文件都读不出来（加密、损坏、根本不是 PDF）时**整批失败**并点名是谁：
    这类文件属于「本该解析却解析不了」，静默跳过等于用户以为传进去了。
    非 PDF 文件的跳过在接口层就完成了，压根不会走到这里。

    **合并前先校验总页数**（审查 H-07）：合并出来的 PDF 会被逐页取文本层、
    逐页解析成发票，页数就是这一批要处理的工作量。不校验的话，一百份
    扫描件会被合成一份几千页的 PDF，然后一路走到解析器里才炸。
    """
    import pypdfium2 as pdfium

    directory = root()
    filename = f"batch_{batch_id}.pdf"
    target = directory / filename
    pages_limit = max(1, int(settings.invoice_max_pdf_pages))
    total_pages = 0
    for relative, source_name in sources:
        try:
            probe = pdfium.PdfDocument(path_for(relative))
        except Exception as exc:  # noqa: BLE001 —— pdfium 的异常类型随版本变
            raise ValueError(f"源文件无法读取（可能已加密或已损坏）：{source_name}") from exc
        try:
            total_pages += len(probe)
        finally:
            probe.close()
        if total_pages > pages_limit:
            raise ValueError(
                f"这批文件加起来有 {total_pages} 页，超过单批上限 {pages_limit} 页"
                f"（超在 {source_name}）；请分成几批上传")
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
