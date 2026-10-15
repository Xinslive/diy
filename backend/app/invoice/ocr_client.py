"""PaddleOCR AI Studio 异步任务 API 客户端。

只负责 HTTP 调用和 Markdown/JSONL 结果展开，不参与发票字段解析。
Token 只从配置读取，日志与异常信息均不回显认证头。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - 运行环境由 requirements 安装
    requests = None


class OcrApiError(RuntimeError):
    """OCR 服务不可用、返回格式不符合约定或任务失败。"""


def _json(response: requests.Response, *, action: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise OcrApiError(f"OCR {action}返回了无法解析的 JSON") from exc
    if not isinstance(value, dict):
        raise OcrApiError(f"OCR {action}返回格式不正确")
    return value


def _error_detail(response: requests.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            return str(data.get("message") or data.get("detail") or data.get("error") or "")[:300]
    except ValueError:
        pass
    return response.text[:300]


def _markdown_pages(jsonl: str) -> list[str]:
    """把 API 返回的 JSONL 结果展开为按页排列的文本。"""
    pages: list[str] = []
    for raw_line in jsonl.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise OcrApiError("OCR 结果 JSONL 中存在无法解析的行") from exc
        result = row.get("result") if isinstance(row, dict) else None
        layouts = result.get("layoutParsingResults", []) if isinstance(result, dict) else []
        if not isinstance(layouts, list):
            continue
        for layout in layouts:
            if not isinstance(layout, dict):
                continue
            markdown = layout.get("markdown") or {}
            text = markdown.get("text") if isinstance(markdown, dict) else ""
            if text:
                pages.append(_markdown_to_plain_text(str(text)))
    if not pages:
        raise OcrApiError("OCR 任务已完成，但结果中没有找到页面文本")
    return pages


def _markdown_to_plain_text(markdown: str) -> str:
    """保留表格单元格文本，让既有标签/正则解析器可以继续工作。"""
    out: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        if line.startswith("![") or line.startswith("<img"):
            continue
        if line.startswith("|") and line.endswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and all(set(cell) <= {"-", ":", " "} for cell in cells):
                continue
            line = " ".join(cell for cell in cells if cell)
        line = line.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
        line = line.replace("`", "")
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        if line:
            out.append(line)
    return "\n".join(out)


def ocr_pdf_to_texts(
    pdf_path: str,
    *,
    job_url: str,
    token: str,
    model: str,
    optional_payload: dict[str, Any],
    timeout_seconds: int = 600,
    poll_seconds: float = 5.0,
) -> list[str]:
    """提交本地 PDF、轮询任务并下载 JSONL 页面文本。"""
    if not job_url or not token:
        raise OcrApiError("未配置 OCR API URL 或 token")
    if requests is None:
        raise OcrApiError("缺少 requests 依赖，请安装 requests 后再使用 OCR API")

    headers = {"Authorization": f"bearer {token}"}
    data = {"model": model, "optionalPayload": json.dumps(optional_payload, ensure_ascii=False)}
    try:
        with open(pdf_path, "rb") as handle:
            response = requests.post(
                job_url,
                headers=headers,
                data=data,
                files={"file": handle},
                timeout=(30, 120),
            )
    except (OSError, requests.RequestException) as exc:
        raise OcrApiError(f"提交 OCR 任务失败：{exc}") from exc
    if response.status_code != 200:
        raise OcrApiError(f"提交 OCR 任务失败（HTTP {response.status_code}）：{_error_detail(response)}")

    submitted = _json(response, action="任务提交")
    job_id = ((submitted.get("data") or {}).get("jobId"))
    if not job_id:
        raise OcrApiError("OCR 任务提交响应缺少 jobId")

    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            status_response = requests.get(
                f"{job_url.rstrip('/')}/{job_id}",
                headers=headers,
                timeout=(30, 60),
            )
        except requests.RequestException as exc:
            raise OcrApiError(f"查询 OCR 任务失败：{exc}") from exc
        if status_response.status_code != 200:
            raise OcrApiError(
                f"查询 OCR 任务失败（HTTP {status_response.status_code}）：{_error_detail(status_response)}"
            )
        status = _json(status_response, action="任务查询")
        payload = status.get("data") or {}
        state = payload.get("state")
        if state == "done":
            json_url = (payload.get("resultUrl") or {}).get("jsonUrl")
            if not json_url:
                raise OcrApiError("OCR 任务完成，但响应缺少 jsonUrl")
            try:
                result_response = requests.get(json_url, timeout=(30, 120))
            except requests.RequestException as exc:
                raise OcrApiError(f"下载 OCR 结果失败：{exc}") from exc
            if result_response.status_code != 200:
                raise OcrApiError(
                    f"下载 OCR 结果失败（HTTP {result_response.status_code}）：{_error_detail(result_response)}"
                )
            return _markdown_pages(result_response.text)
        if state == "failed":
            raise OcrApiError(f"OCR 任务失败：{str(payload.get('errorMsg') or '未知错误')[:300]}")
        time.sleep(max(0.2, poll_seconds))

    raise OcrApiError(f"OCR 任务超过 {timeout_seconds} 秒仍未完成")


def optional_payload_from_env(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except ValueError as exc:
        raise OcrApiError("OCR_OPTIONAL_PAYLOAD 不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise OcrApiError("OCR_OPTIONAL_PAYLOAD 必须是 JSON 对象")
    return value
