"""PaddleOCR AI Studio 异步任务 API 客户端。

只负责 HTTP 调用和 Markdown/JSONL 结果展开，不参与发票字段解析。
Token 只从配置读取，日志与异常信息均不回显认证头。

结果地址（``jsonUrl``）是**不可信输入**：它由 OCR 服务在任务结果里给出，
而 OCR 服务本身可能被诱导指向内网。抓取它之前必须校验协议、网段与跳转
（审计 P2-2）—— 否则这就是一条二阶 SSRF：上传一张无文字层的 PDF 就能让
后端替攻击者访问内网，并把内网的响应内容读回来。
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from app.core.config import settings
from app.invoice.text_parser import scrub_control_chars

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

#: 解析结果的缓存时长（秒）。
#:
#: **为什么要有这个缓存。** SSRF 校验（``getaddrinfo``）与真正连接用的解析
#: **必须是同一次** —— 否则中间有一个空档：攻击者控制的 DNS 第一次给公网 IP
#: （校验通过）、第二次给 ``127.0.0.1``（连接打到内网）。这就是 DNS 重绑定
#: 的 TOCTOU。缓存让「校验」与「连接」用**同一批地址**，那个空档就没有了。
#:
#: 30 秒：短到「域名换了 IP」不会变成半天的旧账，长到同一秒里的多次请求
#: 不必反复解析。
_RESOLVE_TTL = 30.0
_RESOLUTIONS: dict[str, tuple[float, list[str]]] = {}
_RESOLVE_LOCK = threading.Lock()


def _resolve(host: str) -> list[str]:
    """解析主机名，返回**全部**地址（**带短 TTL 缓存**）。"""
    now = time.monotonic()
    with _RESOLVE_LOCK:
        cached = _RESOLUTIONS.get(host)
        if cached is not None and cached[0] > now:
            return list(cached[1])
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise OcrApiError("OCR 结果地址的主机名解析失败") from exc
    addresses: list[str] = []
    for info in infos:
        raw = info[4][0]
        if raw not in addresses:
            addresses.append(raw)
    if not addresses:
        raise OcrApiError("OCR 结果地址解析不出任何地址")
    with _RESOLVE_LOCK:
        _RESOLUTIONS[host] = (now + _RESOLVE_TTL, list(addresses))
        if len(_RESOLUTIONS) > 64:
            # 顺手清掉过期的，别让它随「见过的域名数」只增不减。
            for key, (expires, _addresses) in list(_RESOLUTIONS.items()):
                if expires <= now:
                    _RESOLUTIONS.pop(key, None)
    return addresses


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    """这个 IP 是不是可以放心去连的公网地址。"""
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
        return False
    if ip.is_multicast or ip.is_unspecified:
        return False
    if ip.version == 4 and ip in _CGNAT:
        return False
    return True


def _check_host_is_public(host: str) -> list[str]:
    """主机名必须解析到**全部**公网地址，否则拒绝。

    只要有一个解析结果是内网/环回/链路本地就拒 —— 云厂商的元数据地址
    （``169.254.169.254``）与各家内网服务都在这些段里。

    返回**已校验的那批地址**：连接要用同一批（见 ``_pin``）—— 校验用一次
    解析、连接用另一次解析，中间那个空档就是 DNS 重绑定。
    """
    addresses = _resolve(host)
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


def _pin(url: str, addresses: list[str]) -> tuple[bool, str, dict[str, str]]:
    """把请求**钉到已校验的 IP** 上（Host 头仍然是原来的主机名）。

    **这是 DNS 重绑定（TOCTOU）的正解**：校验用 ``getaddrinfo`` 判「全是公网」，
    随后 ``requests`` **会再解析一次** —— 两次解析之间，攻击者控制的 DNS 可以
    第一次给公网 IP、第二次给 ``127.0.0.1``。把连接目标换成**已校验的那个
    IP**，两次解析就变成一次，绑定不成立。

    **https 不能这么干**：证书是按主机名签发的，连到 IP 会验签失败（那等于
    关掉 TLS 校验，比 TOCTOU 更糟）。https 走「保持主机名 + 连上之后核对
    **真实对端地址**」那条路（见 ``_check_peer_is_public``）：残余的空档是
    「TLS 握手本身可能打到一个被重绑的地址」，但**正文一个字节都不会被读走**
    —— 这正是这条防线要防的东西（把内网的响应内容读回来）。
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme.lower() != "http" or not addresses or not host:
        return False, url, {}
    ip = addresses[0]
    literal = f"[{ip}]" if ":" in ip else ip
    port = parts.port
    netloc = f"{literal}:{port}" if port else literal
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    host_header = f"{host}:{port}" if port else host
    return True, f"http://{netloc}{path}", {"Host": host_header}


