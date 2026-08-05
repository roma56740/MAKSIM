from __future__ import annotations

import asyncio
import html
import logging
import math
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BusinessConnection, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import is_admin, list_db_admin_ids
from db.personal_messages import (
    count_approved_managers,
    get_active_business_connection,
    get_manager,
    list_approved_managers,
    save_business_connection,
)
from keyboards.admin import admin_back_cancel_kb, admin_main_kb

router = Router()
logger = logging.getLogger(__name__)
PAGE_SIZE = 8


class PersonalMessageForm(StatesGroup):
    waiting_text = State()
    confirm_all = State()


async def _admin(user_id: int, settings: Settings) -> bool:
    return await is_admin(settings.db_path, user_id, settings.admin_ids)


async def _excluded_admin_ids(settings: Settings) -> set[int]:
    result = set(settings.admin_ids)
    result.update(await list_db_admin_ids(settings.db_path))
    return result


def _main_kb(has_connection: bool):
    kb = InlineKeyboardBuilder()
    if has_connection:
        kb.button(text="👤 Одному менеджеру", callback_data="pmsg:list:0")
        kb.button(text="👥 Всем менеджерам", callback_data="pmsg:all")
    kb.button(text="🔄 Проверить подключение", callback_data="pmsg:menu")
    kb.adjust(1)
    return kb.as_markup()


def _managers_kb(items: list[dict[str, Any]], page: int, total_pages: int):
    kb = InlineKeyboardBuilder()
    for item in items:
        name = str(item.get("full_name") or item.get("first_name") or item.get("username") or item["tg_id"])
        kb.button(text=f"👤 {name[:42]}", callback_data=f"pmsg:pick:{int(item['tg_id'])}")
    if page > 0:
        kb.button(text="⬅️", callback_data=f"pmsg:list:{page-1}")
    kb.button(text=f"{page+1}/{total_pages}", callback_data="pmsg:noop")
    if page + 1 < total_pages:
        kb.button(text="➡️", callback_data=f"pmsg:list:{page+1}")
    kb.button(text="🏠 Назад", callback_data="pmsg:menu")
    kb.adjust(1)
    return kb.as_markup()


def _confirm_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить всем", callback_data="pmsg:send_all")
    kb.button(text="❌ Отмена", callback_data="pmsg:cancel")
    kb.adjust(1)
    return kb.as_markup()


def _manager_name(manager: dict[str, Any]) -> str:
    first_name = str(manager.get("first_name") or "").strip()
    if first_name:
        return first_name
    full_name = str(manager.get("full_name") or "").strip()
    if full_name:
        return full_name.split()[0]
    return "коллега"


def _personalize(text: str, manager: dict[str, Any]) -> str:
    name = _manager_name(manager)
    body = (text or "").strip()
    if "{name}" in body:
        return body.replace("{name}", name)
    return f"Здравствуйте, {name}!\n\n{body}"


async def _show_root(message: Message, settings: Settings, admin_id: int) -> None:
    connection = await get_active_business_connection(settings.db_path, admin_id)
    if connection:
        text = (
            "✉️ <b>Личные сообщения менеджерам</b>\n\n"
            "✅ Telegram Business подключён. Сообщения будут отправляться "
            "<b>от имени вашего личного аккаунта</b>, а не от имени бота.\n\n"
            "Обращение по имени добавляется автоматически. В тексте также можно использовать "
            "шаблон <code>{name}</code>."
        )
    else:
        text = (
            "✉️ <b>Личные сообщения менеджерам</b>\n\n"
            "Чтобы бот мог отправлять сообщения от имени администратора, подключите его "
            "к своему аккаунту в настройках <b>Telegram Business → Чат-боты</b> и разрешите отправку сообщений.\n\n"
            "После подключения вернитесь сюда и нажмите «Проверить подключение». "
            "Без Telegram Business бот принципиально не может выдавать свои сообщения за сообщения личного аккаунта."
        )
    await message.answer(text, reply_markup=_main_kb(bool(connection)))


@router.business_connection()
async def business_connection_updated(
    business_connection: BusinessConnection,
    settings: Settings,
) -> None:
    admin_id = int(business_connection.user.id)
    if not await _admin(admin_id, settings):
        return
    rights = None
    rights_model = getattr(business_connection, "rights", None)
    if rights_model is not None:
        try:
            rights = rights_model.model_dump(exclude_none=True)
        except Exception:
            rights = {}
    elif hasattr(business_connection, "can_reply"):
        rights = {"can_reply": bool(getattr(business_connection, "can_reply", False))}
    await save_business_connection(
        settings.db_path,
        connection_id=business_connection.id,
        admin_tg_id=admin_id,
        user_chat_id=business_connection.user_chat_id,
        is_enabled=business_connection.is_enabled,
        rights=rights,
    )


