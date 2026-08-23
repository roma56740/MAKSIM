from __future__ import annotations

import base64
import json
import logging
import math
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ITEMS = 150
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
SUPPORTED_SPREADSHEET_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}
SUPPORTED_MIME_TYPES = {
    "application/pdf",
    *SUPPORTED_IMAGE_MIME_TYPES,
    *SUPPORTED_SPREADSHEET_MIME_TYPES,
}


class InvoiceRecognitionError(RuntimeError):
    """Понятная ошибка распознавания накладной."""


@dataclass(frozen=True)
class InvoiceFile:
    data: bytes
    filename: str
    mime_type: str


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is not None:
        return _client

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise InvoiceRecognitionError("В проекте не настроен ключ распознавания накладных.")

    _client = AsyncOpenAI(api_key=api_key, timeout=120.0, max_retries=2)
    return _client


def _model_name() -> str:
    value = (
        os.getenv("OPENAI_INVOICE_MODEL")
        or os.getenv("OPENAI_MODEL")
        or "gpt-4o-mini"
    ).strip()
    return value or "gpt-4o-mini"


def detect_mime_type(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def normalize_mime_type(filename: str | None, mime_type: str | None) -> str:
    filename_lower = (filename or "").casefold()
    if filename_lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if filename_lower.endswith(".xls"):
        return "application/vnd.ms-excel"
    value = (mime_type or "").split(";", 1)[0].strip().lower()
    if value == "image/jpg":
        value = "image/jpeg"
    if value in SUPPORTED_MIME_TYPES:
        return value

    guessed, _ = mimetypes.guess_type(filename or "")
    guessed = (guessed or "").lower()
    if guessed == "image/jpg":
        guessed = "image/jpeg"
    return guessed


def validate_invoice_file(file: InvoiceFile) -> None:
    if not file.data:
        raise InvoiceRecognitionError("Файл накладной пустой. Отправьте его ещё раз.")
    if len(file.data) > MAX_FILE_BYTES:
        raise InvoiceRecognitionError("Файл слишком большой. Максимальный размер — 20 МБ.")
    if file.mime_type not in SUPPORTED_MIME_TYPES:
        raise InvoiceRecognitionError("Поддерживаются PDF, JPG, PNG, WEBP, XLSX и XLS.")


INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document_type": {"type": "string"},
        "invoice_number": {"type": ["string", "null"]},
        "invoice_date": {"type": ["string", "null"]},
        "supplier": {"type": ["string", "null"]},
        "buyer": {"type": ["string", "null"]},
        "responsible_manager": {"type": ["string", "null"]},
        "currency": {"type": "string"},
        "total_amount": {"type": ["number", "null"]},
        "vat_amount": {"type": ["number", "null"]},
        "confidence": {"type": "number"},
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "article": {"type": ["string", "null"]},
                    "product_name": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit": {"type": ["string", "null"]},
                    "unit_price": {"type": "number"},
                    "line_total": {"type": "number"},
                },
                "required": [
                    "article",
                    "product_name",
                    "quantity",
                    "unit",
                    "unit_price",
                    "line_total",
                ],
            },
        },
    },
    "required": [
        "document_type",
        "invoice_number",
        "invoice_date",
        "supplier",
        "buyer",
        "responsible_manager",
        "currency",
        "total_amount",
        "vat_amount",
        "confidence",
        "warnings",
        "items",
    ],
}


PROMPT = """
Ты анализируешь российские счета, заказы, товарные накладные и фотографии таких документов.
Верни строго структурированные данные по документу.

Правила распознавания:
1. Извлекай только реальные товарные позиции из основной таблицы.
2. Не считай товаром заголовки групп, названия брендов, поставщика, покупателя, строки «Итого», НДС, доставку без отдельной цены и служебный текст.
3. Сохраняй полное наименование товара максимально близко к документу, без выдуманных сокращений.
4. quantity — количество проданных единиц; unit_price — цена за одну единицу; line_total — сумма строки.
5. Если сумма строки плохо читается, вычисли quantity × unit_price и добавь предупреждение.
6. Если артикул, единица измерения, номер, дата или реквизит отсутствуют либо не читаются — используй null, ничего не придумывай.
7. currency обычно RUB, но определи её по документу.
8. total_amount — итоговая сумма документа. Не путай её с НДС.
9. confidence — уверенность от 0 до 1. В warnings кратко перечисли сомнительные или нечитаемые места.
10. В документе может быть до 150 позиций и несколько страниц. Проверь все страницы.
11. Числа возвращай без пробелов-разделителей и без символов валюты.
12. Если документ не является накладной/счётом или товарные позиции не найдены, верни пустой items и объясни причину в warnings.
""".strip()


