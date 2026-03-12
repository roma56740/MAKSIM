from __future__ import annotations

import math

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import AdminsCb
from config import Settings
from db import add_admin, is_admin, list_db_admin_ids, remove_admin
from keyboards.admin import admin_back_cancel_kb, admin_main_kb

router = Router()

PAGE_SIZE = 8


class AddAdminForm(StatesGroup):
    tg_id = State()


def _is_superadmin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.admin_ids


def _admins_text(env_admins: set[int], db_admins: list[int], page: int) -> tuple[str, int]:
    all_ids = sorted(set(env_admins) | set(db_admins))
    total = max(1, math.ceil(len(all_ids) / PAGE_SIZE))
    page = max(0, min(page, total - 1))

    start = page * PAGE_SIZE
    items = all_ids[start : start + PAGE_SIZE]

    lines = []
    for i, aid in enumerate(items, start=start + 1):
        mark = "⭐" if aid in env_admins else "•"
        lines.append(f"{i}. {mark} <code>{aid}</code>")

    text = (
        "👥 <b>Админы</b>\n\n"
        "⭐ — супер-админ (из .env)\n"
        "• — админ (из базы)\n\n"
        f"Страница: <b>{page + 1}/{total}</b>\n\n"
        + ("\n".join(lines) if lines else "Пусто.")
    )
    return text, total


def _admins_kb(
    env_admins: set[int],
    db_admins: list[int],
    page: int,
    total_pages: int,
    can_edit: bool,
) -> InlineKeyboardBuilder:
    all_ids = sorted(set(env_admins) | set(db_admins))
    start = page * PAGE_SIZE
    items = all_ids[start : start + PAGE_SIZE]

    kb = InlineKeyboardBuilder()

    # remove buttons (только db админы)
    if can_edit:
        for aid in items:
            if aid in env_admins:
                continue
            kb.add(
                InlineKeyboardButton(
                    text=f"🗑 Удалить {aid}",
                    callback_data=AdminsCb(action="remove", page=page, tg_id=aid).pack(),
                )
            )
        kb.adjust(1)

    # pagination
    prev_page = page - 1 if page > 0 else 0
    next_page = page + 1 if page + 1 < total_pages else page

    kb.row(
        InlineKeyboardButton(text="⬅️", callback_data=AdminsCb(action="page", page=prev_page, tg_id=0).pack()),
        InlineKeyboardButton(text="➡️", callback_data=AdminsCb(action="page", page=next_page, tg_id=0).pack()),
    )

    if can_edit:
        kb.row(
            InlineKeyboardButton(text="➕ Добавить админа", callback_data=AdminsCb(action="add", page=page, tg_id=0).pack())
        )

    kb.row(
        InlineKeyboardButton(text="⬅️ В меню", callback_data=AdminsCb(action="back", page=page, tg_id=0).pack())
    )

    return kb


async def _render(call_or_msg: Message | CallbackQuery, settings: Settings, page: int, edit: bool = False) -> None:
    db_admins = await list_db_admin_ids(settings.db_path)
    text, total = _admins_text(settings.admin_ids, db_admins, page)
    kb = _admins_kb(settings.admin_ids, db_admins, page, total, can_edit=_is_superadmin(call_or_msg.from_user.id, settings))

    if isinstance(call_or_msg, CallbackQuery):
        if edit:
            await call_or_msg.message.edit_text(text, reply_markup=kb.as_markup())
        else:
            await call_or_msg.message.answer(text, reply_markup=kb.as_markup())
    else:
        await call_or_msg.answer(text, reply_markup=kb.as_markup())


@router.message(F.text == "👥 Админы")
async def admins_open(message: Message, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return
    await _render(message, settings, page=0, edit=False)


@router.callback_query(AdminsCb.filter(F.action == "page"))
async def admins_page(call: CallbackQuery, callback_data: AdminsCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return
    await _render(call, settings, page=callback_data.page, edit=True)
    await call.answer()


@router.callback_query(AdminsCb.filter(F.action == "back"))
async def admins_back(call: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())
    await call.answer()


@router.callback_query(AdminsCb.filter(F.action == "add"))
async def admins_add_start(call: CallbackQuery, callback_data: AdminsCb, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    if not _is_superadmin(call.from_user.id, settings):
        await call.answer("Только супер-админ", show_alert=True)
        return

    await state.clear()
    await state.set_state(AddAdminForm.tg_id)

    await call.message.answer(
        "➕ <b>Добавить админа</b>\n\n"
        "Отправьте <b>ID</b> пользователя (число).\n"
        "Управление: «⬅️ Назад» / «❌ Отмена»",
        reply_markup=admin_back_cancel_kb(),
    )
    await call.answer()


@router.message(AddAdminForm.tg_id, F.text == "❌ Отмена")
async def admins_add_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Отменено", reply_markup=admin_main_kb())


@router.message(AddAdminForm.tg_id, F.text == "⬅️ Назад")
async def admins_add_back(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())


@router.message(AddAdminForm.tg_id)
async def admins_add_finish(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return
    if not _is_superadmin(message.from_user.id, settings):
        await message.answer("Только супер-админ.", reply_markup=admin_main_kb())
        await state.clear()
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите ID числом.", reply_markup=admin_back_cancel_kb())
        return

    new_id = int(raw)

    if new_id in settings.admin_ids:
        await message.answer("⭐ Этот ID уже супер-админ (в .env).", reply_markup=admin_main_kb())
        await state.clear()
        return

    await add_admin(settings.db_path, new_id)
    await state.clear()

    await message.answer(f"✅ Админ добавлен: <code>{new_id}</code>")
    await _render(message, settings, page=0, edit=False)


@router.callback_query(AdminsCb.filter(F.action == "remove"))
async def admins_remove_confirm(call: CallbackQuery, callback_data: AdminsCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    if not _is_superadmin(call.from_user.id, settings):
        await call.answer("Только супер-админ", show_alert=True)
        return

    if callback_data.tg_id in settings.admin_ids:
        await call.answer("Нельзя удалить супер-админа", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="✅ Удалить",
            callback_data=AdminsCb(action="confirm", page=callback_data.page, tg_id=callback_data.tg_id).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=AdminsCb(action="cancel", page=callback_data.page, tg_id=0).pack(),
        ),
    )

    await call.message.answer(
        f"🗑 Удалить админа <code>{callback_data.tg_id}</code>?",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(AdminsCb.filter(F.action == "cancel"))
async def admins_remove_cancel(call: CallbackQuery) -> None:
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()


@router.callback_query(AdminsCb.filter(F.action == "confirm"))
async def admins_remove_done(call: CallbackQuery, callback_data: AdminsCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    if not _is_superadmin(call.from_user.id, settings):
        await call.answer("Только супер-админ", show_alert=True)
        return

    if callback_data.tg_id in settings.admin_ids:
        await call.answer("Нельзя удалить супер-админа", show_alert=True)
        return

    await remove_admin(settings.db_path, callback_data.tg_id)

    try:
        await call.message.delete()
    except Exception:
        pass

    await call.answer("Удалено")
    await _render(call, settings, page=callback_data.page, edit=True)