def _peer_address(response) -> str:
    """连接**实际**用到的那一个 IP（socket 对端地址）。

    这是 DNS 重绑定的**精确**判定：请求前的解析说「这是公网」，但连接时
    用的可能是另一个（内网）地址。socket 里拿到的对端地址就是真相。
    """
    raw = getattr(response, "raw", None)
    connection = getattr(raw, "_connection", None)
    sock = getattr(connection, "sock", None)
    if sock is None:
        return ""
    try:
        return str(sock.getpeername()[0])
    except (OSError, IndexError, TypeError):
        return ""


def _check_peer_is_public(response) -> None:
    """连上之后核对**真实对端**是不是公网。不是就拒绝，且**不读正文**。"""
    peer = _peer_address(response)
    if not peer:
        # 拿不到对端地址（代理、实现差异）就不阻断 —— 但正文仍然受字节上限保护。
        return
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return
    if not _is_public_ip(ip):
        raise OcrApiError(
            "OCR 结果地址在连接时指向内网或保留地址（DNS 重绑定），已拒绝抓取")


def _fetch_result_text(url: str, *, timeout: tuple[int, int]) -> str:
    """抓 OCR 结果正文。**每跳重定向都重新校验**，最多 ``_MAX_REDIRECTS`` 跳。

    响应内容**不回显**：失败时只说状态码，不把正文（可能是内网服务的
    页面/数据）带进异常消息。

    **按字节上限流式读**（审查 H-07）：``response.text`` 会把整个响应读进
    内存再解码 —— 而这个响应来自**不可信**的地址（``jsonUrl`` 由 OCR 服务
    给出）。一个自称「返回 2GB JSONL」的服务就能把 API 进程的内存打满。
    这里逐块读、边读边计数，超了立刻中止。
    """
    limit = max(1, int(settings.invoice_ocr_max_response_bytes))
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        current = check_public_url(current)
        # 连接要用**校验时解析出的那一批地址**（短 TTL 缓存里就是它）。
        addresses = _resolve(_host_of(current))
        pinned, request_url, headers = _pin(current, addresses)
        try:
            with requests.get(request_url, timeout=timeout, allow_redirects=False,
                             headers=headers, stream=True) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location") or ""
                    if not location:
                        raise OcrApiError("OCR 结果地址返回了重定向但没有目标")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise OcrApiError(f"下载 OCR 结果失败（HTTP {response.status_code}）")
                if not pinned:
                    # 没有钉住（https）：连上之后核对真实对端，正文一个字节都不先读。
                    _check_peer_is_public(response)
                return _read_capped(response, limit=limit)
        except requests.RequestException as exc:
            raise OcrApiError(f"下载 OCR 结果失败：{exc}") from exc
    raise OcrApiError("OCR 结果地址重定向次数过多")


def _host_of(url: str) -> str:
    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:
        return ""


def _read_capped(response, *, limit: int) -> str:
    """流式读正文，**超过字节上限立刻中止**。"""
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise OcrApiError(
                    f"OCR 结果正文超过上限 {limit} 字节，已中止下载；"
                    "请确认这个结果地址指向的是本次任务的结果")
            chunks.append(chunk)
    except requests.RequestException as exc:
        raise OcrApiError(f"下载 OCR 结果失败：{exc}") from exc
    encoding = response.encoding or "utf-8"
    return b"".join(chunks).decode(encoding, errors="replace")


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
    """把 API 返回的 JSONL 结果展开为按页排列的文本。

    **页数与文字总量都有上限**（审查 H-07）：OCR 的页数来自 PDF，而 PDF 的
    页数已经被 ``merge_pdfs`` 限过；这里再限一次，是为了让「OCR 兜底」这条
    路**不成为绕过页数上限的口子**（文本层解析超限时正是它接手）。
    """
    pages: list[str] = []
    max_pages = max(1, int(settings.invoice_max_pdf_pages))
    max_chars = max(1, int(settings.invoice_max_text_chars))
    total = 0
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
                plain = _markdown_to_plain_text(str(text))
                total += len(plain)
                if len(pages) + 1 > max_pages:
                    raise OcrApiError(
                        f"OCR 结果有超过 {max_pages} 页，超过单次上限；"
                        "请把文件分批上传")
                if total > max_chars:
                    raise OcrApiError(
                        f"OCR 结果的文字共 {total} 个字符，超过单次上限 "
                        f"{max_chars} 个字符；请把文件分批上传")
                pages.append(plain)
    if not pages:
        raise OcrApiError("OCR 任务已完成，但结果中没有找到页面文本")
    return pages


