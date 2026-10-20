"""PaddleOCR AI Studio 异步任务 API 客户端。

只负责 HTTP 调用和 Markdown/JSONL 结果展开，不参与发票字段解析。
Token 只从配置读取，日志与异常信息均不回显认证头。

结果地址（``jsonUrl``）是**不可信输入**：它由 OCR 服务在任务结果里给出，
而 OCR 服务本身可能被诱导指向内网。抓取它之前必须校验协议、网段与跳转
（审计 P2-2）—— 否则这就是一条二阶 SSRF：上传一张无文字层的 PDF 就能让
后端替攻击者访问内网，并把内网的响应内容读回来。
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

try:
    import requests
except ImportError:  # pragma: no cover - 运行环境由 requirements 安装
    requests = None


class OcrApiError(RuntimeError):
    """OCR 服务不可用、返回格式不符合约定或任务失败。"""


# ------------------------------------------------------------------ SSRF 防线

#: 只允许 http/https。``file:`` / ``gopher:`` 之类由 requests 自己挡掉，
#: 但那是**碰巧**：换一个 HTTP 客户端就会变成读本地文件。
_ALLOWED_SCHEMES = ("http", "https")

#: 最多跟几跳重定向。每一跳都重新校验，所以「第一跳公网、第二跳内网」
#: 也会被挡住。
_MAX_REDIRECTS = 3

#: 共享地址段（CGNAT，100.64.0.0/10）：不是 RFC1918 私有段，但同样不是公网。
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    """这个 IP 是不是可以放心去连的公网地址。"""
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
        return False
    if ip.is_multicast or ip.is_unspecified:
        return False
    if ip.version == 4 and ip in _CGNAT:
        return False
    return True


def _check_host_is_public(host: str) -> None:
    """主机名必须解析到**全部**公网地址，否则拒绝。

    只要有一个解析结果是内网/环回/链路本地就拒 —— 云厂商的元数据地址
    （``169.254.169.254``）与各家内网服务都在这些段里。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise OcrApiError("OCR 结果地址的主机名解析失败") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise OcrApiError("OCR 结果地址的主机名解析不出任何地址")
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:  # pragma: no cover - getaddrinfo 不会给出这种值
            raise OcrApiError("OCR 结果地址解析出了非法 IP") from exc
        if not _is_public_ip(ip):
            raise OcrApiError("OCR 结果地址指向内网或保留地址，已拒绝抓取")


def check_public_url(url: str) -> str:
    """校验一个要抓取的 URL：协议白名单 + 主机解析到公网。"""
    try:
        parts = urlsplit(url or "")
        host = parts.hostname
    except ValueError as exc:  # 畸形 URL（括号不配对的 IPv6 等）
        raise OcrApiError("OCR 结果地址不是合法的 URL") from exc
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise OcrApiError(f"OCR 结果地址的协议不允许（只允许 {'/'.join(_ALLOWED_SCHEMES)}）")
    if not host:
        raise OcrApiError("OCR 结果地址缺少主机名")
    _check_host_is_public(host)
    return url


def _fetch_result_text(url: str, *, timeout: tuple[int, int]) -> str:
    """抓 OCR 结果正文。**每跳重定向都重新校验**，最多 ``_MAX_REDIRECTS`` 跳。

    响应内容**不回显**：失败时只说状态码，不把正文（可能是内网服务的
    页面/数据）带进异常消息。
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        current = check_public_url(current)
        try:
            response = requests.get(current, timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            raise OcrApiError(f"下载 OCR 结果失败：{exc}") from exc
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location") or ""
            if not location:
                raise OcrApiError("OCR 结果地址返回了重定向但没有目标")
            current = urljoin(current, location)
            continue
        if response.status_code != 200:
            raise OcrApiError(f"下载 OCR 结果失败（HTTP {response.status_code}）")
        return response.text
    raise OcrApiError("OCR 结果地址重定向次数过多")


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
            # jsonUrl 是**不可信输入**：它由 OCR 服务给出，抓它之前先过
            # 协议 / 网段 / 跳转三道闸（审计 P2-2）。响应内容也不回显。
            result_text = _fetch_result_text(str(json_url), timeout=(30, 120))
            return _markdown_pages(result_text)
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
