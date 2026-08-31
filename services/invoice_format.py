from __future__ import annotations

import html
from typing import Any


# Telegram ограничивает обычное сообщение 4096 символами. Запас нужен для
# emoji и HTML-сущностей, длина которых Telegram считает не так, как Python.
TELEGRAM_SAFE_TEXT_LENGTH = 3800
DIVIDER = "━━━━━━━━━━━━━━"


def money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return html.escape(str(value))


def quantity(value: Any) -> str:
    try:
        return f"{float(value):g}".replace(".", ",")
    except (TypeError, ValueError):
        return html.escape(str(value))


def confidence(value: Any) -> str:
    try:
        return f"{round(float(value) * 100):d}%"
    except (TypeError, ValueError):
        return "—"


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return html.escape(text or "—")


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _document_block(analysis: dict[str, Any]) -> str:
    return "\n".join(
        [
            "📋 <b>ДОКУМЕНТ</b>",
            f"├ Тип: <b>{_short(analysis.get('document_type') or 'Накладная / счёт', 90)}</b>",
            f"├ Номер: <code>{_short(analysis.get('invoice_number'), 100)}</code>",
            f"├ Дата: <b>{_short(analysis.get('invoice_date'), 60)}</b>",
            f"├ Поставщик: <b>{_short(analysis.get('supplier'), 180)}</b>",
            f"└ Точность: <b>{confidence(analysis.get('confidence'))}</b>",
        ]
    )


def _finance_block(analysis: dict[str, Any]) -> str:
    currency = _short(analysis.get("currency") or "RUB", 10)
    payable = analysis.get("amount_payable")
    if payable is None:
        payable = analysis.get("total_amount")
    lines = ["💳 <b>СУММЫ</b>"]
    discount = _positive_number(analysis.get("discount_amount"))
    if discount is not None:
        lines.extend(
            [
                f"├ До скидки: <b>{money(analysis.get('subtotal_before_discount'))} {currency}</b>",
                f"├ Скидка: <b>−{money(discount)} {currency}</b>",
            ]
        )
    lines.append(f"└ К оплате: <b>{money(payable)} {currency}</b>")
    return "\n".join(lines)


def _item_line(index: int, item: dict[str, Any], currency: str) -> str:
    name = _short(item.get("product_name"), 240)
    article = item.get("article")
    article_text = f" · <code>{_short(article, 70)}</code>" if article else ""
    unit = _short(item.get("unit") or "шт", 20)
    price = money(item.get("unit_price"))
    line_total = money(item.get("line_total"))
    detail = ""
    before = _positive_number(item.get("unit_price_before_discount"))
    current = _positive_number(item.get("unit_price"))
    discount = _positive_number(item.get("discount_amount"))
    if before is not None and current is not None and before > current:
        detail = f" · было {money(before)}"
    elif discount is not None:
        detail = f" · скидка {money(discount)}"
    return (
        f"<b>{index:02d}.</b> {name}{article_text}\n"
        f"└ {quantity(item.get('quantity'))} {unit} × {price}{detail} = "
        f"<b>{line_total} {currency}</b>"
    )


def _duplicates_block(analysis: dict[str, Any], include_details: bool) -> str:
    matches = list(analysis.get("duplicate_matches") or [])
    if not matches:
        return ""
    lines = [f"🚨 <b>ВОЗМОЖНЫЙ ДУБЛЬ · {len(matches)}</b>"]
    if include_details:
        for match in matches[:4]:
            reasons = ", ".join(str(value) for value in match.get("reasons") or [])
            lines.append(
                f"• <b>#{int(match.get('invoice_id') or 0)}</b> · "
                f"{_short(match.get('invoice_date') or 'дата —', 35)} · "
                f"{money(match.get('document_total'))} RUB · {_short(reasons or 'совпали реквизиты', 120)}"
            )
    else:
        lines.append("Администратор получил номера совпадений и проверит документ до одобрения.")
    return "\n".join(lines)


