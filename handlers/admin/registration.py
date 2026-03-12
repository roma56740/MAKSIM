from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import AdminRegCb
from config import Settings
from db import (
    approve_registration,
    block_user,
    get_registration,
    is_admin,
    list_registrations,
    reject_registration,
)
from keyboards.admin import admin_main_kb, admin_reg_kb
from keyboards.user import user_main_kb, user_register_kb

router = Router()


class RejectForm(StatesGroup):
    reason = State()


async def _no_access(message: Message | CallbackQuery) -> None:
    if isinstance(message, CallbackQuery):
        await message.answer("Нет доступа", show_alert=True)
    else:
        await message.answer("Нет доступа")


def _reg_card_text(reg: dict) -> str:
    text = (
        f"🧾 <b>Заявка</b>\n\n"
        f"ID: <code>{reg['tg_id']}</code>\n"
        f"Статус: {reg['reg_type']}\n"
        f"ФИО: {reg['full_name']}\n"
        f"Телефон: {reg['phone']}\n"
        f"Состояние: <b>{reg['status']}</b>"
    )
    if reg.get("reason"):
        text += f"\nПричина: <b>{reg['reason']}</b>"
    return text


def _actions_kb(reg: dict) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    status = reg.get("status")
    tg_id = int(reg["tg_id"])

    if status in {"pending", "rejected"}:
        kb.row(
            InlineKeyboardButton(text="✅ Одобрить", callback_data=AdminRegCb(action="approve", tg_id=tg_id).pack()),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=AdminRegCb(action="reject", tg_id=tg_id).pack()),
        )
    elif status == "approved":
        kb.add(
            InlineKeyboardButton(text="🚫 Заблокировать", callback_data=AdminRegCb(action="block", tg_id=tg_id).pack())
        )

    kb.row(InlineKeyboardButton(text="⬅️ К списку", callback_data=AdminRegCb(action="back", tg_id=tg_id).pack()))
    return kb


@router.message(F.text == "👤 Модерация регистрации")
async def admin_reg_menu(message: Message, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        await _no_access(message)
        return
    await message.answer("👤 <b>Модерация регистрации</b>", reply_markup=admin_reg_kb())


@router.message(F.text == "⬅️ Назад", StateFilter(None))
async def admin_back(message: Message, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        await _no_access(message)
        return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())


async def _send_list(message: Message, settings: Settings, status: str, title: str) -> None:
    regs = await list_registrations(settings.db_path, status=status, limit=30, offset=0)
    if not regs:
        await message.answer(f"{title}\n\nПусто.")
        return

    kb = InlineKeyboardBuilder()
    for r in regs:
        kb.add(
            InlineKeyboardButton(
                text=f"{r['full_name']} ({r['tg_id']})",
                callback_data=AdminRegCb(action="view", tg_id=int(r["tg_id"])).pack(),
            )
        )
    kb.adjust(1)
    await message.answer(title, reply_markup=kb.as_markup())


@router.message(F.text == "🆕 Заявки")
async def admin_list_pending(message: Message, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        await _no_access(message)
        return
    await _send_list(message, settings, status="pending", title="🆕 <b>Заявки (на проверке)</b>")


@router.message(F.text == "✅ Одобренные")
async def admin_list_approved(message: Message, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        await _no_access(message)
        return
    await _send_list(message, settings, status="approved", title="✅ <b>Одобренные</b>")


@router.message(F.text == "❌ Отклонённые")
async def admin_list_rejected(message: Message, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        await _no_access(message)
        return
    await _send_list(message, settings, status="rejected", title="❌ <b>Отклонённые</b>")


@router.callback_query(AdminRegCb.filter(F.action == "view"))
async def admin_view(call: CallbackQuery, callback_data: AdminRegCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await _no_access(call)
        return

    reg = await get_registration(settings.db_path, callback_data.tg_id)
    if not reg:
        await call.answer("Заявка не найдена", show_alert=True)
        return

    if reg.get("file_id") and reg.get("file_kind"):
        try:
            if reg["file_kind"] == "document":
                await call.message.answer_document(reg["file_id"], caption=f"Файл по заявке {reg['tg_id']}")
            elif reg["file_kind"] == "photo":
                await call.message.answer_photo(reg["file_id"], caption=f"Фото по заявке {reg['tg_id']}")
        except Exception:
            pass

    await call.message.answer(_reg_card_text(reg), reply_markup=_actions_kb(reg).as_markup())
    await call.answer()


@router.callback_query(AdminRegCb.filter(F.action == "back"))
async def admin_back_to_list(call: CallbackQuery, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await _no_access(call)
        return
    await call.message.answer("👤 <b>Модерация регистрации</b>", reply_markup=admin_reg_kb())
    await call.answer()


@router.callback_query(AdminRegCb.filter(F.action == "approve"))
async def admin_approve(call: CallbackQuery, callback_data: AdminRegCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await _no_access(call)
        return

    chat_id = call.message.chat.id
    await approve_registration(settings.db_path, callback_data.tg_id)

    try:
        await call.message.delete()
    except Exception:
        pass

    await call.bot.send_message(chat_id, "✅ <b>Успешно одобрено</b>")

    try:
        await call.bot.send_message(
            callback_data.tg_id,
            "✅ Регистрация одобрена. Доступ открыт.",
            reply_markup=user_main_kb(),
        )
    except Exception:
        pass

    await call.answer()


@router.callback_query(AdminRegCb.filter(F.action == "block"))
async def admin_block(call: CallbackQuery, callback_data: AdminRegCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await _no_access(call)
        return

    await block_user(settings.db_path, callback_data.tg_id)

    # удалить сообщение с кнопками
    try:
        await call.message.delete()
    except Exception:
        pass

    # написать в этот же чат
    chat_id = call.message.chat.id
    await call.bot.send_message(chat_id, "Пользователь удалён.")

    # уведомить пользователя (как и было)
    try:
        await call.bot.send_message(callback_data.tg_id, "🚫 Доступ заблокирован. Обратитесь к администратору.")
    except Exception:
        pass

    await call.answer()



@router.callback_query(AdminRegCb.filter(F.action == "reject"))
async def admin_reject_start(
    call: CallbackQuery,
    callback_data: AdminRegCb,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await _no_access(call)
        return

    await state.update_data(tg_id=callback_data.tg_id, admin_chat_id=call.message.chat.id)

    try:
        await call.message.delete()
    except Exception:
        pass

    await state.set_state(RejectForm.reason)
    await call.bot.send_message(call.from_user.id, f"Введите причину отказа для ID <code>{callback_data.tg_id}</code>:")
    await call.answer()


@router.message(RejectForm.reason)
async def admin_reject_finish(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        await _no_access(message)
        return

    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("Причина слишком короткая. Введите подробнее:")
        return

    data = await state.get_data()
    tg_id = int(data["tg_id"])
    admin_chat_id = int(data["admin_chat_id"])

    await reject_registration(settings.db_path, tg_id, reason)
    await state.clear()

    await message.bot.send_message(admin_chat_id, "❌ <b>Успешно отклонено</b>")

    try:
        await message.bot.send_message(
            tg_id,
            f"❌ Регистрация отклонена.\nПричина: <b>{reason}</b>\n\nНажмите «📝 Регистрация», чтобы отправить заново.",
            reply_markup=user_register_kb(),
        )
    except Exception:
        pass
