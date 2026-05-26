from __future__ import annotations

import math
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import AdminUsersCb
from config import Settings
from db import count_admin_users, get_admin_user_analytics, is_admin, list_admin_users
from keyboards.admin import BTN_ADMIN_USERS, admin_main_kb

router = Router()

PAGE_SIZE = 6

STATUS_TITLES = {
    "all": "Все",
    "approved": "Одобренные",
    "pending": "На проверке",
    "rejected": "Отклонённые",
    "blocked": "Заблокированные",
}

STATUS_BADGES = {
    "approved": "🟢 Одобрен",
    "pending": "🟡 На проверке",
    "rejected": "🔴 Отклонён",
    "blocked": "⛔️ Заблокирован",
}

STATUS_FILTERS = [
    ("all", "Все"),
    ("approved", "Одобренные"),
    ("pending", "Проверка"),
    ("rejected", "Отклонённые"),
    ("blocked", "Блок"),
]


def _safe(value: Any, default: str = "—") -> str:
    text = str(value).strip() if value is not None else ""
    return escape(text or default)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _money(value: Any) -> str:
    try:
        amount = float(value or 0)
    except Exception:
        amount = 0.0
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    return escape(text.replace("T", " ").replace("+00:00", " UTC"))


def _status(value: Any) -> str:
    text = str(value or "").strip().lower()
    return STATUS_BADGES.get(text, f"⚪️ {_safe(text or '—')}")


def _clamp_page(page: int, total_pages: int) -> int:
    return max(1, min(int(page or 1), max(1, total_pages)))


async def _check_admin(message_or_call: Message | CallbackQuery, settings: Settings) -> bool:
    allowed = await is_admin(settings.db_path, message_or_call.from_user.id, settings.admin_ids)
    if allowed:
        return True

    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.answer("Нет доступа", show_alert=True)
    return False


async def _build_users_list(settings: Settings, page: int, status: str) -> tuple[str, InlineKeyboardBuilder]:
    if status not in STATUS_TITLES:
        status = "all"

    total = await count_admin_users(settings.db_path, status=status)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = _clamp_page(page, total_pages)
    offset = (page - 1) * PAGE_SIZE

    users = await list_admin_users(settings.db_path, status=status, limit=PAGE_SIZE, offset=offset)

    text = (
        "👥 <b>Все пользователи</b>\n\n"
        f"Фильтр: <b>{STATUS_TITLES[status]}</b>\n"
        f"Найдено: <b>{total}</b>\n"
        f"Страница: <b>{page}/{total_pages}</b>\n\n"
    )

    if not users:
        text += "Пользователей нет."
    else:
        lines = []
        for index, user in enumerate(users, start=offset + 1):
            name = _safe(user.get("full_name"), "Без имени")
            line = (
                f"{index}. <b>{name}</b>\n"
                f"   ID: <code>{_safe(user.get('tg_id'))}</code> · {_status(user.get('status'))}\n"
                f"   Накладные: <b>{_int(user.get('invoices_total'))}</b> · "
                f"КП: <b>{_int(user.get('kp_items_total'))}</b> · "
                f"Счета: <b>{_int(user.get('bills_total'))}</b> · "
                f"Чаты: <b>{_int(user.get('active_chats'))}</b>"
            )
            lines.append(line)
        text += "\n\n".join(lines)

    kb = InlineKeyboardBuilder()

    for user in users:
        name = str(user.get("full_name") or "Без имени").strip()
        if len(name) > 22:
            name = name[:19] + "..."
        kb.row(
            InlineKeyboardButton(
                text=f"👤 {name} · {user.get('tg_id')}",
                callback_data=AdminUsersCb(
                    action="view",
                    page=page,
                    tg_id=int(user["tg_id"]),
                    status=status,
                ).pack(),
            )
        )

    kb.row(
        InlineKeyboardButton(
            text="⬅️",
            callback_data=AdminUsersCb(action="list", page=max(1, page - 1), status=status).pack(),
        ),
        InlineKeyboardButton(
            text="➡️",
            callback_data=AdminUsersCb(action="list", page=min(total_pages, page + 1), status=status).pack(),
        ),
    )

    for status_code, title in STATUS_FILTERS:
        marker = "✓ " if status_code == status else ""
        kb.add(
            InlineKeyboardButton(
                text=f"{marker}{title}",
                callback_data=AdminUsersCb(action="list", page=1, status=status_code).pack(),
            )
        )
    kb.adjust(1, 2, 3, 2)

    kb.row(
        InlineKeyboardButton(
            text="⬅️ В меню",
            callback_data=AdminUsersCb(action="menu", page=page, status=status).pack(),
        )
    )

    return text, kb


