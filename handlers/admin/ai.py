from __future__ import annotations

import math
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import is_admin
from db.ai_instructions import (
    AI_KIND_DIALOG,
    AI_KIND_SEARCH,
    count_ai_instructions,
    create_ai_instruction,
    delete_ai_instruction,
    get_ai_instruction,   # ✅ нужно для просмотра полного текста
    list_ai_instructions,
    update_ai_instruction,
)
from keyboards.admin import admin_ai_kb, admin_ai_cancel_kb, admin_main_kb


router = Router()
PAGE_SIZE = 8


# --------------------- helpers ---------------------

def _kind_title(kind: str) -> str:
    return "🔎 Для поиска" if kind == AI_KIND_SEARCH else "💬 Для диалога"


def _safe_kind(kind: str) -> str:
    return kind if kind in (AI_KIND_SEARCH, AI_KIND_DIALOG) else AI_KIND_SEARCH


def _excerpt(s: str, n: int = 40) -> str:
    s = (s or "").strip().replace("\n", " ")
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _button_text_for_item(text: str) -> str:
    # Короткий текст на кнопке, чтобы красиво помещалось
    t = _excerpt(text, 42)
    # Уберём совсем "пустые" штуки
    return t if t else "Без текста"


async def _edit_or_send(cbq: CallbackQuery, text: str, reply_markup):
    """
    edit_text принимает только InlineKeyboardMarkup.
    Если сюда прилетит ReplyKeyboardMarkup — убираем, чтобы не падало.
    """
    if cbq.message is None:
        await cbq.answer()
        return

    if reply_markup is not None and not isinstance(reply_markup, InlineKeyboardMarkup):
        # если по ошибке передали ReplyKeyboardMarkup — не ломаемся
        reply_markup = None

    try:
        await cbq.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await cbq.message.answer(text, reply_markup=reply_markup)


def _inline_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📚 Посмотреть", callback_data="ai|cats"),
        InlineKeyboardButton(text="➕ Для поиска", callback_data=f"ai|add|{AI_KIND_SEARCH}"),
        width=2,
    )
    kb.row(
        InlineKeyboardButton(text="➕ Для диалога", callback_data=f"ai|add|{AI_KIND_DIALOG}"),
        InlineKeyboardButton(text="🏠 Админ-меню", callback_data="ai|back_admin"),
        width=2,
    )
    return kb.as_markup()


def _cats_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🔎 Поиск", callback_data=f"ai|list|{AI_KIND_SEARCH}|0"),
        InlineKeyboardButton(text="💬 Диалог", callback_data=f"ai|list|{AI_KIND_DIALOG}|0"),
        width=2,
    )
    kb.row(
        InlineKeyboardButton(text="⬅️ В меню ИИ", callback_data="ai|menu"),
        width=1,
    )
    return kb.as_markup()


def _pairs_rows(kb: InlineKeyboardBuilder, buttons: list[InlineKeyboardButton], per_row: int = 2) -> None:
    row: list[InlineKeyboardButton] = []
    for b in buttons:
        row.append(b)
        if len(row) == per_row:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)


async def _render_pick_list(*, settings: Settings, kind: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    """
    Экран списка:
    - текст: только заголовок/страница/всего
    - клавиатура: кнопки с кратким текстом, нажатие => view
    """
    kind = _safe_kind(kind)
    total = await count_ai_instructions(db_path=settings.db_path, kind=kind)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    offset = page * PAGE_SIZE

    items = await list_ai_instructions(
        db_path=settings.db_path,
        kind=kind,
        limit=PAGE_SIZE,
        offset=offset,
    )

    text = (
        f"<b>📚 {_kind_title(kind)}</b>\n"
        f"Выбери инструкцию ниже 👇\n"
        f"Страница <b>{page+1}/{total_pages}</b> • Всего: <b>{total}</b>"
    )

    kb = InlineKeyboardBuilder()

    if items:
        btns: list[InlineKeyboardButton] = []
        for it in items:
            instr_id = int(it["id"])
            btns.append(
                InlineKeyboardButton(
                    text=_button_text_for_item(it.get("text") or ""),
                    callback_data=f"ai|view|{kind}|{instr_id}|{page}",
                )
            )
        _pairs_rows(kb, btns, per_row=1)  # 1 в строке (красивее читается)
    else:
        kb.row(InlineKeyboardButton(text="➕ Добавить первую", callback_data=f"ai|add|{kind}"))

    # Навигация
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="« Назад", callback_data=f"ai|list|{kind}|{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд »", callback_data=f"ai|list|{kind}|{page+1}"))
    if nav:
        kb.row(*nav)

    # Нижние кнопки
    kb.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data=f"ai|add|{kind}"),
        InlineKeyboardButton(text="📂 Категории", callback_data="ai|cats"),
        width=2,
    )
    kb.row(
        InlineKeyboardButton(text="⬅️ В меню ИИ", callback_data="ai|menu"),
        width=1,
    )

    return text, kb.as_markup()


