import html
from datetime import datetime

from aiogram import Router, F
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from keyboards.user import (
    BTN_USER_ADMIN_CHAT,
    BTN_CHAT_CLOSE,
    user_main_kb,
    user_support_chat_kb,
)
from db import support_chat as sch
from handlers.support_chat_states import SupportChatState


router = Router()


class UThreadsCB(CallbackData, prefix="u_th"):
    page: int


class UOpenThreadCB(CallbackData, prefix="u_op"):
    thread_id: int
    page_from_newest: int


class UNewThreadCB(CallbackData, prefix="u_new"):
    pass


def _fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso


def _truncate(s: str, n: int = 800) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


async def _render_user_threads(settings: Settings, user_id: int, page: int):
    threads, total, pages, page = await sch.list_threads_for_user(settings.db_path, user_id, page, per_page=8)

    text_lines = [
        "💬 <b>Чат с админом</b>",
        "Выберите диалог или начните новый:",
        "",
    ]
    if not threads:
        text_lines.append("Пока нет диалогов. Нажмите <b>➕ Новый чат</b> 🙂")

    kb = InlineKeyboardBuilder()

    for t in threads:
        status_emoji = "🟢" if t.status == "active" else "⚪️"
        kb.button(
            text=f"{status_emoji} Чат #{t.id} • {_fmt_dt(t.updated_at)}",
            callback_data=UOpenThreadCB(thread_id=t.id, page_from_newest=0),
        )

    kb.button(text="➕ Новый чат", callback_data=UNewThreadCB())

    # пагинация
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=UThreadsCB(page=page - 1))
    nav.button(text=f"📄 {page+1}/{pages}", callback_data=UThreadsCB(page=page))
    if page < pages - 1:
        nav.button(text="➡️", callback_data=UThreadsCB(page=page + 1))

    kb.adjust(1)
    nav.adjust(3)

    # объединяем
    merged = InlineKeyboardBuilder()
    for row in kb.as_markup().inline_keyboard:
        merged.row(*row)
    merged.row(*nav.as_markup().inline_keyboard[0])

    return "\n".join(text_lines), merged.as_markup()


async def _render_user_thread_history(settings: Settings, user_id: int, thread_id: int, page_from_newest: int):
    thread = await sch.get_thread(settings.db_path, thread_id)
    if not thread or thread.user_id != user_id:
        return "❌ Диалог не найден.", None, None

    msgs, total, pages, page_from_newest = await sch.list_messages_newest_page(
        settings.db_path, thread_id, page_from_newest, per_page=10
    )

    status_emoji = "🟢" if thread.status == "active" else "⚪️"
    status_txt = "Активный" if thread.status == "active" else "Закрыт"

    head = [
        f"💬 <b>Чат #{thread.id}</b> • {status_emoji} <b>{status_txt}</b>",
        f"🕓 Обновлён: <code>{html.escape(_fmt_dt(thread.updated_at))}</code>",
        "",
    ]

    body = []
    if not msgs:
        body.append("Пока сообщений нет. Напишите первое сообщение 🙂")
    else:
        for m in msgs:
            ts = _fmt_dt(m.created_at)
            if m.sender_role == "admin":
                who = "🛡️ <b>Админ</b>"
            elif m.sender_role == "user":
                who = "👤 <b>Вы</b>"
            else:
                who = "ℹ️ <b>Система</b>"
            body.append(
                f"🕒 <code>{html.escape(ts)}</code> {who}\n{html.escape(_truncate(m.text))}"
            )

    text = "\n".join(head) + "\n\n".join(body)

    kb = InlineKeyboardBuilder()
    # листание истории (от новых к старым)
    if page_from_newest < pages - 1:
        kb.button(text="⬅️ Старее", callback_data=UOpenThreadCB(thread_id=thread_id, page_from_newest=page_from_newest + 1))
    kb.button(text=f"📜 {page_from_newest+1}/{pages}", callback_data=UOpenThreadCB(thread_id=thread_id, page_from_newest=page_from_newest))
    if page_from_newest > 0:
        kb.button(text="Новее ➡️", callback_data=UOpenThreadCB(thread_id=thread_id, page_from_newest=page_from_newest - 1))

    kb.button(text="⬅️ К списку диалогов", callback_data=UThreadsCB(page=0))
    kb.adjust(3, 1)

    return text, kb.as_markup(), thread


