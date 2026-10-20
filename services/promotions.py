from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message
from services.chat_cleanup import keep_admin_messages

from db.personal_messages import list_approved_managers
from db.promotions import (
    add_promotion_delivery,
    archive_promotion,
    get_promotion,
    list_active_deliveries,
    list_due_promotion_ids,
    mark_delivery_deleted,
)

logger = logging.getLogger(__name__)
DISPLAY_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Moscow"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def promotion_caption(promotion: dict[str, Any], *, include_status: bool = False, include_text: bool = True) -> str:
    kind = "🔥 Акция" if promotion.get("kind") == "promotion" else "🎁 Спецпредложение"
    title = html.escape(str(promotion.get("title") or "Без названия"))
    text = html.escape(str(promotion.get("text") or "").strip()) if include_text else ""

    lines = [f"{kind}", f"<b>{title}</b>"]
    if text:
        lines.extend(["", text])

    expires_at = promotion.get("expires_at")
    if expires_at:
        try:
            dt = datetime.fromisoformat(str(expires_at)).astimezone(DISPLAY_TZ)
            lines.extend(["", f"⏳ Действует до: <b>{dt.strftime('%d.%m.%Y %H:%M')}</b>"])
        except Exception:
            pass
    else:
        lines.extend(["", "♾ Предложение бессрочное"])

    if include_status:
        status = "Активно" if promotion.get("status") == "active" else "В архиве"
        lines.extend(["", f"Статус: <b>{status}</b>"])

    return "\n".join(lines)


def _visible_length(markup: str) -> int:
    plain = html.unescape(markup.replace("<b>", "").replace("</b>", ""))
    return sum(2 if ord(char) > 0xFFFF else 1 for char in plain)


def split_promotion_text(text: str, limit: int = 3500) -> list[str]:
    """Split before escaping HTML, preserving all characters and emoji boundaries."""
    chunks = []
    current = []
    units = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            chunks.append("".join(current))
            current = []
            units = 0
        current.append(char)
        units += width
    if current:
        chunks.append("".join(current))
    return chunks


async def send_promotion(
    bot: Bot,
    chat_id: int,
    promotion: dict[str, Any],
    *,
    include_status: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> list[Message]:
    caption = promotion_caption(promotion, include_status=include_status)
    kind = (promotion.get("file_kind") or "text").lower()
    file_id = promotion.get("file_id")
    has_media = kind in {"photo", "document"} and bool(file_id)
    limit = 1024 if has_media else 4096
    long_text = _visible_length(caption) > limit
    if long_text:
        caption = promotion_caption(promotion, include_status=include_status, include_text=False)
    markup = None if long_text else reply_markup
    sent = []
    if kind == "photo" and file_id:
        sent.append(await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption, parse_mode="HTML", reply_markup=markup))
    elif kind == "document" and file_id:
        sent.append(await bot.send_document(chat_id=chat_id, document=file_id, caption=caption, parse_mode="HTML", reply_markup=markup))
    else:
        sent.append(await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", reply_markup=markup))
    if long_text:
        chunks = split_promotion_text(str(promotion.get("text") or "").strip())
        for index, chunk in enumerate(chunks):
            sent.append(await bot.send_message(
                chat_id=chat_id,
                text=html.escape(chunk),
                parse_mode="HTML",
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            ))
    return sent


@keep_admin_messages
async def publish_promotion(
    bot: Bot,
    db_path: str,
    promotion_id: int,
    *,
    excluded_ids: set[int] | None = None,
) -> tuple[int, int]:
    promotion = await get_promotion(db_path, promotion_id)
    if not promotion or promotion.get("status") != "active":
        return 0, 0

    excluded_ids = excluded_ids or set()
    offset = 0
    ok = 0
    fail = 0

    while True:
        managers = await list_approved_managers(db_path, limit=100, offset=offset)
        if not managers:
            break
        for manager in managers:
            tg_id = int(manager["tg_id"])
            if tg_id in excluded_ids:
                continue
            try:
                sent = await send_promotion(bot, tg_id, promotion)
                for message in sent:
                    await add_promotion_delivery(db_path, promotion_id, tg_id, message.message_id)
                ok += 1
            except Exception as exc:
                logger.warning("Promotion %s delivery to %s failed: %s", promotion_id, tg_id, exc)
                fail += 1
            await asyncio.sleep(0.05)
        offset += len(managers)

    return ok, fail


async def archive_and_remove_promotion(bot: Bot, db_path: str, promotion_id: int) -> bool:
    promotion = await get_promotion(db_path, promotion_id)
    if not promotion:
        return False

    changed = await archive_promotion(db_path, promotion_id)
    deliveries = await list_active_deliveries(db_path, promotion_id)

    for delivery in deliveries:
        tg_id = int(delivery["tg_id"])
        message_id = int(delivery["message_id"])
        removed = False
        try:
            await bot.delete_message(chat_id=tg_id, message_id=message_id)
            removed = True
        except Exception:
            # Telegram не удаляет сообщения старше 48 часов. В таком случае
            # убираем текст предложения, чтобы оно больше не выглядело активным.
            try:
                replacement = "⛔ <b>Предложение завершено</b>"
                try:
                    await bot.edit_message_text(
                        chat_id=tg_id,
                        message_id=message_id,
                        text=replacement,
                    )
                except Exception:
                    await bot.edit_message_caption(
                        chat_id=tg_id,
                        message_id=message_id,
                        caption=replacement,
                    )
                removed = True
            except Exception as exc:
                logger.warning(
                    "Promotion %s message %s in chat %s could not be removed or cleared: %s",
                    promotion_id,
                    message_id,
                    tg_id,
                    exc,
                )

        if removed:
            await mark_delivery_deleted(db_path, promotion_id, tg_id, message_id)
        await asyncio.sleep(0.02)

    return changed or promotion.get("status") == "archived"


async def process_expired_promotions(bot: Bot, db_path: str) -> int:
    due_ids = await list_due_promotion_ids(db_path, _now_iso())
    for promotion_id in due_ids:
        try:
            await archive_and_remove_promotion(bot, db_path, promotion_id)
        except Exception:
            logger.exception("Failed to archive expired promotion %s", promotion_id)
    return len(due_ids)


async def promotion_expiry_worker(bot: Bot, db_path: str, interval_seconds: int = 60) -> None:
    while True:
        try:
            await process_expired_promotions(bot, db_path)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Promotion expiration worker failed")
        await asyncio.sleep(interval_seconds)