def _warnings_block(analysis: dict[str, Any]) -> str:
    warnings = [str(value) for value in analysis.get("warnings") or [] if str(value).strip()]
    if not warnings:
        return ""
    lines = ["⚠️ <b>НУЖНО ПРОВЕРИТЬ</b>"]
    lines.extend(f"• {_short(value, 180)}" for value in warnings[:4])
    if len(warnings) > 4:
        lines.append(f"• Ещё замечаний: <b>{len(warnings) - 4}</b>")
    return "\n".join(lines)


def render_invoice_message(
    analysis: dict[str, Any],
    *,
    invoice_id: int | None = None,
    include_duplicate_details: bool = False,
    footer: str | None = None,
    max_length: int = TELEGRAM_SAFE_TEXT_LENGTH,
) -> str:
    """Собирает один валидный HTML-текст, не превышающий лимит Telegram."""
    title = (
        f"🧾 <b>НАКЛАДНАЯ #{invoice_id} · РАСПОЗНАНА</b>"
        if invoice_id is not None
        else "🧾 <b>НАКЛАДНАЯ · РАСПОЗНАНА</b>"
    )
    items = list(analysis.get("items") or [])
    currency = _short(analysis.get("currency") or "RUB", 10)
    fixed_blocks = [title, DIVIDER, _document_block(analysis), _finance_block(analysis)]
    tail_blocks = [
        block
        for block in (
            _duplicates_block(analysis, include_duplicate_details),
            _warnings_block(analysis),
            footer,
        )
        if block
    ]

    item_header = f"📦 <b>ТОВАРЫ · {len(items)}</b>"
    shown: list[str] = []
    omitted = 0
    for index, item in enumerate(items, start=1):
        line = _item_line(index, item, currency)
        remaining = len(items) - index
        omission_note = (
            f"… ещё позиций: <b>{remaining}</b> — полный список сохранён для проверки администратора."
            if remaining
            else ""
        )
        candidate_items = [item_header, *shown, line]
        if omission_note:
            candidate_items.append(omission_note)
        candidate = "\n\n".join([*fixed_blocks, "\n\n".join(candidate_items), *tail_blocks])
        if len(candidate) > max_length:
            omitted = len(items) - len(shown)
            break
        shown.append(line)

    if items:
        item_lines = [item_header, *shown]
        if omitted:
            item_lines.append(
                f"… ещё позиций: <b>{omitted}</b> — полный список сохранён для проверки администратора."
            )
        item_block = "\n\n".join(item_lines)
    else:
        item_block = f"{item_header}\nТоварные позиции не найдены."

    result = "\n\n".join([*fixed_blocks, item_block, *tail_blocks])
    while len(result) > max_length and shown:
        shown.pop()
        omitted = len(items) - len(shown)
        item_block = "\n\n".join(
            [
                item_header,
                *shown,
                f"… ещё позиций: <b>{omitted}</b> — полный список сохранён для проверки администратора.",
            ]
        )
        result = "\n\n".join([*fixed_blocks, item_block, *tail_blocks])
    if len(result) <= max_length:
        return result

    # Крайний случай: очень длинные реквизиты/предупреждения. Основные суммы и
    # статус остаются, товарные строки сворачиваются без обрезки HTML-тегов.
    compact_item = (
        f"{item_header}\nПолный список сохранён в накладной и доступен администратору при проверке."
    )
    result = "\n\n".join([*fixed_blocks, compact_item, *tail_blocks])
    if len(result) <= max_length:
        return result

    result = "\n\n".join([title, DIVIDER, _finance_block(analysis), compact_item, *(tail_blocks[-1:] if tail_blocks else [])])
    if len(result) <= max_length:
        return result
    # Все значения выше уже ограничены; этот вариант не содержит динамических
    # длинных реквизитов и потому всегда безопасен для HTML и Telegram.
    return "\n\n".join([title, DIVIDER, _finance_block(analysis), compact_item])
