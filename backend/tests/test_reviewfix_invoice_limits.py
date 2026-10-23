"""审查修复：发票解析的容量闸与 OCR 抓取的上限（H-09 / L-24）。

**H-09（中）：发票 PDF 无页数/文本上限，OCR 结果响应体无大小上限。**

* ``extract_pages_text`` 在**读页之前**判页数；逐页累加判文字总量；
* ``merge_pdfs`` 在**合并之前**判总页数；
* OCR 结果改为**流式读取 + 字节上限**（不再用 ``response.text``）。

**L-24（低）：SSRF 校验的 DNS 重绑定 TOCTOU。**

校验用 ``getaddrinfo`` 判「全部公网」，随后 ``requests`` **会再解析一次** ——
两次解析之间，攻击者控制的 DNS 可以第一次给公网 IP、第二次给 ``127.0.0.1``。
现在：解析结果进**短 TTL 缓存**，连接**直接用已校验的那个 IP**（Host 头保留
原主机名）；https 因为证书按主机名签发而不能改连 IP，改为**连上之后核对真实
对端地址**，正文一个字节都不先读。
"""
from __future__ import annotations

import types

import pytest

from app.core.config import settings
from app.invoice import ocr_client, storage, text_parser


class _FakePage:
    def __init__(self, text: str):
        self._text = text

    def get_textpage(self):
        return self

    def get_text_range(self):
        return self._text


class _FakeDoc:
    """一份假的 PDF：页数与每页文字都可控。"""

    def __init__(self, pages: int = 1, text: str = "发票号码：12345678"):
        self._pages = pages
        self._text = text
        self.closed = False

    def __len__(self):
        return self._pages

    def __getitem__(self, index):
        return _FakePage(self._text)

    def close(self):
        self.closed = True


# --------------------------------------------------------------------- 页数


def test_页数超过上限时在读页之前就报错(monkeypatch, tmp_path):
    """页数闸在**读页之前**判 —— 500 页的 PDF 不该被逐页取文本层。"""
    monkeypatch.setattr(settings, "invoice_max_pdf_pages", 3)
    fake = _FakeDoc(pages=10, text="x" * 100)
    monkeypatch.setattr(text_parser, "p", types.SimpleNamespace(PdfDocument=lambda path: fake))

    with pytest.raises(ValueError) as exc:
        text_parser.extract_pages_text(str(tmp_path / "x.pdf"))

    message = str(exc.value)
    assert "10" in message and "3" in message, message
    assert "分批" in message or "批" in message, message


def test_页数在上限之内就正常读(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "invoice_max_pdf_pages", 3)
    fake = _FakeDoc(pages=3, text="发票号码：12345678")
    monkeypatch.setattr(text_parser, "p", types.SimpleNamespace(PdfDocument=lambda path: fake))

    pages = text_parser.extract_pages_text(str(tmp_path / "x.pdf"))
    assert len(pages) == 3
    assert fake.closed, "读完必须把文档关掉"


# --------------------------------------------------------------------- 文字量


def test_文字总量超过上限时中止并报错(monkeypatch, tmp_path):
    """文字总量按**所有页加起来**算，不看单页。"""
    monkeypatch.setattr(settings, "invoice_max_pdf_pages", 100)
    monkeypatch.setattr(settings, "invoice_max_text_chars", 250)
    fake = _FakeDoc(pages=10, text="x" * 100)
    monkeypatch.setattr(text_parser, "p", types.SimpleNamespace(PdfDocument=lambda path: fake))

    with pytest.raises(ValueError) as exc:
        text_parser.extract_pages_text(str(tmp_path / "x.pdf"))
    message = str(exc.value)
    assert "250" in message, message


# --------------------------------------------------------------------- 合并


def test_合并前先校验总页数(monkeypatch, tmp_path):
    """合并出来的 PDF 会被逐页解析 ⇒ 总页数要在**合并之前**就判。"""
    import pypdfium2 as pdfium

    monkeypatch.setattr(settings, "invoice_max_pdf_pages", 5)

    class _Source:
        def __init__(self, pages):
            self._pages = pages

        def __len__(self):
            return self._pages

        def close(self):
            pass

    monkeypatch.setattr(pdfium, "PdfDocument", lambda path: _Source(4))

    with pytest.raises(ValueError) as exc:
        storage.merge_pdfs([("a.pdf", "甲.pdf"), ("b.pdf", "乙.pdf")], 1)
    message = str(exc.value)
    assert "8" in message and "5" in message, message
    assert "乙.pdf" in message, message


