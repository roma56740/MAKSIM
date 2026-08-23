from __future__ import annotations

import html
import math
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import is_admin
from db.promotions import (
    count_promotions,
    create_promotion,
    get_promotion,
    list_promotions,
)
from keyboards.admin import admin_back_cancel_kb, admin_main_kb
from services.promotions import (
    archive_and_remove_promotion,
    promotion_caption,
)

router = Router()
PAGE_SIZE = 6
BOT_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Moscow"))


class PromotionForm(StatesGroup):
    title = State()
    content = State()
    custom_expiry = State()


async def _admin(user_id: int, settings: Settings) -> bool:
    return await is_admin(settings.db_path, user_id, settings.admin_ids)


def _main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать", callback_data="promo:create")
    kb.button(text="✅ Активные", callback_data="promo:list:active:0")
    kb.button(text="🗄 Архив", callback_data="promo:list:archived:0")
    kb.adjust(1, 2)
    return kb.as_markup()


def _kind_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Акция", callback_data="promo:kind:promotion")
    kb.button(text="🎁 Спецпредложение", callback_data="promo:kind:special")
    kb.button(text="⬅️ Назад", callback_data="promo:menu")
    kb.adjust(2, 1)
    return kb.as_markup()


def _expiry_kb(source_id: int = 0):
    kb = InlineKeyboardBuilder()
    for label, key in (
        ("1 день", "1d"),
        ("3 дня", "3d"),
        ("7 дней", "7d"),
        ("30 дней", "30d"),
        ("♾ Бессрочно", "forever"),
        ("📅 Своя дата", "custom"),
    ):
        kb.button(text=label, callback_data=f"promo:expiry:{key}:{source_id}")
    kb.button(text="❌ Отмена", callback_data="promo:cancel")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def _list_kb(status: str, page: int, total_pages: int, items: list[dict]):
    kb = InlineKeyboardBuilder()
    for item in items:
        icon = "🔥" if item.get("kind") == "promotion" else "🎁"
        title = str(item.get("title") or "Без названия")[:38]
        kb.button(
            text=f"{icon} {title}",
            callback_data=f"promo:view:{int(item['id'])}:{status}:{page}",
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"promo:list:{status}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="promo:noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"promo:list:{status}:{page+1}"))
    kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🏠 Раздел акций", callback_data="promo:menu"))
    return kb.as_markup()


def _detail_kb(item: dict, status: str, page: int):
    kb = InlineKeyboardBuilder()
    promotion_id = int(item["id"])
    if status == "active":
        kb.button(text="🗄 Убрать у менеджеров", callback_data=f"promo:archive:{promotion_id}")
    kb.button(text="📑 Продублировать", callback_data=f"promo:duplicate:{promotion_id}")
    kb.button(text="⬅️ К списку", callback_data=f"promo:list:{status}:{page}")
    kb.adjust(1)
    return kb.as_markup()


def _expiry_iso(key: str) -> str | None:
    days = {"1d": 1, "3d": 3, "7d": 7, "30d": 30}.get(key)
    if days is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


async def _finish_creation(
    message: Message,
    state: FSMContext,
    settings: Settings,
    *,
    expires_at: str | None,
    source_id: int = 0,
) -> None:
    if source_id:
        source = await get_promotion(settings.db_path, source_id)
        if not source:
            await state.clear()
            await message.answer("⚠️ Исходное предложение не найдено.", reply_markup=admin_main_kb())
            return
        payload = {
            "kind": source.get("kind") or "promotion",
            "title": source.get("title") or "Без названия",
            "text": source.get("text"),
            "file_id": source.get("file_id"),
            "file_kind": source.get("file_kind"),
        }
    else:
        payload = await state.get_data()

    promotion_id = await create_promotion(
        settings.db_path,
        kind=str(payload.get("kind") or "promotion"),
        title=str(payload.get("title") or "Без названия"),
        text=payload.get("text"),
        file_id=payload.get("file_id"),
        file_kind=payload.get("file_kind"),
        expires_at=expires_at,
        created_by=message.from_user.id,
        duplicated_from=source_id or None,
    )
    await state.clear()

    await message.answer(
        "✅ <b>Предложение сохранено</b>\n\n"
        "Уведомления менеджерам не отправлялись. Предложение доступно только "
        "в разделе «🎁 Акции» и будет храниться там до окончания срока."
    )
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())


