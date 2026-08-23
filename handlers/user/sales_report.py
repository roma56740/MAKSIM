from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db.analytics import get_user_sales_summary
from filters.admin import NotAdmin
from keyboards.user import user_back_cancel_kb, user_main_kb
from services.analytics_report import build_user_report_xlsx

router = Router()
router.message.filter(NotAdmin())
router.callback_query.filter(NotAdmin())
TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Moscow"))


class UserReportRange(StatesGroup):
    date_from = State()
    date_to = State()


def _period_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 За день", callback_data="ureport:period:d")
    kb.button(text="🗓 За неделю", callback_data="ureport:period:w")
    kb.button(text="🗓 За месяц", callback_data="ureport:period:m")
    kb.button(text="🗂 Выбрать даты", callback_data="ureport:custom")
    kb.adjust(1)
    return kb.as_markup()


def _range(period: str) -> tuple[datetime, datetime, str]:
    end = datetime.now(timezone.utc)
    days = {"d": 1, "w": 7, "m": 30}.get(period, 30)
    label = {"d": "за последние сутки", "w": "за последние 7 дней", "m": "за последние 30 дней"}.get(period, "за период")
    return end - timedelta(days=days), end, label


def _parse_date(value: str) -> date | None:
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((value or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _money(value) -> str:
    try:
        return f"{float(value or 0):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return "0,00"


def _summary_text(summary: dict, label: str) -> str:
    return (
        f"📊 <b>Ваши продажи {label}</b>\n\n"
        f"🧾 Накладных: <b>{int(summary.get('total') or 0)}</b>\n"
        f"• принято: <b>{int(summary.get('approved') or 0)}</b>\n"
        f"• на проверке: <b>{int(summary.get('pending') or 0)}</b>\n"
        f"• отклонено: <b>{int(summary.get('rejected') or 0)}</b>\n\n"
        f"💰 Сумма продаж: <b>{_money(summary.get('deal_sum'))}</b> ₽\n"
        f"🎁 Вознаграждение: <b>{_money(summary.get('reward_sum'))}</b> ₽\n"
        f"📦 Продано единиц: <b>{_money(summary.get('items_qty'))}</b>\n"
        f"🧩 Товарных позиций: <b>{int(summary.get('item_lines') or 0)}</b>\n"
        f"💳 Выплачено: <b>{_money(summary.get('paid_sum'))}</b> ₽\n"
        f"⏳ Выплаты в ожидании: <b>{_money(summary.get('pending_payout_sum'))}</b> ₽"
    )


async def _send_report(
    message: Message,
    settings: Settings,
    user_id: int,
    start: datetime,
    end: datetime,
    label: str,
    period: str,
) -> None:
    start_iso = start.astimezone(timezone.utc).isoformat(timespec="seconds")
    end_iso = end.astimezone(timezone.utc).isoformat(timespec="seconds")
    summary = await get_user_sales_summary(settings.db_path, user_id, start_iso, end_iso)
    await message.answer(_summary_text(summary, label))

    status = await message.answer("⏳ Формирую подробный Excel-отчёт…")
    path = ""
    try:
        path = await build_user_report_xlsx(settings.db_path, user_id, start_iso, end_iso, period)
        await message.answer_document(
            FSInputFile(path, filename=f"sales_report_{start:%Y%m%d}_{end:%Y%m%d}.xlsx"),
            caption="📄 Подробный отчёт: накладные, товары, суммы, вознаграждения и выплаты.",
            reply_markup=user_main_kb(),
        )
        await status.delete()
    except Exception:
        await status.edit_text("⚠️ Не удалось сформировать Excel. Краткая сводка показана выше.")
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


@router.message(F.text == "📊 Мой отчёт")
async def user_report_root(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "📊 <b>Отчёт по вашим продажам</b>\n\nВыберите период:",
        reply_markup=_period_kb(),
    )


@router.callback_query(F.data.startswith("ureport:period:"))
async def user_report_period(call: CallbackQuery, settings: Settings) -> None:
    period = call.data.rsplit(":", 1)[-1]
    start, end, label = _range(period)
    await call.answer("Собираю отчёт…")
    await _send_report(call.message, settings, call.from_user.id, start, end, label, period)


@router.callback_query(F.data == "ureport:custom")
async def user_report_custom(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(UserReportRange.date_from)
    await call.message.answer(
        "Введите начальную дату в формате <code>ДД.ММ.ГГГГ</code>:",
        reply_markup=user_back_cancel_kb(),
    )
    await call.answer()


@router.message(UserReportRange.date_from, F.text)
async def user_report_date_from(message: Message, state: FSMContext) -> None:
    if message.text in {"❌ Отмена", "⬅️ Назад"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=user_main_kb())
        return
    value = _parse_date(message.text or "")
    if not value:
        await message.answer("Неверная дата. Пример: <code>01.08.2026</code>")
        return
    await state.update_data(date_from=value.isoformat())
    await state.set_state(UserReportRange.date_to)
    await message.answer("Введите конечную дату в формате <code>ДД.ММ.ГГГГ</code>:")


@router.message(UserReportRange.date_to, F.text)
async def user_report_date_to(message: Message, state: FSMContext, settings: Settings) -> None:
    if message.text in {"❌ Отмена", "⬅️ Назад"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=user_main_kb())
        return
    value = _parse_date(message.text or "")
    if not value:
        await message.answer("Неверная дата. Пример: <code>31.08.2026</code>")
        return
    data = await state.get_data()
    start_date = date.fromisoformat(str(data["date_from"]))
    if value < start_date:
        await message.answer("Конечная дата не может быть раньше начальной.")
        return
    if (value - start_date).days > 366:
        await message.answer("Максимальный период отчёта — 366 дней.")
        return
    await state.clear()
    start = datetime.combine(start_date, time.min, tzinfo=TZ)
    end = datetime.combine(value + timedelta(days=1), time.min, tzinfo=TZ)
    label = f"за {start_date:%d.%m.%Y} — {value:%d.%m.%Y}"
    await _send_report(message, settings, message.from_user.id, start, end, label, "c")
