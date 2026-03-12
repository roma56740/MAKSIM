from __future__ import annotations

import math
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks.bills import UBillsCb
from config import Settings
from db import list_db_admin_ids, get_user
from db.bills import create_bill, count_bills, list_bills, get_bill
from keyboards.user import user_main_kb, user_back_cancel_kb
from utils.msg_clean import remember_msg, cleanup

from aiogram.filters import BaseFilter
from db import is_admin

router = Router()
PAGE_SIZE = 6

class UserOnly(BaseFilter):
    async def __call__(self, event, settings: Settings) -> bool:
        uid = event.from_user.id if event.from_user else 0
        env_admins = getattr(settings, "admin_ids", set()) or set()
        return not await is_admin(settings.db_path, uid, env_admins)

router.message.filter(UserOnly())
router.callback_query.filter(UserOnly())

class BillCreate(StatesGroup):
    waiting_content = State()


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso


def _status_line(status: str, reason: str | None) -> str:
    if status == "pending":
        return "⏳ <b>На проверке</b>"
    if status == "paid":
        return "✅ <b>Оплачено</b>"
    if status == "rejected":
        rr = f"\n🧾 Причина: <i>{reason}</i>" if reason else ""
        return f"❌ <b>Отклонено</b>{rr}"
    return f"ℹ️ <b>{status}</b>"


async def _send_user_menu(message: Message) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="🧾 Создать счёт", callback_data=UBillsCb(action="new").pack())
    kb.button(text="📂 Мои счета", callback_data=UBillsCb(action="list", status="all", page=1).pack())
    kb.button(text="⬅️ Назад", callback_data=UBillsCb(action="back").pack())
    kb.adjust(1)

    await message.answer(
        "💳 <b>Счета</b>\n\n"
        "Здесь вы можете отправить счёт на оплату и отслеживать статус.\n"
        "Отправляйте счёт <b>одним сообщением</b>: текст или фото/файл + подпись ✍️",
        reply_markup=kb.as_markup(),
    )


@router.message(F.text == "💳 Счета")
async def user_bills_menu(message: Message) -> None:
    await _send_user_menu(message)


@router.callback_query(UBillsCb.filter(F.action == "back"))
async def user_bills_back(cbq: CallbackQuery) -> None:
    if cbq.message:
        try:
            await cbq.message.delete()
        except Exception:
            pass
    await cbq.message.answer("🏠 <b>Главное меню</b>", reply_markup=user_main_kb())
    await cbq.answer()


@router.callback_query(UBillsCb.filter(F.action == "new"))
async def user_bills_new(cbq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BillCreate.waiting_content)

    if cbq.message:
        try:
            await cbq.message.delete()
        except Exception:
            pass

    msg = await cbq.message.answer(
        "🧾 <b>Создание счёта</b>\n\n"
        "Отправьте счёт <b>одним сообщением</b>:\n"
        "• текст ✍️\n"
        "• фото 🖼️ (можно с подписью)\n"
        "• файл 📎 (PDF/Doc/Excel и т.д., можно с подписью)\n\n"
        "❗️Если нужно пояснение — добавьте его в подпись к фото/файлу.",
        reply_markup=user_back_cancel_kb(),
    )
    await remember_msg(state, msg)
    await cbq.answer()


@router.message(BillCreate.waiting_content, F.text.in_({"❌ Отмена", "⬅️ Назад"}))
async def user_bills_cancel(message: Message, state: FSMContext, bot, settings: Settings) -> None:
    await remember_msg(state, message)
    await cleanup(bot, message.chat.id, state)
    await state.clear()
    await message.answer("✅ Действие отменено.", reply_markup=user_main_kb())