#: OCR 返回的表格是 **HTML**（``<table><tr><td>``），不是 Markdown 管道表。
#: Markdown 管道表是「一条表格占一行」的形态，HTML 表格往往是**一整行**
#: ——不拆开的话，一整张表会变成**一行文本**，下游按行切的逻辑就全废了。
_HTML_TABLE = re.compile(r"<table[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
_HTML_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")


def _html_table_to_lines(table_html: str) -> list[str]:
    """一段 HTML 表格 → 若干行文本（每行是这一行各格的空格连接）。

    **格子里换行的是同一个值被拆成几行** —— 银行对账单里「18050.00」
    在版面上是「1805」与「0.00」上下两行。所以格内换行**不加分隔符**地
    接起来（``18050.00``），这与文字层那一层拼格子的做法一致
    （``pdf_table._row_cells`` 同样是「".join(...)」）。

    分隔符不加分，格与格之间才加空格 —— 行与行之间的列数因此对得上表头，
    下游「段数与表头相同 → 按位置对号入座」那一档才认得出。
    """
    lines: list[str] = []
    for row_html in _HTML_ROW.findall(table_html):
        cells: list[str] = []
        for cell_html in _HTML_CELL.findall(row_html):
            text = _HTML_TAG.sub(" ", cell_html)
            text = html.unescape(text)
            # OCR 用**字面的两个字符** ``\n`` 表示格内换行（不是真换行），
            # 两者都要处理 —— 只当真换行的话，格子里会留下「1805\n0.00」
            # 这么一串字面量，金额当场认不出来。
            text = text.replace("\\n", "")
            text = re.sub(r"[\r\n\t]+", "", text)
            text = re.sub(r"\s{2,}", " ", text).strip()
            cells.append(text)
        if cells:
            lines.append(" ".join(cells))
    return lines


def _markdown_to_plain_text(markdown: str) -> str:
    """保留表格单元格文本，让既有标签/正则解析器可以继续工作。"""
    out: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        if line.startswith("![") or line.startswith("<img"):
            continue
        # HTML 表格：整块拆成「一行一格」的文本行。
        if line.startswith("<table"):
            out.extend(_html_table_to_lines(line))
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


def _normalize_optional_payload(raw: Any) -> dict[str, Any]:
    """``optionalPayload`` 必须是**对象**，不能是「对象的 JSON 字符串」。

    **为什么要有这一层。** 这一项配置是从库里读的**文本**（管理页上它就是
    一个 JSON 文本框），而服务端要的是一个 JSON **对象**。直接把那段文本
    丢给 ``json.dumps``，发出去的就是**一段被引号包起来的字符串** ——
    服务端解析出来是个字符串而不是对象，于是任务直接失败，而且报的是
    「系统错误-聚合」这种**看不出原因**的错。

    **实测（2026-10-27）**：生产上 OCR 通道配置齐全（端点、token、模型
    都在库里），但每一份文件都失败；把同一份文件按官方示例的写法直接调，
    同样的 token、同样的模型，**当场就成功**。差别只有这一个字段的类型。

    两种入参都收：字典直接用；字符串先解析，解析不出来就当空对象 ——
    **宁可少传可选参数，也不能把整个任务搞失败**（那几个开关只影响
    版面预处理的方向分类/去扭曲/图表识别，缺了不影响文字识别）。
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


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
    # optionalPayload 必须是**对象**。配置里存的是文本，直接 dumps 会多套
    # 一层引号，服务端解析出来是个字符串而不是对象，任务会以
    # 「系统错误-聚合」失败 —— 见 :func:`_normalize_optional_payload`。
    data = {"model": model,
            "optionalPayload": json.dumps(_normalize_optional_payload(optional_payload),
                                          ensure_ascii=False)}
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
            # OCR 结果同样可能带 XML 非法字符（PDF 文字层/OCR 都见过
            # U+FFFE）。解析层统一挡掉，别让脏字符进摘要 ——
            # 详见 ``app.invoice.text_parser.scrub_control_chars``。
            return [scrub_control_chars(p) for p in _markdown_pages(result_text)]
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