def _user_card_text(data: dict[str, Any]) -> str:
    user = data.get("user") or {}
    reg = data.get("registration") or {}
    invoices = data.get("invoices") or {}
    payouts = data.get("payouts") or {}
    bills = data.get("bills") or {}
    kp = data.get("kp") or {}
    kp_session = data.get("kp_session") or {}
    ai = data.get("ai") or {}
    ai_searches = data.get("ai_searches") or {}
    support = data.get("support") or {}
    support_messages = data.get("support_messages") or {}
    balance = data.get("balance") or {}

    reg_file = "есть" if reg.get("file_id") else "нет"
    current_supplier = kp_session.get("supplier_name") or "—"

    return (
        "👤 <b>Карточка пользователя</b>\n\n"
        "<b>Основное</b>\n"
        f"ID: <code>{_safe(user.get('tg_id'))}</code>\n"
        f"ФИО: <b>{_safe(user.get('full_name'), 'Без имени')}</b>\n"
        f"Телефон: <b>{_safe(user.get('phone'))}</b>\n"
        f"Тип: <b>{_safe(user.get('reg_type'))}</b>\n"
        f"Статус: {_status(user.get('status'))}\n"
        f"Создан: {_date(user.get('created_at'))}\n"
        f"Обновлён: {_date(user.get('updated_at'))}\n\n"

        "<b>Регистрация</b>\n"
        f"Статус: {_status(reg.get('status'))}\n"
        f"Файл: <b>{reg_file}</b>\n"
        f"Причина отказа: <b>{_safe(reg.get('reason'))}</b>\n"
        f"Создана: {_date(reg.get('created_at'))}\n"
        f"Обновлена: {_date(reg.get('updated_at'))}\n\n"

        "<b>Накладные</b>\n"
        f"Всего: <b>{_int(invoices.get('total'))}</b> · "
        f"на проверке: <b>{_int(invoices.get('pending'))}</b> · "
        f"одобрено: <b>{_int(invoices.get('approved'))}</b> · "
        f"отклонено: <b>{_int(invoices.get('rejected'))}</b>\n"
        f"Сумма сделок: <b>{_money(invoices.get('approved_deal_sum'))}</b>\n"
        f"Начислено: <b>{_money(invoices.get('approved_reward_sum'))}</b>\n"
        f"Ожидает проверки: <b>{_money(invoices.get('pending_deal_sum'))}</b>\n"
        f"Последняя активность: {_date(invoices.get('last_at'))}\n\n"

        "<b>Выплаты</b>\n"
        f"Всего: <b>{_int(payouts.get('total'))}</b> · "
        f"ожидают: <b>{_int(payouts.get('pending'))}</b> · "
        f"выплачено: <b>{_int(payouts.get('paid'))}</b> · "
        f"отклонено: <b>{_int(payouts.get('rejected'))}</b>\n"
        f"Выплачено: <b>{_money(payouts.get('paid_sum'))}</b>\n"
        f"В заявках: <b>{_money(payouts.get('pending_sum'))}</b>\n"
        f"Доступно: <b>{_money(balance.get('available'))}</b>\n\n"

        "<b>Счета</b>\n"
        f"Всего: <b>{_int(bills.get('total'))}</b> · "
        f"ожидают: <b>{_int(bills.get('pending'))}</b> · "
        f"оплачено: <b>{_int(bills.get('paid'))}</b> · "
        f"отклонено: <b>{_int(bills.get('rejected'))}</b>\n"
        f"Последняя активность: {_date(bills.get('last_at'))}\n\n"

        "<b>КП</b>\n"
        f"Позиций: <b>{_int(kp.get('items_total'))}</b> · "
        f"количество: <b>{_int(kp.get('qty_total'))}</b> · "
        f"поставщиков: <b>{_int(kp.get('suppliers_total'))}</b>\n"
        f"Сумма КП: <b>{_money(kp.get('sum_total'))}</b>\n"
        f"Текущий поставщик: <b>{_safe(current_supplier)}</b>\n"
        f"Последняя активность: {_date(kp.get('last_at'))}\n\n"

        "<b>ИИ и поддержка</b>\n"
        f"Сообщений с ИИ: <b>{_int(ai.get('messages_total'))}</b> "
        f"(вопросов: <b>{_int(ai.get('user_messages'))}</b>)\n"
        f"Поисковых сессий: <b>{_int(ai_searches.get('total'))}</b>\n"
        f"Чатов с админом: <b>{_int(support.get('threads_total'))}</b> · "
        f"активных: <b>{_int(support.get('active_threads'))}</b> · "
        f"закрытых: <b>{_int(support.get('closed_threads'))}</b>\n"
        f"Сообщений в поддержке: <b>{_int(support_messages.get('messages_total'))}</b>\n"
        f"Последняя активность: {_date(support.get('last_at') or support_messages.get('last_at') or ai.get('last_at'))}"
    )


def _user_card_kb(page: int, status: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=AdminUsersCb(action="list", page=page, status=status).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ В меню",
            callback_data=AdminUsersCb(action="menu", page=page, status=status).pack(),
        )
    )
    return kb


@router.message(F.text == BTN_ADMIN_USERS)
async def admin_users_open(message: Message, settings: Settings) -> None:
    if not await _check_admin(message, settings):
        return

    text, kb = await _build_users_list(settings, page=1, status="all")
    await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(AdminUsersCb.filter(F.action == "list"))
async def admin_users_list(call: CallbackQuery, callback_data: AdminUsersCb, settings: Settings) -> None:
    if not await _check_admin(call, settings):
        return

    text, kb = await _build_users_list(settings, page=callback_data.page, status=callback_data.status)
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(AdminUsersCb.filter(F.action == "view"))
async def admin_users_view(call: CallbackQuery, callback_data: AdminUsersCb, settings: Settings) -> None:
    if not await _check_admin(call, settings):
        return

    data = await get_admin_user_analytics(settings.db_path, callback_data.tg_id)
    if not data:
        await call.answer("Пользователь не найден", show_alert=True)
        return

    await call.message.edit_text(
        _user_card_text(data),
        reply_markup=_user_card_kb(callback_data.page, callback_data.status).as_markup(),
    )
    await call.answer()


@router.callback_query(AdminUsersCb.filter(F.action == "menu"))
async def admin_users_menu(call: CallbackQuery, settings: Settings) -> None:
    if not await _check_admin(call, settings):
        return

    try:
        await call.message.delete()
    except Exception:
        pass

    await call.message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())
    await call.answer()
