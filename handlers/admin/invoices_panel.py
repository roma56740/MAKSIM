from __future__ import annotations

import html
import math

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import is_admin
from db.invoices import decode_invoice_analysis, get_invoice_full, list_invoice_items
from db.invoices_admin import count_invoices_by_status, list_invoices_by_status
from filters.admin import IsAdmin


router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())
PAGE_SIZE = 6


def _status_badge(status: str) -> str:
    if status == "approved":
        return "✅ Принято"
    if status == "rejected":
        return "❌ Отклонено"
    return "🟡 На проверке"


def _status_title(status: str) -> str:
    if status == "approved":
        return "✅ Одобренные"
    if status == "rejected":
        return "❌ Отклонённые"
    return "🟡 На проверке"


def _money(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(v)


def _percent(deal_amount, reward_amount) -> str:
    try:
        deal = float(deal_amount or 0)
        reward = float(reward_amount or 0)
        if deal <= 0:
            return "—"
        value = reward / deal * 100
        return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    except Exception:
        return "—"


def _analysis_badge(invoice: dict) -> str:
    status = str(invoice.get("analysis_status") or "pending")
    analysis = decode_invoice_analysis(invoice)
    if status == "completed" and analysis:
        duplicate_count = len(analysis.get("duplicate_matches") or [])
        duplicate_badge = f"\n🚨 Возможный повтор: <b>{duplicate_count}</b>" if duplicate_count else ""
        return f"🤖 Распознано: <b>{len(analysis.get('items') or [])}</b> поз.{duplicate_badge}"
    if status == "failed":
        return "⚠️ Распознавание не завершено"
    if status == "processing":
        return "🔎 Идёт распознавание"
    return "⏳ Ожидает распознавания"

def _panel_text(
    status: str,
    items: list[dict],
    page: int,
    total_pages: int,
    total: int,
    pending_count: int,
    approved_count: int,
    rejected_count: int,
) -> str:
    title = _status_title(status)

    if not items:
        return (
            f"🧾 <b>Накладные • {title}</b>\n\n"
            f"📊 На проверке: <b>{pending_count}</b> • "
            f"Одобренные: <b>{approved_count}</b> • "
            f"Отклонённые: <b>{rejected_count}</b>\n\n"
            "Пока пусто."
        )

    lines = [
        f"🧾 <b>Накладные • {title}</b>",
        f"📊 На проверке: <b>{pending_count}</b> • Одобренные: <b>{approved_count}</b> • Отклонённые: <b>{rejected_count}</b>",
        f"📄 Стр. <b>{page+1}</b>/<b>{total_pages}</b> • Всего: <b>{total}</b>\n",
    ]

    for it in items:
        inv_id = it["id"]
        user_name = it.get("user_full_name") or "—"
        phone = it.get("user_phone") or "—"
        created = it.get("created_at") or "—"
        comment = (it.get("comment") or "").strip()

        block = (
            f"{_status_badge(it.get('status', status))} <b>#{inv_id}</b>\n"
            f"👤 {html.escape(str(user_name)[:80])}\n"
            f"📱 {html.escape(str(phone))}\n"
            f"🕒 {created}\n"
            f"{_analysis_badge(it)}"
        )

        if status == "approved":
            block += (
                f"\n💰 Продажи: <b>{_money(it.get('deal_amount'))}</b> ₽"
                f"\n📈 Процент: <b>{_percent(it.get('deal_amount'), it.get('reward_amount'))}</b>%"
                f"\n🎁 Вознаграждение: <b>{_money(it.get('reward_amount'))}</b> ₽"
            )

        if status == "rejected":
            block += f"\n🧾 Причина: <b>{html.escape(str(it.get('reason') or '—')[:120])}</b>"

        if comment:
            block += f"\n🔁 Повторная проверка\n💬 {html.escape(comment[:120])}"

        lines.append(block)

    return "\n\n".join(lines)


def _panel_kb(status: str, page: int, total_pages: int, items: list[dict]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()

    kb.row(
        InlineKeyboardButton(
            text=("• " if status == "pending" else "") + "🟡 На проверке",
            callback_data="ainv:panel:pending:0",
        ),
        InlineKeyboardButton(
            text=("• " if status == "approved" else "") + "✅ Одобренные",
            callback_data="ainv:panel:approved:0",
        ),
        InlineKeyboardButton(
            text=("• " if status == "rejected" else "") + "❌ Отклонённые",
            callback_data="ainv:panel:rejected:0",
        ),
    )

    from handlers.admin.invoice_editor import cb
    for it in items:
        company = str(it.get('company_name') or it.get('user_full_name') or 'Накладная')[:28]
        kb.row(InlineKeyboardButton(text=f"#{it['id']} · {company} · {_money(it.get('document_total'))}",
                                    callback_data=cb('page', it), style='primary'))

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"ainv:panel:{status}:{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"ainv:panel:{status}:{page+1}"))
    if nav_buttons:
        kb.row(*nav_buttons)

    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"ainv:panel:{status}:{page}"))

    return kb


