from __future__ import annotations

import math
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import SuppliersCb
from config import Settings
from db import (
    count_suppliers,
    create_supplier,
    delete_supplier,
    get_supplier,
    is_admin,
    list_suppliers,
    update_supplier,
)
from keyboards.admin import admin_main_kb, admin_back_cancel_kb, admin_skip_back_cancel_kb

router = Router()
PAGE_SIZE = 8

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_phone(raw: str) -> str:
    raw = (raw or "").strip()
    plus = raw.startswith("+")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return ("+" + digits) if plus else digits


def _valid_phone(phone: str) -> bool:
    p = _normalize_phone(phone)
    digits_only = "".join(ch for ch in p if ch.isdigit())
    return 7 <= len(digits_only) <= 15


def _short(text: str | None, n: int = 280) -> str:
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return (t[: n - 1] + "…") if len(t) > n else t


class SupplierForm(StatesGroup):
    # add
    add_name = State()
    add_site = State()
    add_email = State()
    add_phone = State()
    add_desc = State()
    # edit
    edit_name = State()
    edit_site = State()
    edit_email = State()
    edit_phone = State()
    edit_desc = State()


def _list_text(items: list[dict], page: int, total_pages: int, total: int) -> str:
    if not items:
        body = "Пока пусто.\nНажмите «➕ Добавить поставщика»."
    else:
        lines = []
        start_num = page * PAGE_SIZE + 1
        for i, s in enumerate(items, start=start_num):
            site = s.get("website") or "—"
            email = s.get("email") or "—"
            phone = s.get("phone") or "—"
            lines.append(
                f"{i}. <b>{s.get('name') or '—'}</b>\n"
                f"   🌐 {site}\n"
                f"   ✉️ {email}\n"
                f"   📞 {phone}\n"
                f"   🆔 <code>{s.get('id')}</code>"
            )
        body = "\n\n".join(lines)

    return (
        "🏢 <b>Поставщики</b>\n"
        f"Всего: <b>{total}</b>\n"
        f"Страница: <b>{page + 1}/{total_pages}</b>\n\n"
        f"{body}"
    )


