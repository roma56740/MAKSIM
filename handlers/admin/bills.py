from __future__ import annotations

import math
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks.bills import ABillsCb
from config import Settings
from db import is_admin, get_user
from db.bills import count_bills, list_bills, get_bill, reject_bill, mark_bill_paid
from keyboards.admin import admin_main_kb, admin_back_cancel_kb
from utils.msg_clean import remember_msg, cleanup


router = Router()
PAGE_SIZE = 8


class AdminOnly(BaseFilter):
    async def __call__(self, event, settings: Settings) -> bool:
        uid = event.from_user.id if event.from_user else 0
        env_admins = getattr(settings, "admin_ids", set()) or set()
        return await is_admin(settings.db_path, uid, env_admins)

router.message.filter(AdminOnly())
router.callback_query.filter(AdminOnly())


class BillReject(StatesGroup):
    waiting_reason = State()


class BillPayConfirm(StatesGroup):
    waiting_confirm = State()


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso


def _status_icon(status: str) -> str:
    return {"pending": "⏳", "paid": "✅", "rejected": "❌"}.get(status, "ℹ️")


async def _render_admin_list(cbq: CallbackQuery, settings: Settings, page: int) -> None:
    total = await count_bills(settings.db_path, status="pending")
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE
    items = await list_bills(settings.db_path, status="pending", limit=PAGE_SIZE, offset=offset)

    paid_cnt = await count_bills(settings.db_path, status="paid")
    rej_cnt = await count_bills(settings.db_path, status="rejected")

    lines = [
        "💳 <b>Счета (админ)</b>",
        "",
        f"⏳ В ожидании: <b>{total}</b>",
        f"✅ Оплачено: <b>{paid_cnt}</b>",
        f"❌ Отклонено: <b>{rej_cnt}</b>",
        "",
    ]

    if not items:
        lines.append("Пока нет счетов на проверке 🙂")
    else:
        for b in items:
            lines.append(f"⏳ <b>#{b['id']}</b> — {_fmt_dt(b.get('created_at'))}")

    lines.append(f"\n📄 Страница <b>{page}/{total_pages}</b>")

    kb = InlineKeyboardBuilder()
    for b in items:
        kb.button(text=f"🔎 Открыть #{b['id']}", callback_data=ABillsCb(action="open", bill_id=b["id"]).pack())
    kb.adjust(2)

    nav = InlineKeyboardBuilder()
    if page > 1:
        nav.button(text="⬅️", callback_data=ABillsCb(action="list", page=page - 1).pack())
    nav.button(text="🔄 Обновить", callback_data=ABillsCb(action="list", page=page).pack())
    if page < total_pages:
        nav.button(text="➡️", callback_data=ABillsCb(action="list", page=page + 1).pack())

    kb.attach(nav)
    kb.button(text="⬅️ Назад", callback_data=ABillsCb(action="back").pack())
    kb.adjust(2, 1, 2, 1)

    await cbq.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await cbq.answer()


