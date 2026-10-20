import asyncio
from functools import wraps
from weakref import WeakValueDictionary

from aiogram.exceptions import TelegramBadRequest


_entry_locks = WeakValueDictionary()


def serialize_chat_entry(handler):
    """Serialize rapid callbacks from the same account, including FSM updates."""
    @wraps(handler)
    async def wrapped(call, *args, **kwargs):
        user_id = call.from_user.id
        lock = _entry_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _entry_locks[user_id] = lock
        async with lock:
            return await handler(call, *args, **kwargs)
    return wrapped


async def edit_chat_message(message, text, reply_markup):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if 'message is not modified' not in str(exc).lower():
            raise


async def is_current_chat(state, chat_state, thread_id):
    return (
        await state.get_state() == chat_state.state
        and (await state.get_data()).get('thread_id') == thread_id
    )
