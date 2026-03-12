from __future__ import annotations

import html
import logging
import math
import re
import time
from typing import Optional, Tuple

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import (
    add_kp_product,
    count_kp_items,
    get_kp_session,
    get_supplier,
    list_kp_items,
    set_kp_supplier,
)
from db.ai_user_chat import (
    add_dialog_message,
    clear_dialog_history,
    create_search_session,
    get_dialog_history,
    get_search_session,
)
from db.catalog import (
    count_products_global_search,
    get_product,
    prepare_search_query,
    search_products_global,
)
from keyboards.user import user_ai_chat_kb, user_main_kb
from services.gpt_dialog import ask_gpt_dialog

router = Router()
log = logging.getLogger(__name__)

PAGE_SIZE = 10
SEARCH_TAG = re.compile(r"\[\[PRODUCT_SEARCH:(.+?)\]\]", re.IGNORECASE)

_GREETING_RE = re.compile(r"^(привет|здравств|добрый|хай|hello|hi|спасибо|благодарю)\b", re.I)


class UserAiChat(StatesGroup):
    chatting = State()


def _short(s: str, n: int = 44) -> str:
    s = (s or "").strip().replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _split_answer_and_tag(answer: str) -> Tuple[str, Optional[str]]:
    a = (answer or "").strip()
    m = SEARCH_TAG.search(a)
    if not m:
        return a, None
    query = (m.group(1) or "").strip()
    cleaned = SEARCH_TAG.sub("", a).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, (query or None)


def _stock_qty(p: dict) -> int:
    try:
        x = p.get("stock_qty")
        if x is None or x == "":
            return 0
        return int(float(x))
    except Exception:
        return 0


def _money(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(v)


async def _edit_or_send(cbq: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup]):
    if cbq.message is None:
        await cbq.answer()
        return
    try:
        await cbq.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await cbq.message.answer(text, reply_markup=reply_markup)


def _search_header(query: str, page: int, total_pages: int, total: int) -> str:
    return (
        f"🔎 <b>Подбор из каталога</b>\n"
        f"Запрос: <b>{html.escape(query)}</b>\n"
        f"Страница <b>{page+1}/{total_pages}</b> • Всего: <b>{total}</b>\n\n"
        f"Выбери товар ниже 👇"
    )


def _product_card_text(prod: dict, supplier_name: str) -> str:
    title = prod.get("title") or prod.get("description") or "—"
    code = prod.get("code") or "—"
    strength = prod.get("strength") or prod.get("product_type") or "—"
    volume = prod.get("volume")
    volume_str = f"{volume}" if volume not in (None, "") else "—"
    price = prod.get("final_price") or prod.get("price") or "—"
    stock = _stock_qty(prod)
    desc = (prod.get("description") or "").strip()
    url = (prod.get("url") or "").strip()
    image_url = (prod.get("image_url") or "").strip()

    lines = [
        f"📦 <b>{html.escape(str(title))}</b>",
        f"🏭 Поставщик: <b>{html.escape(supplier_name or '—')}</b>",
        f"🔢 Код: <code>{html.escape(str(code))}</code>",
        f"🧾 Тип/крепость: <b>{html.escape(str(strength))}</b>",
        f"📏 Объём: <b>{html.escape(volume_str)}</b>",
        f"📦 Наличие: <b>{stock}</b>",
        f"💳 Цена: <b>{html.escape(_money(price))}</b>",
    ]

    if desc:
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) > 350:
            desc = desc[:349].rstrip() + "…"
        lines.append("")
        lines.append(html.escape(desc))

    if image_url:
        lines.append("")
        lines.append(f"🖼 {html.escape(image_url)}")

    if url:
        lines.append("")
        lines.append(f"🔗 {html.escape(url)}")

    return "\n".join(lines).strip()


def _search_results_kb(session_id: int, items: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for it in items:
        pid = int(it["id"])
        title = it.get("title") or it.get("description") or it.get("code") or "Товар"
        supplier = it.get("supplier_name") or ""
        badge = "✅" if _stock_qty(it) > 0 else "❌"
        price = it.get("final_price") or it.get("price")
        btn_text = _short(f"{badge} {title} • {_money(price)} • {supplier}".strip(" •"))
        kb.row(InlineKeyboardButton(text=btn_text, callback_data=f"uai|prod|{session_id}|{pid}|{page}"))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="« Назад", callback_data=f"uai|list|{session_id}|{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд »", callback_data=f"uai|list|{session_id}|{page+1}"))
    if nav:
        kb.row(*nav)

    kb.row(InlineKeyboardButton(text="⬅️ Вернуться к чату", callback_data="uai|back_chat"))
    return kb.as_markup()