@router.message(F.text == "✉️ Личные сообщения")
async def personal_messages_root(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _admin(message.from_user.id, settings):
        return
    await state.clear()
    await _show_root(message, settings, message.from_user.id)


@router.callback_query(F.data == "pmsg:menu")
async def personal_messages_menu(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    await state.clear()
    await _show_root(call.message, settings, call.from_user.id)
    await call.answer()


@router.callback_query(F.data.startswith("pmsg:list:"))
async def personal_managers_list(call: CallbackQuery, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    page = max(0, int(call.data.rsplit(":", 1)[-1]))
    excluded_ids = await _excluded_admin_ids(settings)
    total = await count_approved_managers(settings.db_path, excluded_ids)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, total_pages - 1)
    items = await list_approved_managers(
        settings.db_path,
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
        excluded_ids=excluded_ids,
    )
    await call.message.answer(
        "👤 <b>Выберите менеджера</b>:",
        reply_markup=_managers_kb(items, page, total_pages),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pmsg:pick:"))
async def personal_pick_manager(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    tg_id = int(call.data.rsplit(":", 1)[-1])
    manager = await get_manager(settings.db_path, tg_id)
    excluded_ids = await _excluded_admin_ids(settings)
    if not manager or manager.get("status") != "approved" or tg_id in excluded_ids:
        await call.answer("Менеджер не найден", show_alert=True)
        return
    await state.clear()
    await state.set_state(PersonalMessageForm.waiting_text)
    await state.update_data(target_id=tg_id)
    await call.message.answer(
        f"✉️ Сообщение для <b>{html.escape(str(manager.get('full_name') or manager.get('first_name') or tg_id))}</b>\n\n"
        "Введите текст. Обращение по имени будет добавлено автоматически. "
        "Для обращения внутри текста используйте <code>{name}</code>.",
        reply_markup=admin_back_cancel_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "pmsg:all")
async def personal_all_start(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    await state.clear()
    await state.set_state(PersonalMessageForm.waiting_text)
    await state.update_data(target_id=0)
    await call.message.answer(
        "👥 <b>Сообщение всем менеджерам</b>\n\n"
        "Введите общий текст. Для каждого менеджера бот автоматически подставит его имя. "
        "Можно использовать <code>{name}</code> в нужном месте текста.",
        reply_markup=admin_back_cancel_kb(),
    )
    await call.answer()


@router.message(PersonalMessageForm.waiting_text, F.text)
async def personal_text_input(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _admin(message.from_user.id, settings):
        return
    if message.text in {"❌ Отмена", "⬅️ Назад"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb())
        return
    text = (message.text or "").strip()
    if not text or len(text) > 3500:
        await message.answer("Введите текст длиной до 3500 символов.")
        return
    connection = await get_active_business_connection(settings.db_path, message.from_user.id)
    if not connection:
        await state.clear()
        await message.answer("Подключение Telegram Business не найдено.", reply_markup=admin_main_kb())
        return

    data = await state.get_data()
    target_id = int(data.get("target_id") or 0)
    await state.update_data(text=text, connection_id=connection["connection_id"])

    if target_id == 0:
        excluded_ids = await _excluded_admin_ids(settings)
        total = await count_approved_managers(settings.db_path, excluded_ids)
        preview_manager = {"first_name": "Имя"}
        preview_text = _personalize(text, preview_manager)
        if len(preview_text) > 1200:
            preview_text = preview_text[:1197] + "…"
        preview = html.escape(preview_text)
        await state.set_state(PersonalMessageForm.confirm_all)
        await message.answer(
            f"👁 <b>Предпросмотр</b>\n\n<pre>{preview}</pre>\n\n"
            f"Получателей: <b>{total}</b>. Подтвердите отправку от имени вашего Telegram Business-аккаунта.",
            reply_markup=_confirm_kb(),
        )
        return

    manager = await get_manager(settings.db_path, target_id)
    if not manager:
        await state.clear()
        await message.answer("Менеджер не найден.", reply_markup=admin_main_kb())
        return
    try:
        await message.bot.send_message(
            chat_id=target_id,
            text=_personalize(text, manager),
            business_connection_id=str(connection["connection_id"]),
            parse_mode=None,
        )
    except Exception as exc:
        logger.warning(
            "Business message from admin %s to manager %s failed: %s",
            message.from_user.id,
            target_id,
            exc,
        )
        await state.clear()
        await message.answer(
            "⚠️ Не удалось отправить сообщение от личного аккаунта. "
            "Проверьте подключение Telegram Business и доступ к переписке с менеджером.",
            reply_markup=admin_main_kb(),
        )
        return

    await state.clear()
    await message.answer("✅ Сообщение отправлено от имени администратора.", reply_markup=admin_main_kb())


@router.callback_query(F.data == "pmsg:send_all")
async def personal_send_all(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    data = await state.get_data()
    text = str(data.get("text") or "").strip()
    connection_id = str(data.get("connection_id") or "")
    if not text or not connection_id:
        await state.clear()
        await call.answer("Данные отправки потеряны", show_alert=True)
        return

    await call.answer("Начинаю отправку")
    progress = await call.message.answer("⏳ Отправляю сообщения от имени администратора…")
    offset = 0
    ok = 0
    fail = 0
    excluded_ids = await _excluded_admin_ids(settings)

    while True:
        managers = await list_approved_managers(
            settings.db_path, limit=100, offset=offset, excluded_ids=excluded_ids
        )
        if not managers:
            break
        for manager in managers:
            tg_id = int(manager["tg_id"])
            try:
                await call.bot.send_message(
                    chat_id=tg_id,
                    text=_personalize(text, manager),
                    business_connection_id=connection_id,
                    parse_mode=None,
                )
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.15)
        offset += len(managers)
        try:
            await progress.edit_text(
                f"⏳ Отправка продолжается…\nУспешно: <b>{ok}</b>\nОшибки: <b>{fail}</b>"
            )
        except Exception:
            pass

    await state.clear()
    await progress.edit_text(
        "✅ <b>Отправка завершена</b>\n\n"
        f"Успешно: <b>{ok}</b>\nОшибки: <b>{fail}</b>"
    )
    await call.message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())


@router.callback_query(F.data.in_({"pmsg:cancel", "pmsg:noop"}))
async def personal_cancel_or_noop(call: CallbackQuery, state: FSMContext) -> None:
    if call.data == "pmsg:cancel":
        await state.clear()
        await call.message.answer("Отменено.", reply_markup=admin_main_kb())
    await call.answer()