def _detail_kb(invoice_id: int, status: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()

    if status == "pending":
        kb.row(
            InlineKeyboardButton(text="🔎 Проверить", callback_data=f"ainv:approve:{invoice_id}"),
            InlineKeyboardButton(text="🔄 Распознать заново", callback_data=f"ainv:retry:{invoice_id}"),
        )
        kb.row(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ainv:reject:{invoice_id}"))
    elif status == "approved":
        kb.row(
            InlineKeyboardButton(text="🟡 На проверку", callback_data=f"ainv:pending:{invoice_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ainv:reject:{invoice_id}"),
        )
    else:
        kb.row(
            InlineKeyboardButton(text="✅ Принять", callback_data=f"ainv:approve:{invoice_id}"),
            InlineKeyboardButton(text="🟡 На проверку", callback_data=f"ainv:pending:{invoice_id}"),
        )

    kb.row(InlineKeyboardButton(text="📋 К списку", callback_data=f"ainv:panel:{status}:0"))
    return kb


async def _ensure_admin(tg_id: int, settings: Settings) -> bool:
    return await is_admin(settings.db_path, tg_id, settings.admin_ids)


@router.message(F.text.in_({"🧾 Накладные", "🧾 Управление накладными"}))
async def admin_invoices_panel(message: Message, settings: Settings, state: FSMContext) -> None:
    if not await _ensure_admin(message.from_user.id, settings):
        return

    await state.clear()
    await _send_panel(message, settings, status="pending", page=0)


@router.callback_query(F.data.startswith("ainv:panel:"))
async def admin_invoices_panel_page(cbq: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    if not await _ensure_admin(cbq.from_user.id, settings):
        await cbq.answer("Нет доступа", show_alert=True)
        return

    await cbq.answer()
    await state.clear()
    _, _, status, page_s = cbq.data.split(":", 3)
    page = int(page_s)
    await _edit_panel(cbq, settings, status=status, page=page)


@router.callback_query(F.data.startswith("ainv:open:"))
async def admin_open_invoice_file(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _ensure_admin(cbq.from_user.id, settings):
        await cbq.answer("Нет доступа", show_alert=True)
        return

    await cbq.answer()
    invoice_id = int(cbq.data.split(":")[-1])
    inv = await get_invoice_full(settings.db_path, invoice_id)
    if not inv:
        await cbq.message.answer("⚠️ Накладная не найдена.")
        return

    from handlers.admin.invoice_editor import cb
    caption = f"📎 <b>Оригинал накладной #{invoice_id}</b>"
    reply_markup = InlineKeyboardBuilder()
    reply_markup.button(text="‹ Карточка и товары", callback_data=cb('page', inv))
    if (inv.get("file_kind") or "").lower() == "photo":
        await cbq.message.answer_photo(inv["file_id"], caption=caption, reply_markup=reply_markup.as_markup())
    else:
        await cbq.message.answer_document(inv["file_id"], caption=caption, reply_markup=reply_markup.as_markup())


async def _send_panel(message: Message, settings: Settings, status: str, page: int) -> None:
    pending_count = await count_invoices_by_status(settings.db_path, "pending")
    approved_count = await count_invoices_by_status(settings.db_path, "approved")
    rejected_count = await count_invoices_by_status(settings.db_path, "rejected")

    total = await count_invoices_by_status(settings.db_path, status)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    items = await list_invoices_by_status(
        settings.db_path,
        status,
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
    )

    text = _panel_text(
        status=status,
        items=items,
        page=page,
        total_pages=total_pages,
        total=total,
        pending_count=pending_count,
        approved_count=approved_count,
        rejected_count=rejected_count,
    )
    kb = _panel_kb(status, page, total_pages, items).as_markup()

    await message.answer(text, reply_markup=kb)


async def _edit_panel(cbq: CallbackQuery, settings: Settings, status: str, page: int) -> None:
    pending_count = await count_invoices_by_status(settings.db_path, "pending")
    approved_count = await count_invoices_by_status(settings.db_path, "approved")
    rejected_count = await count_invoices_by_status(settings.db_path, "rejected")

    total = await count_invoices_by_status(settings.db_path, status)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    items = await list_invoices_by_status(
        settings.db_path,
        status,
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
    )

    text = _panel_text(
        status=status,
        items=items,
        page=page,
        total_pages=total_pages,
        total=total,
        pending_count=pending_count,
        approved_count=approved_count,
        rejected_count=rejected_count,
    )
    kb = _panel_kb(status, page, total_pages, items).as_markup()

    try:
        await cbq.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cbq.message.answer(text, reply_markup=kb)
