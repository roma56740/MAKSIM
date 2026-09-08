"""Rich Bot API messages with classic HTML fallback, using the configured aiogram session."""
from __future__ import annotations
from typing import Any, ClassVar
import html
import re
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


def cell_text(value, limit=500):
    """Soft wrapping for long visible identifiers. Stored values/callbacks remain untouched."""
    value = str(value if value is not None and value != '' else '—')[:limit]
    value = value.replace('\u00a0', ' ').replace('\u202f', ' ')
    value = re.sub(r'\S{13,}', lambda m: '\u200b'.join(m[0][i:i+12] for i in range(0, len(m[0]), 12)), value)
    return html.escape(value)


def rich_button(label, data, style='primary'):
    """Escape both visible text and callback attributes; no arbitrary HTML values."""
    if style not in {'primary', 'success', 'danger', 'link'}:
        style = 'primary'
    return (f'<tg-button type="callback_data" style="{style}" '
            f'data="{html.escape(str(data), quote=True)}">{html.escape(str(label))}</tg-button>')


def rich_text(text):
    """Convert the editor's trusted classic HTML blocks without nesting block elements."""
    blocks = re.split(r'(<blockquote(?: expandable)?>.*?</blockquote>)', text, flags=re.S)
    return ''.join(block.replace('\n', '<br>') if block.startswith('<blockquote') else
                   ''.join('<p>'+part.replace('\n', '<br>')+'</p>' for part in block.split('\n\n') if part)
                   for block in blocks)


def embed_controls(rich, keyboard):
    """Only append actions not already embedded in a table. Classic markup is fallback-only."""
    if keyboard is None:
        return rich
    present = {html.unescape(x) for x in re.findall(r'\bdata="([^"]+)"', rich)}
    for row in keyboard.inline_keyboard:
        buttons = []
        for item in row:
            if item.callback_data and item.callback_data not in present:
                buttons.append(rich_button(item.text, item.callback_data, getattr(item, 'style', None) or 'primary'))
                present.add(item.callback_data)
            elif item.url:
                buttons.append(f'<tg-button type="url" url="{html.escape(item.url, quote=True)}">{html.escape(item.text)}</tg-button>')
        if buttons:
            rich += ''.join('<tg-button-row>'+b+'</tg-button-row>' for b in buttons)
    return rich


async def display(message, text, keyboard, *, rich=None, edit=False, chat_id=None):
    target = chat_id or message.chat.id
    content = embed_controls(rich if rich is not None else rich_text(text), keyboard)
    # Explicit empty markup clears a keyboard from an older version when editing.
    empty_keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    try:
        if edit:
            return await message.bot(EditRichInvoice(chat_id=target, message_id=message.message_id,
                rich_message={'html': content, 'skip_entity_detection': True}, reply_markup=empty_keyboard))
        return await message.bot(SendRichInvoice(chat_id=target,
            rich_message={'html': content, 'skip_entity_detection': True}, reply_markup=empty_keyboard))
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
