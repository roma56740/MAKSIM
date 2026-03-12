from __future__ import annotations

from aiogram import Router
from aiogram.filters import BaseFilter
from aiogram.types import Message, ReplyKeyboardRemove

from config import Settings
from db import get_user, is_admin

router = Router()


class IsPendingUser(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        if await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
            return False
        user = await get_user(settings.db_path, message.from_user.id)
        return bool(user and user.get("status") == "pending")


@router.message(IsPendingUser())
async def block_pending(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass

    await message.answer(
        "⏳ Ваша заявка <b>на проверке</b>.\nПожалуйста, дождитесь решения администратора.",
        reply_markup=ReplyKeyboardRemove(),
    )

from aiogram.types import CallbackQuery

@router.callback_query(IsPendingUser())
async def block_pending_callback(call: CallbackQuery) -> None:
    await call.answer("⏳ Ваша заявка на проверке. Дождитесь решения администратора.", show_alert=True)