def _data_url(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _finite_number(value: Any, *, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _clean_optional_text(value: Any, max_length: int = 500) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    if not text:
        return None
    return text[:max_length]


def _clean_result(raw: dict[str, Any]) -> dict[str, Any]:
    warnings = [
        " ".join(str(item).split())[:300]
        for item in (raw.get("warnings") or [])
        if str(item).strip()
    ][:20]

    cleaned_items: list[dict[str, Any]] = []
    for index, source in enumerate(raw.get("items") or [], start=1):
        if not isinstance(source, dict):
            warnings.append(f"Позиция {index} пропущена: неверный формат.")
            continue

        name = _clean_optional_text(source.get("product_name"), 500)
        quantity = _finite_number(source.get("quantity"))
        unit_price = _finite_number(source.get("unit_price"))
        source_total = _finite_number(source.get("line_total"))

        if not name or quantity <= 0 or unit_price < 0:
            warnings.append(f"Позиция {index} пропущена: не удалось надёжно прочитать название, количество или цену.")
            continue

        calculated_total = round(quantity * unit_price, 2)
        tolerance = max(1.0, abs(calculated_total) * 0.01)
        if source_total <= 0 or abs(source_total - calculated_total) > tolerance:
            if source_total > 0:
                warnings.append(
                    f"Позиция {index}: сумма строки проверена и пересчитана по количеству и цене."
                )
            source_total = calculated_total

        cleaned_items.append(
            {
                "article": _clean_optional_text(source.get("article"), 100),
                "product_name": name,
                "quantity": round(quantity, 4),
                "unit": _clean_optional_text(source.get("unit"), 30),
                "unit_price": round(unit_price, 2),
                "line_total": round(source_total, 2),
            }
        )
        if len(cleaned_items) >= MAX_ITEMS:
            warnings.append(f"Взяты первые {MAX_ITEMS} позиций — это максимальное количество для одной накладной.")
            break

    calculated_total = round(sum(item["line_total"] for item in cleaned_items), 2)
    total_amount_raw = raw.get("total_amount")
    total_amount = None if total_amount_raw is None else round(_finite_number(total_amount_raw), 2)
    if total_amount is not None and total_amount < 0:
        total_amount = None

    if total_amount is None and cleaned_items:
        total_amount = calculated_total
        warnings.append("Итог документа не прочитан — он рассчитан по товарным позициям.")
    elif total_amount is not None and cleaned_items:
        tolerance = max(2.0, abs(total_amount) * 0.01)
        if abs(total_amount - calculated_total) > tolerance:
            warnings.append(
                "Напечатанный итог отличается от суммы распознанных позиций. Перед принятием проверьте документ."
            )

    confidence = min(1.0, max(0.0, _finite_number(raw.get("confidence"), default=0.0)))
    if not cleaned_items:
        confidence = min(confidence, 0.35)
        if not warnings:
            warnings.append("Товарные позиции не найдены.")

    return {
        "document_type": _clean_optional_text(raw.get("document_type"), 100) or "Накладная / счёт",
        "invoice_number": _clean_optional_text(raw.get("invoice_number"), 150),
        "invoice_date": _clean_optional_text(raw.get("invoice_date"), 100),
        "supplier": _clean_optional_text(raw.get("supplier"), 500),
        "buyer": _clean_optional_text(raw.get("buyer"), 500),
        "responsible_manager": _clean_optional_text(raw.get("responsible_manager"), 300),
        "currency": (_clean_optional_text(raw.get("currency"), 10) or "RUB").upper(),
        "total_amount": total_amount,
        "calculated_total": calculated_total,
        "vat_amount": None if raw.get("vat_amount") is None else round(_finite_number(raw.get("vat_amount")), 2),
        "confidence": round(confidence, 3),
        "warnings": list(dict.fromkeys(warnings)),
        "items": cleaned_items,
        "recognized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


async def recognize_invoice(file: InvoiceFile) -> dict[str, Any]:
    validate_invoice_file(file)

    if file.mime_type in SUPPORTED_SPREADSHEET_MIME_TYPES:
        raise InvoiceRecognitionError("Excel-файлы должны обрабатываться табличным распознавателем.")

    content: list[dict[str, Any]] = [{"type": "input_text", "text": PROMPT}]
    if file.mime_type == "application/pdf":
        content.append(
            {
                "type": "input_file",
                "filename": file.filename or "invoice.pdf",
                "file_data": _data_url(file.data, file.mime_type),
            }
        )
    else:
        content.append(
            {
                "type": "input_image",
                "image_url": _data_url(file.data, file.mime_type),
                "detail": "high",
            }
        )

    try:
        response = await _get_client().responses.create(
            model=_model_name(),
            input=[{"role": "user", "content": content}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "invoice_extraction",
                    "description": "Структурированные данные накладной или счёта",
                    "strict": True,
                    "schema": INVOICE_SCHEMA,
                }
            },
            temperature=0,
            store=False,
        )
    except Exception as exc:  # SDK/API details must not leak to Telegram users
        logger.exception("Invoice recognition API request failed")
        raise InvoiceRecognitionError(
            "Не удалось распознать документ. Попробуйте ещё раз немного позже."
        ) from exc

    output_text = (getattr(response, "output_text", None) or "").strip()
    if not output_text:
        raise InvoiceRecognitionError("Сервис не вернул результат распознавания.")

    try:
        raw = json.loads(output_text)
    except json.JSONDecodeError as exc:
        logger.error("Invoice recognition returned invalid JSON: %s", output_text[:1000])
        raise InvoiceRecognitionError("Результат распознавания получен в неверном формате.") from exc

    if not isinstance(raw, dict):
        raise InvoiceRecognitionError("Результат распознавания получен в неверном формате.")

    result = _clean_result(raw)
    if not result["items"]:
        warning = result["warnings"][0] if result["warnings"] else "Товарные позиции не найдены."
        raise InvoiceRecognitionError(warning)
    return result
