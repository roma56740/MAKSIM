import html
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import support_chat as sch
from handlers.support_chat_states import SupportChatState
from keyboards.admin import BTN_ADMIN_CHATS, BTN_CHAT_CLOSE, admin_main_kb, admin_support_chat_kb


router = Router()


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        return event.from_user and event.from_user.id in settings.admin_ids


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def _fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso


def _truncate(s: str, n: int = 900) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


async def _render_admin_threads(settings: Settings, status: str, page: int):
    threads, total, pages, page = await sch.list_threads_for_admin(settings.db_path, status, page, per_page=10)

    status_emoji = "🟢" if status == "active" else "⚪️"
    status_txt = "Активные" if status == "active" else "Закрытые"

    text_lines = [
        f"💬 <b>Чаты</b> • {status_emoji} <b>{status_txt}</b>",
        "",
    ]
    if not threads:
        text_lines.append("Пока пусто 🙂")

    switch = InlineKeyboardBuilder()
    switch.row(
        InlineKeyboardButton(text="🟢 Активные", callback_data="a_pick:active"),
        InlineKeyboardButton(text="⚪️ Закрытые", callback_data="a_pick:closed"),
    )
    switch.row(
        InlineKeyboardButton(text="➕ Новый чат", callback_data="a_new:0"),
    )

    kb = InlineKeyboardBuilder()
    for t in threads:
        kb.button(
            text=f"{'🟢' if t.status == 'active' else '⚪️'} #{t.id} • user:{t.user_id} • {_fmt_dt(t.updated_at)}",
            callback_data=f"a_open:{t.id}:0",
        )
    kb.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"a_list:{status}:{page-1}")
    nav.button(text=f"📄 {page+1}/{pages}", callback_data=f"a_list:{status}:{page}")
    if page < pages - 1:
        nav.button(text="➡️", callback_data=f"a_list:{status}:{page+1}")
    nav.adjust(3)

    merged = InlineKeyboardBuilder()
    for row in switch.as_markup().inline_keyboard:
        merged.row(*row)
    for row in kb.as_markup().inline_keyboard:
        merged.row(*row)
    if nav.as_markup().inline_keyboard:
        merged.row(*nav.as_markup().inline_keyboard[0])

    return "\n".join(text_lines), merged.as_markup()


async def _render_admin_user_picker(settings: Settings, page: int):
    users, total, pages, page = await sch.list_users_for_admin_picker(settings.db_path, page, per_page=10)

    text_lines = [
        "💬 <b>Новый чат</b>",
        "Выберите пользователя:",
        "",
    ]
    if not users:
        text_lines.append("Пользователей пока нет.")

    kb = InlineKeyboardBuilder()
    for u in users:
        name = (u.full_name or "").strip() or "Без имени"
        phone = (u.phone or "").strip() or "без телефона"
        status = (u.status or "—").strip()
        kb.button(
            text=f"👤 {name} • {phone} • {status} • {u.tg_id}",
            callback_data=f"a_new_pick:{u.tg_id}",
        )
    kb.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="⬅️", callback_data=f"a_new:{page-1}")
    nav.button(text=f"📄 {page+1}/{pages}", callback_data=f"a_new:{page}")
    if page < pages - 1:
        nav.button(text="➡️", callback_data=f"a_new:{page+1}")
    nav.adjust(3)

    back = InlineKeyboardBuilder()
    back.button(text="⬅️ К чатам", callback_data="a_pick:active")
    back.adjust(1)

    merged = InlineKeyboardBuilder()
    for row in kb.as_markup().inline_keyboard:
        merged.row(*row)
    if nav.as_markup().inline_keyboard:
        merged.row(*nav.as_markup().inline_keyboard[0])
    for row in back.as_markup().inline_keyboard:
        merged.row(*row)

    return "\n".join(text_lines), merged.as_markup()


async def _render_admin_thread_history(settings: Settings, thread_id: int, page_from_newest: int):
    thread = await sch.get_thread(settings.db_path, thread_id)
    if not thread:
        return "❌ Чат не найден.", None, None

    msgs, total, pages, page_from_newest = await sch.list_messages_newest_page(
        settings.db_path, thread_id, page_from_newest, per_page=10
    )

    status_emoji = "🟢" if thread.status == "active" else "⚪️"
    status_txt = "Активный" if thread.status == "active" else "Закрыт"

    head = [
        f"💬 <b>Чат #{thread.id}</b> • {status_emoji} <b>{status_txt}</b>",
        f"👤 Пользователь: <code>{thread.user_id}</code>",
        f"🕓 Обновлён: <code>{html.escape(_fmt_dt(thread.updated_at))}</code>",
        "",
    ]

    body = []
    if not msgs:
        body.append("Пока сообщений нет.")
    else:
        for m in msgs:
            ts = _fmt_dt(m.created_at)
            if m.sender_role == "admin":
                who = f"🛡️ <b>Админ</b> <code>{m.sender_id}</code>"
            elif m.sender_role == "user":
                who = f"👤 <b>Пользователь</b> <code>{m.sender_id}</code>"
            else:
                who = "ℹ️ <b>Система</b>"
            body.append(
                f"🕒 <code>{html.escape(ts)}</code> {who}\n{html.escape(_truncate(m.text))}"
            )

    text = "\n".join(head) + "\n\n".join(body)

    back_status = thread.status
    kb = InlineKeyboardBuilder()
    if page_from_newest < pages - 1:
        kb.button(text="⬅️ Старее", callback_data=f"a_open:{thread_id}:{page_from_newest+1}")
    kb.button(text=f"📜 {page_from_newest+1}/{pages}", callback_data=f"a_open:{thread_id}:{page_from_newest}")
    if page_from_newest > 0:
        kb.button(text="Новее ➡️", callback_data=f"a_open:{thread_id}:{page_from_newest-1}")
    kb.button(text="⬅️ К списку", callback_data=f"a_pick:{back_status}")
    kb.adjust(3, 1)

    return text, kb.as_markup(), thread