async def _render_view(*, settings: Settings, kind: str, instr_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    kind = _safe_kind(kind)
    it = await get_ai_instruction(db_path=settings.db_path, instr_id=instr_id)
    if not it:
        text = "⚠️ Инструкция не найдена (возможно, уже удалена)."
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"ai|list|{kind}|{page}"))
        kb.row(InlineKeyboardButton(text="📂 Категории", callback_data="ai|cats"))
        return text, kb.as_markup()

    full_text = html.escape((it.get("text") or "").strip())
    text = (
        f"<b>{_kind_title(kind)}</b>\n"
        f"<code>#{instr_id}</code>\n\n"
        f"{full_text}"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"ai|edit|{kind}|{instr_id}|{page}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ai|del|{kind}|{instr_id}|{page}"),
        width=2,
    )
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"ai|list|{kind}|{page}"),
        width=1,
    )
    kb.row(
        InlineKeyboardButton(text="📂 Категории", callback_data="ai|cats"),
        InlineKeyboardButton(text="⬅️ В меню ИИ", callback_data="ai|menu"),
        width=2,
    )
    return text, kb.as_markup()


# --------------------- states ---------------------

class AiAdd(StatesGroup):
    waiting_text = State()


class AiEdit(StatesGroup):
    waiting_text = State()


# --------------------- entry points (reply keyboard) ---------------------

