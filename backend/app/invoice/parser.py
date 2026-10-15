"""发票解析调度：本地文本层优先，必要时调用 PaddleOCR API。"""
from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.invoice.ocr_client import OcrApiError, ocr_pdf_to_texts, optional_payload_from_env
from app.invoice.text_parser import Invoice, extract_pages_text, parse_page


MIN_TEXT_LEN_PER_PAGE = 30
MIN_LINES_WITH_DETAIL = 0.5


def is_text_extraction_poor(pages_text: list[str], invoices: list[Invoice]) -> bool:
    if not pages_text:
        return True
    avg_len = sum(len(text.strip()) for text in pages_text) / max(len(pages_text), 1)
    if avg_len < MIN_TEXT_LEN_PER_PAGE:
        return True
    pages_with_lines = sum(1 for invoice in invoices if invoice.lines)
    return bool(invoices and pages_with_lines / len(invoices) < MIN_LINES_WITH_DETAIL)


def _append_warning(invoice: Invoice, message: str) -> None:
    invoice.parse_warning = f"{invoice.parse_warning}; {message}" if invoice.parse_warning else message


def parse_pdf_with_fallback(pdf_path: str) -> tuple[list[Invoice], bool]:
    """返回 ``(发票列表, 是否使用 OCR)``。"""
    try:
        pages_text = extract_pages_text(pdf_path)
        invoices = [parse_page(index, text) for index, text in enumerate(pages_text)]
    except Exception as exc:  # noqa: BLE001
        pages_text = []
        invoices = []
        local_error = str(exc)
    else:
        local_error = ""

    ocr_configured = bool(settings.invoice_ocr_api_url and settings.invoice_ocr_api_token)
    if not is_text_extraction_poor(pages_text, invoices) and invoices:
        return invoices, False
    if not ocr_configured:
        if not invoices:
            raise ValueError(local_error or "PDF 未解析出任何页面")
        for invoice in invoices:
            _append_warning(invoice, "文本层解析不充分，未配置 OCR API 兜底")
        return invoices, False

    try:
        ocr_pages = ocr_pdf_to_texts(
            pdf_path,
            job_url=settings.invoice_ocr_api_url,
            token=settings.invoice_ocr_api_token,
            model=settings.invoice_ocr_model,
            optional_payload=optional_payload_from_env(settings.invoice_ocr_optional_payload),
            timeout_seconds=settings.invoice_ocr_timeout_seconds,
            poll_seconds=settings.invoice_ocr_poll_seconds,
        )
    except OcrApiError as exc:
        if not invoices:
            raise ValueError(f"PDF 文本解析失败，OCR API 兜底也失败：{exc}") from exc
        for invoice in invoices:
            _append_warning(invoice, f"OCR API 兜底失败：{exc}")
        return invoices, False

    # OCR 是否使用已经通过返回值传给批次，成功本身不是需要用户修正的解析警告。
    # 这里只保留页面字段解析产生的真实问题，避免把成功的 OCR 兜底标成红色阻断项。
    return [parse_page(index, text) for index, text in enumerate(ocr_pages)], True


def invoice_dict(invoice: Invoice) -> dict[str, Any]:
    """去掉原始全文，避免把 OCR 文本重复塞进 JSONB；原 PDF 是事实原件。"""
    value = invoice.to_dict()
    value.pop("raw_text", None)
    return value
