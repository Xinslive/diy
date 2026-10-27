"""OCR 请求的 ``optionalPayload`` 必须是**对象**，不能是「对象的 JSON 字符串」。

**这条坑踩过的现场**（2026-10-27）：生产上 OCR 的端点、token、模型全都配在
库里，看起来「接好了」，但**每一份文件都失败**，报的是「系统错误-聚合」——
一个看不出原因的服务端错误。把同一份文件按官方示例的写法直接调，同样的
token、同样的模型，当场就成功。

差别只有一个字段：``optionalPayload``。配置里存的是**文本**（管理页上它就是
一个 JSON 文本框），直接 ``json.dumps`` 一次，发出去的是**被引号包起来的
字符串**；服务端要的是 JSON **对象**。

所以这里钉住三件事：

1. 传字符串 → 发出的是对象；
2. 传字典 → 原样发出；
3. 传坏 JSON → **降级成空对象**，而不是让整个任务失败
   （那几个开关只影响版面预处理，缺了不影响文字识别）。
"""
from __future__ import annotations

import json

import pytest

from app.invoice import ocr_client
from app.invoice.ocr_client import OcrApiError, ocr_pdf_to_texts

_RAW = '{"useDocOrientationClassify":false,"useDocUnwarping":false,"useChartRecognition":false}'


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.encoding = "utf-8"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeRequests:
    """只实现「提交任务」这一段：提交完立刻让任务失败，好把请求体截下来。"""

    #: 客户端的异常处理会引用它（``except requests.RequestException``）。
    RequestException = OSError

    def __init__(self):
        self.posted: dict | None = None

    def post(self, url, headers=None, data=None, files=None, timeout=None):
        self.posted = dict(data or {})
        return _Response({"code": 0, "data": {"jobId": "job-1"}})

    def get(self, url, headers=None, timeout=None, **kwargs):
        return _Response({"code": 0, "data": {"state": "failed", "errorMsg": "到此为止"}})


@pytest.fixture()
def fake_requests(monkeypatch):
    fake = _FakeRequests()
    monkeypatch.setattr(ocr_client, "requests", fake)
    return fake


@pytest.fixture()
def pdf_path(tmp_path) -> str:
    """真的建一个文件 —— 客户端是 ``open()`` 出来再上传的。"""
    path = tmp_path / "对账单.pdf"
    path.write_bytes(b"%PDF-1.4\n% fake\n")
    return str(path)


def _run(fake_requests, pdf_path, optional_payload) -> dict:
    with pytest.raises(OcrApiError):
        ocr_pdf_to_texts(
            pdf_path,
            job_url="https://ocr.example.com/api/v2/ocr/jobs",
            token="t",
            model="PaddleOCR-VL-1.6",
            optional_payload=optional_payload,
        )
    assert fake_requests.posted is not None
    return fake_requests.posted


def test_配置里的JSON文本要发成对象(fake_requests, pdf_path):
    """库里读出来的是**字符串**（管理页上它就是一个 JSON 文本框）。"""
    sent = _run(fake_requests, pdf_path, _RAW)
    value = json.loads(sent["optionalPayload"])
    assert isinstance(value, dict), f"optionalPayload 必须是对象，实得 {type(value).__name__}"
    assert value["useDocOrientationClassify"] is False
    assert value["useDocUnwarping"] is False
    assert value["useChartRecognition"] is False


def test_字典原样发出(fake_requests, pdf_path):
    """已经是对象时不该被再包一层。"""
    sent = _run(fake_requests, pdf_path, {"useChartRecognition": False})
    value = json.loads(sent["optionalPayload"])
    assert isinstance(value, dict)
    assert value == {"useChartRecognition": False}


def test_坏JSON降级成空对象而不是让任务失败(fake_requests, pdf_path):
    """宁可少传可选参数，也不能把整个 OCR 任务搞失败。"""
    sent = _run(fake_requests, pdf_path, "{这不是 JSON")
    value = json.loads(sent["optionalPayload"])
    assert value == {}


def test_空值也发成空对象(fake_requests, pdf_path):
    for raw in (None, "", "   "):
        sent = _run(fake_requests, pdf_path, raw)
        assert json.loads(sent["optionalPayload"]) == {}


def test_模型名照原样发出去(fake_requests, pdf_path):
    """修 optionalPayload 时别把 model 弄坏了。"""
    sent = _run(fake_requests, pdf_path, _RAW)
    assert sent["model"] == "PaddleOCR-VL-1.6"
