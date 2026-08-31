from __future__ import annotations

import base64
import json
import logging
import math
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ITEMS = 150
API_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
HEIF_MIME_TYPES = {"image/heic", "image/heif"}
SUPPORTED_IMAGE_MIME_TYPES = {*API_IMAGE_MIME_TYPES, *HEIF_MIME_TYPES}
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

    _client = AsyncOpenAI(api_key=api_key, timeout=180.0, max_retries=2)
    return _client


def _model_candidates(preferred: str | None = None) -> list[str]:
    """Сначала используем модель, к которой у проекта уже точно есть доступ."""
    values = [
        preferred,
        (os.getenv("OPENAI_INVOICE_MODEL") or "").strip(),
        (os.getenv("OPENAI_MODEL") or "").strip(),
        (os.getenv("OPENAI_INVOICE_FALLBACK_MODEL") or "").strip(),
        "gpt-4o-mini",
        "gpt-4o",
    ]
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() not in {"0", "false", "no", "off"}


def detect_mime_type(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12].lower()
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"}:
            return "image/heic"
        if brand in {b"mif1", b"msf1"}:
            return "image/heif"
    return ""


def normalize_mime_type(filename: str | None, mime_type: str | None) -> str:
    filename_lower = (filename or "").casefold()
    if filename_lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if filename_lower.endswith(".xls"):
        return "application/vnd.ms-excel"
    if filename_lower.endswith(".heic"):
        return "image/heic"
    if filename_lower.endswith(".heif"):
        return "image/heif"
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
        raise InvoiceRecognitionError("Поддерживаются PDF, JPG, PNG, WEBP, HEIC, HEIF, XLSX и XLS.")


def _convert_heif_to_jpeg(file: InvoiceFile) -> InvoiceFile:
    if file.mime_type not in HEIF_MIME_TYPES:
        return file
    try:
        from PIL import Image, ImageOps
        from pillow_heif import register_heif_opener

        register_heif_opener()
        with Image.open(BytesIO(file.data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=92, optimize=True)
            data = output.getvalue()
    except Exception as exc:
        logger.exception("Could not convert HEIC/HEIF invoice")
        raise InvoiceRecognitionError(
            "Не удалось открыть HEIC/HEIF. Отправьте фото ещё раз или сохраните его как JPG."
        ) from exc
    if not data or len(data) > MAX_FILE_BYTES:
        raise InvoiceRecognitionError("Фото после преобразования получилось слишком большим. Отправьте JPG до 20 МБ.")
    stem = (file.filename or "invoice").rsplit(".", 1)[0]
    return InvoiceFile(data=data, filename=f"{stem}.jpg", mime_type="image/jpeg")


NULLABLE_NUMBER = {"type": ["number", "null"]}

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
        "subtotal_before_discount": NULLABLE_NUMBER,
        "discount_amount": NULLABLE_NUMBER,
        "amount_payable": NULLABLE_NUMBER,
        # Оставлено для совместимости с уже сохранёнными результатами.
        "total_amount": NULLABLE_NUMBER,
        "vat_amount": NULLABLE_NUMBER,
        "confidence": {"type": "number"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "article": {"type": ["string", "null"]},
                    "product_name": {"type": "string"},
                    "quantity": NULLABLE_NUMBER,
                    "unit": {"type": ["string", "null"]},
                    "unit_price_before_discount": NULLABLE_NUMBER,
                    "discount_percent": NULLABLE_NUMBER,
                    "discount_amount": NULLABLE_NUMBER,
                    "unit_price": NULLABLE_NUMBER,
                    "line_total": NULLABLE_NUMBER,
                },
                "required": [
                    "article",
                    "product_name",
                    "quantity",
                    "unit",
                    "unit_price_before_discount",
                    "discount_percent",
                    "discount_amount",
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
        "subtotal_before_discount",
        "discount_amount",
        "amount_payable",
        "total_amount",
        "vat_amount",
        "confidence",
        "warnings",
        "items",
    ],
}