# --------------------------------------------------------------------- OCR 字节上限


class _FakeResponse:
    """一个只会流式吐内容的假响应。"""

    def __init__(self, chunks, encoding=None):
        self._chunks = chunks
        self.encoding = encoding
        self.raw = types.SimpleNamespace(_connection=types.SimpleNamespace(sock=None))

    def iter_content(self, chunk_size=1024):
        for chunk in self._chunks:
            yield chunk


def test_OCR结果超过字节上限时中止(monkeypatch):
    """正文按块读、边读边计数；超了立刻中止（不等它全部下完）。"""
    monkeypatch.setattr(settings, "invoice_ocr_max_response_bytes", 1000)
    response = _FakeResponse([b"x" * 600, b"y" * 600])

    with pytest.raises(ocr_client.OcrApiError) as exc:
        ocr_client._read_capped(response, limit=1000)
    assert "1000" in str(exc.value), str(exc.value)


def test_OCR结果在上限之内就正常读完(monkeypatch):
    monkeypatch.setattr(settings, "invoice_ocr_max_response_bytes", 1000)
    response = _FakeResponse([b"x" * 600, b"y" * 100])
    assert ocr_client._read_capped(response, limit=1000) == "x" * 600 + "y" * 100


def test_OCR结果的页数与文字量也有上限(monkeypatch):
    """OCR 兜底这条路**不能成为绕过页数/文字上限的口子**。"""
    monkeypatch.setattr(settings, "invoice_max_pdf_pages", 2)
    monkeypatch.setattr(settings, "invoice_max_text_chars", 1000)
    row = {"result": {"layoutParsingResults": [{"markdown": {"text": "发票内容"}}]}}
    import json
    jsonl = "\n".join(json.dumps(row) for _ in range(5))

    with pytest.raises(ocr_client.OcrApiError) as exc:
        ocr_client._markdown_pages(jsonl)
    assert "2" in str(exc.value), str(exc.value)


# --------------------------------------------------------------------- L-24


def test_连接目标被钉到已校验的IP上():
    """**校验用哪次解析，连接就用哪次** —— 那个空档就是 DNS 重绑定。"""
    pinned, url, headers = ocr_client._pin("http://example.test:8080/a/b?c=1", ["93.184.216.34"])
    assert pinned is True
    assert url == "http://93.184.216.34:8080/a/b?c=1", url
    # Host 头保留原主机名 —— 服务端看到的还是它认识的名字。
    assert headers["Host"] == "example.test:8080", headers


def test_https不改成连IP_改用连上之后核对真实对端():
    """https 的证书按主机名签发 ⇒ 不能改成连 IP（那等于关掉 TLS 校验）。"""
    pinned, url, headers = ocr_client._pin("https://example.test/a", ["93.184.216.34"])
    assert pinned is False
    assert url == "https://example.test/a"
    assert headers == {}


def test_解析结果有短TTL缓存_校验与连接用的是同一批地址(monkeypatch):
    """短 TTL 缓存让「校验」与「连接」用**同一批地址**。"""
    calls: list[str] = []

    def fake_getaddrinfo(host, port, **kwargs):
        calls.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(ocr_client.socket, "getaddrinfo", fake_getaddrinfo)
    ocr_client._RESOLUTIONS.clear()

    first = ocr_client._resolve("cache.test")
    second = ocr_client._resolve("cache.test")
    assert first == second == ["93.184.216.34"]
    assert len(calls) == 1, f"缓存没生效，解析了 {len(calls)} 次"


def test_连上之后发现对端是内网就拒绝且不读正文():
    """对端不是公网 ⇒ 拒绝，**正文一个字节都不读**。"""
    import ipaddress

    class _Sock:
        def getpeername(self):
            return ("127.0.0.1", 12345)

    response = _FakeResponse(["内部机密".encode()])
    response.raw = types.SimpleNamespace(_connection=types.SimpleNamespace(sock=_Sock()))

    with pytest.raises(ocr_client.OcrApiError) as exc:
        ocr_client._check_peer_is_public(response)
    assert "内网" in str(exc.value) or "重绑定" in str(exc.value), str(exc.value)


def test_错误信息不回显内网内容():
    """失败时**不回显响应正文** —— 那正是这条防线要防的东西。"""
    error = ocr_client.OcrApiError("下载 OCR 结果失败（HTTP 500）")
    assert "内部机密" not in str(error)