@router.message(F.text == BTN_ADMIN_CHATS)
async def admin_chats_entry(message: Message, settings: Settings):
    text, markup = await _render_admin_threads(settings, status="active", page=0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("a_pick:"))
async def admin_pick_status(call: CallbackQuery, settings: Settings):
    status = call.data.split(":", 1)[1]
    await call.answer()
    text, markup = await _render_admin_threads(settings, status=status, page=0)
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("a_list:"))
async def admin_threads_page(call: CallbackQuery, settings: Settings):
    _, status, page_s = call.data.split(":")
    await call.answer()
    text, markup = await _render_admin_threads(settings, status=status, page=int(page_s))
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("a_new:"))
async def admin_new_chat_picker(call: CallbackQuery, settings: Settings):
    page = int(call.data.split(":")[1])
    await call.answer()
    text, markup = await _render_admin_user_picker(settings, page=page)
    await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("a_new_pick:"))
async def admin_create_new_chat(call: CallbackQuery, settings: Settings, state: FSMContext):
    user_id = int(call.data.split(":")[1])
    await call.answer()

    thread_id = await sch.create_thread(settings.db_path, user_id)
    await sch.set_thread_admin(settings.db_path, thread_id, call.from_user.id)
    await sch.add_message(settings.db_path, thread_id, "system", call.from_user.id, "Чат открыт администратором.")

    text, markup, thread = await _render_admin_thread_history(settings, thread_id, 0)
    await call.message.edit_text(text, reply_markup=markup)

    await state.set_state(SupportChatState.admin_chat)
    await state.update_data(thread_id=thread_id)

    await call.message.answer(
        "✍️ Новый чат создан. Пишите сообщение пользователю.\nЗакрыть: <b>🔒 Закрыть чат</b>",
        reply_markup=admin_support_chat_kb(),
    )

    try:
        kb = InlineKeyboardBuilder()
        kb.button(text=f"💬 Открыть чат #{thread_id}", callback_data=f"u_op:{thread_id}:0")
        kb.adjust(1)

        await call.bot.send_message(
            user_id,
            f"🛡️ <b>Админ открыл для вас новый чат</b> #{thread_id}\nНажмите кнопку ниже, чтобы открыть диалог.",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("a_open:"))
async def admin_open_thread(call: CallbackQuery, settings: Settings, state: FSMContext):
    _, thread_id_s, page_s = call.data.split(":")
    thread_id = int(thread_id_s)
    page_from_newest = int(page_s)

    await call.answer()

    text, markup, thread = await _render_admin_thread_history(settings, thread_id, page_from_newest)
    await call.message.edit_text(text, reply_markup=markup)

    if thread and thread.status == "active":
        await sch.set_thread_admin(settings.db_path, thread_id, call.from_user.id)
        await state.set_state(SupportChatState.admin_chat)
        await state.update_data(thread_id=thread_id)

        await call.message.answer(
            "✍️ Вы в активном чате. Пишите ответ пользователю.\nЗакрыть: <b>🔒 Закрыть чат</b>",
            reply_markup=admin_support_chat_kb(),
        )
    else:
        await state.clear()


@router.message(SupportChatState.admin_chat, F.text == BTN_CHAT_CLOSE)
async def admin_close_chat(message: Message, settings: Settings, state: FSMContext):
    data = await state.get_data()
    thread_id = data.get("thread_id")
    if not thread_id:
        await state.clear()
        await message.answer("ℹ️ Нет активного чата.", reply_markup=admin_main_kb())
        return

    thread = await sch.get_thread(settings.db_path, thread_id)
    if not thread or thread.status != "active":
        await state.clear()
        await message.answer("ℹ️ Чат уже закрыт.", reply_markup=admin_main_kb())
        return

    await sch.close_thread(settings.db_path, thread_id)
    await state.clear()

    await message.answer(f"🔒 Чат #{thread_id} закрыт. Возврат в меню ✅", reply_markup=admin_main_kb())

    try:
        await message.bot.send_message(
            thread.user_id,
            f"🔒 Чат #{thread_id} закрыт админом.",
        )
        from keyboards.user import user_main_kb
        await message.bot.send_message(thread.user_id, "🏠 Главное меню", reply_markup=user_main_kb())
    except Exception:
        pass


@router.message(SupportChatState.admin_chat)
async def admin_send_to_user(message: Message, settings: Settings, state: FSMContext):
    data = await state.get_data()
    thread_id = data.get("thread_id")
    if not thread_id or not message.text:
        return

    thread = await sch.get_thread(settings.db_path, thread_id)
    if not thread or thread.status != "active":
        await state.clear()
        await message.answer("🔒 Этот чат уже закрыт.", reply_markup=admin_main_kb())
        return

    await sch.add_message(settings.db_path, thread_id, "admin", message.from_user.id, message.text)
    await sch.set_thread_admin(settings.db_path, thread_id, message.from_user.id)

    user_text = f"🛡️ <b>Админ</b>:\n{html.escape(message.text)}"
    try:
        from keyboards.user import user_support_chat_kb
        await message.bot.send_message(thread.user_id, user_text, reply_markup=user_support_chat_kb())
    except Exception:
        pass