PROMPT = """
Ты внимательно читаешь российские счета, заказы, УПД, товарные накладные и фотографии таких документов.
Нужен точный результат для учёта продаж. Перепроверь каждую цифру по самому документу.

Правила:
1. Извлеки все реальные товарные строки из основной таблицы на всех страницах. Не пропускай продолжение таблицы.
2. Не считай товаром заголовки, названия групп, поставщика, покупателя, подписи, НДС, подытоги и служебный текст.
3. product_name — полное название ровно по документу. Ничего не сокращай и не придумывай.
4. quantity — проданное количество. unit_price — фактическая цена одной единицы ПОСЛЕ скидки. line_total — фактическая сумма строки ПОСЛЕ скидки.
5. Если в строке указаны цена до скидки, процент или сумма скидки, заполни отдельные поля. Не подменяй цену после скидки базовой ценой.
6. Если сумма строки отличается от quantity × напечатанная цена, приоритет у итоговой суммы строки: это может быть скидка. Тогда unit_price рассчитай как line_total / quantity, а исходную цену сохрани в unit_price_before_discount.
7. amount_payable и total_amount — последняя итоговая сумма К ОПЛАТЕ после всех скидок. Не выбирай «Сумма без скидки», «Итого без скидки», промежуточный итог, НДС или сумму до скидки.
8. subtotal_before_discount — итог до скидок. discount_amount — общая скидка. Если их нет или они не читаются, верни null.
9. Если есть несколько итогов, выбери строку с наиболее точным смыслом: «Итого к оплате», «Всего к оплате», «Сумма к оплате», «Итого со скидкой». Обычно она расположена ниже остальных итогов.
10. Артикул, единицу измерения, номер, дату и реквизиты не угадывай: если данных нет, верни null.
11. currency обычно RUB. Числа верни без пробелов, валюты и знака процента.
12. confidence — общая уверенность от 0 до 1. В warnings кратко укажи только реальные сомнения: нечитаемую строку, возможный пропуск или конфликт итогов.
13. В документе может быть до 150 позиций. Если это не накладная/счёт или товаров нет, верни пустой items и объясни причину.
""".strip()