@router.message(BillCreate.waiting_content)
async def user_bills_receive(
    message: Message,
    state: FSMContext,
    bot,
    settings: Settings,
) -> None:
    await remember_msg(state, message)

    file_id: str | None = None
    file_kind: str | None = None
    text: str | None = None

    # Текст
    if message.text and not message.photo and not message.document:
        text = message.text.strip()

    # Фото (с подписью или без)
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_kind = "photo"
        text = (message.caption or "").strip() or None

    # Документ (с подписью или без)
    elif message.document:
        file_id = message.document.file_id
        file_kind = "document"
        text = (message.caption or "").strip() or None

    else:
        warn = await message.answer(
            "❗️Я пока не умею принимать этот тип сообщения.\n"
            "Отправьте, пожалуйста: <b>текст</b>, <b>фото</b> или <b>файл</b> (можно с подписью).",
        )
        await remember_msg(state, warn)
        return

    bill_id = await create_bill(
        settings.db_path,
        tg_id=message.from_user.id,
        text=text,
        file_id=file_id,
        file_kind=file_kind,
    )

    # уведомление админам
    env_admins = set(getattr(settings, "admin_ids", set()) or set())
    db_admins = set(await list_db_admin_ids(settings.db_path))
    admin_ids = sorted(env_admins | db_admins)

    u = await get_user(settings.db_path, message.from_user.id)
    u_name = (u or {}).get("full_name") or (message.from_user.full_name if message.from_user else "—")
    u_phone = (u or {}).get("phone") or "—"

    extra_desc = f"💬 Описание: <i>{text}</i>\n" if text else ""

    admin_text = (
        f"🧾 <b>Новый счёт #{bill_id}</b>\n\n"
        f"👤 Пользователь: <b>{u_name}</b>\n"
        f"🆔 TG: <code>{message.from_user.id}</code>\n"
        f"📞 Телефон: <b>{u_phone}</b>\n"
        f"🕒 Время: <b>{_fmt_dt(datetime.utcnow().isoformat(timespec='seconds'))}</b>\n"
        f"{extra_desc}"
        "\nВыберите действие ниже 👇"
    )


    # импорт здесь, чтобы не ловить циклы
    from callbacks.bills import ABillsCb

    for admin_id in admin_ids:
        try:
            header = await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=InlineKeyboardBuilder()
                .button(text="🔎 Открыть", callback_data=ABillsCb(action="open", bill_id=bill_id).pack())
                .as_markup(),
            )
            # если есть вложение — копируем админам оригинал сообщения (самый красивый вариант)
            if file_kind in {"photo", "document"}:
                await bot.copy_message(chat_id=admin_id, from_chat_id=message.chat.id, message_id=message.message_id)
        except Exception:
            pass

    # чистим “служебку”
    await cleanup(bot, message.chat.id, state)
    await state.clear()

    # финал пользователю
    kb = InlineKeyboardBuilder()
    kb.button(text="📂 Мои счета", callback_data=UBillsCb(action="list", status="all", page=1).pack())
    kb.button(text="🏠 Главное меню", callback_data=UBillsCb(action="back").pack())
    kb.adjust(1)

    await message.answer(
        "✅ <b>Счёт отправлен!</b>\n\n"
        f"🧾 Номер: <b>#{bill_id}</b>\n"
        "📌 Статус: ⏳ <b>На проверке</b>\n\n"
        "Мы уведомим вас, когда администратор отметит оплату или отклонит счёт.",
        reply_markup=kb.as_markup(),
    )


