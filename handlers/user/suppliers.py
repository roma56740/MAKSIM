from __future__ import annotations

import html
import math

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import count_suppliers, get_supplier, get_user, is_admin, list_suppliers
from keyboards.user import user_main_kb

router = Router()
PAGE_SIZE = 8


async def _can_view(event: Message | CallbackQuery, settings: Settings) -> bool:
    if not event.from_user:
        return False
    uid = event.from_user.id
    if await is_admin(settings.db_path, uid, settings.admin_ids):
        return False
    user = await get_user(settings.db_path, uid)
    return bool(user and user.get("status") == "approved")


router.message.filter(_can_view)
router.callback_query.filter(_can_view)


async def _show_list(message: Message, settings: Settings, page: int = 0, *, edit: bool = False) -> None:
    total = await count_suppliers(settings.db_path)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    suppliers = await list_suppliers(settings.db_path, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    kb = InlineKeyboardBuilder()
    for supplier in suppliers:
        kb.button(text=supplier["name"], callback_data=f"usup:view:{supplier['id']}:{page}")
    kb.adjust(1)
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"usup:list:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"usup:list:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="usup:menu"))
    text = "🏢 <b>Поставщики</b>\n\n"
    text += "Выберите поставщика, чтобы посмотреть контакты и описание." if suppliers else "Поставщики пока не добавлены."
    if pages > 1:
        text += f"\n\nСтраница {page+1} из {pages}"
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb.as_markup())
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    else:
        await message.answer(text, reply_markup=kb.as_markup())


@router.message(F.text == "🏢 Поставщики")
async def suppliers_menu(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await _show_list(message, settings)


@router.callback_query(F.data == "usup:menu")
async def suppliers_main_menu(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await state.clear()
    await query.message.answer("🏠 <b>Главное меню</b>", reply_markup=user_main_kb())


@router.callback_query(F.data.startswith("usup:list:"))
async def suppliers_page(query: CallbackQuery, settings: Settings) -> None:
    try:
        _, _, raw_page = query.data.split(":")
        page = int(raw_page)
    except (ValueError, AttributeError):
        await query.answer("Откройте список поставщиков заново.", show_alert=True)
        return
    await query.answer()
    await _show_list(query.message, settings, page, edit=True)


@router.callback_query(F.data.startswith("usup:view:"))
async def supplier_description(query: CallbackQuery, settings: Settings) -> None:
    try:
        _, _, raw_id, raw_page = query.data.split(":")
        supplier_id, page = int(raw_id), max(0, int(raw_page))
    except (ValueError, AttributeError):
        await query.answer("Откройте список поставщиков заново.", show_alert=True)
        return
    supplier = await get_supplier(settings.db_path, supplier_id)
    if not supplier:
        await query.answer("Поставщик больше недоступен. Обновите список.", show_alert=True)
        return
    await query.answer()
    description = (supplier.get("description") or "").strip() or "Описание пока не добавлено."
    name = html.escape(str(supplier.get("name") or "—"))
    website = html.escape(str(supplier.get("website") or "—"))
    email = html.escape(str(supplier.get("email") or "—"))
    phone = html.escape(str(supplier.get("phone") or "—"))
    text = (
        f"🏢 <b>{name}</b>\n"
        f"🌐 Сайт: {website}\n"
        f"✉️ Почта: {email}\n"
        f"📞 Телефон: {phone}\n\n"
        f"📝 <b>Описание:</b>\n{html.escape(description)}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К списку поставщиков", callback_data=f"usup:list:{page}")
    await query.message.edit_text(text, reply_markup=kb.as_markup(), link_preview_options=LinkPreviewOptions(is_disabled=True))