@router.message(F.text == BTN_USER_ADMIN_CHAT)
async def user_open_threads(message: Message, settings: Settings):
    text, markup = await _render_user_threads(settings, message.from_user.id, page=0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(UThreadsCB.filter())
async def user_threads_page(call: CallbackQuery, callback_data: UThreadsCB, settings: Settings):
    text, markup = await _render_user_threads(settings, call.from_user.id, page=callback_data.page)
    await call.answer()
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(UNewThreadCB.filter())
async def user_new_thread(call: CallbackQuery, settings: Settings, state: FSMContext):
    await call.answer()
    thread_id = await sch.create_thread(settings.db_path, call.from_user.id)

    # уведомляем админов
    username = call.from_user.username or "без username"
    mention = f"@{username}" if call.from_user.username else f"<a href='tg://user?id={call.from_user.id}'>пользователь</a>"
    text_to_admin = (
        f"🆕 <b>Открыт новый чат</b> #{thread_id}\n"
        f"👤 {mention} • <code>{call.from_user.id}</code>\n"
        f"Откройте чат, чтобы ответить."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💬 Открыть чат #{thread_id}", callback_data=f"a_open:{thread_id}:0")
    kb.adjust(1)

    for admin_id in settings.admin_ids:
        try:
            await call.bot.send_message(admin_id, text_to_admin, reply_markup=kb.as_markup())
        except Exception:
            pass

    # открываем чат пользователю (режим переписки)
    await state.set_state(SupportChatState.user_chat)
    await state.update_data(thread_id=thread_id)

    hist_text, hist_kb, _thread = await _render_user_thread_history(settings, call.from_user.id, thread_id, 0)
    await call.message.answer(hist_text, reply_markup=hist_kb)
    await call.message.answer(
        "✅ Чат открыт. Пишите сообщение — оно уйдёт админу.\nЧтобы завершить — нажмите <b>🔒 Закрыть чат</b>.",
        reply_markup=user_support_chat_kb(),
    )


@router.callback_query(UOpenThreadCB.filter())
async def user_open_thread(call: CallbackQuery, callback_data: UOpenThreadCB, settings: Settings, state: FSMContext):
    await call.answer()
    text, markup, thread = await _render_user_thread_history(
        settings, call.from_user.id, callback_data.thread_id, callback_data.page_from_newest
    )
    await call.message.edit_text(text, reply_markup=markup)

    # если чат активен — переводим в режим переписки (клавиатура “закрыть чат”)
    if thread and thread.status == "active":
        await state.set_state(SupportChatState.user_chat)
        await state.update_data(thread_id=thread.id)
        await call.message.answer(
            "✍️ Вы в активном чате. Пишите сообщение.\nЗакрыть: <b>🔒 Закрыть чат</b>",
            reply_markup=user_support_chat_kb(),
        )
    else:
        await state.clear()
        await call.message.answer("ℹ️ Этот чат закрыт. Можно начать новый.", reply_markup=user_main_kb())


@router.message(SupportChatState.user_chat, F.text == BTN_CHAT_CLOSE)
async def user_close_chat(message: Message, settings: Settings, state: FSMContext):
    data = await state.get_data()
    thread_id = data.get("thread_id")
    if not thread_id:
        await state.clear()
        await message.answer("ℹ️ Нет активного чата.", reply_markup=user_main_kb())
        return

    thread = await sch.get_thread(settings.db_path, thread_id)
    if not thread or thread.user_id != message.from_user.id:
        await state.clear()
        await message.answer("ℹ️ Чат уже закрыт или недоступен.", reply_markup=user_main_kb())
        return

    thread = await sch.close_thread(settings.db_path, thread_id)
    await state.clear()

    # пользователю
    await message.answer(f"🔒 Чат #{thread_id} закрыт. Возвращаю меню.", reply_markup=user_main_kb())

    # админу (или всем админам, если не назначен)
    admins = [thread.admin_id] if thread and thread.admin_id else list(settings.admin_ids)
    for aid in admins:
        if not aid:
            continue
        try:
            await message.bot.send_message(aid, f"🔒 Чат #{thread_id} закрыт пользователем. Возврат в меню ✅")
        except Exception:
            pass


@router.message(SupportChatState.user_chat)
async def user_send_to_admin(message: Message, settings: Settings, state: FSMContext):
    data = await state.get_data()
    thread_id = data.get("thread_id")
    if not thread_id or not message.text:
        return

    thread = await sch.get_thread(settings.db_path, thread_id)
    if not thread or thread.status != "active":
        await state.clear()
        await message.answer("🔒 Этот чат уже закрыт.", reply_markup=user_main_kb())
        return

    # сохраняем
    await sch.add_message(settings.db_path, thread_id, "user", message.from_user.id, message.text)

    # кому отправлять: назначенному админу или всем
    username = message.from_user.username or "без_username"
    user_title = f"@{username}" if message.from_user.username else f"id:{message.from_user.id}"
    admin_text = (
        f"📩 <b>Сообщение от пользователя</b> 👤 <b>{html.escape(user_title)}</b>\n"
        f"💬 <b>Чат #{thread_id}</b>\n\n"
        f"{html.escape(message.text)}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💬 Открыть чат #{thread_id}", callback_data=f"a_open:{thread_id}:0")
    kb.adjust(1)

    recipients = [thread.admin_id] if thread.admin_id else list(settings.admin_ids)
    for aid in recipients:
        if not aid:
            continue
        try:
            await message.bot.send_message(aid, admin_text, reply_markup=kb.as_markup())
        except Exception:
            pass