def _product_kb(session_id: int, product_id: int, page: int, can_add: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    if can_add:
        kb.row(InlineKeyboardButton(text="➕ Добавить в КП", callback_data=f"uai|add|{session_id}|{product_id}|{page}"))
    else:
        kb.row(InlineKeyboardButton(text="🚫 Нет в наличии", callback_data="uai|noop"))

    kb.row(
        InlineKeyboardButton(text="⬅️ Назад к результатам", callback_data=f"uai|list|{session_id}|{page}"),
        InlineKeyboardButton(text="⬅️ В чат", callback_data="uai|back_chat"),
        width=2,
    )
    return kb.as_markup()


async def _kp_has_product(settings: Settings, tg_id: int, product_id: int) -> bool:
    total = await count_kp_items(settings.db_path, tg_id)
    if total <= 0:
        return False
    items = await list_kp_items(settings.db_path, tg_id, limit=total, offset=0)
    return any(int(it.get("product_id") or 0) == int(product_id) for it in items)


async def _try_add_to_kp(settings: Settings, tg_id: int, product_id: int) -> Tuple[bool, str]:
    prod = await get_product(settings.db_path, product_id)
    if not prod:
        return False, "Товар не найден"

    stock = _stock_qty(prod)
    if stock <= 0:
        return False, "Нет в наличии"

    if await _kp_has_product(settings, tg_id, product_id):
        return False, "Этот товар уже в КП"

    supplier_id = int(prod.get("supplier_id") or 0)
    if not supplier_id:
        return False, "Ошибка: supplier_id пустой"

    # Разрешаем одно КП с товарами от разных поставщиков.
    # supplier_id в kp_sessions используем только как "последний выбранный" (не ограничиваем).
    try:
        await set_kp_supplier(settings.db_path, tg_id, supplier_id)
    except Exception:
        pass

    await add_kp_product(settings.db_path, tg_id, supplier_id=supplier_id, product_id=product_id)
    return True, "Добавлено в КП ✅"


async def _send_search_results(message: Message, settings: Settings, raw_query: str) -> None:
    tg_id = message.from_user.id
    t0 = time.monotonic()

    query = prepare_search_query(raw_query)
    log.info("[AI_CHAT] search start tg_id=%s raw=%r prepared=%r", tg_id, raw_query, query)

    if not query:
        await message.answer(
            "Чтобы подобрать товары — напиши, что именно нужно 👇\n"
            "Например: «вино красное сухое 0.75», «виски jameson», «код 12345».",
            reply_markup=user_ai_chat_kb(),
        )
        return

    session_id = await create_search_session(db_path=settings.db_path, tg_id=tg_id, query=query)
    total = await count_products_global_search(settings.db_path, query)

    if total == 0:
        await message.answer(
            "🔎 По этому запросу ничего не нашёл.\n\n"
            "Попробуй иначе: бренд/название/код/объём/тип/вкус.\n"
            "Пример: «вино сухое красное», «0.5л пиво», «код 7788».",
            reply_markup=user_ai_chat_kb(),
        )
        log.info("[AI_CHAT] search stop: 0 items tg_id=%s dt=%.0fms", tg_id, (time.monotonic() - t0) * 1000)
        return

    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = 0
    items = await search_products_global(settings.db_path, query, limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    await message.answer(
        _search_header(query, page, total_pages, total),
        reply_markup=_search_results_kb(session_id, items, page, total_pages),
    )
    log.info("[AI_CHAT] search done tg_id=%s total=%s dt=%.0fms", tg_id, total, (time.monotonic() - t0) * 1000)


async def _safe_search(message: Message, settings: Settings, query: str) -> None:
    try:
        await _send_search_results(message, settings, query)
    except Exception:
        log.exception("[AI_CHAT] search FAILED tg_id=%s query=%r", getattr(message.from_user, "id", None), query)
        await message.answer(
            "⚠️ Подбор товаров сейчас временно недоступен (ошибка поиска).\n"
            "Попробуй ещё раз или уточни запрос.",
            reply_markup=user_ai_chat_kb(),
        )


@router.message(F.text == "💬 Вопросы")
async def user_ai_entry(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.from_user is None:
        return
    await state.set_state(UserAiChat.chatting)
    await message.answer(
        "💬 <b>Вопросы к менеджеру</b>\n\n"
        "Пиши вопрос обычным сообщением.\n"
        "Если попросишь подобрать товары — я покажу варианты из каталога (страницами).",
        reply_markup=user_ai_chat_kb(),
    )


@router.message(UserAiChat.chatting, F.text == "⬅️ В меню")
async def user_ai_back(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню:", reply_markup=user_main_kb())


@router.message(UserAiChat.chatting, F.text == "🧹 Очистить диалог")
async def user_ai_clear(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    await clear_dialog_history(db_path=settings.db_path, tg_id=message.from_user.id)
    await message.answer("✅ История диалога очищена. Можешь писать заново 🙂", reply_markup=user_ai_chat_kb())


# ✅ КЛЮЧЕВОЕ ИЗМЕНЕНИЕ:
# Поиск запускается ТОЛЬКО если GPT добавил [[PRODUCT_SEARCH:...]]
@router.message(UserAiChat.chatting)
async def user_ai_chat(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return

    tg_id = message.from_user.id
    user_text = (message.text or "").strip()
    if not user_text:
        await message.answer("Отправь текстом 🙂", reply_markup=user_ai_chat_kb())
        return

    log.info("[AI_CHAT] msg tg_id=%s text=%r", tg_id, _short(user_text, 140))

    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    except Exception:
        pass

    progress = await message.answer("🤔 Думаю…")

    await add_dialog_message(db_path=settings.db_path, tg_id=tg_id, role="user", content=user_text)
    history = await get_dialog_history(db_path=settings.db_path, tg_id=tg_id, limit=20)

    t0 = time.monotonic()
    try:
        raw_answer = await ask_gpt_dialog(db_path=settings.db_path, history=history, user_text=user_text)
        log.info("[AI_CHAT] gpt done tg_id=%s dt=%.0fms", tg_id, (time.monotonic() - t0) * 1000)
    except Exception as e:
        log.exception("[AI_CHAT] gpt FAILED tg_id=%s", tg_id)
        try:
            await progress.delete()
        except Exception:
            pass
        await message.answer(
            "⚠️ Сейчас ИИ недоступен.\n"
            f"Ошибка: <code>{html.escape(str(e))}</code>",
            reply_markup=user_ai_chat_kb(),
        )
        return

    try:
        await progress.delete()
    except Exception:
        pass

    answer_text, tag_query = _split_answer_and_tag(raw_answer)
    answer_text = (answer_text or "").strip()

    # 1) Сначала всегда отдаём ответ (если он есть)
    if answer_text:
        await add_dialog_message(db_path=settings.db_path, tg_id=tg_id, role="assistant", content=answer_text)
        await message.answer(answer_text, reply_markup=user_ai_chat_kb())

    # 2) Поиск — ТОЛЬКО если GPT явно дал тег
    if tag_query and not _GREETING_RE.search(user_text):
        q = prepare_search_query(tag_query)
        if q:
            try:
                await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            except Exception:
                pass
            await _safe_search(message, settings, q)
            return

    # 3) Если вообще пусто — попросим уточнить
    if not answer_text:
        await message.answer("Подскажи чуть подробнее 🙂", reply_markup=user_ai_chat_kb())


@router.callback_query(F.data.startswith("uai|"))
async def user_ai_callbacks(cbq: CallbackQuery, settings: Settings) -> None:
    if cbq.from_user is None:
        await cbq.answer()
        return

    parts = (cbq.data or "").split("|")
    if len(parts) < 2:
        await cbq.answer()
        return

    action = parts[1]

    if action == "noop":
        await cbq.answer("❌ Нет в наличии.", show_alert=True)
        return

    if action == "back_chat":
        await cbq.answer()
        await _edit_or_send(cbq, "💬 Вернулся в чат. Пиши следующий вопрос 🙂", None)
        return

    if action == "list" and len(parts) >= 4:
        session_id = int(parts[2])
        page = int(parts[3])

        sess = await get_search_session(db_path=settings.db_path, session_id=session_id)
        if not sess or int(sess["tg_id"]) != cbq.from_user.id:
            await cbq.answer("Сессия поиска устарела", show_alert=True)
            return

        query = str(sess["query"])
        total = await count_products_global_search(settings.db_path, query)
        total_pages = max(1, math.ceil(total / PAGE_SIZE))
        page = max(0, min(page, total_pages - 1))

        items = await search_products_global(settings.db_path, query, limit=PAGE_SIZE, offset=page * PAGE_SIZE)

        await cbq.answer()
        await _edit_or_send(
            cbq,
            _search_header(query, page, total_pages, total),
            _search_results_kb(session_id, items, page, total_pages),
        )
        return

    if action == "prod" and len(parts) >= 5:
        session_id = int(parts[2])
        product_id = int(parts[3])
        page = int(parts[4])

        sess = await get_search_session(db_path=settings.db_path, session_id=session_id)
        if not sess or int(sess["tg_id"]) != cbq.from_user.id:
            await cbq.answer("Сессия поиска устарела", show_alert=True)
            return

        prod = await get_product(settings.db_path, product_id)
        if not prod:
            await cbq.answer("Товар не найден", show_alert=True)
            return

        supplier_name = "—"
        if prod.get("supplier_id"):
            s = await get_supplier(settings.db_path, int(prod["supplier_id"]))
            if s and s.get("name"):
                supplier_name = str(s["name"])

        text = _product_card_text(prod, supplier_name)
        can_add = _stock_qty(prod) > 0

        await cbq.answer()
        await _edit_or_send(cbq, text, _product_kb(session_id, product_id, page, can_add))
        return

    if action == "add" and len(parts) >= 5:
        session_id = int(parts[2])
        product_id = int(parts[3])
        page = int(parts[4])

        sess = await get_search_session(db_path=settings.db_path, session_id=session_id)
        if not sess or int(sess["tg_id"]) != cbq.from_user.id:
            await cbq.answer("Сессия поиска устарела", show_alert=True)
            return

        ok, msg = await _try_add_to_kp(settings, tg_id=cbq.from_user.id, product_id=product_id)
        await cbq.answer(msg, show_alert=not ok)
        return

    await cbq.answer()