VERIFY_PROMPT = """
Повтори распознавание как независимую контрольную проверку. Ниже дан первый результат.
Сверь с документом каждое название, количество, цену и сумму строки. Особенно проверь:
- не перепутаны ли цена до скидки и цена после скидки;
- выбран ли самый последний итог к оплате после всех скидок;
- не пропущены ли строки или страницы;
- не попали ли в товары заголовки и итоговые строки.
Верни полностью исправленный результат по той же структуре, даже если первый вариант был верным.

ПЕРВЫЙ РЕЗУЛЬТАТ:
{first_result}
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


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clean_optional_text(value: Any, max_length: int = 500) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text[:max_length] if text else None


def _tolerance(value: float) -> float:
    return max(2.0, abs(value) * 0.001)


def _line_tolerance(value: float) -> float:
    return max(0.02, abs(value) * 0.0005)


def _line_subtotal(item: dict[str, Any]) -> float:
    quantity = float(item["quantity"])
    before_price = _optional_number(item.get("unit_price_before_discount"))
    if before_price is not None and before_price >= 0:
        return round(quantity * before_price, 2)
    line_total = float(item["line_total"])
    discount = _optional_number(item.get("discount_amount"))
    return round(line_total + max(0.0, discount or 0.0), 2)


def _distribute_document_discount(
    items: list[dict[str, Any]],
    target_total: float,
) -> None:
    """Распределяет общую скидку, чтобы товарная аналитика совпала с суммой к оплате."""
    current_total = round(sum(float(item["line_total"]) for item in items), 2)
    if not items or current_total <= 0 or target_total < 0 or target_total >= current_total:
        return

    factor = target_total / current_total
    distributed = 0.0
    for index, item in enumerate(items):
        old_total = float(item["line_total"])
        quantity = float(item["quantity"])
        before_total = max(_line_subtotal(item), old_total)
        if index + 1 == len(items):
            new_total = round(target_total - distributed, 2)
        else:
            new_total = round(old_total * factor, 2)
            distributed += new_total

        if item.get("unit_price_before_discount") is None:
            item["unit_price_before_discount"] = round(float(item["unit_price"]), 2)
        item["line_total"] = new_total
        item["unit_price"] = round(new_total / quantity, 2)
        line_discount = max(0.0, round(before_total - new_total, 2))
        item["discount_amount"] = line_discount or None
        item["discount_percent"] = (
            round(line_discount / before_total * 100, 4) if before_total > 0 and line_discount > 0 else None
        )


def normalize_invoice_result(raw: dict[str, Any]) -> dict[str, Any]:
    warnings = [
        " ".join(str(item).split())[:300]
        for item in (raw.get("warnings") or [])
        if str(item).strip()
    ][:20]

    cleaned_items: list[dict[str, Any]] = []
    adjusted_line_prices = 0
    for index, source in enumerate(raw.get("items") or [], start=1):
        if not isinstance(source, dict):
            warnings.append(f"Позиция {index} пропущена: неверный формат.")
            continue

        name = _clean_optional_text(source.get("product_name"), 500)
        quantity = _optional_number(source.get("quantity"))
        unit_price = _optional_number(source.get("unit_price"))
        line_total = _optional_number(source.get("line_total"))
        before_price = _optional_number(source.get("unit_price_before_discount"))
        discount_percent = _optional_number(source.get("discount_percent"))
        line_discount = _optional_number(source.get("discount_amount"))

        if not name or quantity is None or quantity <= 0:
            warnings.append(f"Позиция {index} пропущена: не удалось прочитать название или количество.")
            continue
        if unit_price is not None and unit_price < 0:
            unit_price = None
        if line_total is not None and line_total < 0:
            line_total = None
        if before_price is not None and before_price < 0:
            before_price = None
        if line_discount is not None and line_discount < 0:
            line_discount = None
        if discount_percent is not None and not 0 <= discount_percent <= 100:
            discount_percent = None

        if unit_price is None and line_total is not None:
            unit_price = line_total / quantity
        if line_total is None and unit_price is not None:
            line_total = quantity * unit_price
        if unit_price is None or line_total is None:
            warnings.append(f"Позиция {index} пропущена: не удалось прочитать цену или сумму строки.")
            continue

        expected = round(quantity * unit_price, 2)
        if abs(line_total - expected) > _line_tolerance(line_total):
            derived_price = line_total / quantity
            if before_price is None and unit_price > derived_price:
                before_price = unit_price
            unit_price = derived_price
            adjusted_line_prices += 1

        if before_price is None and discount_percent and discount_percent < 100:
            before_price = unit_price / (1 - discount_percent / 100)
        if before_price is None and line_discount and line_discount > 0:
            before_price = (line_total + line_discount) / quantity

        before_total = round(quantity * before_price, 2) if before_price is not None else None
        if line_discount is None and before_total is not None and before_total > line_total:
            line_discount = round(before_total - line_total, 2)
        if discount_percent is None and before_total and line_discount and line_discount > 0:
            discount_percent = round(line_discount / before_total * 100, 4)

        cleaned_items.append(
            {
                "article": _clean_optional_text(source.get("article"), 100),
                "product_name": name,
                "quantity": round(quantity, 4),
                "unit": _clean_optional_text(source.get("unit"), 30),
                "unit_price_before_discount": None if before_price is None else round(before_price, 2),
                "discount_percent": None if discount_percent is None else round(discount_percent, 4),
                "discount_amount": None if line_discount is None else round(line_discount, 2),
                "unit_price": round(unit_price, 2),
                "line_total": round(line_total, 2),
            }
        )
        if len(cleaned_items) >= MAX_ITEMS:
            warnings.append(f"Взяты первые {MAX_ITEMS} позиций — это максимум для одной накладной.")
            break

    if adjusted_line_prices:
        warnings.append(
            f"В {adjusted_line_prices} поз. цена после скидки рассчитана по напечатанной сумме строки."
        )

    calculated_before_distribution = round(
        sum(float(item["line_total"]) for item in cleaned_items), 2
    )
    subtotal_raw = _optional_number(raw.get("subtotal_before_discount"))
    discount_raw = _optional_number(raw.get("discount_amount"))
    payable = _optional_number(raw.get("amount_payable"))
    if payable is None:
        payable = _optional_number(raw.get("total_amount"))
    if payable is None and subtotal_raw is not None and discount_raw is not None:
        payable = max(0.0, subtotal_raw - discount_raw)
    if payable is not None and payable < 0:
        payable = None

    if payable is None and cleaned_items:
        payable = calculated_before_distribution
        warnings.append("Итог к оплате не прочитан — он рассчитан по товарным строкам.")

    if payable is not None and cleaned_items:
        if payable < calculated_before_distribution - _tolerance(calculated_before_distribution):
            _distribute_document_discount(cleaned_items, payable)
            warnings.append("Общая скидка распределена по товарам, чтобы суммы совпали с итогом к оплате.")

    calculated_total = round(sum(float(item["line_total"]) for item in cleaned_items), 2)
    if payable is not None and cleaned_items and abs(payable - calculated_total) > _tolerance(payable):
        warnings.append(
            "Итог к оплате отличается от суммы распознанных товаров. Перед принятием проверьте состав документа."
        )

    subtotal_from_items = round(sum(_line_subtotal(item) for item in cleaned_items), 2)
    subtotal_candidates = [
        value
        for value in (subtotal_raw, subtotal_from_items, calculated_before_distribution)
        if value is not None and value >= 0
    ]
    subtotal = max(subtotal_candidates) if subtotal_candidates else calculated_total
    if payable is not None and discount_raw is not None and discount_raw >= 0:
        subtotal = max(subtotal, payable + discount_raw)
    total_discount = max(0.0, round(subtotal - (payable if payable is not None else calculated_total), 2))

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
        "subtotal_before_discount": round(subtotal, 2),
        "discount_amount": round(total_discount, 2),
        "amount_payable": None if payable is None else round(payable, 2),
        "total_amount": None if payable is None else round(payable, 2),
        "calculated_total": calculated_total,
        "vat_amount": None
        if raw.get("vat_amount") is None
        else round(_finite_number(raw.get("vat_amount")), 2),
        "confidence": round(confidence, 3),
        "warnings": list(dict.fromkeys(warnings))[:20],
        "items": cleaned_items,
        "recognized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# Совместимость с ранними тестами и внутренними импортами.
_clean_result = normalize_invoice_result


def _enhanced_image(data: bytes) -> bytes | None:
    """Контрастная копия помогает прочитать мелкий текст на фото из Telegram."""
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps

        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("L")
            width, height = image.size
            longest = max(width, height)
            if longest < 2400:
                scale = min(2.5, 2400 / max(1, longest))
                image = image.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            elif longest > 6000:
                scale = 6000 / longest
                image = image.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            image = ImageOps.autocontrast(image, cutoff=0.5)
            image = ImageEnhance.Contrast(image).enhance(1.12)
            image = image.filter(ImageFilter.UnsharpMask(radius=1.4, percent=145, threshold=3))
            output = BytesIO()
            image.save(output, format="JPEG", quality=94, optimize=True)
            result = output.getvalue()
            return result if result and len(result) <= MAX_FILE_BYTES else None
    except Exception:
        logger.warning("Could not prepare enhanced invoice image", exc_info=True)
        return None


def _image_detail(model: str) -> str:
    configured = (os.getenv("OPENAI_INVOICE_IMAGE_DETAIL") or "").strip().casefold()
    if configured in {"auto", "high", "original"}:
        return configured
    return "original" if model.casefold().startswith("gpt-5.6") else "high"


def _request_content(file: InvoiceFile, prompt: str, model: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if file.mime_type == "application/pdf":
        content.append(
            {
                "type": "input_file",
                "filename": file.filename or "invoice.pdf",
                "file_data": _data_url(file.data, file.mime_type),
            }
        )
        return content

    detail = _image_detail(model)
    content.append(
        {
            "type": "input_image",
            "image_url": _data_url(file.data, file.mime_type),
            "detail": detail,
        }
    )
    enhanced = _enhanced_image(file.data)
    if enhanced and len(file.data) + len(enhanced) <= MAX_FILE_BYTES:
        content.extend(
            [
                {
                    "type": "input_text",
                    "text": "Ниже контрастная копия того же фото для проверки мелкого текста. Это не вторая страница.",
                },
                {
                    "type": "input_image",
                    "image_url": _data_url(enhanced, "image/jpeg"),
                    "detail": detail,
                },
            ]
        )
    return content


async def _extract_with_model(
    file: InvoiceFile,
    prompt: str,
    *,
    preferred_model: str | None = None,
) -> tuple[dict[str, Any], str]:
    client = _get_client()
    errors: list[Exception] = []
    for model in _model_candidates(preferred_model):
        try:
            response = await client.responses.create(
                model=model,
                input=[{"role": "user", "content": _request_content(file, prompt, model)}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "invoice_extraction",
                        "description": "Проверенные данные накладной или счёта",
                        "strict": True,
                        "schema": INVOICE_SCHEMA,
                    }
                },
                store=False,
            )
            output_text = (getattr(response, "output_text", None) or "").strip()
            if not output_text:
                raise ValueError("empty response")
            raw = json.loads(output_text)
            if not isinstance(raw, dict):
                raise ValueError("response is not an object")
            return raw, model
        except Exception as exc:  # SDK/API details must not leak to Telegram users
            errors.append(exc)
            logger.exception("Invoice recognition failed with model %s", model)

    raise InvoiceRecognitionError(
        _friendly_api_error(errors)
    ) from (errors[-1] if errors else None)


def _friendly_api_error(errors: list[Exception]) -> str:
    """Возвращает полезную, но безопасную причину без токенов и тела запроса."""
    statuses: set[int] = set()
    names: set[str] = set()
    for error in errors:
        names.add(type(error).__name__.casefold())
        status = getattr(error, "status_code", None)
        if status is None:
            status = getattr(getattr(error, "response", None), "status_code", None)
        try:
            if status is not None:
                statuses.add(int(status))
        except (TypeError, ValueError):
            pass
    joined_names = " ".join(names)
    if 401 in statuses or "authentication" in joined_names:
        return "Ключ сервиса распознавания не принят. Администратору нужно проверить OPENAI_API_KEY."
    if 429 in statuses or "ratelimit" in joined_names:
        return "Сервис распознавания временно достиг лимита. Файл сохранён — повторите проверку немного позже."
    if 403 in statuses or "permissiondenied" in joined_names:
        return "У проекта нет доступа к настроенной модели распознавания. Проверьте модель и права API-ключа."
    if statuses and statuses.issubset({400, 404}):
        return "Настроенные модели не приняли документ. Проверьте OPENAI_MODEL или OPENAI_INVOICE_MODEL."
    if any(token in joined_names for token in ("connection", "timeout", "apitimeout")):
        return "Сервис распознавания сейчас недоступен по сети. Файл сохранён — попробуйте повторно позже."
    return "Не удалось распознать документ. Файл сохранён; администратор может повторить обработку."


def _quality_score(result: dict[str, Any]) -> float:
    items = list(result.get("items") or [])
    score = min(len(items), 40) * 0.2 + float(result.get("confidence") or 0) * 10
    score += sum(0.5 for key in ("invoice_number", "invoice_date", "supplier") if result.get(key))
    payable = _optional_number(result.get("amount_payable"))
    calculated = _optional_number(result.get("calculated_total"))
    if payable is not None and calculated is not None:
        score += 4 if abs(payable - calculated) <= _tolerance(payable) else -3
    score -= len(result.get("warnings") or []) * 0.25
    return score


def _prefer_verified(first: dict[str, Any], verified: dict[str, Any]) -> bool:
    first_items = list(first.get("items") or [])
    verified_items = list(verified.get("items") or [])
    if not verified_items:
        return False
    enough_rows = len(verified_items) >= max(1, math.ceil(len(first_items) * 0.8))
    has_final_total = _optional_number(verified.get("amount_payable")) is not None
    confidence = float(verified.get("confidence") or 0)
    if enough_rows and has_final_total and confidence >= 0.65:
        return True
    return _quality_score(verified) > _quality_score(first)


def _needs_verification(result: dict[str, Any], file: InvoiceFile) -> bool:
    if file.mime_type in SUPPORTED_IMAGE_MIME_TYPES:
        return True
    if float(result.get("confidence") or 0) < 0.9:
        return True
    if not result.get("amount_payable") or not result.get("supplier"):
        return True
    payable = _optional_number(result.get("amount_payable"))
    calculated = _optional_number(result.get("calculated_total"))
    return bool(
        payable is not None
        and calculated is not None
        and abs(payable - calculated) > _tolerance(payable)
    )


async def recognize_invoice(file: InvoiceFile) -> dict[str, Any]:
    validate_invoice_file(file)
    if file.mime_type in SUPPORTED_SPREADSHEET_MIME_TYPES:
        raise InvoiceRecognitionError("Excel-файлы должны обрабатываться табличным распознавателем.")
    file = _convert_heif_to_jpeg(file)

    raw, model = await _extract_with_model(file, PROMPT)
    result = normalize_invoice_result(raw)
    result["recognition_model"] = model
    result["verification_performed"] = False

    should_verify = _bool_env(
        "OPENAI_INVOICE_DOUBLE_CHECK",
        _needs_verification(result, file),
    )
    if should_verify:
        verify_prompt = VERIFY_PROMPT.format(
            first_result=json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        )
        try:
            verified_raw, verified_model = await _extract_with_model(
                file,
                verify_prompt,
                preferred_model=model,
            )
            verified = normalize_invoice_result(verified_raw)
            if _prefer_verified(result, verified):
                result = verified
                result["recognition_model"] = verified_model
            result["verification_performed"] = True
        except InvoiceRecognitionError:
            logger.warning("Invoice double-check failed; keeping first valid result", exc_info=True)

    if not result["items"]:
        warning = result["warnings"][0] if result["warnings"] else "Товарные позиции не найдены."
        raise InvoiceRecognitionError(warning)
    return result