async def _render_user_list(cbq: CallbackQuery, settings: Settings, tg_id: int, status: str, page: int) -> None:
    total = await count_bills(settings.db_path, status=status, tg_id=tg_id)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE
    items = await list_bills(settings.db_path, status=status, tg_id=tg_id, limit=PAGE_SIZE, offset=offset)

    title_map = {
        "all": "📂 <b>Мои счета</b>",
        "pending": "⏳ <b>Счета на проверке</b>",
        "paid": "✅ <b>Оплаченные счета</b>",
        "rejected": "❌ <b>Отклонённые счета</b>",
    }

    lines = [title_map.get(status, "📂 <b>Мои счета</b>")]
    if not items:
        lines.append("\nПока здесь пусто 🙂\nНажмите «🧾 Создать счёт», чтобы отправить новый.")
    else:
        lines.append("")
        for b in items:
            st = b["status"]
            icon = {"pending": "⏳", "paid": "✅", "rejected": "❌"}.get(st, "ℹ️")
            dt = _fmt_dt(b.get("created_at"))
            lines.append(f"{icon} <b>#{b['id']}</b> — {dt}")

    lines.append(f"\n📄 Страница <b>{page}/{total_pages}</b> • Всего: <b>{total}</b>")

    kb = InlineKeyboardBuilder()
    # фильтры
    kb.button(text="📂 Все", callback_data=UBillsCb(action="list", status="all", page=1).pack())
    kb.button(text="⏳ В ожидании", callback_data=UBillsCb(action="list", status="pending", page=1).pack())
    kb.button(text="✅ Оплачено", callback_data=UBillsCb(action="list", status="paid", page=1).pack())
    kb.button(text="❌ Отклонено", callback_data=UBillsCb(action="list", status="rejected", page=1).pack())
    kb.adjust(2, 2)

    # элементы
    for b in items:
        kb.button(text=f"🔎 Открыть #{b['id']}", callback_data=UBillsCb(action="open", bill_id=b["id"]).pack())
    kb.adjust(2, 2)

    # пагинация
    nav = InlineKeyboardBuilder()
    if page > 1:
        nav.button(text="⬅️", callback_data=UBillsCb(action="list", status=status, page=page - 1).pack())
    nav.button(text="🏠 Меню", callback_data=UBillsCb(action="menu").pack())
    if page < total_pages:
        nav.button(text="➡️", callback_data=UBillsCb(action="list", status=status, page=page + 1).pack())

    kb.attach(nav)
    kb.adjust(2, 1, 2)

    await cbq.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await cbq.answer()


@router.callback_query(UBillsCb.filter(F.action == "menu"))
async def user_bills_menu_cb(cbq: CallbackQuery) -> None:
    if cbq.message:
        try:
            await cbq.message.delete()
        except Exception:
            pass
        await _send_user_menu(cbq.message)  # type: ignore
    await cbq.answer()


@router.callback_query(UBillsCb.filter(F.action == "list"))
async def user_bills_list(cbq: CallbackQuery, callback_data: UBillsCb, settings: Settings) -> None:
    await _render_user_list(cbq, settings, cbq.from_user.id, callback_data.status, callback_data.page)


@router.callback_query(UBillsCb.filter(F.action == "open"))
async def user_bills_open(cbq: CallbackQuery, callback_data: UBillsCb, settings: Settings, bot) -> None:
    b = await get_bill(settings.db_path, callback_data.bill_id)
    if not b or b["tg_id"] != cbq.from_user.id:
        await cbq.answer("Счёт не найден.", show_alert=True)
        return

    text = (
        f"🧾 <b>Счёт #{b['id']}</b>\n\n"
        f"🕒 Создан: <b>{_fmt_dt(b.get('created_at'))}</b>\n"
        f"📌 Статус: {_status_line(b['status'], b.get('reason'))}\n"
    )
    if b.get("text"):
        text += f"\n💬 Комментарий:\n<i>{b['text']}</i>\n"

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К списку", callback_data=UBillsCb(action="list", status="all", page=1).pack())
    kb.button(text="🏠 Меню", callback_data=UBillsCb(action="menu").pack())
    kb.adjust(1)

    await cbq.message.edit_text(text, reply_markup=kb.as_markup())

    # если есть вложение — покажем отдельно (без спама: только когда открыли)
    try:
        if b.get("file_kind") == "photo" and b.get("file_id"):
            await bot.send_photo(cbq.from_user.id, b["file_id"], caption=f"📎 Вложение к счёту #{b['id']}")
        elif b.get("file_kind") == "document" and b.get("file_id"):
            await bot.send_document(cbq.from_user.id, b["file_id"], caption=f"📎 Вложение к счёту #{b['id']}")
    except Exception:
        pass

    await cbq.answer()
