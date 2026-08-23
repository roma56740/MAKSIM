from __future__ import annotations

import html
from io import BytesIO
from typing import Any

from aiogram import Bot

from db.invoices import (
    fail_invoice_analysis,
    get_invoice_full,
    mark_invoice_analysis_processing,
    save_invoice_analysis,
)
from services.invoice_recognition import (
    InvoiceFile,
    InvoiceRecognitionError,
    detect_mime_type,
    normalize_mime_type,
    recognize_invoice,
    SUPPORTED_SPREADSHEET_MIME_TYPES,
)
from services.invoice_excel import recognize_invoice_excel


def money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(value)


def quantity(value: Any) -> str:
    try:
        return f"{float(value):g}".replace(".", ",")
    except Exception:
        return str(value)


def confidence(value: Any) -> str:
    try:
        return f"{round(float(value) * 100):d}%"
    except Exception:
        return "—"


def analysis_header(analysis: dict[str, Any], *, invoice_id: int | None = None) -> str:
    prefix = f"🧾 <b>Накладная #{invoice_id}</b>\n" if invoice_id is not None else "🧾 <b>Результат распознавания</b>\n"
    items = list(analysis.get("items") or [])
    lines = [
        prefix.rstrip(),
        f"📄 Документ: <b>{html.escape(str(analysis.get('document_type') or 'Накладная / счёт'))}</b>",
        f"🔢 Номер: <b>{html.escape(str(analysis.get('invoice_number') or '—'))}</b>",
        f"📅 Дата: <b>{html.escape(str(analysis.get('invoice_date') or '—'))}</b>",
        f"🏢 Поставщик: <b>{html.escape(str(analysis.get('supplier') or '—'))}</b>",
        f"📦 Позиций: <b>{len(items)}</b>",
        f"💰 Итог по позициям: <b>{money(analysis.get('calculated_total'))}</b> {html.escape(str(analysis.get('currency') or 'RUB'))}",
        f"🎯 Точность распознавания: <b>{confidence(analysis.get('confidence'))}</b>",
    ]
    printed_total = analysis.get("total_amount")
    if printed_total is not None:
        lines.insert(-1, f"🧮 Итог в документе: <b>{money(printed_total)}</b> {html.escape(str(analysis.get('currency') or 'RUB'))}")
    warnings = list(analysis.get("warnings") or [])
    if warnings:
        lines.append("\n⚠️ <b>Нужно проверить:</b>")
        for warning in warnings[:5]:
            lines.append(f"• {html.escape(str(warning))}")
        if len(warnings) > 5:
            lines.append(f"• … ещё {len(warnings) - 5}")
    return "\n".join(lines)


def item_lines(analysis: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for index, item in enumerate(analysis.get("items") or [], start=1):
        name = html.escape(str(item.get("product_name") or "—"))
        article = item.get("article")
        article_text = f" · арт. {html.escape(str(article))}" if article else ""
        unit = html.escape(str(item.get("unit") or "шт"))
        result.append(
            f"<b>{index}.</b> {name}{article_text}\n"
            f"   {quantity(item.get('quantity'))} {unit} × {money(item.get('unit_price'))} = "
            f"<b>{money(item.get('line_total'))}</b>"
        )
    return result


def split_item_messages(analysis: dict[str, Any], *, max_length: int = 3600) -> list[str]:
    lines = item_lines(analysis)
    if not lines:
        return ["📦 Товарные позиции не найдены."]

    chunks: list[str] = []
    current = "📦 <b>Распознанные товары:</b>\n\n"
    for line in lines:
        candidate = current + ("\n\n" if current else "") + line
        if len(candidate) > max_length and current.strip():
            chunks.append(current)
            current = "📦 <b>Продолжение:</b>\n\n" + line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks


def edit_template(analysis: dict[str, Any]) -> str:
    lines = []
    for item in analysis.get("items") or []:
        name = " ".join(str(item.get("product_name") or "").split())
        qty = f"{float(item.get('quantity') or 0):g}"
        price = f"{float(item.get('unit_price') or 0):.2f}".rstrip("0").rstrip(".")
        lines.append(f"{name} | {qty} | {price}")
    return "\n".join(lines)


async def download_invoice_file(bot: Bot, invoice: dict[str, Any]) -> InvoiceFile:
    file_id = str(invoice.get("file_id") or "")
    if not file_id:
        raise InvoiceRecognitionError("У накладной отсутствует файл.")

    telegram_file = await bot.get_file(file_id)
    telegram_path = str(telegram_file.file_path or "")
    if not telegram_path:
        raise InvoiceRecognitionError("Telegram не вернул путь к файлу накладной.")

    buffer = BytesIO()
    await bot.download_file(telegram_path, destination=buffer)
    data = buffer.getvalue()

    kind = str(invoice.get("file_kind") or "").lower()
    filename = str(invoice.get("source_file_name") or "").strip()
    if not filename and telegram_path:
        filename = telegram_path.rsplit("/", 1)[-1]

    mime_type = normalize_mime_type(filename, invoice.get("source_mime_type"))
    if not mime_type:
        mime_type = detect_mime_type(data)

    if kind == "photo":
        mime_type = "image/jpeg"
        filename = filename or f"invoice_{invoice.get('id')}.jpg"
    elif not filename:
        extension = {
            "application/pdf": ".pdf",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.ms-excel": ".xls",
        }.get(mime_type, "")
        filename = f"invoice_{invoice.get('id')}{extension}"

    return InvoiceFile(data=data, filename=filename, mime_type=mime_type)


async def analyze_invoice_from_telegram(bot: Bot, db_path: str, invoice_id: int) -> dict[str, Any]:
    invoice = await get_invoice_full(db_path, invoice_id)
    if not invoice:
        raise InvoiceRecognitionError("Накладная не найдена.")

    await mark_invoice_analysis_processing(db_path, invoice_id)
    try:
        invoice_file = await download_invoice_file(bot, invoice)
        if invoice_file.mime_type in SUPPORTED_SPREADSHEET_MIME_TYPES:
            analysis = recognize_invoice_excel(invoice_file)
        else:
            analysis = await recognize_invoice(invoice_file)
        await save_invoice_analysis(db_path, invoice_id, analysis)
        return analysis
    except InvoiceRecognitionError as exc:
        await fail_invoice_analysis(db_path, invoice_id, str(exc))
        raise
    except Exception as exc:
        await fail_invoice_analysis(db_path, invoice_id, "Не удалось скачать или обработать файл.")
        raise InvoiceRecognitionError("Не удалось скачать или обработать файл накладной.") from exc