@router.message(F.text == "🎁 Акции и предложения")
async def promotions_root(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _admin(message.from_user.id, settings):
        return
    await state.clear()
    active = await count_promotions(settings.db_path, "active")
    archived = await count_promotions(settings.db_path, "archived")
    await message.answer(
        "🎁 <b>Акции и спецпредложения</b>\n\n"
        f"Активных: <b>{active}</b>\n"
        f"В архиве: <b>{archived}</b>\n\n"
        "Это накопительный раздел: новые материалы не отправляются менеджерам уведомлениями. "
        "Можно задать таймер или оставить предложение бессрочным.",
        reply_markup=_main_kb(),
    )


@router.callback_query(F.data == "promo:menu")
async def promotions_menu_cb(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    await state.clear()
    active = await count_promotions(settings.db_path, "active")
    archived = await count_promotions(settings.db_path, "archived")
    await call.message.answer(
        "🎁 <b>Акции и спецпредложения</b>\n\n"
        f"Активных: <b>{active}</b>\nВ архиве: <b>{archived}</b>",
        reply_markup=_main_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "promo:create")
async def promotion_create(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    await state.clear()
    await call.message.answer("Выберите тип публикации:", reply_markup=_kind_kb())
    await call.answer()


@router.callback_query(F.data.startswith("promo:kind:"))
async def promotion_kind(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    kind = call.data.rsplit(":", 1)[-1]
    await state.update_data(kind=kind)
    await state.set_state(PromotionForm.title)
    await call.message.answer(
        "✏️ Введите короткий заголовок акции или спецпредложения:",
        reply_markup=admin_back_cancel_kb(),
    )
    await call.answer()


@router.message(PromotionForm.title, F.text)
async def promotion_title(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _admin(message.from_user.id, settings):
        return
    if message.text in {"❌ Отмена", "⬅️ Назад"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb())
        return
    title = (message.text or "").strip()
    if len(title) < 3 or len(title) > 120:
        await message.answer("Заголовок должен содержать от 3 до 120 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(PromotionForm.content)
    await message.answer(
        "📝 Отправьте одним сообщением:\n"
        "• текст;\n"
        "• фото с подписью;\n"
        "• файл с подписью.\n\n"
        "Для фото или файла подпись должна быть не длиннее 700 символов.",
        reply_markup=admin_back_cancel_kb(),
    )


@router.message(PromotionForm.content, F.photo)
async def promotion_content_photo(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _admin(message.from_user.id, settings):
        return
    caption = (message.caption or "").strip()
    if len(caption) > 700:
        await message.answer("Подпись слишком длинная. Сократите её до 700 символов.")
        return
    await state.update_data(text=caption, file_id=message.photo[-1].file_id, file_kind="photo")
    await message.answer("⏳ На какой срок показать предложение?", reply_markup=_expiry_kb())


@router.message(PromotionForm.content, F.document)
async def promotion_content_document(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _admin(message.from_user.id, settings):
        return
    caption = (message.caption or "").strip()
    if len(caption) > 700:
        await message.answer("Подпись слишком длинная. Сократите её до 700 символов.")
        return
    await state.update_data(text=caption, file_id=message.document.file_id, file_kind="document")
    await message.answer("⏳ На какой срок показать предложение?", reply_markup=_expiry_kb())


@router.message(PromotionForm.content, F.text)
async def promotion_content_text(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _admin(message.from_user.id, settings):
        return
    if message.text in {"❌ Отмена", "⬅️ Назад"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb())
        return
    text = (message.text or "").strip()
    if not text or len(text) > 3000:
        await message.answer("Текст должен содержать от 1 до 3000 символов.")
        return
    await state.update_data(text=text, file_id=None, file_kind="text")
    await message.answer("⏳ На какой срок показать предложение?", reply_markup=_expiry_kb())


@router.callback_query(F.data.startswith("promo:expiry:"))
async def promotion_expiry(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    _, _, key, source_s = call.data.split(":", 3)
    source_id = int(source_s)
    if key == "custom":
        await state.update_data(duplicate_source=source_id)
        await state.set_state(PromotionForm.custom_expiry)
        await call.message.answer(
            "📅 Введите дату и время окончания по московскому времени.\n"
            "Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
            "Например: <code>20.08.2026 18:30</code>",
            reply_markup=admin_back_cancel_kb(),
        )
        await call.answer()
        return

    expires_at = None if key == "forever" else _expiry_iso(key)
    await call.answer()
    await _finish_creation(call.message, state, settings, expires_at=expires_at, source_id=source_id)


@router.message(PromotionForm.custom_expiry, F.text)
async def promotion_custom_expiry(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _admin(message.from_user.id, settings):
        return
    if message.text in {"❌ Отмена", "⬅️ Назад"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb())
        return
    try:
        local_dt = datetime.strptime((message.text or "").strip(), "%d.%m.%Y %H:%M").replace(tzinfo=BOT_TZ)
    except ValueError:
        await message.answer("Неверный формат. Пример: <code>20.08.2026 18:30</code>")
        return
    utc_dt = local_dt.astimezone(timezone.utc)
    if utc_dt <= datetime.now(timezone.utc):
        await message.answer("Дата окончания должна быть в будущем.")
        return
    data = await state.get_data()
    await _finish_creation(
        message,
        state,
        settings,
        expires_at=utc_dt.isoformat(timespec="seconds"),
        source_id=int(data.get("duplicate_source") or 0),
    )


@router.callback_query(F.data.startswith("promo:list:"))
async def promotion_list(call: CallbackQuery, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    _, _, status, page_s = call.data.split(":", 3)
    page = max(0, int(page_s))
    total = await count_promotions(settings.db_path, status)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, total_pages - 1)
    items = await list_promotions(
        settings.db_path,
        status=status,
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
    )
    title = "✅ Активные предложения" if status == "active" else "🗄 Архив предложений"
    text = f"{title}\n\n" + ("Выберите публикацию:" if items else "Пока пусто.")
    await call.message.answer(text, reply_markup=_list_kb(status, page, total_pages, items))
    await call.answer()


@router.callback_query(F.data.startswith("promo:view:"))
async def promotion_view(call: CallbackQuery, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    _, _, promotion_s, status, page_s = call.data.split(":", 4)
    item = await get_promotion(settings.db_path, int(promotion_s))
    if not item:
        await call.answer("Не найдено", show_alert=True)
        return
    caption = promotion_caption(item, include_status=True)
    markup = _detail_kb(item, status, int(page_s))
    kind = (item.get("file_kind") or "text").lower()
    if kind == "photo" and item.get("file_id"):
        await call.message.answer_photo(item["file_id"], caption=caption, reply_markup=markup)
    elif kind == "document" and item.get("file_id"):
        await call.message.answer_document(item["file_id"], caption=caption, reply_markup=markup)
    else:
        await call.message.answer(caption, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("promo:archive:"))
async def promotion_archive(call: CallbackQuery, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    promotion_id = int(call.data.rsplit(":", 1)[-1])
    await call.answer("Убираю у менеджеров…")
    await archive_and_remove_promotion(call.bot, settings.db_path, promotion_id)
    await call.message.answer(
        "✅ Предложение убрано у менеджеров и сохранено в архиве. Его можно продублировать в любое время."
    )


@router.callback_query(F.data.startswith("promo:duplicate:"))
async def promotion_duplicate(call: CallbackQuery, settings: Settings) -> None:
    if not await _admin(call.from_user.id, settings):
        return
    promotion_id = int(call.data.rsplit(":", 1)[-1])
    item = await get_promotion(settings.db_path, promotion_id)
    if not item:
        await call.answer("Не найдено", show_alert=True)
        return
    await call.message.answer(
        f"📑 <b>Дублирование:</b> {html.escape(str(item.get('title') or 'Без названия'))}\n\n"
        "Выберите новый срок публикации:",
        reply_markup=_expiry_kb(promotion_id),
    )
    await call.answer()


@router.callback_query(F.data.in_({"promo:cancel", "promo:noop"}))
async def promotion_cancel_or_noop(call: CallbackQuery, state: FSMContext) -> None:
    if call.data == "promo:cancel":
        await state.clear()
        await call.message.answer("Отменено.", reply_markup=admin_main_kb())
    await call.answer()
