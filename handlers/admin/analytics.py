from __future__ import annotations

import asyncio
import html
import math
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import is_admin
from db.analytics import (
    count_user_stats,
    get_bot_summary,
    list_active_user_ids,
    list_user_stats,
)
from keyboards.admin import admin_main_kb
from services.analytics_report import (
    build_admin_report_xlsx,
    build_user_report_xlsx,
)

router = Router()
PAGE_SIZE = 10
TZ = ZoneInfo("Europe/Vienna")


class AnalyticsCustomRange(StatesGroup):
    waiting_date_from = State()
    waiting_date_to = State()


# -------------------- helpers: admin check --------------------

def _get_env_admin_ids(settings: Settings) -> list[int]:
    val = getattr(settings, "env_admin_ids", None)
    if val is None:
        val = getattr(settings, "admin_ids", None)
    if val is None:
        return []
    return list(val)


async def _is_admin(tg_id: int, settings: Settings) -> bool:
    env_admin_ids = _get_env_admin_ids(settings)
    return await is_admin(settings.db_path, tg_id, env_admin_ids)


class AdminOnly(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        if not message.from_user:
            return False
        return await _is_admin(message.from_user.id, settings)


async def _cbq_admin_guard(cbq: CallbackQuery, settings: Settings) -> bool:
    if not cbq.from_user:
        return False
    return await _is_admin(cbq.from_user.id, settings)


# -------------------- callback data --------------------

class AnalyticsCb(CallbackData, prefix="an"):
    action: str
    period: str = "d"   # d|w|m|c
    page: int = 1
    ds: str = ""        # YYYYMMDD
    de: str = ""        # YYYYMMDD


# -------------------- period helpers --------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _period_range(period: str) -> Tuple[datetime, datetime]:
    now = _now_utc()
    if period == "d":
        return now - timedelta(days=1), now
    if period == "w":
        return now - timedelta(days=7), now
    return now - timedelta(days=30), now


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M (UTC)")


def _period_title(period: str) -> str:
    return {
        "d": "день",
        "w": "неделю",
        "m": "месяц",
        "c": "выбранный период",
    }.get(period, period)


def _money(v: Any) -> str:
    if v is None or v == "":
        return "0,00"
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(v)


def _parse_input_date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    return None


def _date_to_key(d: date) -> str:
    return d.strftime("%Y%m%d")


def _key_to_date(s: str) -> date:
    return datetime.strptime(s, "%Y%m%d").date()


def _custom_range_from_keys(ds: str, de: str) -> Tuple[datetime, datetime]:
    d_from = _key_to_date(ds)
    d_to = _key_to_date(de)

    start_local = datetime.combine(d_from, time.min, tzinfo=TZ)
    end_local = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=TZ)

    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _resolve_period_range(period: str, ds: str = "", de: str = "") -> Tuple[datetime, datetime]:
    if period == "c" and ds and de:
        return _custom_range_from_keys(ds, de)
    return _period_range(period)


def _period_label(period: str, start: datetime, end: datetime, ds: str = "", de: str = "") -> str:
    if period == "c" and ds and de:
        return f"{_key_to_date(ds).strftime('%d.%m.%Y')} — {_key_to_date(de).strftime('%d.%m.%Y')}"
    return f"{_fmt_dt(start)} → {_fmt_dt(end)}"


def _report_caption(period: str, start: datetime, end: datetime, ds: str = "", de: str = "") -> str:
    if period == "c" and ds and de:
        return f"📄 Общий отчет за период {_period_label(period, start, end, ds, de)} (Excel)"
    return f"📄 Общий отчет за {_period_title(period)} (Excel)"


def _personal_report_caption(period: str, start: datetime, end: datetime, ds: str = "", de: str = "") -> str:
    if period == "c" and ds and de:
        return f"📊 Ваш отчет за период {_period_label(period, start, end, ds, de)}"
    return f"📊 Ваш отчет за {_period_title(period)}"


# -------------------- keyboards --------------------

def _main_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Получить отчет (Excel)", callback_data=AnalyticsCb(action="pick_report").pack())
    kb.button(text="📣 Разослать отчеты всем (персональные)", callback_data=AnalyticsCb(action="pick_broadcast").pack())
    kb.button(text="👥 Пользователи (с пагинацией)", callback_data=AnalyticsCb(action="pick_users").pack())
    kb.button(text="⬅️ В админ-меню", callback_data=AnalyticsCb(action="back").pack())
    kb.adjust(1)
    return kb