@router.message(AdminOnly(), F.text == "💳 Счета")
async def admin_bills_menu(message: Message, settings: Settings) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="⏳ Счета в ожидании", callback_data=ABillsCb(action="list", page=1).pack())
    kb.adjust(1)

    await message.answer(
        "💳 <b>Счета</b>\n\n"
        "Здесь отображаются счета пользователей, которые ждут проверки.\n"
        "Открывайте счёт и отмечайте: ✅ оплачено или ❌ отклонено.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ABillsCb.filter(F.action == "back"))
async def admin_bills_back(cbq: CallbackQuery) -> None:
    if cbq.message:
        try:
            await cbq.message.delete()
        except Exception:
            pass
    await cbq.message.answer("🏠 <b>Админ-меню</b>", reply_markup=admin_main_kb())
    await cbq.answer()


@router.callback_query(ABillsCb.filter(F.action == "list"))
async def admin_bills_list(cbq: CallbackQuery, callback_data: ABillsCb, settings: Settings) -> None:
    await _render_admin_list(cbq, settings, callback_data.page)


@router.callback_query(ABillsCb.filter(F.action == "open"))
async def admin_bills_open(cbq: CallbackQuery, callback_data: ABillsCb, settings: Settings, bot) -> None:
    b = await get_bill(settings.db_path, callback_data.bill_id)
    if not b:
        await cbq.answer("Счёт не найден.", show_alert=True)
        return

    u = await get_user(settings.db_path, b["tg_id"])
    u_name = (u or {}).get("full_name") or "—"
    u_phone = (u or {}).get("phone") or "—"

    text = (
        f"🧾 <b>Счёт #{b['id']}</b>\n\n"
        f"👤 Пользователь: <b>{u_name}</b>\n"
        f"🆔 TG: <code>{b['tg_id']}</code>\n"
        f"📞 Телефон: <b>{u_phone}</b>\n"
        f"🕒 Создан: <b>{_fmt_dt(b.get('created_at'))}</b>\n"
        f"📌 Статус: {_status_icon(b['status'])} <b>{b['status']}</b>\n"
    )
    if b.get("text"):
        text += f"\n💬 Комментарий:\n<i>{b['text']}</i>\n"

    kb = InlineKeyboardBuilder()

    if b["status"] == "pending":
        kb.button(text="✅ Оплачено", callback_data=ABillsCb(action="pay", bill_id=b["id"]).pack())
        kb.button(text="❌ Отклонить", callback_data=ABillsCb(action="reject", bill_id=b["id"]).pack())
        kb.adjust(2)
    else:
        kb.button(text="🔄 Обновить", callback_data=ABillsCb(action="open", bill_id=b["id"]).pack())
        kb.adjust(1)

    kb.button(text="⬅️ К списку", callback_data=ABillsCb(action="list", page=1).pack())
    kb.button(text="⬅️ Назад", callback_data=ABillsCb(action="back").pack())
    kb.adjust(2)

    await cbq.message.edit_text(text, reply_markup=kb.as_markup())

    # вложение покажем отдельно (если есть)
    try:
        if b.get("file_kind") == "photo" and b.get("file_id"):
            await bot.send_photo(cbq.from_user.id, b["file_id"], caption=f"📎 Вложение к счёту #{b['id']}")
        elif b.get("file_kind") == "document" and b.get("file_id"):
            await bot.send_document(cbq.from_user.id, b["file_id"], caption=f"📎 Вложение к счёту #{b['id']}")
    except Exception:
        pass

    await cbq.answer()


@router.callback_query(ABillsCb.filter(F.action == "reject"))
async def admin_bills_reject_start(cbq: CallbackQuery, callback_data: ABillsCb, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BillReject.waiting_reason)
    await state.update_data(bill_id=callback_data.bill_id)

    msg = await cbq.message.answer(
        "❌ <b>Отклонение счёта</b>\n\n"
        "Введите причину отклонения (она будет отправлена пользователю):",
        reply_markup=admin_back_cancel_kb(),
    )
    await remember_msg(state, msg)
    await cbq.answer()


@router.message(BillReject.waiting_reason, F.text.in_({"❌ Отмена", "⬅️ Назад"}))
async def admin_bills_reject_cancel(message: Message, state: FSMContext, bot) -> None:
    await remember_msg(state, message)
    await cleanup(bot, message.chat.id, state)
    await state.clear()
    await message.answer("✅ Отменено.", reply_markup=admin_main_kb())


@router.message(BillReject.waiting_reason)
async def admin_bills_reject_finish(message: Message, state: FSMContext, settings: Settings, bot) -> None:
    await remember_msg(state, message)

    reason = (message.text or "").strip()
    if len(reason) < 3:
        warn = await message.answer("❗️Причина слишком короткая. Напишите чуть подробнее.")
        await remember_msg(state, warn)
        return

    data = await state.get_data()
    bill_id = int(data.get("bill_id", 0))

    b = await get_bill(settings.db_path, bill_id)
    if not b or b["status"] != "pending":
        await cleanup(bot, message.chat.id, state)
        await state.clear()
        await message.answer("⚠️ Этот счёт уже обработан или не найден.", reply_markup=admin_main_kb())
        return

    await reject_bill(settings.db_path, bill_id, admin_id=message.from_user.id, reason=reason)

    # уведомление пользователю
    try:
        await bot.send_message(
            b["tg_id"],
            "❌ <b>Счёт отклонён</b>\n\n"
            f"🧾 Номер: <b>#{bill_id}</b>\n"
            f"🧾 Причина: <i>{reason}</i>\n\n"
            "Если нужно — отправьте новый счёт с уточнениями.",
        )
    except Exception:
        pass

    await cleanup(bot, message.chat.id, state)
    await state.clear()
    await message.answer(
        f"✅ Готово. Счёт <b>#{bill_id}</b> отклонён, причина отправлена пользователю.",
        reply_markup=admin_main_kb(),
    )


@router.callback_query(ABillsCb.filter(F.action == "pay"))
async def admin_bills_pay_confirm(cbq: CallbackQuery, callback_data: ABillsCb) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить оплату", callback_data=ABillsCb(action="pay_confirm", bill_id=callback_data.bill_id).pack())
    kb.button(text="↩️ Отмена", callback_data=ABillsCb(action="open", bill_id=callback_data.bill_id).pack())
    kb.adjust(1)

    await cbq.message.edit_text(
        "⚠️ <b>Подтверждение оплаты</b>\n\n"
        f"Вы действительно хотите отметить счёт <b>#{callback_data.bill_id}</b> как <b>оплаченный</b>?\n"
        "Пользователь сразу увидит этот статус.",
        reply_markup=kb.as_markup(),
    )
    await cbq.answer()


@router.callback_query(ABillsCb.filter(F.action == "pay_confirm"))
async def admin_bills_pay_finish(cbq: CallbackQuery, callback_data: ABillsCb, settings: Settings, bot) -> None:
    b = await get_bill(settings.db_path, callback_data.bill_id)
    if not b or b["status"] != "pending":
        await cbq.answer("Этот счёт уже обработан.", show_alert=True)
        return

    await mark_bill_paid(settings.db_path, callback_data.bill_id, admin_id=cbq.from_user.id)

    # уведомление пользователю
    try:
        await bot.send_message(
            b["tg_id"],
            "✅ <b>Счёт оплачен</b>\n\n"
            f"🧾 Номер: <b>#{callback_data.bill_id}</b>\n"
            "Спасибо! Если нужно — можете отправить следующий счёт 😊",
        )
    except Exception:
        pass

    await cbq.message.edit_text(
        f"✅ Готово. Счёт <b>#{callback_data.bill_id}</b> отмечен как <b>оплаченный</b>.",
        reply_markup=InlineKeyboardBuilder()
        .button(text="⏳ К списку ожидания", callback_data=ABillsCb(action="list", page=1).pack())
        .button(text="⬅️ Назад", callback_data=ABillsCb(action="back").pack())
        .as_markup(),
    )
    await cbq.answer()
