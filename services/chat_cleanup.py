from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
import logging
import time

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message, MessageId

from db import get_user, is_admin
from db.chat_cleanup import due_messages, forget_message, postpone_due_messages, retry_message, schedule_message

logger = logging.getLogger(__name__)
MESSAGE_LIFETIME_SECONDS = 2 * 60 * 60
_keep_messages = ContextVar('keep_administrator_messages', default=False)


def keep_admin_messages(handler):
    """Mark explicitly authored administrator content, including every attachment."""
    @wraps(handler)
    async def wrapped(*args, **kwargs):
        token = _keep_messages.set(True)
        try:
            return await handler(*args, **kwargs)
        finally:
            _keep_messages.reset(token)
    return wrapped


async def _manager_chat(settings, chat_id: int) -> bool:
    if chat_id <= 0 or await is_admin(settings.db_path, chat_id, settings.admin_ids):
        return False
    return bool(await get_user(settings.db_path, chat_id))


async def track_message(settings, bot_id, message):
    if message.chat.type != 'private' or message.business_connection_id or message.message_id <= 0:
        return
    if not await _manager_chat(settings, message.chat.id):
        return
    sent_at = message.date.timestamp()
    # Telegram allows deleting a private dice message only after 24 hours.
    lifetime = max(MESSAGE_LIFETIME_SECONDS, 86401) if message.dice else MESSAGE_LIFETIME_SECONDS
    await schedule_message(settings.db_path, bot_id, message.chat.id, message.message_id,
                           sent_at, sent_at + lifetime)


class ManagerIncomingCleanup(BaseMiddleware):
    def __init__(self, settings):
        self.settings = settings

    async def __call__(self, handler, event, data):
        message = event.message
        if message and message.from_user and not message.from_user.is_bot:
            try:
                await track_message(self.settings, data['bot'].id, message)
            except Exception:
                logger.exception('Could not schedule incoming manager message %s', message.message_id)
        return await handler(event, data)


class ManagerOutgoingCleanup(BaseRequestMiddleware):
    def __init__(self, settings):
        self.settings = settings

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        name = method.__api_method__
        if _keep_messages.get() or getattr(method, 'business_connection_id', None):
            return result
        if not name.startswith(('send', 'copyMessage', 'forwardMessage')):
            return result
        try:
            for sent in result if isinstance(result, list) else [result]:
                if isinstance(sent, Message):
                    await track_message(self.settings, bot.id, sent)
                elif isinstance(sent, MessageId):
                    chat_id = int(method.chat_id)
                    if await _manager_chat(self.settings, chat_id):
                        now = time.time()
                        await schedule_message(self.settings.db_path, bot.id, chat_id, sent.message_id,
                                               now, now + MESSAGE_LIFETIME_SECONDS)
        except Exception:
            # A scheduling failure must never turn a successful send into a duplicate send.
            logger.exception('Could not schedule manager messages from %s', name)
        return result


async def process_message_cleanup(bot, settings, *, now=None):
    now = time.time() if now is None else now
    deleted = 0
    for row in await due_messages(settings.db_path, bot.id, now):
        chat_id, message_id = row['chat_id'], row['message_id']
        key = (settings.db_path, bot.id, chat_id, message_id)
        if not await _manager_chat(settings, chat_id) or now - row['sent_at'] >= 48 * 3600:
            await forget_message(*key)
            continue
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramRetryAfter as exc:
            await postpone_due_messages(settings.db_path, bot.id, now, now + max(1, exc.retry_after))
            break
        except (TelegramBadRequest, TelegramForbiddenError):
            # Already removed, too old, or Telegram forbids deletion of this message.
            await forget_message(*key)
        except Exception:
            logger.warning('Will retry cleanup of message %s in manager chat %s', message_id, chat_id)
            await retry_message(*key, now + 60)
        else:
            await forget_message(*key)
            deleted += 1
    return deleted


async def message_cleanup_worker(bot, settings, interval_seconds=30):
    logger.info('Manager chat cleanup enabled: lifetime %s seconds', MESSAGE_LIFETIME_SECONDS)
    while True:
        try:
            await process_message_cleanup(bot, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Manager message cleanup failed')
        await asyncio.sleep(interval_seconds)