def _period_kb(next_action: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 За день", callback_data=AnalyticsCb(action=next_action, period="d").pack())
    kb.button(text="🗓️ За неделю", callback_data=AnalyticsCb(action=next_action, period="w").pack())
    kb.button(text="🗓️ За месяц", callback_data=AnalyticsCb(action=next_action, period="m").pack())
    kb.button(text="🗂️ Выборочные даты", callback_data=AnalyticsCb(action=f"custom_{next_action}", period="c").pack())
    kb.button(text="⬅️ Назад", callback_data=AnalyticsCb(action="menu").pack())
    kb.adjust(1)
    return kb


def _users_nav_kb(period: str, page: int, total_pages: int, ds: str = "", de: str = "") -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()

    prev_page = max(1, page - 1)
    next_page = min(total_pages, page + 1)

    row = []
    if page > 1:
        row.append(("⬅️", AnalyticsCb(action="users", period=period, page=prev_page, ds=ds, de=de).pack()))
    row.append((f"📄 {page}/{total_pages}", AnalyticsCb(action="noop", period=period, page=page, ds=ds, de=de).pack()))
    if page < total_pages:
        row.append(("➡️", AnalyticsCb(action="users", period=period, page=next_page, ds=ds, de=de).pack()))

    for text, cb in row:
        kb.button(text=text, callback_data=cb)

    kb.button(text="📥 Скачать общий Excel", callback_data=AnalyticsCb(action="report", period=period, ds=ds, de=de).pack())
    kb.button(text="⬅️ Назад", callback_data=AnalyticsCb(action="menu").pack())

    kb.adjust(3, 1, 1)
    return kb


def _summary_text(
    summary: Dict[str, Any],
    period: str,
    start: datetime,
    end: datetime,
    ds: str = "",
    de: str = "",
) -> str:
    period_name = "выбранный период" if period == "c" else _period_title(period)
    period_line = _period_label(period, start, end, ds, de)

    lines: List[str] = []
    lines.append(f"📊 <b>Аналитика за {period_name}</b>")
    lines.append(f"🕒 Период: <code>{period_line}</code>")
    lines.append("")
    lines.append("👤 <b>Пользователи</b>")
    lines.append(f"• Всего на платформе: <b>{summary.get('users_total', 0)}</b>")
    lines.append(f"• Новых за период: <b>{summary.get('users_new', 0)}</b>")
    lines.append("")
    lines.append("🆕 <b>Заявки на регистрацию</b>")
    lines.append(f"• Всего за период: <b>{summary.get('registrations_total', 0)}</b>")
    lines.append(
        f"• В ожидании: <b>{summary.get('registrations_pending', 0)}</b> | "
        f"Одобрено: <b>{summary.get('registrations_approved', 0)}</b> | "
        f"Отклонено: <b>{summary.get('registrations_rejected', 0)}</b>"
    )
    lines.append("")
    lines.append("🧾 <b>Накладные</b>")
    lines.append(f"• Всего за период: <b>{summary.get('invoices_total', 0)}</b>")
    lines.append(
        f"• В ожидании: <b>{summary.get('invoices_pending', 0)}</b> | "
        f"Одобрено: <b>{summary.get('invoices_approved', 0)}</b> | "
        f"Отклонено: <b>{summary.get('invoices_rejected', 0)}</b>"
    )
    lines.append(f"• Сумма сделок: <b>{_money(summary.get('deal_sum', 0))}</b> ₽")
    lines.append(f"• Вознаграждение: <b>{_money(summary.get('reward_sum', 0))}</b> ₽")
    lines.append("")
    lines.append("💸 <b>Выплаты</b>")
    lines.append(f"• Запросов за период: <b>{summary.get('payouts_total', 0)}</b>")
    lines.append(
        f"• В ожидании: <b>{summary.get('payouts_pending', 0)}</b> | "
        f"Выплачено: <b>{summary.get('payouts_paid', 0)}</b> | "
        f"Отклонено: <b>{summary.get('payouts_rejected', 0)}</b>"
    )
    lines.append(f"• Сумма выплат: <b>{_money(summary.get('payouts_sum', 0))}</b> ₽")
    lines.append("")
    lines.append("📦 <b>Каталог / КП</b>")
    lines.append(f"• Поставщиков: <b>{summary.get('suppliers_total', 0)}</b> (создано за период: <b>{summary.get('suppliers_new', 0)}</b>)")
    lines.append(f"• Товаров: <b>{summary.get('products_total', 0)}</b> (создано за период: <b>{summary.get('products_new', 0)}</b>)")
    lines.append(f"• Сессий КП: <b>{summary.get('kp_sessions_total', 0)}</b> (за период: <b>{summary.get('kp_sessions_new', 0)}</b>)")
    lines.append(f"• Позиции КП: <b>{summary.get('kp_items_total', 0)}</b> (за период: <b>{summary.get('kp_items_new', 0)}</b>)")
    lines.append("")
    lines.append("📦 <b>Excel-прайсы</b>")
    lines.append(f"• Загружено файлов за период: <b>{summary.get('price_uploads_total', 0)}</b>")
    return "\n".join(lines)


# -------------------- entry --------------------

@router.message(AdminOnly(), F.text == "📊 Аналитика")
async def analytics_entry(message: Message) -> None:
    text = (
        "📊 <b>Аналитика</b>\n\n"
        "Тут можно:\n"
        "• сформировать общий отчет по боту (Excel)\n"
        "• посмотреть активность пользователей с пагинацией\n"
        "• разослать персональные отчеты всем пользователям\n\n"
        "Выберите действие:"
    )
    await message.answer(text, reply_markup=_main_kb().as_markup())


# -------------------- navigation --------------------

@router.callback_query(AnalyticsCb.filter(F.action == "back"))
async def analytics_back(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return
    await cbq.message.answer("Админ-меню:", reply_markup=admin_main_kb())
    await cbq.answer()


@router.callback_query(AnalyticsCb.filter(F.action == "menu"))
async def analytics_menu(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return
    await cbq.message.edit_text("📊 <b>Аналитика</b>\n\nВыберите действие:", reply_markup=_main_kb().as_markup())
    await cbq.answer()


@router.callback_query(AnalyticsCb.filter(F.action == "pick_report"))
async def pick_report(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return
    await cbq.message.edit_text(
        "📄 <b>Получить отчет (Excel)</b>\n\nВыберите период:",
        reply_markup=_period_kb("report").as_markup(),
    )
    await cbq.answer()


@router.callback_query(AnalyticsCb.filter(F.action == "pick_broadcast"))
async def pick_broadcast(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return
    await cbq.message.edit_text(
        "📣 <b>Разослать отчеты всем</b>\n\n"
        "Каждому пользователю придет <b>персональный Excel</b> (только его данные).\n"
        "Выберите период:",
        reply_markup=_period_kb("broadcast").as_markup(),
    )
    await cbq.answer()


@router.callback_query(AnalyticsCb.filter(F.action == "pick_users"))
async def pick_users(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return
    await cbq.message.edit_text(
        "👥 <b>Пользователи</b>\n\nВыберите период, чтобы посмотреть активность:",
        reply_markup=_period_kb("users").as_markup(),
    )
    await cbq.answer()


@router.callback_query(AnalyticsCb.filter(F.action == "custom_report"))
async def custom_report_start(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await state.set_state(AnalyticsCustomRange.waiting_date_from)
    await state.update_data(next_action="report")
    await cbq.message.answer("📅 Введите дату <b>С</b> в формате <code>ДД.ММ.ГГГГ</code>\nНапример: <code>03.03.2026</code>")
    await cbq.answer()


@router.callback_query(AnalyticsCb.filter(F.action == "custom_broadcast"))
async def custom_broadcast_start(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await state.set_state(AnalyticsCustomRange.waiting_date_from)
    await state.update_data(next_action="broadcast")
    await cbq.message.answer("📅 Введите дату <b>С</b> в формате <code>ДД.ММ.ГГГГ</code>\nНапример: <code>03.03.2026</code>")
    await cbq.answer()


@router.callback_query(AnalyticsCb.filter(F.action == "custom_users"))
async def custom_users_start(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return
    await state.clear()
    await state.set_state(AnalyticsCustomRange.waiting_date_from)
    await state.update_data(next_action="users")
    await cbq.message.answer("📅 Введите дату <b>С</b> в формате <code>ДД.ММ.ГГГГ</code>\nНапример: <code>03.03.2026</code>")
    await cbq.answer()


@router.message(AnalyticsCustomRange.waiting_date_from)
async def custom_date_from_input(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _is_admin(message.from_user.id, settings):
        return

    text = (message.text or "").strip()
    if text.lower() in {"отмена", "/cancel"}:
        await state.clear()
        await message.answer("Ок", reply_markup=admin_main_kb())
        return

    d_from = _parse_input_date(text)
    if not d_from:
        await message.answer("⚠️ Неверная дата. Введите в формате <code>ДД.ММ.ГГГГ</code>.\nНапример: <code>03.03.2026</code>")
        return

    await state.update_data(date_from=_date_to_key(d_from))
    await state.set_state(AnalyticsCustomRange.waiting_date_to)
    await message.answer("📅 Теперь введите дату <b>ПО</b> в формате <code>ДД.ММ.ГГГГ</code>\nНапример: <code>15.03.2026</code>")


@router.message(AnalyticsCustomRange.waiting_date_to)
async def custom_date_to_input(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _is_admin(message.from_user.id, settings):
        return

    text = (message.text or "").strip()
    if text.lower() in {"отмена", "/cancel"}:
        await state.clear()
        await message.answer("Ок", reply_markup=admin_main_kb())
        return

    d_to = _parse_input_date(text)
    if not d_to:
        await message.answer("⚠️ Неверная дата. Введите в формате <code>ДД.ММ.ГГГГ</code>.\nНапример: <code>15.03.2026</code>")
        return

    data = await state.get_data()
    ds = str(data.get("date_from") or "")
    next_action = str(data.get("next_action") or "")

    if not ds:
        await state.clear()
        await message.answer("⚠️ Не удалось определить начальную дату. Начните заново.", reply_markup=admin_main_kb())
        return

    d_from = _key_to_date(ds)
    if d_to < d_from:
        await message.answer("⚠️ Дата <b>по</b> не может быть раньше даты <b>с</b>.")
        return

    de = _date_to_key(d_to)
    await state.clear()

    if next_action == "report":
        await _send_admin_report_message(message, settings, "c", ds, de)
        return

    if next_action == "broadcast":
        await _run_broadcast_reports(message, settings, "c", ds, de)
        return

    if next_action == "users":
        text_out, kb = await _build_users_page(settings, "c", 1, ds, de)
        await message.answer(text_out, reply_markup=kb)
        return

    await message.answer("⚠️ Не удалось определить действие.", reply_markup=admin_main_kb())


@router.callback_query(AnalyticsCb.filter(F.action == "noop"))
async def noop(cbq: CallbackQuery) -> None:
    await cbq.answer()


# -------------------- shared logic --------------------

async def _send_admin_report_message(
    message: Message,
    settings: Settings,
    period: str,
    ds: str = "",
    de: str = "",
) -> None:
    start, end = _resolve_period_range(period, ds, de)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    summary = await get_bot_summary(settings.db_path, start_iso, end_iso)
    file_path = await build_admin_report_xlsx(settings.db_path, start_iso, end_iso, period)

    await message.answer(_summary_text(summary, period, start, end, ds, de))
    await message.answer_document(
        FSInputFile(file_path),
        caption=_report_caption(period, start, end, ds, de),
    )

    try:
        os.remove(file_path)
    except Exception:
        pass


async def _build_users_page(
    settings: Settings,
    period: str,
    page: int,
    ds: str = "",
    de: str = "",
) -> Tuple[str, Any]:
    start, end = _resolve_period_range(period, ds, de)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    total = await count_user_stats(settings.db_path, start_iso, end_iso)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(1, page), total_pages)

    items = await list_user_stats(
        settings.db_path,
        start_iso,
        end_iso,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )

    lines: List[str] = []
    lines.append(f"👥 <b>Пользователи за {_period_title(period)}</b>")
    lines.append(f"🕒 Период: <code>{_period_label(period, start, end, ds, de)}</code>")
    lines.append("")
    if not items:
        lines.append("Нет активности у пользователей за выбранный период.")
    else:
        base_idx = (page - 1) * PAGE_SIZE
        for i, u in enumerate(items, start=1):
            idx = base_idx + i
            name = html.escape(str(u.get("full_name") or "—"))
            tg_id = u.get("tg_id")
            inv_cnt = u.get("invoices_cnt", 0)
            reward_sum = _money(u.get("reward_sum", 0))
            deal_sum = _money(u.get("deal_sum", 0))
            kp_cnt = u.get("kp_items_cnt", 0)
            payouts_sum = _money(u.get("payout_sum", 0))

            lines.append(
                f"{idx}. <b>{name}</b> (<code>{tg_id}</code>)\n"
                f"   🧾 накладных: <b>{inv_cnt}</b> | 💰 вознаграждение: <b>{reward_sum}</b> ₽\n"
                f"   🤝 сделки: <b>{deal_sum}</b> ₽ | 🧩 КП позиций: <b>{kp_cnt}</b> | 💸 выплат: <b>{payouts_sum}</b> ₽"
            )

    kb = _users_nav_kb(period, page, total_pages, ds, de).as_markup()
    return "\n".join(lines), kb


async def _run_broadcast_reports(
    message: Message,
    settings: Settings,
    period: str,
    ds: str = "",
    de: str = "",
) -> None:
    start, end = _resolve_period_range(period, ds, de)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    user_ids = await list_active_user_ids(settings.db_path)
    if not user_ids:
        await message.answer("❗️Не нашёл пользователей со статусом <b>approved</b>.")
        return

    info_msg = await message.answer(
        f"📣 Начал рассылку <b>персональных отчетов</b> за {_period_title(period)}.\n"
        f"Получателей: <b>{len(user_ids)}</b>\n"
        f"Период: <code>{_period_label(period, start, end, ds, de)}</code>\n\n"
        f"⏳ Отправляю…"
    )

    ok = 0
    fail = 0
    failed_ids: List[int] = []

    for n, user_id in enumerate(user_ids, start=1):
        file_path: str | None = None
        try:
            file_path = await build_user_report_xlsx(settings.db_path, int(user_id), start_iso, end_iso, period)
            caption = _personal_report_caption(period, start, end, ds, de)
            await message.bot.send_document(
                chat_id=int(user_id),
                document=FSInputFile(file_path),
                caption=caption,
            )
            ok += 1
        except Exception:
            fail += 1
            failed_ids.append(int(user_id))
        finally:
            if file_path:
                try:
                    os.remove(file_path)
                except Exception:
                    pass

        if n % 10 == 0 or n == len(user_ids):
            try:
                await info_msg.edit_text(
                    f"📣 Рассылка персональных отчетов за {_period_title(period)}\n"
                    f"Прогресс: <b>{n}/{len(user_ids)}</b>\n"
                    f"✅ Успешно: <b>{ok}</b>\n"
                    f"❌ Ошибки: <b>{fail}</b>"
                )
            except Exception:
                pass

        await asyncio.sleep(0.05)

    if failed_ids:
        preview = ", ".join(str(x) for x in failed_ids[:20])
        more = "" if len(failed_ids) <= 20 else f" …и еще {len(failed_ids) - 20}"
        await message.answer(
            "⚠️ <b>Не удалось отправить отчет</b> пользователям (часто это значит, что бот заблокирован):\n"
            f"<code>{preview}</code>{more}"
        )

    await message.answer(
        f"✅ <b>Рассылка завершена</b>\n"
        f"Получателей: <b>{len(user_ids)}</b>\n"
        f"Успешно: <b>{ok}</b>\n"
        f"Ошибки: <b>{fail}</b>"
    )


# -------------------- report (admin) --------------------

@router.callback_query(AnalyticsCb.filter(F.action == "report"))
async def make_admin_report(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return

    data = AnalyticsCb.unpack(cbq.data)  # type: ignore
    await cbq.answer("Собираю отчет…", show_alert=False)
    await _send_admin_report_message(cbq.message, settings, data.period, data.ds, data.de)


# -------------------- users list (pagination) --------------------

@router.callback_query(AnalyticsCb.filter(F.action == "users"))
async def users_list(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return

    data = AnalyticsCb.unpack(cbq.data)  # type: ignore
    text_out, kb = await _build_users_page(settings, data.period, max(1, int(data.page)), data.ds, data.de)

    await cbq.message.edit_text(text_out, reply_markup=kb)
    await cbq.answer()


# -------------------- broadcast --------------------

@router.callback_query(AnalyticsCb.filter(F.action == "broadcast"))
async def broadcast_reports(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _cbq_admin_guard(cbq, settings):
        await cbq.answer("Доступ запрещен", show_alert=True)
        return

    data = AnalyticsCb.unpack(cbq.data)  # type: ignore
    await cbq.answer("Запускаю рассылку…", show_alert=False)
    await _run_broadcast_reports(cbq.message, settings, data.period, data.ds, data.de)