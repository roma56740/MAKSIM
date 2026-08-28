from __future__ import annotations

import math
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db.promotions import get_promotion, list_active_promotions
from filters.admin import NotAdmin
from services.promotions import promotion_caption

router = Router()
router.message.filter(NotAdmin())
router.callback_query.filter(NotAdmin())
PAGE_SIZE = 8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_expired(value: object) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)
    except ValueError:
        # Старая некорректная дата не должна удалять предложение из раздела.
        return False


def _list_kb(items: list[dict], page: int, total_pages: int):
    kb = InlineKeyboardBuilder()
    for item in items:
        icon = "🔥" if item.get("kind") == "promotion" else "🎁"
        title = str(item.get("title") or "Без названия")[:40]
        kb.button(text=f"{icon} {title}", callback_data=f"upromo:view:{int(item['id'])}")
    if total_pages > 1:
        if page > 0:
            kb.button(text="⬅️", callback_data=f"upromo:list:{page-1}")
        kb.button(text=f"{page+1}/{total_pages}", callback_data="upromo:noop")
        if page + 1 < total_pages:
            kb.button(text="➡️", callback_data=f"upromo:list:{page+1}")
    kb.adjust(1)
    return kb.as_markup()


async def _show_list(message: Message, settings: Settings, page: int = 0) -> None:
    all_items = await list_active_promotions(settings.db_path, _now_iso())
    total_pages = max(1, math.ceil(len(all_items) / PAGE_SIZE))
    page = min(max(0, page), total_pages - 1)
    items = all_items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    if not items:
        await message.answer(
            "🎁 <b>Акции и спецпредложения</b>\n\n"
            "Сейчас активных предложений нет. Новые предложения появятся здесь автоматически."
        )
        return

    await message.answer(
        "🎁 <b>Акции и спецпредложения</b>\n\n"
        "Выберите предложение, чтобы открыть подробности:",
        reply_markup=_list_kb(items, page, total_pages),
    )


@router.message(F.text == "🎁 Акции")
async def user_promotions(message: Message, settings: Settings) -> None:
    await _show_list(message, settings)


@router.callback_query(F.data.startswith("upromo:list:"))
async def user_promotions_page(call: CallbackQuery, settings: Settings) -> None:
    page = int(call.data.rsplit(":", 1)[-1])
    await _show_list(call.message, settings, page)
    await call.answer()


@router.callback_query(F.data.startswith("upromo:view:"))
async def user_promotion_view(call: CallbackQuery, settings: Settings) -> None:
    promotion_id = int(call.data.rsplit(":", 1)[-1])
    item = await get_promotion(settings.db_path, promotion_id)
    if (
        not item
        or item.get("status") != "active"
        or _is_expired(item.get("expires_at"))
    ):
        await call.answer("Предложение уже завершено", show_alert=True)
        return

    caption = promotion_caption(item)
    kind = (item.get("file_kind") or "text").lower()
    if kind == "photo" and item.get("file_id"):
        await call.message.answer_photo(item["file_id"], caption=caption)
    elif kind == "document" and item.get("file_id"):
        await call.message.answer_document(item["file_id"], caption=caption)
    else:
        await call.message.answer(caption)
    await call.answer()


@router.callback_query(F.data == "upromo:noop")
async def user_promotion_noop(call: CallbackQuery) -> None:
    await call.answer()
