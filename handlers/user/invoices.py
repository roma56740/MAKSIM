from __future__ import annotations

import html
import math

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from filters.admin import NotAdmin
from config import Settings
from db import list_db_admin_ids
from db.invoices import (
    create_invoice,
    count_invoices_for_user,
    count_user_invoices_by_status,
    decode_invoice_analysis,
    get_invoice_full,
    list_invoice_items,
    list_invoices_for_user,
    request_invoice_recheck,
)
from keyboards.user import user_back_cancel_kb, user_main_kb
from services.invoice_recognition import InvoiceRecognitionError, normalize_mime_type
from services.invoice_workflow import (
    analysis_header,
    analyze_invoice_from_telegram,
    money,
    split_item_messages,
)

router = Router()
router.message.filter(NotAdmin())
router.callback_query.filter(NotAdmin())
PAGE_SIZE = 5


class NewInvoice(StatesGroup):
    waiting_file = State()
    waiting_recheck_comment = State()


def _status_badge(status: str) -> str:
    if status == "approved":
        return "✅ Принято"
    if status == "rejected":
        return "❌ Отклонено"
    return "🟡 На проверке"


def _menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новая", callback_data="uinv:new")
    kb.button(text="✅ Принятые", callback_data="uinv:list:approved:0")
    kb.button(text="❌ Отклонённые", callback_data="uinv:list:rejected:0")
    kb.adjust(2, 1)
    return kb