def _list_kb(page: int, total_pages: int, items: list[dict]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()

    for s in items:
        kb.add(
            InlineKeyboardButton(
                text=f"📌 {s.get('name') or '—'}",
                callback_data=SuppliersCb(action="view", page=page, supplier_id=int(s["id"])).pack(),
            )
        )
    kb.adjust(1)

    prev_page = page - 1 if page > 0 else 0
    next_page = page + 1 if page + 1 < total_pages else page

    kb.row(
        InlineKeyboardButton(text="⬅️", callback_data=SuppliersCb(action="page", page=prev_page, supplier_id=0).pack()),
        InlineKeyboardButton(text="➡️", callback_data=SuppliersCb(action="page", page=next_page, supplier_id=0).pack()),
    )
    kb.row(
        InlineKeyboardButton(
            text="➕ Добавить поставщика",
            callback_data=SuppliersCb(action="add", page=page, supplier_id=0).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ В меню",
            callback_data=SuppliersCb(action="menu", page=page, supplier_id=0).pack(),
        )
    )
    return kb


def _card_text(s: dict) -> str:
    site = s.get("website") or "—"
    email = s.get("email") or "—"
    phone = s.get("phone") or "—"
    desc = s.get("description") or "—"

    return (
        "🏢 <b>Поставщик</b>\n\n"
        f"🆔 ID: <code>{s.get('id')}</code>\n"
        f"📌 Название: <b>{s.get('name') or '—'}</b>\n"
        f"🌐 Сайт: {site}\n"
        f"✉️ Почта: {email}\n"
        f"📞 Телефон: {phone}\n\n"
        f"📝 Описание:\n{desc}\n\n"
        f"🕒 Обновлён: <code>{s.get('updated_at') or '—'}</code>"
    )


def _card_kb(page: int, supplier_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()

    kb.row(
        InlineKeyboardButton(
            text="✏️ Изменить название",
            callback_data=SuppliersCb(action="edit_name", page=page, supplier_id=supplier_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="✏️ Изменить сайт",
            callback_data=SuppliersCb(action="edit_site", page=page, supplier_id=supplier_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="✏️ Изменить почту",
            callback_data=SuppliersCb(action="edit_email", page=page, supplier_id=supplier_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="✏️ Изменить телефон",
            callback_data=SuppliersCb(action="edit_phone", page=page, supplier_id=supplier_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="✏️ Изменить описание",
            callback_data=SuppliersCb(action="edit_desc", page=page, supplier_id=supplier_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=SuppliersCb(action="del", page=page, supplier_id=supplier_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(text="⬅️ К списку", callback_data=SuppliersCb(action="list", page=page, supplier_id=0).pack()),
        InlineKeyboardButton(text="⬅️ В меню", callback_data=SuppliersCb(action="menu", page=page, supplier_id=0).pack()),
    )
    return kb


def _confirm_del_kb(page: int, supplier_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="✅ Удалить",
            callback_data=SuppliersCb(action="confirm", page=page, supplier_id=supplier_id).pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=SuppliersCb(action="view", page=page, supplier_id=supplier_id).pack(),
        ),
    )
    return kb


async def _render_list(target: Message | CallbackQuery, settings: Settings, page: int, edit: bool) -> None:
    total = await count_suppliers(settings.db_path)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    items = await list_suppliers(settings.db_path, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    text = _list_text(items, page, total_pages, total)
    kb = _list_kb(page, total_pages, items).as_markup()

    if isinstance(target, CallbackQuery):
        if edit:
            await target.message.edit_text(text, reply_markup=kb)
        else:
            await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def _open_card(message: Message, settings: Settings, supplier_id: int, page: int) -> None:
    s = await get_supplier(settings.db_path, supplier_id)
    if not s:
        await message.answer("⚠️ Поставщик не найден.", reply_markup=admin_main_kb())
        return
    await message.answer(_card_text(s), reply_markup=_card_kb(page, supplier_id).as_markup())


# -------------------- OPEN / LIST --------------------

@router.message(F.text == "🏢 Поставщики")
async def suppliers_open(message: Message, settings: Settings, state: FSMContext) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return
    await state.clear()
    await _render_list(message, settings, page=0, edit=False)


@router.callback_query(SuppliersCb.filter(F.action == "page"))
async def suppliers_page(call: CallbackQuery, callback_data: SuppliersCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return
    await _render_list(call, settings, page=callback_data.page, edit=True)
    await call.answer()


@router.callback_query(SuppliersCb.filter(F.action == "list"))
async def suppliers_list(call: CallbackQuery, callback_data: SuppliersCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return
    await _render_list(call, settings, page=callback_data.page, edit=False)
    await call.answer()


@router.callback_query(SuppliersCb.filter(F.action == "menu"))
async def suppliers_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())
    await call.answer()


@router.callback_query(SuppliersCb.filter(F.action == "view"))
async def supplier_view(call: CallbackQuery, callback_data: SuppliersCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    s = await get_supplier(settings.db_path, callback_data.supplier_id)
    if not s:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.message.answer(_card_text(s), reply_markup=_card_kb(callback_data.page, int(s["id"])).as_markup())
    await call.answer()


# -------------------- ADD FLOW --------------------

@router.callback_query(SuppliersCb.filter(F.action == "add"))
async def supplier_add_start(call: CallbackQuery, callback_data: SuppliersCb, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    await state.clear()
    await state.update_data(return_page=callback_data.page)
    await state.set_state(SupplierForm.add_name)

    await call.message.answer(
        "➕ <b>Создание поставщика</b>\n\nВведите <b>название</b>:",
        reply_markup=admin_back_cancel_kb(),
    )
    await call.answer()


@router.message(
    SupplierForm.add_name,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
@router.message(
    SupplierForm.add_site,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
@router.message(
    SupplierForm.add_email,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
@router.message(
    SupplierForm.add_phone,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
@router.message(
    SupplierForm.add_desc,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
@router.message(
    SupplierForm.edit_name,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
@router.message(
    SupplierForm.edit_site,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
@router.message(
    SupplierForm.edit_email,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
@router.message(
    SupplierForm.edit_phone,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
@router.message(
    SupplierForm.edit_desc,
    F.text.in_(["❌ Отмена", "⬅️ Назад"]),
)
async def supplier_back_cancel(message: Message, state: FSMContext, settings: Settings) -> None:
    txt = (message.text or "").strip()
    data = await state.get_data()

    page = int(data.get("return_page", 0))
    supplier_id = int(data.get("supplier_id", 0))
    mode = data.get("mode")

    await state.clear()
    await message.answer("Ок.", reply_markup=ReplyKeyboardRemove())

    # если это была отмена — просто в меню админа
    if txt == "❌ Отмена":
        await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())
        return

    # назад: если редактирование — вернём карточку
    if mode and supplier_id:
        await _open_card(message, settings, supplier_id=supplier_id, page=page)
        return

    # назад: иначе вернём список
    await _render_list(message, settings, page=page, edit=False)


@router.message(SupplierForm.add_name)
async def supplier_add_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Название слишком короткое. Введите ещё раз:", reply_markup=admin_back_cancel_kb())
        return

    await state.update_data(name=name)
    await state.set_state(SupplierForm.add_site)

    await message.answer(
        "🌐 Укажите сайт (можно ссылку или текст).\nЕсли не нужно — нажмите «⏭ Пропустить».",
        reply_markup=admin_skip_back_cancel_kb("⏭ Пропустить"),
    )


@router.message(SupplierForm.add_site, F.text == "⏭ Пропустить")
async def supplier_add_site_skip(message: Message, state: FSMContext) -> None:
    await state.update_data(website=None)
    await state.set_state(SupplierForm.add_email)
    await message.answer("✉️ Введите <b>почту</b> поставщика:", reply_markup=admin_back_cancel_kb())


@router.message(SupplierForm.add_site)
async def supplier_add_site(message: Message, state: FSMContext) -> None:
    website = (message.text or "").strip()
    await state.update_data(website=website or None)
    await state.set_state(SupplierForm.add_email)
    await message.answer("✉️ Введите <b>почту</b> поставщика:", reply_markup=admin_back_cancel_kb())


@router.message(SupplierForm.add_email)
async def supplier_add_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if not _EMAIL_RE.match(email):
        await message.answer("Похоже на неверную почту. Введите ещё раз:", reply_markup=admin_back_cancel_kb())
        return

    await state.update_data(email=email)
    await state.set_state(SupplierForm.add_phone)
    await message.answer("📞 Введите <b>телефон</b> поставщика:", reply_markup=admin_back_cancel_kb())


@router.message(SupplierForm.add_phone)
async def supplier_add_phone(message: Message, state: FSMContext) -> None:
    phone_raw = (message.text or "").strip()
    if not _valid_phone(phone_raw):
        await message.answer("Телефон выглядит неверно. Введите ещё раз:", reply_markup=admin_back_cancel_kb())
        return

    await state.update_data(phone=_normalize_phone(phone_raw))
    await state.set_state(SupplierForm.add_desc)
    await message.answer(
        "📝 Введите <b>описание</b> поставщика.\nЕсли не нужно — нажмите «⏭ Пропустить».",
        reply_markup=admin_skip_back_cancel_kb("⏭ Пропустить"),
    )


@router.message(SupplierForm.add_desc, F.text == "⏭ Пропустить")
async def supplier_add_desc_skip(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    page = int(data.get("return_page", 0))

    supplier_id = await create_supplier(
        settings.db_path,
        name=data["name"],
        website=data.get("website"),
        email=data.get("email"),
        phone=data.get("phone"),
        description=None,
    )
    await state.clear()

    await message.answer("✅ <b>Поставщик создан</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


@router.message(SupplierForm.add_desc)
async def supplier_add_desc(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    page = int(data.get("return_page", 0))
    desc = (message.text or "").strip()

    supplier_id = await create_supplier(
        settings.db_path,
        name=data["name"],
        website=data.get("website"),
        email=data.get("email"),
        phone=data.get("phone"),
        description=desc or None,
    )
    await state.clear()

    await message.answer("✅ <b>Поставщик создан</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


# -------------------- EDIT START (inline buttons) --------------------

@router.callback_query(SuppliersCb.filter(F.action == "edit_name"))
async def edit_name_start(call: CallbackQuery, callback_data: SuppliersCb, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    s = await get_supplier(settings.db_path, callback_data.supplier_id)
    if not s:
        await call.answer("Не найдено", show_alert=True)
        return

    await state.clear()
    await state.update_data(mode="edit_name", supplier_id=int(s["id"]), return_page=callback_data.page)
    await state.set_state(SupplierForm.edit_name)

    await call.message.answer(
        "✏️ <b>Изменить название</b>\n\n"
        f"Текущее: <b>{s.get('name') or '—'}</b>\n"
        "Введите новое название:",
        reply_markup=admin_back_cancel_kb(),
    )
    await call.answer()


@router.callback_query(SuppliersCb.filter(F.action == "edit_site"))
async def edit_site_start(call: CallbackQuery, callback_data: SuppliersCb, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    s = await get_supplier(settings.db_path, callback_data.supplier_id)
    if not s:
        await call.answer("Не найдено", show_alert=True)
        return

    await state.clear()
    await state.update_data(mode="edit_site", supplier_id=int(s["id"]), return_page=callback_data.page)
    await state.set_state(SupplierForm.edit_site)

    current = s.get("website") or "—"
    await call.message.answer(
        "✏️ <b>Изменить сайт</b>\n\n"
        f"Текущее: {current}\n"
        "Введите новое значение или нажмите «🗑 Очистить».",
        reply_markup=admin_skip_back_cancel_kb("🗑 Очистить"),
    )
    await call.answer()


@router.callback_query(SuppliersCb.filter(F.action == "edit_email"))
async def edit_email_start(call: CallbackQuery, callback_data: SuppliersCb, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    s = await get_supplier(settings.db_path, callback_data.supplier_id)
    if not s:
        await call.answer("Не найдено", show_alert=True)
        return

    await state.clear()
    await state.update_data(mode="edit_email", supplier_id=int(s["id"]), return_page=callback_data.page)
    await state.set_state(SupplierForm.edit_email)

    current = s.get("email") or "—"
    await call.message.answer(
        "✏️ <b>Изменить почту</b>\n\n"
        f"Текущая: {current}\n"
        "Введите новую почту или нажмите «🗑 Очистить».",
        reply_markup=admin_skip_back_cancel_kb("🗑 Очистить"),
    )
    await call.answer()


@router.callback_query(SuppliersCb.filter(F.action == "edit_phone"))
async def edit_phone_start(call: CallbackQuery, callback_data: SuppliersCb, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    s = await get_supplier(settings.db_path, callback_data.supplier_id)
    if not s:
        await call.answer("Не найдено", show_alert=True)
        return

    await state.clear()
    await state.update_data(mode="edit_phone", supplier_id=int(s["id"]), return_page=callback_data.page)
    await state.set_state(SupplierForm.edit_phone)

    current = s.get("phone") or "—"
    await call.message.answer(
        "✏️ <b>Изменить телефон</b>\n\n"
        f"Текущий: {current}\n"
        "Введите новый телефон (можно с +) или нажмите «🗑 Очистить».",
        reply_markup=admin_skip_back_cancel_kb("🗑 Очистить"),
    )
    await call.answer()


@router.callback_query(SuppliersCb.filter(F.action == "edit_desc"))
async def edit_desc_start(call: CallbackQuery, callback_data: SuppliersCb, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    s = await get_supplier(settings.db_path, callback_data.supplier_id)
    if not s:
        await call.answer("Не найдено", show_alert=True)
        return

    await state.clear()
    await state.update_data(mode="edit_desc", supplier_id=int(s["id"]), return_page=callback_data.page)
    await state.set_state(SupplierForm.edit_desc)

    current = _short(s.get("description"), 700) or "—"
    await call.message.answer(
        "✏️ <b>Изменить описание</b>\n\n"
        f"Текущее: {current}\n\n"
        "Введите новое описание или нажмите «🗑 Очистить».",
        reply_markup=admin_skip_back_cancel_kb("🗑 Очистить"),
    )
    await call.answer()


# -------------------- EDIT FINISH (messages) --------------------

@router.message(SupplierForm.edit_name)
async def edit_name_finish(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    supplier_id = int(data.get("supplier_id") or 0)
    page = int(data.get("return_page") or 0)

    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Название слишком короткое. Введите ещё раз:", reply_markup=admin_back_cancel_kb())
        return

    await update_supplier(settings.db_path, supplier_id=supplier_id, name=name)
    await state.clear()

    await message.answer("✅ <b>Название обновлено</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


@router.message(SupplierForm.edit_site, F.text == "🗑 Очистить")
async def edit_site_clear(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    supplier_id = int(data.get("supplier_id") or 0)
    page = int(data.get("return_page") or 0)

    await update_supplier(settings.db_path, supplier_id=supplier_id, website=None)
    await state.clear()

    await message.answer("✅ <b>Сайт очищен</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


@router.message(SupplierForm.edit_site)
async def edit_site_finish(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    supplier_id = int(data.get("supplier_id") or 0)
    page = int(data.get("return_page") or 0)

    website = (message.text or "").strip()
    await update_supplier(settings.db_path, supplier_id=supplier_id, website=website or None)
    await state.clear()

    await message.answer("✅ <b>Сайт обновлён</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


@router.message(SupplierForm.edit_email, F.text == "🗑 Очистить")
async def edit_email_clear(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    supplier_id = int(data.get("supplier_id") or 0)
    page = int(data.get("return_page") or 0)

    await update_supplier(settings.db_path, supplier_id=supplier_id, email=None)
    await state.clear()

    await message.answer("✅ <b>Почта очищена</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


@router.message(SupplierForm.edit_email)
async def edit_email_finish(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    supplier_id = int(data.get("supplier_id") or 0)
    page = int(data.get("return_page") or 0)

    email = (message.text or "").strip()
    if email and not _EMAIL_RE.match(email):
        await message.answer("Похоже на неверную почту. Введите ещё раз:", reply_markup=admin_back_cancel_kb())
        return

    await update_supplier(settings.db_path, supplier_id=supplier_id, email=email or None)
    await state.clear()

    await message.answer("✅ <b>Почта обновлена</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


@router.message(SupplierForm.edit_phone, F.text == "🗑 Очистить")
async def edit_phone_clear(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    supplier_id = int(data.get("supplier_id") or 0)
    page = int(data.get("return_page") or 0)

    await update_supplier(settings.db_path, supplier_id=supplier_id, phone=None)
    await state.clear()

    await message.answer("✅ <b>Телефон очищен</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


@router.message(SupplierForm.edit_phone)
async def edit_phone_finish(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    supplier_id = int(data.get("supplier_id") or 0)
    page = int(data.get("return_page") or 0)

    phone_raw = (message.text or "").strip()
    if phone_raw and not _valid_phone(phone_raw):
        await message.answer("Телефон выглядит неверно. Введите ещё раз:", reply_markup=admin_back_cancel_kb())
        return

    phone = _normalize_phone(phone_raw) if phone_raw else None
    await update_supplier(settings.db_path, supplier_id=supplier_id, phone=phone)
    await state.clear()

    await message.answer("✅ <b>Телефон обновлён</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


@router.message(SupplierForm.edit_desc, F.text == "🗑 Очистить")
async def edit_desc_clear(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    supplier_id = int(data.get("supplier_id") or 0)
    page = int(data.get("return_page") or 0)

    await update_supplier(settings.db_path, supplier_id=supplier_id, description=None)
    await state.clear()

    await message.answer("✅ <b>Описание очищено</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


@router.message(SupplierForm.edit_desc)
async def edit_desc_finish(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    supplier_id = int(data.get("supplier_id") or 0)
    page = int(data.get("return_page") or 0)

    desc = (message.text or "").strip()
    await update_supplier(settings.db_path, supplier_id=supplier_id, description=desc or None)
    await state.clear()

    await message.answer("✅ <b>Описание обновлено</b>", reply_markup=ReplyKeyboardRemove())
    await _open_card(message, settings, supplier_id=supplier_id, page=page)


# -------------------- DELETE --------------------

@router.callback_query(SuppliersCb.filter(F.action == "del"))
async def supplier_delete_confirm(call: CallbackQuery, callback_data: SuppliersCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    s = await get_supplier(settings.db_path, callback_data.supplier_id)
    if not s:
        await call.answer("Не найдено", show_alert=True)
        return

    await call.message.answer(
        f"🗑 Удалить поставщика <b>{s.get('name') or '—'}</b> (ID <code>{s.get('id')}</code>)?\n"
        "Это действие необратимо.",
        reply_markup=_confirm_del_kb(callback_data.page, int(s["id"])).as_markup(),
    )
    await call.answer()


@router.callback_query(SuppliersCb.filter(F.action == "confirm"))
async def supplier_delete_done(call: CallbackQuery, callback_data: SuppliersCb, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    await delete_supplier(settings.db_path, callback_data.supplier_id)

    try:
        await call.message.delete()
    except Exception:
        pass

    await call.message.answer("✅ <b>Поставщик удалён</b>")
    await _render_list(call, settings, page=callback_data.page, edit=False)
    await call.answer()
