from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import get_user, get_registration, is_admin
from db.profile import get_profile_analytics

router = Router()


def _safe(v) -> str:
    return str(v) if v not in (None, "", "None") else "—"


def _doc_label(file_kind: str | None, file_id: str | None) -> str:
    if not file_id:
        return "❌ Нет"
    k = (file_kind or "").lower()
    if k in ("photo", "image", "jpg", "png"):
        return "🖼 Фото загружено"
    return "📎 Документ загружен"


def _build_profile_text(data: dict) -> str:
    fmt_money = data["format"]["money"]
    badge = data["format"]["badge"]

    user = data["user"] or {}
    reg = data["registration"] or {}
    kp = data["kp_session"] or {}
    inv = data["invoices"] or {}
    pay = data["payouts"] or {}
    bal = data["balance"] or {}

    tg_id = user.get("tg_id") or reg.get("tg_id") or "—"

    # user
    full_name = _safe(user.get("full_name") or reg.get("full_name"))
    phone = _safe(user.get("phone") or reg.get("phone"))
    reg_type = _safe(user.get("reg_type") or reg.get("reg_type"))
    user_status = badge(user.get("status"))
    reg_status = badge(reg.get("status"))
    reason = _safe(reg.get("reason"))

    doc_label = _doc_label(reg.get("file_kind"), reg.get("file_id"))

    created = _safe(user.get("created_at") or reg.get("created_at"))
    updated = _safe(user.get("updated_at") or reg.get("updated_at"))

    # balance
    available = fmt_money(bal.get("available"))
    earned = fmt_money(bal.get("earned"))
    paid = fmt_money(bal.get("paid"))
    pending_payouts = fmt_money(bal.get("pending_payouts"))

    # invoices
    inv_total = int(inv.get("total") or 0)
    inv_pending = int(inv.get("pending") or 0)
    inv_approved = int(inv.get("approved") or 0)
    inv_rejected = int(inv.get("rejected") or 0)
    approved_deal_sum = fmt_money(inv.get("approved_deal_sum"))
    approved_reward_sum = fmt_money(inv.get("approved_reward_sum"))
    last_approved_at = _safe(inv.get("last_approved_at"))

    # payouts
    pay_total = int(pay.get("total") or 0)
    pay_pending = int(pay.get("pending") or 0)
    pay_paid = int(pay.get("paid") or 0)
    last_paid_at = _safe(pay.get("last_paid_at"))

    # KP
    kp_has = "✅ Есть" if kp else "❌ Нет"
    kp_supplier = _safe(kp.get("supplier_name"))
    kp_updated = _safe(kp.get("updated_at"))
    kp_items = int(data.get("kp_items_count") or 0)

    # reason show only if meaningful
    reason_block = ""
    if reg.get("status") == "rejected" and reason not in ("—", ""):
        reason_block = f"\n🧾 <b>Причина:</b> {reason}"

    text = (
        "👤 <b>Профиль</b>\n"
        f"🆔 <b>ID:</b> <code>{tg_id}</code>\n"
        f"👤 <b>ФИО:</b> {full_name}\n"
        f"📱 <b>Телефон:</b> {phone}\n"
        f"🧩 <b>Тип:</b> {reg_type}\n\n"
        f"🔐 <b>Статус аккаунта:</b> {user_status}\n"
        f"📝 <b>Статус заявки:</b> {reg_status}{reason_block}\n"
        f"📎 <b>Документ:</b> {doc_label}\n\n"
        "💰 <b>Баланс</b>\n"
        f"• Доступно: <b>{available}</b>\n"
        f"• Начислено (принято): {earned}\n"
        f"• Выплачено: {paid}\n"
        f"• К выплате (ожидает): {pending_payouts}\n\n"
        "🧾 <b>Накладные</b>\n"
        f"• Всего: {inv_total}\n"
        f"• На проверке: {inv_pending}\n"
        f"• Принято: {inv_approved} (сумма сделок: {approved_deal_sum}, начислено: {approved_reward_sum})\n"
        f"• Отклонено: {inv_rejected}\n"
        f"• Последнее принятие: {last_approved_at}\n\n"
        "🏦 <b>Выплаты</b>\n"
        f"• Всего: {pay_total}\n"
        f"• В ожидании: {pay_pending}\n"
        f"• Выплачено: {pay_paid}\n"
        f"• Последняя выплата: {last_paid_at}\n\n"
        "📄 <b>КП</b>\n"
        f"• Текущая сессия: {kp_has}\n"
        f"• Поставщик: {kp_supplier}\n"
        f"• Позиции: {kp_items}\n"
        f"• Обновлено: {kp_updated}\n\n"
        f"🕒 <b>Создан:</b> {created}\n"
        f"🔄 <b>Обновлён:</b> {updated}"
    )
    return text


def _profile_inline_kb(has_doc: bool) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if has_doc:
        kb.button(text="📎 Мой документ", callback_data="profile:doc")
    kb.button(text="🔄 Обновить", callback_data="profile:refresh")
    kb.adjust(2)
    return kb


@router.message(F.text == "👤 Профиль")
async def user_profile(message: Message, settings: Settings) -> None:
    data = await get_profile_analytics(settings.db_path, message.from_user.id)
    reg = data.get("registration") or {}
    has_doc = bool(reg.get("file_id"))

    text = _build_profile_text(data)
    kb = _profile_inline_kb(has_doc).as_markup()

    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "profile:refresh")
async def profile_refresh(cbq: CallbackQuery, settings: Settings) -> None:
    await cbq.answer("Обновляю…")
    data = await get_profile_analytics(settings.db_path, cbq.from_user.id)
    reg = data.get("registration") or {}
    has_doc = bool(reg.get("file_id"))

    text = _build_profile_text(data)
    kb = _profile_inline_kb(has_doc).as_markup()

    try:
        await cbq.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cbq.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "profile:doc")
async def profile_doc(cbq: CallbackQuery, settings: Settings) -> None:
    await cbq.answer()
    reg = await get_registration(settings.db_path, cbq.from_user.id)
    if not reg or not reg.get("file_id"):
        await cbq.message.answer("📎 Документ не найден.")
        return

    file_id = reg["file_id"]
    file_kind = (reg.get("file_kind") or "").lower()

    caption = "📎 Ваш документ"

    # Если это фото — отправим как фото, иначе как документ
    if file_kind in ("photo", "image", "jpg", "png"):
        await cbq.message.answer_photo(file_id, caption=caption)
    else:
        await cbq.message.answer_document(file_id, caption=caption)