def _list_kb(status: str, page: int, total_pages: int, items: list[dict]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()

    for it in items:
        kb.button(text=f"📎 Открыть #{it['id']}", callback_data=f"uinv:open:{it['id']}")

    nav = []
    if page > 0:
        nav.append(("⬅️", f"uinv:list:{status}:{page-1}"))
    if page + 1 < total_pages:
        nav.append(("➡️", f"uinv:list:{status}:{page+1}"))
    for t, cb in nav:
        kb.button(text=t, callback_data=cb)

    kb.button(text="🏠 Меню накладных", callback_data="uinv:menu")

    # 1 кнопка на строку + навигация
    if items:
        kb.adjust(1)
    if nav:
        kb.adjust(1, len(nav), 1)
    else:
        kb.adjust(1, 1)
    return kb


def _menu_text(stats: dict[str, int]) -> str:
    return (
        "🧾 <b>Накладные</b>\n\n"
        "Выберите раздел ниже:\n"
        "➕ <b>Новая</b> — отправить накладную на проверку\n"
        "✅ <b>Принятые</b> — начисленные вознаграждения\n"
        "❌ <b>Отклонённые</b> — с причиной\n\n"
        "📊 <b>Статистика:</b>\n"
        f"• 🟡 На проверке: <b>{stats['pending']}</b>\n"
        f"• ✅ Принято: <b>{stats['approved']}</b>\n"
        f"• ❌ Отклонено: <b>{stats['rejected']}</b>\n"
        f"• 📦 Всего: <b>{stats['total']}</b>\n"
    )


def _list_text(title: str, status: str, page: int, total_pages: int, items: list[dict]) -> str:
    if not items:
        return f"🧾 <b>{title}</b>\n\nПока пусто."

    lines = [f"🧾 <b>{title}</b>\n📄 Стр. <b>{page+1}</b>/<b>{total_pages}</b>\n"]
    for it in items:
        created = it.get("created_at") or "—"
        badge = _status_badge(it.get("status", status))
        block = f"{badge} <b>#{it['id']}</b>\n🕒 {created}"

        if status == "approved":
            reward = it.get("reward_amount")
            reward_txt = "—" if reward is None else f"{float(reward):,.2f}".replace(",", " ").replace(".", ",")
            block += f"\n🎁 Вознаграждение: <b>{reward_txt}</b>"
            block += f"\n📦 Позиций: <b>{int(it.get('items_count') or 0)}</b>"

        if status == "rejected":
            reason = it.get("reason") or "—"
            block += f"\n🧾 Причина: <b>{reason}</b>"

        lines.append(block)

    return "\n\n".join(lines)


async def _notify_admins_new_invoice(message: Message, settings: Settings, invoice_id: int) -> None:
    inv = await get_invoice_full(settings.db_path, invoice_id)
    if not inv:
        return

    admin_ids = set(settings.admin_ids)
    try:
        for x in await list_db_admin_ids(settings.db_path):
            admin_ids.add(int(x))
    except Exception:
        pass

    user_name = inv.get("user_full_name") or "—"
    user_phone = inv.get("user_phone") or "—"
    tg_id = inv.get("tg_id")
    comment = (inv.get("comment") or "").strip()[:350]
    analysis = decode_invoice_analysis(inv)
    analysis_status = str(inv.get("analysis_status") or "pending")

    caption = (
        ("🔁 <b>Накладная повторно отправлена на проверку</b>\n\n" if comment else "🧾 <b>Новая накладная на проверку</b>\n\n")
        + f"🆔 Накладная: <b>#{invoice_id}</b>\n"
        + f"👤 Пользователь: <b>{html.escape(str(user_name))}</b> (<code>{tg_id}</code>)\n"
        + f"📱 Телефон: <b>{html.escape(str(user_phone))}</b>\n"
        + f"🕒 {inv.get('created_at') or '—'}"
    )

    if analysis_status == "completed" and analysis:
        caption += (
            "\n\n🤖 <b>ИИ распознал документ</b>"
            f"\n📦 Позиций: <b>{len(analysis.get('items') or [])}</b>"
            f"\n💰 Сумма: <b>{money(analysis.get('calculated_total'))}</b> {html.escape(str(analysis.get('currency') or 'RUB'))}"
        )
    elif analysis_status == "failed":
        caption += "\n\n⚠️ <b>Автораспознавание не завершено.</b> Администратор может запустить его повторно."
    else:
        caption += "\n\n🔎 <b>Документ обрабатывается.</b>"

    if comment:
        caption += f"\n\n💬 <b>Комментарий менеджера:</b>\n{html.escape(comment)}"

    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Проверить данные", callback_data=f"ainv:approve:{invoice_id}")
    kb.button(text="🔄 Распознать заново", callback_data=f"ainv:retry:{invoice_id}")
    kb.button(text="❌ Отклонить", callback_data=f"ainv:reject:{invoice_id}")
    kb.adjust(1, 2)

    file_id = inv.get("file_id")
    kind = (inv.get("file_kind") or "").lower()

    for admin_id in admin_ids:
        try:
            if kind == "photo":
                await message.bot.send_photo(admin_id, file_id, caption=caption, reply_markup=kb.as_markup())
            else:
                await message.bot.send_document(admin_id, file_id, caption=caption, reply_markup=kb.as_markup())
        except Exception:
            continue


@router.message(F.text == "🧾 Накладные")
async def invoices_root(message: Message, settings: Settings) -> None:
    stats = await count_user_invoices_by_status(settings.db_path, message.from_user.id)
    await message.answer(_menu_text(stats), reply_markup=_menu_kb().as_markup())


@router.callback_query(F.data == "uinv:menu")
async def invoices_menu(cbq: CallbackQuery, settings: Settings) -> None:
    await cbq.answer()
    stats = await count_user_invoices_by_status(settings.db_path, cbq.from_user.id)
    try:
        await cbq.message.edit_text(_menu_text(stats), reply_markup=_menu_kb().as_markup())
    except Exception:
        await cbq.message.answer(_menu_text(stats), reply_markup=_menu_kb().as_markup())


# --------- Новая накладная: только файл ---------

@router.callback_query(F.data == "uinv:new")
async def invoice_new_start(cbq: CallbackQuery, state: FSMContext) -> None:
    await cbq.answer()
    await state.clear()
    await state.set_state(NewInvoice.waiting_file)

    await cbq.message.answer(
        "📎 <b>Отправьте накладную</b> (фото или документ).\n\n"
        "После отправки она автоматически уйдёт админам на проверку ✅",
        reply_markup=user_back_cancel_kb(),
    )


@router.message(NewInvoice.waiting_file, F.photo | F.document)
async def invoice_got_file(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.photo:
        file_id = message.photo[-1].file_id
        file_kind = "photo"
        source_file_name = f"invoice_{message.from_user.id}.jpg"
        source_mime_type = "image/jpeg"
    else:
        file_id = message.document.file_id
        file_kind = "document"
        source_file_name = message.document.file_name or f"invoice_{message.from_user.id}"
        source_mime_type = normalize_mime_type(source_file_name, message.document.mime_type)
        if source_mime_type not in {"application/pdf", "image/jpeg", "image/png", "image/webp"}:
            await message.answer(
                "⚠️ Отправьте накладную в формате <b>PDF, JPG, PNG или WEBP</b>."
            )
            return

    invoice_id = await create_invoice(
        settings.db_path,
        message.from_user.id,
        file_id,
        file_kind,
        source_file_name=source_file_name,
        source_mime_type=source_mime_type,
    )
    await state.clear()

    progress = await message.answer(
        "🔎 <b>Анализирую накладную…</b>\n\n"
        "Распознаю номер, дату, товары, количество, цены и итоговую сумму. "
        "Для большой накладной это может занять до минуты."
    )

    try:
        analysis = await analyze_invoice_from_telegram(
            message.bot, settings.db_path, invoice_id
        )
        try:
            await progress.edit_text(
                "✅ <b>Накладная распознана</b>\n\n"
                "Проверьте краткий результат ниже. Администратор сможет принять данные "
                "или исправить позиции перед начислением."
            )
        except Exception:
            pass

        await message.answer(analysis_header(analysis, invoice_id=invoice_id))
        for chunk in split_item_messages(analysis):
            await message.answer(chunk)
        await message.answer(
            "🟡 <b>Отправлено администратору на проверку</b>\n\n"
            "После решения вы получите уведомление 🔔",
            reply_markup=user_main_kb(),
        )
    except InvoiceRecognitionError as exc:
        try:
            await progress.edit_text(
                "⚠️ <b>Автоматически распознать накладную не удалось</b>\n\n"
                f"{html.escape(str(exc))}\n\n"
                "Файл сохранён и отправлен администратору. Он сможет запустить распознавание повторно.",
                reply_markup=user_main_kb(),
            )
        except Exception:
            await message.answer(
                "⚠️ Автоматически распознать накладную не удалось. "
                "Файл сохранён и отправлен администратору.",
                reply_markup=user_main_kb(),
            )

    await _notify_admins_new_invoice(message, settings, invoice_id)


@router.message(NewInvoice.waiting_file, ~F.text.in_(["❌ Отмена", "⬅️ Назад"]))
async def invoice_need_file(message: Message) -> None:
    await message.answer("⚠️ Нужно отправить именно <b>фото</b> или <b>документ</b>.")


@router.message(NewInvoice.waiting_file, F.text.in_(["❌ Отмена", "⬅️ Назад"]))
@router.message(NewInvoice.waiting_recheck_comment, F.text.in_(["❌ Отмена", "⬅️ Назад"]))
async def invoice_cancel(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    stats = await count_user_invoices_by_status(settings.db_path, message.from_user.id)
    await message.answer("Ок ✅", reply_markup=user_main_kb())
    await message.answer(_menu_text(stats), reply_markup=_menu_kb().as_markup())


# --------- списки ---------

@router.callback_query(F.data.startswith("uinv:list:"))
async def invoice_list(cbq: CallbackQuery, settings: Settings) -> None:
    await cbq.answer()
    _, _, status, page_s = cbq.data.split(":", 3)
    page = int(page_s)

    total = await count_invoices_for_user(settings.db_path, cbq.from_user.id, status)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    items = await list_invoices_for_user(
        settings.db_path,
        cbq.from_user.id,
        status=status,
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
    )

    title = "✅ Принятые накладные" if status == "approved" else "❌ Отклонённые накладные"
    text = _list_text(title, status, page, total_pages, items)
    kb = _list_kb(status, page, total_pages, items).as_markup()

    try:
        await cbq.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cbq.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("uinv:open:"))
async def invoice_open_file(cbq: CallbackQuery, settings: Settings) -> None:
    await cbq.answer()
    invoice_id = int(cbq.data.split(":")[-1])

    inv = await get_invoice_full(settings.db_path, invoice_id)
    if not inv or int(inv.get("tg_id")) != cbq.from_user.id:
        await cbq.message.answer("⚠️ Накладная не найдена.")
        return

    caption = f"📎 Накладная <b>#{invoice_id}</b> • {_status_badge(inv.get('status', 'pending'))}"

    items = await list_invoice_items(settings.db_path, invoice_id)
    if items:
        caption += "\n\n📦 <b>Товары:</b>"
        for item in items[:6]:
            qty = f"{float(item['quantity']):g}"
            price = f"{float(item['unit_price']):,.2f}".replace(",", " ").replace(".", ",")
            total = f"{float(item['line_total']):,.2f}".replace(",", " ").replace(".", ",")
            product_name = str(item["product_name"])
            if len(product_name) > 45:
                product_name = product_name[:42] + "…"
            caption += f"\n• {html.escape(product_name)} — {qty} × {price} ₽ = <b>{total} ₽</b>"
        if len(items) > 6:
            caption += f"\n… ещё {len(items) - 6} позиций"

    kb = None
    if inv.get("status") == "approved":
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Попросить пересчёт", callback_data=f"uinv:recheck:{invoice_id}")
        builder.adjust(1)
        kb = builder.as_markup()

    if (inv.get("file_kind") or "").lower() == "photo":
        await cbq.message.answer_photo(inv["file_id"], caption=caption, reply_markup=kb)
    else:
        await cbq.message.answer_document(inv["file_id"], caption=caption, reply_markup=kb)


@router.callback_query(F.data.startswith("uinv:recheck:"))
async def invoice_recheck_start(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await cbq.answer()
    invoice_id = int(cbq.data.split(":")[-1])

    inv = await get_invoice_full(settings.db_path, invoice_id)
    if not inv or int(inv.get("tg_id")) != cbq.from_user.id:
        await cbq.message.answer("⚠️ Накладная не найдена.")
        return

    if inv.get("status") != "approved":
        await cbq.message.answer("⚠️ Пересчёт можно запросить только по принятой накладной.")
        return

    await state.clear()
    await state.set_state(NewInvoice.waiting_recheck_comment)
    await state.update_data(invoice_id=invoice_id)

    await cbq.message.answer(
        "✍️ <b>Напишите корректировку текстом</b>\n\n"
        "Например:\n"
        "<i>Прошу пересчитать, некорректно посчитан %.</i>",
        reply_markup=user_back_cancel_kb(),
    )


@router.message(NewInvoice.waiting_recheck_comment, ~F.text.in_(["❌ Отмена", "⬅️ Назад"]))
async def invoice_recheck_comment(message: Message, state: FSMContext, settings: Settings) -> None:
    comment = (message.text or "").strip()
    if len(comment) < 5:
        await message.answer("⚠️ Напишите комментарий хотя бы в пару слов.")
        return
    if len(comment) > 700:
        await message.answer("⚠️ Комментарий слишком длинный. Сократите его, пожалуйста.")
        return

    data = await state.get_data()
    invoice_id = int(data["invoice_id"])

    inv = await get_invoice_full(settings.db_path, invoice_id)
    if not inv or int(inv.get("tg_id")) != message.from_user.id:
        await state.clear()
        await message.answer("⚠️ Накладная не найдена.", reply_markup=user_main_kb())
        return

    await request_invoice_recheck(settings.db_path, invoice_id, comment)
    await state.clear()

    await message.answer(
        "✅ <b>Запрос на пересчёт отправлен</b>\n\n"
        f"🆔 Накладная: <b>#{invoice_id}</b>\n"
        f"💬 Комментарий: <b>{html.escape(comment)}</b>\n\n"
        "Накладная снова отправлена админам на проверку.",
        reply_markup=user_main_kb(),
    )

    await _notify_admins_new_invoice(message, settings, invoice_id)