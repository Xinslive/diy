"""OCR 结果地址的 SSRF 防线（审查报告 P2-2）。

``jsonUrl`` 是**不可信输入**：它由 OCR 服务在任务结果里给出，而 OCR 服务
本身可能被诱导指向内网。抓它之前不校验，就是一条二阶 SSRF —— 上传一张
无文字层的 PDF 就能让后端替攻击者访问内网，**并把内网的响应内容读回来**
（响应被解析成页面文本、写进发票记录、回显给上传者）。

这里钉住三件事：

1. 协议只允许 http/https；
2. 主机必须解析到**公网**地址（环回 / 私有段 / 链路本地 / CGNAT / 保留段
   一律拒绝，云厂商的元数据地址 169.254.169.254 就在被拒的段里）；
3. **每一跳**重定向都重新校验 —— 「第一跳公网、第二跳内网」也要挡住。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.invoice import ocr_client
from app.invoice.ocr_client import OcrApiError, check_public_url, ocr_pdf_to_texts


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:15432/",
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://[::1]:8080/x",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://0.0.0.0/",
    "http://100.64.0.1/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "file:///etc/passwd",
    "gopher://127.0.0.1:56503/_evil",
    "ftp://127.0.0.1/x",
])
def test_内网与保留地址一律拒绝(url):
    """环回 / 私有段 / 链路本地 / 保留段 / 非 http 协议，全部拒绝。"""
    with pytest.raises(OcrApiError):
        check_public_url(url)


def test_公网地址放行():
    """别把闸修成「全拦」—— 正常的 OCR 结果地址要能抓。"""
    # 用 IP 字面量，免得这条用例依赖 DNS。
    assert check_public_url("http://93.184.216.34/result.jsonl") == "http://93.184.216.34/result.jsonl"


class _FakeOcrHandler(BaseHTTPRequestHandler):
    """假 OCR 服务：提交任务 → 报 done → 结果地址指向「内网服务」。

    三条路径：

    * ``/job-1``   —— 任务查询，回 done + 结果地址；
    * ``/result``  —— 结果正文（正常是 JSONL；``redirect`` 模式下回 302）；
    * ``/internal/secret`` —— 「内网服务」，**任何用例都不许打到它**。
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - 测试里不要刷屏
        pass

    def _send(self, payload, status=200, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_jsonl(self, text: str):
        """OCR 结果正文（JSONL 一行）—— 这就是「内网机密」被读回来的样子。"""
        body = json.dumps({
            "result": {"layoutParsingResults": [
                {"markdown": {"text": f"发票号码：24417000000077777777 {text}"}}]},
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/jsonl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._send({"data": {"jobId": "job-1"}})

    def do_GET(self):  # noqa: N802
        self.server.hits.append(self.path)  # type: ignore[attr-defined]
        if self.path.startswith("/job-1"):
            self._send({"data": {"state": "done", "resultUrl": {
                "jsonUrl": f"http://127.0.0.1:{self.server.server_address[1]}/result"}}})
            return
        if self.path.startswith("/result"):
            if self.server.redirect_to_internal:  # type: ignore[attr-defined]
                # 第一跳落在公网地址上，第二跳指向内网 —— 每一跳都要重新校验。
                self._send({}, status=302, headers={
                    "Location": "http://10.0.0.5/internal/secret"})
                return
            self._send_jsonl("内网机密")
            return
        # 「内网服务」的响应：里面是攻击者想读出来的内容
        self._send_jsonl("内网机密")


@pytest.fixture
def fake_ocr():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOcrHandler)
    server.hits = []  # type: ignore[attr-defined]
    server.redirect_to_internal = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _ocr_args(server):
    return dict(
        job_url=f"http://127.0.0.1:{server.server_address[1]}",
        token="token",
        model="PaddleOCR-VL",
        optional_payload={},
        poll_seconds=0.1,
    )


def test_结果地址指向内网时后端不替攻击者去访问(tmp_path, fake_ocr):
    """OCR 服务给一个内网地址 → 拒绝，**结果一次都没被抓**。

    这里走的是**真的**校验：``127.0.0.1`` 是环回地址 → 直接被拒，
    连抓取动作都不发生。
    """
    pdf = tmp_path / "发票.pdf"
    pdf.write_bytes(b"%PDF-1.7 not really a pdf")

    with pytest.raises(OcrApiError) as err:
        ocr_pdf_to_texts(str(pdf), **_ocr_args(fake_ocr))

    assert "内网" in str(err.value), str(err.value)
    assert not any(path.startswith("/result") for path in fake_ocr.hits), \
        f"后端替攻击者访问了内网：{fake_ocr.hits}"
    assert not any(path.startswith("/internal") for path in fake_ocr.hits), \
        f"后端替攻击者访问了内网：{fake_ocr.hits}"


def test_跳转到内网同样被拒(tmp_path, fake_ocr, monkeypatch):
    """第一跳放行、第二跳指向内网 —— **每一跳**都要重新校验。

    校验函数在这里被换成「第一跳放行、第二跳拒绝」的桩：真服务器只能跑在
    回环上（回环本来就该被拒），而这一条要验的是**跳转落点也会被校验**这件事。
    真实的网段判定由上面的参数化用例与
    ``test_结果地址指向内网时后端不替攻击者去访问`` 覆盖。
    """
    fake_ocr.redirect_to_internal = True
    checked: list[str] = []

    def fake_check(url: str) -> str:
        checked.append(url)
        if len(checked) > 1:
            raise OcrApiError("OCR 结果地址指向内网或保留地址，已拒绝抓取")
        return url

    monkeypatch.setattr(ocr_client, "check_public_url", fake_check)

    pdf = tmp_path / "发票.pdf"
    pdf.write_bytes(b"%PDF-1.7 not really a pdf")

    with pytest.raises(OcrApiError) as err:
        ocr_pdf_to_texts(str(pdf), **_ocr_args(fake_ocr))

    assert "内网" in str(err.value), str(err.value)
    assert len(checked) == 2, f"跳转落点没有被校验：{checked}"
    assert "10.0.0.5" in checked[1], f"第二跳校验的不是跳转落点：{checked}"
    assert not any(path.startswith("/internal") for path in fake_ocr.hits), \
        f"后端跟着重定向访问了内网：{fake_ocr.hits}"


def test_正常的结果地址仍然抓得到(tmp_path, fake_ocr, monkeypatch):
    """闸不能修成「全拦」：正常路径要照旧工作。"""
    monkeypatch.setattr(ocr_client, "check_public_url", lambda url: url)
    pdf = tmp_path / "发票.pdf"
    pdf.write_bytes(b"%PDF-1.7 not really a pdf")

    pages = ocr_pdf_to_texts(str(pdf), **_ocr_args(fake_ocr))
    assert pages, "正常路径要能拿到页面文本"
    assert "24417000000077777777" in pages[0]
    assert any(path.startswith("/result") for path in fake_ocr.hits), \
        "正常路径要真的抓到结果"
