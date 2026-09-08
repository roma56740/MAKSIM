"""Rich Bot API messages with classic HTML fallback, using the configured aiogram session."""
from __future__ import annotations
from typing import Any, ClassVar
from aiogram.methods.base import TelegramMethod
from aiogram.types import Message, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound


class SendRichInvoice(TelegramMethod[Message]):
    __returning__: ClassVar = Message
    __api_method__: ClassVar[str] = 'sendRichMessage'
    chat_id: int | str
    rich_message: dict[str, Any]
    reply_markup: InlineKeyboardMarkup | None = None


class EditRichInvoice(TelegramMethod[Message | bool]):
    __returning__: ClassVar = Message | bool
    __api_method__: ClassVar[str] = 'editMessageText'
    chat_id: int | str
    message_id: int
    rich_message: dict[str, Any]
    reply_markup: InlineKeyboardMarkup | None = None


async def display(message, text, keyboard, *, rich=None, edit=False, chat_id=None):
    target = chat_id or message.chat.id
    if rich:
        try:
            if edit:
                return await message.bot(EditRichInvoice(chat_id=target, message_id=message.message_id,
                    rich_message={'html': rich, 'skip_entity_detection': True}, reply_markup=keyboard))
            return await message.bot(SendRichInvoice(chat_id=target,
                rich_message={'html': rich, 'skip_entity_detection': True}, reply_markup=keyboard))
        except (TelegramBadRequest, TelegramNotFound) as exc:
            if 'message is not modified' in str(exc).lower():
                return None
            # Old/local Bot API servers can lack rich methods/markup.
    if edit:
        try:
            return await message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return None
            if not any(x in str(exc).lower() for x in ('no text', "can't be edited", 'not found', 'rich')):
                raise
    return await message.bot.send_message(target, text, parse_mode='HTML', reply_markup=keyboard)