@router.message(F.text == "🤖 ИИ")
async def ai_entry(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    await message.answer(
        "🤖 <b>ИИ-инструкции</b>\n\n"
        "• <b>Поиск</b> — правила/ограничения для «поисковика»\n"
        "• <b>Диалог</b> — системные правила для чата\n\n"
        "Выбери действие 👇",
        reply_markup=admin_ai_kb(),  # ReplyKeyboard OK
    )
    # + дублируем inline-меню (удобно)
    await message.answer("Быстрые действия:", reply_markup=_inline_menu_kb())


@router.message(F.text == "📚 Посмотреть инструкции")
async def ai_view_categories(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    await message.answer(
        "<b>📂 Категории инструкций</b>\n\nВыбери категорию:",
        reply_markup=_cats_kb(),
    )


@router.message(F.text == "➕ Инструкция для поиска")
async def ai_add_search(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user is None:
        return
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    await state.set_state(AiAdd.waiting_text)
    await state.update_data(kind=AI_KIND_SEARCH)
    await message.answer(
        "➕ <b>Новая инструкция (Поиск)</b>\n\n"
        "Отправь текст одним сообщением.",
        reply_markup=admin_ai_cancel_kb(),  # ReplyKeyboard OK
    )


@router.message(F.text == "➕ Инструкция для диалога")
async def ai_add_dialog(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user is None:
        return
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    await state.set_state(AiAdd.waiting_text)
    await state.update_data(kind=AI_KIND_DIALOG)
    await message.answer(
        "➕ <b>Новая инструкция (Диалог)</b>\n\n"
        "Отправь текст одним сообщением.",
        reply_markup=admin_ai_cancel_kb(),  # ReplyKeyboard OK
    )


@router.message(F.text == "⬅️ В админ-меню")
async def ai_back_to_admin(message: Message, settings: Settings, state: FSMContext) -> None:
    if message.from_user is None:
        return
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    await state.clear()
    await message.answer("Админ-меню:", reply_markup=admin_main_kb())


# --------------------- add flow ---------------------

@router.message(AiAdd.waiting_text, F.text == "❌ Отмена")
async def ai_add_cancel(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user is None:
        return
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    await state.clear()
    await message.answer("Ок, отменил.", reply_markup=admin_ai_kb())
    await message.answer("Быстрые действия:", reply_markup=_inline_menu_kb())


@router.message(AiAdd.waiting_text)
async def ai_add_save(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user is None:
        return
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    data = await state.get_data()
    kind = _safe_kind(data.get("kind") or AI_KIND_SEARCH)
    text_in = (message.text or "").strip()

    if not text_in:
        await message.answer("Текст пустой. Отправь инструкцию одним сообщением.")
        return

    try:
        new_id = await create_ai_instruction(
            db_path=settings.db_path,
            kind=kind,
            text=text_in,
            created_by=message.from_user.id,
        )
    except Exception as e:
        await message.answer(f"Не смог сохранить. Ошибка: <code>{html.escape(str(e))}</code>")
        return
    finally:
        await state.clear()

    view_text, view_kb = await _render_view(settings=settings, kind=kind, instr_id=int(new_id), page=0)
    await message.answer("✅ Сохранено.")
    await message.answer(view_text, reply_markup=view_kb)


# --------------------- callbacks (INLINE ONLY) ---------------------

@router.callback_query(F.data.startswith("ai|"))
async def ai_callbacks(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if cbq.from_user is None:
        await cbq.answer()
        return

    if not await is_admin(settings.db_path, cbq.from_user.id, settings.admin_ids):
        await cbq.answer("Нет доступа", show_alert=True)
        return

    parts = (cbq.data or "").split("|")
    if len(parts) < 2:
        await cbq.answer()
        return

    action = parts[1]

    # меню ИИ (inline)
    if action == "menu":
        await cbq.answer()
        await _edit_or_send(
            cbq,
            "🤖 <b>ИИ-инструкции</b>\n\nВыбери действие:",
            _inline_menu_kb(),
        )
        return

    # вернуть в админ-меню (reply)
    if action == "back_admin":
        await cbq.answer()
        if cbq.message:
            await cbq.message.answer("Админ-меню:", reply_markup=admin_main_kb())
        return

    # категории
    if action == "cats":
        await cbq.answer()
        await _edit_or_send(
            cbq,
            "<b>📂 Категории инструкций</b>\n\nВыбери категорию:",
            _cats_kb(),
        )
        return

    # начать добавление (переводим в state, просим текст новым сообщением)
    if action == "add" and len(parts) >= 3:
        kind = _safe_kind(parts[2])
        await cbq.answer()
        await state.set_state(AiAdd.waiting_text)
        await state.update_data(kind=kind)

        if cbq.message:
            await cbq.message.answer(
                f"➕ <b>Новая инструкция ({_kind_title(kind)})</b>\n\n"
                "Отправь текст одним сообщением.",
                reply_markup=admin_ai_cancel_kb(),
            )
        return

    # список по категории
    if action == "list" and len(parts) >= 4:
        kind = _safe_kind(parts[2])
        page = int(parts[3])
        await cbq.answer()
        text, kb = await _render_pick_list(settings=settings, kind=kind, page=page)
        await _edit_or_send(cbq, text, kb)
        return

    # просмотр полного текста
    if action == "view" and len(parts) >= 5:
        kind = _safe_kind(parts[2])
        instr_id = int(parts[3])
        page = int(parts[4])
        await cbq.answer()
        text, kb = await _render_view(settings=settings, kind=kind, instr_id=instr_id, page=page)
        await _edit_or_send(cbq, text, kb)
        return

    # подтверждение удаления
    if action == "del" and len(parts) >= 5:
        kind = _safe_kind(parts[2])
        instr_id = int(parts[3])
        page = int(parts[4])
        await cbq.answer()

        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"ai|del_ok|{kind}|{instr_id}|{page}"),
            InlineKeyboardButton(text="↩️ Отмена", callback_data=f"ai|view|{kind}|{instr_id}|{page}"),
            width=2,
        )

        await _edit_or_send(
            cbq,
            f"🗑 <b>Удалить инструкцию</b>\n\n"
            f"Точно удалить <code>#{instr_id}</code>?",
            kb.as_markup(),
        )
        return

    # удалить
    if action == "del_ok" and len(parts) >= 5:
        kind = _safe_kind(parts[2])
        instr_id = int(parts[3])
        page = int(parts[4])
        await cbq.answer()

        await delete_ai_instruction(db_path=settings.db_path, instr_id=instr_id)

        text, kb = await _render_pick_list(settings=settings, kind=kind, page=page)
        await _edit_or_send(cbq, "✅ Удалено.\n\n" + text, kb)
        return

    # редактирование (в state, ввод текстом)
    if action == "edit" and len(parts) >= 5:
        kind = _safe_kind(parts[2])
        instr_id = int(parts[3])
        page = int(parts[4])
        await cbq.answer()

        await state.set_state(AiEdit.waiting_text)
        await state.update_data(kind=kind, instr_id=instr_id, page=page)

        if cbq.message:
            await cbq.message.answer(
                f"✏️ <b>Редактирование</b>\n\n"
                f"Отправь новый текст для <code>#{instr_id}</code> ({_kind_title(kind)}).\n\n"
                "<i>Кнопка «❌ Отмена» — отменит.</i>",
                reply_markup=admin_ai_cancel_kb(),
            )
        return

    await cbq.answer()


# --------------------- edit flow ---------------------

@router.message(AiEdit.waiting_text, F.text == "❌ Отмена")
async def ai_edit_cancel(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user is None:
        return
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    data = await state.get_data()
    kind = _safe_kind(data.get("kind") or AI_KIND_SEARCH)
    instr_id = int(data.get("instr_id") or 0)
    page = int(data.get("page") or 0)
    await state.clear()

    text, kb = await _render_view(settings=settings, kind=kind, instr_id=instr_id, page=page)
    await message.answer("Ок, отменил.", reply_markup=admin_ai_kb())
    await message.answer(text, reply_markup=kb)


@router.message(AiEdit.waiting_text)
async def ai_edit_save(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user is None:
        return
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    data = await state.get_data()
    kind = _safe_kind(data.get("kind") or AI_KIND_SEARCH)
    instr_id = int(data.get("instr_id") or 0)
    page = int(data.get("page") or 0)

    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("Текст пустой. Отправь новый текст одним сообщением.")
        return

    try:
        await update_ai_instruction(db_path=settings.db_path, instr_id=instr_id, new_text=new_text)
    except Exception as e:
        await message.answer(f"Не смог обновить. Ошибка: <code>{html.escape(str(e))}</code>")
        return
    finally:
        await state.clear()

    text, kb = await _render_view(settings=settings, kind=kind, instr_id=instr_id, page=page)
    await message.answer("✅ Обновлено.", reply_markup=admin_ai_kb())
    await message.answer(text, reply_markup=kb)
