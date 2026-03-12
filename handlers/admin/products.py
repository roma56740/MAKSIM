from __future__ import annotations

import os
import math
import html
from typing import Any, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import PricesCb, ProductsCb
from config import Settings
from db import is_admin, get_supplier
from db.catalog import (
    count_products,
    list_products,
    get_product,
    update_product_field,
    delete_product,
)
from keyboards.admin import admin_back_cancel_kb

from services.product_enrich import enrich_from_url

# optional: default image (если есть в проекте)
try:
    from services.image_store import default_image_path
except Exception:  # pragma: no cover
    default_image_path = None


router = Router()
PAGE_SIZE = 8


class ProductEdit(StatesGroup):
    waiting_value = State()


def _money(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(v)


def _is_empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() in ("", "—"):
        return True
    return False


def _prod_list_text(supplier_name: str, items: list[dict], page: int, total_pages: int, total: int) -> str:
    if not items:
        body = "Пока товаров нет. Загрузите Excel."
    else:
        lines = []
        n0 = page * PAGE_SIZE + 1
        for i, p in enumerate(items, start=n0):
            lines.append(
                f"{i}. <b>{p.get('code','—')}</b>  •  {_money(p.get('final_price') or p.get('price'))} ₽  •  ID <code>{p['id']}</code>"
            )
        body = "\n".join(lines)

    return (
        "👀 <b>Товары поставщика</b>\n"
        f"🏢 <b>{html.escape(supplier_name)}</b>\n"
        f"Всего: <b>{total}</b>\n"
        f"Страница: <b>{page + 1}/{total_pages}</b>\n\n"
        f"{body}\n\n"
        "Выберите товар ниже."
    )


def _prod_list_kb(page: int, total_pages: int, supplier_id: int, items: list[dict]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()

    for p in items:
        kb.add(
            InlineKeyboardButton(
                text=f"📦 {p.get('code','—')}",
                callback_data=ProductsCb(
                    action="view",
                    page=page,
                    supplier_id=supplier_id,
                    product_id=int(p["id"]),
                    field="",
                ).pack(),
            )
        )
    kb.adjust(1)

    prev_page = page - 1 if page > 0 else 0
    next_page = page + 1 if page + 1 < total_pages else page

    kb.row(
        InlineKeyboardButton(
            text="⬅️",
            callback_data=ProductsCb(action="page", page=prev_page, supplier_id=supplier_id, product_id=0, field="").pack(),
        ),
        InlineKeyboardButton(
            text="➡️",
            callback_data=ProductsCb(action="page", page=next_page, supplier_id=supplier_id, product_id=0, field="").pack(),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ К поставщику",
            callback_data=PricesCb(action="view", page=0, supplier_id=supplier_id, mode="").pack(),
        )
    )
    return kb


def _prod_card_text(p: dict) -> str:
    return (
        "📦 <b>Товар</b>\n\n"
        f"🆔 ID: <code>{p['id']}</code>\n"
        f"🏷 Код: <b>{html.escape(str(p.get('code') or '—'))}</b>\n"
        f"🔑 PK (из Excel): <code>{html.escape(str(p.get('source_pk') or '—'))}</code>\n\n"
        f"💰 Цена: <b>{_money(p.get('price'))} ₽</b>\n"
        f"🏷 Скидка: <b>{p.get('discount_percent') if p.get('discount_percent') is not None else '—'}</b>%\n"
        f"✅ Финальная: <b>{_money(p.get('final_price'))} ₽</b>\n\n"
        f"📦 Тип: <b>{html.escape(str(p.get('product_type') or p.get('strength') or '—'))}</b>\n"
        f"📦 Остаток: <b>{p.get('stock_qty') if p.get('stock_qty') is not None else '—'}</b>\n"
        f"🔗 Ссылка: {html.escape(str(p.get('url') or '—'))}\n"
        f"🖼 image_url: {html.escape(str(p.get('image_url') or '—'))}\n\n"
        f"📝 Описание:\n{html.escape((p.get('description') or '—')[:1500])}"
    )


def _prod_card_kb(p: dict, page: int, supplier_id: int) -> InlineKeyboardBuilder:
    pid = int(p["id"])
    kb = InlineKeyboardBuilder()

    # ✅ НОВОЕ: обогащение по ссылке (заполняет только пустые поля)
    kb.row(
        InlineKeyboardButton(
            text="🪄 Обновить по ссылке",
            callback_data=ProductsCb(action="enrich", page=page, supplier_id=supplier_id, product_id=pid, field="").pack(),
        )
    )

    kb.row(
        InlineKeyboardButton(
            text="✏️ Описание",
            callback_data=ProductsCb(action="edit", page=page, supplier_id=supplier_id, product_id=pid, field="description").pack(),
        ),
        InlineKeyboardButton(
            text="✏️ Тип",
            callback_data=ProductsCb(action="edit", page=page, supplier_id=supplier_id, product_id=pid, field="product_type").pack(),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="✏️ Цена",
            callback_data=ProductsCb(action="edit", page=page, supplier_id=supplier_id, product_id=pid, field="price").pack(),
        ),
        InlineKeyboardButton(
            text="✏️ Скидка%",
            callback_data=ProductsCb(action="edit", page=page, supplier_id=supplier_id, product_id=pid, field="discount_percent").pack(),
        ),
        InlineKeyboardButton(
            text="✏️ Финальная",
            callback_data=ProductsCb(action="edit", page=page, supplier_id=supplier_id, product_id=pid, field="final_price").pack(),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="✏️ Остаток",
            callback_data=ProductsCb(action="edit", page=page, supplier_id=supplier_id, product_id=pid, field="stock_qty").pack(),
        ),
        InlineKeyboardButton(
            text="✏️ Ссылка",
            callback_data=ProductsCb(action="edit", page=page, supplier_id=supplier_id, product_id=pid, field="url").pack(),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=ProductsCb(action="del", page=page, supplier_id=supplier_id, product_id=pid, field="").pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=ProductsCb(action="list", page=page, supplier_id=supplier_id, product_id=0, field="").pack(),
        )
    )
    return kb


def _confirm_del_kb(page: int, supplier_id: int, product_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="✅ Удалить",
            callback_data=ProductsCb(action="confirm", page=page, supplier_id=supplier_id, product_id=product_id, field="").pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=ProductsCb(action="view", page=page, supplier_id=supplier_id, product_id=product_id, field="").pack(),
        ),
    )
    return kb


async def _send_product_photo(message: Message, p: dict) -> None:
    """
    Отправляет фото товара (локальный image_path -> image_url -> default).
    Ошибки глотаем (чтобы карточка всё равно показывалась).
    """
    try:
        img_path = p.get("image_path")
        if img_path and os.path.exists(img_path):
            await message.answer_photo(FSInputFile(img_path))
            return
    except Exception:
        pass

    try:
        img_url = p.get("image_url")
        if img_url:
            await message.answer_photo(str(img_url))
            return
    except Exception:
        pass

    if default_image_path:
        try:
            dp = default_image_path()
            if dp and os.path.exists(dp):
                await message.answer_photo(FSInputFile(dp))
        except Exception:
            pass


async def _render_products(call_or_msg: Message | CallbackQuery, settings: Settings, supplier_id: int, page: int, edit: bool) -> None:
    s = await get_supplier(settings.db_path, supplier_id)
    if not s:
        if isinstance(call_or_msg, CallbackQuery):
            await call_or_msg.answer("Поставщик не найден", show_alert=True)
        return

    total = await count_products(settings.db_path, supplier_id)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    items = await list_products(settings.db_path, supplier_id=supplier_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    text = _prod_list_text(s["name"], items, page, total_pages, total)
    kb = _prod_list_kb(page, total_pages, supplier_id, items).as_markup()

    if isinstance(call_or_msg, CallbackQuery):
        if call_or_msg.message:
            if edit:
                await call_or_msg.message.edit_text(text, reply_markup=kb)
            else:
                await call_or_msg.message.answer(text, reply_markup=kb)
    else:
        await call_or_msg.answer(text, reply_markup=kb)


@router.callback_query(PricesCb.filter(F.action == "list"))
async def products_open_from_supplier(call: CallbackQuery, callback_data: PricesCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return
    await _render_products(call, settings, supplier_id=callback_data.supplier_id, page=0, edit=False)
    await call.answer()


@router.callback_query(ProductsCb.filter(F.action == "page"))
async def products_page(call: CallbackQuery, callback_data: ProductsCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return
    await _render_products(call, settings, supplier_id=callback_data.supplier_id, page=callback_data.page, edit=True)
    await call.answer()


@router.callback_query(ProductsCb.filter(F.action == "list"))
async def products_list(call: CallbackQuery, callback_data: ProductsCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return
    await _render_products(call, settings, supplier_id=callback_data.supplier_id, page=callback_data.page, edit=False)
    await call.answer()


@router.callback_query(ProductsCb.filter(F.action == "view"))
async def product_view(call: CallbackQuery, callback_data: ProductsCb, settings: Settings, state: FSMContext) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    await state.clear()
    p = await get_product(settings.db_path, callback_data.product_id)
    if not p:
        await call.answer("Товар не найден", show_alert=True)
        return

    if call.message:
        await _send_product_photo(call.message, p)
        await call.message.answer(
            _prod_card_text(p),
            reply_markup=_prod_card_kb(p, callback_data.page, callback_data.supplier_id).as_markup(),
        )
    await call.answer()


@router.callback_query(ProductsCb.filter(F.action == "enrich"))
async def product_enrich_from_url(call: CallbackQuery, callback_data: ProductsCb, settings: Settings, state: FSMContext) -> None:
    """
    ✅ Кнопка админа: подтянуть данные по url и заполнить ТОЛЬКО пустые поля.
    """
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    await state.clear()
    p = await get_product(settings.db_path, callback_data.product_id)
    if not p:
        await call.answer("Товар не найден", show_alert=True)
        return

    url = (p.get("url") or "").strip()
    if not url:
        await call.answer("Сначала добавьте ссылку (поле url).", show_alert=True)
        return

    # небольшое уведомление
    try:
        await call.answer("🪄 Обновляю по ссылке…")
    except Exception:
        pass

    try:
        info = await enrich_from_url(url)
    except Exception:
        await call.answer("Не получилось прочитать страницу.", show_alert=True)
        return

    # маппинг того, что можем заполнить
    image_url = (info.get("image_url") or info.get("image") or "").strip() if isinstance(info.get("image_url") or info.get("image"), str) else ""

    candidates: dict[str, Any] = {
        "title": (info.get("title") or "").strip() if isinstance(info.get("title"), str) else None,
        "description": (info.get("description") or "").strip() if isinstance(info.get("description"), str) else None,
        "product_type": (info.get("product_type") or "").strip() if isinstance(info.get("product_type"), str) else None,
        "strength": (info.get("strength") or "").strip() if isinstance(info.get("strength"), str) else None,
        "volume": info.get("volume"),
        "image_url": image_url or None,
    }

    # заполняем только если пусто в БД и есть значение из парсинга
    updates: list[tuple[str, Any]] = []

    def want(field: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        if _is_empty(p.get(field)) and not _is_empty(value):
            updates.append((field, value))

    want("title", candidates["title"])
    want("description", candidates["description"])

    # strength / product_type: часто одно заменяет другое — заполним аккуратно
    if _is_empty(p.get("strength")) and not _is_empty(candidates["strength"]):
        updates.append(("strength", candidates["strength"]))
    if _is_empty(p.get("product_type")) and not _is_empty(candidates["product_type"]):
        updates.append(("product_type", candidates["product_type"]))

    # volume
    if _is_empty(p.get("volume")) and candidates["volume"] not in (None, ""):
        updates.append(("volume", candidates["volume"]))

    # image_url
    want("image_url", candidates["image_url"])

    if not updates:
        await call.answer("Нечего обновлять — все поля уже заполнены.", show_alert=True)
        # покажем карточку снова
        p2 = await get_product(settings.db_path, callback_data.product_id)
        if p2 and call.message:
            await _send_product_photo(call.message, p2)
            await call.message.answer(
                _prod_card_text(p2),
                reply_markup=_prod_card_kb(p2, callback_data.page, callback_data.supplier_id).as_markup(),
            )
        return

    # применяем
    for field, value in updates:
        try:
            await update_product_field(settings.db_path, callback_data.product_id, field, value)
        except Exception:
            # не валим всё из-за одного поля
            continue

    p2 = await get_product(settings.db_path, callback_data.product_id)
    if call.message and p2:
        await call.message.answer(f"✅ Обновлено: заполнено <b>{len(updates)}</b> полей.")
        await _send_product_photo(call.message, p2)
        await call.message.answer(
            _prod_card_text(p2),
            reply_markup=_prod_card_kb(p2, callback_data.page, callback_data.supplier_id).as_markup(),
        )


@router.callback_query(ProductsCb.filter(F.action == "del"))
async def product_delete_ask(call: CallbackQuery, callback_data: ProductsCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return
    p = await get_product(settings.db_path, callback_data.product_id)
    if not p:
        await call.answer("Товар не найден", show_alert=True)
        return

    if call.message:
        await call.message.answer(
            f"🗑 Удалить товар <b>{html.escape(str(p.get('code','—')))}</b> (ID <code>{p['id']}</code>)?",
            reply_markup=_confirm_del_kb(callback_data.page, callback_data.supplier_id, int(p["id"])).as_markup(),
        )
    await call.answer()


@router.callback_query(ProductsCb.filter(F.action == "confirm"))
async def product_delete_confirm(call: CallbackQuery, callback_data: ProductsCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    await delete_product(settings.db_path, callback_data.product_id)

    try:
        if call.message:
            await call.message.delete()
    except Exception:
        pass

    if call.message:
        await call.message.answer("✅ <b>Удалено</b>")
    await _render_products(call, settings, supplier_id=callback_data.supplier_id, page=callback_data.page, edit=False)
    await call.answer()


@router.callback_query(ProductsCb.filter(F.action == "edit"))
async def product_edit_start(call: CallbackQuery, callback_data: ProductsCb, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    p = await get_product(settings.db_path, callback_data.product_id)
    if not p:
        await call.answer("Товар не найден", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        product_id=callback_data.product_id,
        supplier_id=callback_data.supplier_id,
        page=callback_data.page,
        field=callback_data.field,
    )
    await state.set_state(ProductEdit.waiting_value)

    cur = p.get(callback_data.field)
    if call.message:
        await call.message.answer(
            "✏️ <b>Редактирование</b>\n\n"
            f"Поле: <b>{html.escape(str(callback_data.field))}</b>\n"
            f"Текущее: <code>{html.escape(str(cur if cur is not None else '—'))}</code>\n\n"
            "Введите новое значение:",
            reply_markup=admin_back_cancel_kb(),
        )
    await call.answer()


@router.message(ProductEdit.waiting_value, F.text.in_(["⬅️ Назад", "❌ Отмена"]))
async def product_edit_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок.")


@router.message(ProductEdit.waiting_value)
async def product_edit_finish(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    product_id = int(data["product_id"])
    supplier_id = int(data["supplier_id"])
    page = int(data["page"])
    field = str(data["field"])

    raw = (message.text or "").strip()

    # простая типизация
    if field in {"price", "discount_percent", "final_price"}:
        raw2 = raw.replace("₽", "").replace(" ", "").replace(",", ".")
        value = float(raw2) if raw2 else None
    elif field in {"stock_qty"}:
        value = int(raw) if raw else None
    else:
        value = raw or None

    await update_product_field(settings.db_path, product_id, field, value)
    await state.clear()

    p = await get_product(settings.db_path, product_id)
    if p:
        await message.answer("✅ <b>Обновлено</b>")
        # покажем фото + карточку
        await _send_product_photo(message, p)
        await message.answer(
            _prod_card_text(p),
            reply_markup=_prod_card_kb(p, page, supplier_id).as_markup(),
        )
