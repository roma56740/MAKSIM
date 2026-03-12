from __future__ import annotations

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

_TRASH_KEY = "_trash_msg_ids"


async def remember(state: FSMContext, message_id: int) -> None:
    data = await state.get_data()
    ids = list(data.get(_TRASH_KEY, []))
    ids.append(int(message_id))
    await state.update_data({_TRASH_KEY: ids})


async def remember_msg(state: FSMContext, msg: Message | None) -> None:
    if msg:
        await remember(state, msg.message_id)


async def safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def cleanup(bot: Bot, chat_id: int, state: FSMContext, keep_ids: set[int] | None = None) -> None:
    keep_ids = keep_ids or set()
    data = await state.get_data()
    ids = list(data.get(_TRASH_KEY, []))

    for mid in ids:
        if mid in keep_ids:
            continue
        await safe_delete(bot, chat_id, int(mid))

    await state.update_data({_TRASH_KEY: []})
