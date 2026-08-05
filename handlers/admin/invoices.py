from __future__ import annotations

import html
import re
from datetime import datetime, timezone

import aiosqlite
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import is_admin
from db.invoices import (
    approve_invoice,
    decode_invoice_analysis,
    get_invoice_full,
    reject_invoice,
    save_invoice_analysis,
)
from services.invoice_recognition import InvoiceRecognitionError
from services.invoice_workflow import (
    analysis_header,
    analyze_invoice_from_telegram,
    edit_template,
    money,
    split_item_messages,
)


router = Router()


class AdminInvoice(StatesGroup):
    waiting_items = State()
    waiting_reward_percent = State()
    waiting_reject_reason = State()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_amount(text: str) -> float | None:
    t = text.strip().replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    return float(t)


def _parse_invoice_items(text: str) -> tuple[list[dict], str | None]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return [], "Добавьте хотя бы одну товарную позицию."
    if len(lines) > 150:
        return [], "В одной накладной можно указать не более 150 позиций."

    items: list[dict] = []
    for index, raw_line in enumerate(lines, start=1):
        line = re.sub(r"^\s*\d+[.)]\s*", "", raw_line)
        parts = [part.strip() for part in re.split(r"[|;]", line)]
        if len(parts) != 3:
            return [], (
                f"Строка {index} заполнена неверно. Используйте формат: "
                "Название | количество | цена"
            )
        product_name, quantity_text, price_text = parts
        quantity = _parse_amount(quantity_text)
        unit_price = _parse_amount(price_text)
        if len(product_name) < 2:
            return [], f"В строке {index} не указано название товара."
        if quantity is None or quantity <= 0:
            return [], f"В строке {index} неверно указано количество."
        if unit_price is None or unit_price < 0:
            return [], f"В строке {index} неверно указана цена."
        items.append(
            {
                "article": None,
                "product_name": product_name[:500],
                "quantity": quantity,
                "unit": None,
                "unit_price": unit_price,
                "line_total": round(quantity * unit_price, 2),
            }
        )
    return items, None


def _money(value) -> str:
    return money(value)


def _percent(value) -> str:
    if value is None:
        return "—"
    try:
        result = float(value)
        return f"{result:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    except Exception:
        return str(value)


def _calc_reward(deal_amount: float, reward_percent: float) -> float:
    return round(deal_amount * reward_percent / 100, 2)


async def _ensure_admin(cbq_or_msg, settings: Settings) -> bool:
    return await is_admin(settings.db_path, cbq_or_msg.from_user.id, settings.admin_ids)


async def _set_invoice_pending(db_path: str, invoice_id: int) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        await db.execute(
            """
            UPDATE invoices
            SET status='pending', deal_amount=NULL, reward_amount=NULL,
                reason=NULL, handled_at=NULL, updated_at=?
            WHERE id=?
            """,
            (now, invoice_id),
        )
        await db.commit()


async def _send_invoice_to_user(message: Message, inv: dict, caption: str, reply_markup=None) -> None:
    if (inv.get("file_kind") or "").lower() == "photo":
        await message.bot.send_photo(
            int(inv["tg_id"]), inv["file_id"], caption=caption, reply_markup=reply_markup
        )
    else:
        await message.bot.send_document(
            int(inv["tg_id"]), inv["file_id"], caption=caption, reply_markup=reply_markup
        )


def _review_kb(invoice_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Данные верны", callback_data=f"ainv:confirm:{invoice_id}")
    kb.button(text="✏️ Исправить позиции", callback_data=f"ainv:edit:{invoice_id}")
    kb.button(text="🔄 Распознать заново", callback_data=f"ainv:retry:{invoice_id}")
    kb.button(text="❌ Отклонить", callback_data=f"ainv:reject:{invoice_id}")
    kb.adjust(1, 1, 2)
    return kb


async def _show_review(message: Message, invoice_id: int, analysis: dict) -> None:
    await message.answer(analysis_header(analysis, invoice_id=invoice_id))
    for chunk in split_item_messages(analysis):
        await message.answer(chunk)
    await message.answer(
        "🔍 <b>Проверьте распознанные данные</b>\n\n"
        "Если всё совпадает с накладной — подтвердите. "
        "При необходимости можно исправить список товаров или запустить распознавание ещё раз.",
        reply_markup=_review_kb(invoice_id).as_markup(),
    )


async def _recognize_and_show(message: Message, settings: Settings, invoice_id: int) -> None:
    progress = await message.answer(
        "🔎 <b>Распознаю накладную…</b>\n\n"
        "Проверяю все страницы, товары, количество, цены и итог. Это может занять до минуты."
    )
    try:
        analysis = await analyze_invoice_from_telegram(message.bot, settings.db_path, invoice_id)
    except InvoiceRecognitionError as exc:
        try:
            await progress.edit_text(
                "⚠️ <b>Распознавание не завершено</b>\n\n"
                f"{html.escape(str(exc))}\n\n"
                "Можно попробовать ещё раз или исправить позиции вручную только как резервный вариант.",
                reply_markup=_review_kb(invoice_id).as_markup(),
            )
        except Exception:
            await message.answer(f"⚠️ {html.escape(str(exc))}")
        return

    try:
        await progress.edit_text("✅ <b>Документ распознан. Проверьте результат ниже.</b>")
    except Exception:
        pass
    await _show_review(message, invoice_id, analysis)


@router.callback_query(F.data.startswith("ainv:approve:"))
async def admin_invoice_approve(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_admin(cbq, settings):
        await cbq.answer("Нет доступа", show_alert=True)
        return

    invoice_id = int(cbq.data.split(":")[-1])
    inv = await get_invoice_full(settings.db_path, invoice_id)
    if not inv:
        await cbq.answer("Не найдено", show_alert=True)
        return

    await cbq.answer()
    await state.clear()
    analysis = decode_invoice_analysis(inv)
    if str(inv.get("analysis_status") or "") == "completed" and analysis and analysis.get("items"):
        await _show_review(cbq.message, invoice_id, analysis)
        return
    await _recognize_and_show(cbq.message, settings, invoice_id)


@router.callback_query(F.data.startswith("ainv:retry:"))
async def admin_invoice_retry(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_admin(cbq, settings):
        await cbq.answer("Нет доступа", show_alert=True)
        return
    invoice_id = int(cbq.data.split(":")[-1])
    if not await get_invoice_full(settings.db_path, invoice_id):
        await cbq.answer("Не найдено", show_alert=True)
        return
    await cbq.answer("Запускаю распознавание")
    await state.clear()
    await _recognize_and_show(cbq.message, settings, invoice_id)


@router.callback_query(F.data.startswith("ainv:confirm:"))
async def admin_invoice_confirm(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_admin(cbq, settings):
        await cbq.answer("Нет доступа", show_alert=True)
        return

    invoice_id = int(cbq.data.split(":")[-1])
    inv = await get_invoice_full(settings.db_path, invoice_id)
    analysis = decode_invoice_analysis(inv)
    items = list((analysis or {}).get("items") or [])
    if not inv or not items:
        await cbq.answer("Сначала распознайте позиции", show_alert=True)
        return

    deal_amount = round(sum(float(item["quantity"]) * float(item["unit_price"]) for item in items), 2)
    total_qty = sum(float(item["quantity"]) for item in items)

    await cbq.answer()
    await state.clear()
    await state.set_state(AdminInvoice.waiting_reward_percent)
    await state.update_data(invoice_id=invoice_id, deal_amount=deal_amount, items=items)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="ainv:cancel")
    kb.adjust(1)

    await cbq.message.answer(
        "📈 <b>Процент вознаграждения</b>\n\n"
        f"🆔 Накладная: <b>#{invoice_id}</b>\n"
        f"📦 Позиций: <b>{len(items)}</b>\n"
        f"🔢 Количество товаров: <b>{_percent(total_qty)}</b>\n"
        f"💰 Сумма продаж: <b>{_money(deal_amount)}</b> ₽\n\n"
        "Введите процент числом. Например: <code>5</code> или <code>7,5</code>.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("ainv:edit:"))
async def admin_invoice_edit(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_admin(cbq, settings):
        await cbq.answer("Нет доступа", show_alert=True)
        return

    invoice_id = int(cbq.data.split(":")[-1])
    inv = await get_invoice_full(settings.db_path, invoice_id)
    if not inv:
        await cbq.answer("Не найдено", show_alert=True)
        return

    analysis = decode_invoice_analysis(inv) or {"items": []}
    template = edit_template(analysis)

    await cbq.answer()
    await state.clear()
    await state.set_state(AdminInvoice.waiting_items)
    await state.update_data(invoice_id=invoice_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="ainv:cancel")
    kb.adjust(1)

    await cbq.message.answer(
        "✏️ <b>Редактирование распознанных позиций</b>\n\n"
        "Отправьте исправленный список. Каждая позиция — с новой строки:\n"
        "<code>Название | количество | цена за единицу</code>\n\n"
        "Итог будет пересчитан автоматически. Можно изменить только ошибочные строки, "
        "но в ответе должен остаться полный список товаров.",
        reply_markup=kb.as_markup(),
    )

    if template:
        escaped = html.escape(template)
        if len(escaped) <= 3400:
            await cbq.message.answer(f"<b>Текущие данные для копирования:</b>\n\n<code>{escaped}</code>")
        else:
            await cbq.message.answer_document(
                BufferedInputFile(template.encode("utf-8"), filename=f"invoice_{invoice_id}_positions.txt"),
                caption="📄 Текущий список позиций. Скопируйте, исправьте и отправьте текстом в чат.",
            )


@router.callback_query(F.data.startswith("ainv:reject:"))
async def admin_invoice_reject(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_admin(cbq, settings):
        await cbq.answer("Нет доступа", show_alert=True)
        return

    invoice_id = int(cbq.data.split(":")[-1])
    if not await get_invoice_full(settings.db_path, invoice_id):
        await cbq.answer("Не найдено", show_alert=True)
        return

    await cbq.answer()
    await state.clear()
    await state.set_state(AdminInvoice.waiting_reject_reason)
    await state.update_data(invoice_id=invoice_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="ainv:cancel")
    kb.adjust(1)
    await cbq.message.answer(
        "❌ <b>Отклонение накладной</b>\n\n"
        f"🆔 <b>#{invoice_id}</b>\n"
        "Напишите короткую причину отклонения.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("ainv:pending:"))
async def admin_invoice_pending(cbq: CallbackQuery, settings: Settings) -> None:
    if not await _ensure_admin(cbq, settings):
        await cbq.answer("Нет доступа", show_alert=True)
        return

    invoice_id = int(cbq.data.split(":")[-1])
    inv = await get_invoice_full(settings.db_path, invoice_id)
    if not inv:
        await cbq.answer("Не найдено", show_alert=True)
        return

    await _set_invoice_pending(settings.db_path, invoice_id)
    await cbq.answer("Возвращено на проверку")
    await cbq.message.answer(
        "🟡 <b>Готово!</b>\n"
        f"Накладная <b>#{invoice_id}</b> возвращена в статус <b>На проверке</b>."
    )

    try:
        await _send_invoice_to_user(
            cbq.message,
            inv,
            "🟡 <b>Статус накладной изменён</b>\n\n"
            f"🆔 Номер: <b>#{invoice_id}</b>\n"
            "Накладная снова находится <b>на проверке</b>.",
        )
    except Exception:
        pass


@router.callback_query(F.data == "ainv:cancel")
async def admin_invoice_cancel(cbq: CallbackQuery, state: FSMContext) -> None:
    await cbq.answer("Отменено")
    await state.clear()


@router.message(AdminInvoice.waiting_items)
async def admin_invoice_items_input(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_admin(message, settings):
        return

    items, error = _parse_invoice_items(message.text or "")
    if error:
        await message.answer(f"⚠️ {error}")
        return

    data = await state.get_data()
    invoice_id = int(data["invoice_id"])
    inv = await get_invoice_full(settings.db_path, invoice_id)
    analysis = decode_invoice_analysis(inv) or {
        "document_type": "Накладная / счёт",
        "invoice_number": None,
        "invoice_date": None,
        "supplier": None,
        "buyer": None,
        "responsible_manager": None,
        "currency": "RUB",
        "vat_amount": None,
        "warnings": [],
    }
    deal_amount = round(sum(float(item["line_total"]) for item in items), 2)
    analysis.update(
        {
            "items": items,
            "total_amount": deal_amount,
            "calculated_total": deal_amount,
            "confidence": 1.0,
            "warnings": [
                *[str(x) for x in analysis.get("warnings") or [] if str(x).strip()],
                "Товарные позиции проверены и отредактированы администратором.",
            ],
        }
    )
    analysis["warnings"] = list(dict.fromkeys(analysis["warnings"]))
    await save_invoice_analysis(settings.db_path, invoice_id, analysis)
    await state.clear()

    await message.answer(
        "✅ <b>Изменения сохранены</b>\n\n"
        f"📦 Позиций: <b>{len(items)}</b>\n"
        f"💰 Новый итог: <b>{_money(deal_amount)}</b> ₽"
    )
    await _show_review(message, invoice_id, analysis)


@router.message(AdminInvoice.waiting_reward_percent)
async def admin_reward_percent_input(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_admin(message, settings):
        return

    data = await state.get_data()
    invoice_id = int(data["invoice_id"])
    deal_amount = float(data["deal_amount"])
    items = list(data.get("items") or [])

    reward_percent = _parse_amount(message.text or "")
    if reward_percent is None or reward_percent < 0 or reward_percent > 100:
        await message.answer("⚠️ Введите процент от 0 до 100. Например: <b>5</b> или <b>7,5</b>.")
        return

    reward = _calc_reward(deal_amount, reward_percent)
    await approve_invoice(
        settings.db_path,
        invoice_id,
        deal_amount=deal_amount,
        reward_amount=reward,
        items=items,
    )
    inv = await get_invoice_full(settings.db_path, invoice_id)
    await state.clear()

    await message.answer(
        "✅ <b>Готово!</b>\n"
        f"Накладная <b>#{invoice_id}</b> принята.\n\n"
        f"📦 Товарных позиций: <b>{len(items)}</b>\n"
        f"💰 Сумма продаж: <b>{_money(deal_amount)}</b> ₽\n"
        f"📈 Процент: <b>{_percent(reward_percent)}</b>%\n"
        f"🎁 Начислено: <b>{_money(reward)}</b> ₽"
    )

    if inv:
        try:
            caption = (
                "✅ <b>Ваша накладная принята!</b>\n\n"
                f"🆔 Номер: <b>#{invoice_id}</b>\n"
                f"📦 Товарных позиций: <b>{len(items)}</b>\n"
                f"💰 Сумма продаж: <b>{_money(deal_amount)}</b> ₽\n"
                f"📈 Процент: <b>{_percent(reward_percent)}</b>%\n"
                f"🎁 Вознаграждение: <b>{_money(reward)}</b> ₽\n\n"
                "Баланс обновлён 💰\n\n"
                "Если есть расхождение, нажмите кнопку ниже."
            )
            kb = InlineKeyboardBuilder()
            kb.button(text="✍️ Попросить пересчёт", callback_data=f"uinv:recheck:{invoice_id}")
            kb.adjust(1)
            await _send_invoice_to_user(message, inv, caption, kb.as_markup())
        except Exception:
            pass


@router.message(AdminInvoice.waiting_reject_reason)
async def admin_reject_reason_input(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_admin(message, settings):
        return

    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("⚠️ Причина слишком короткая. Напишите пару слов.")
        return

    data = await state.get_data()
    invoice_id = int(data["invoice_id"])
    await reject_invoice(settings.db_path, invoice_id, reason=reason)
    inv = await get_invoice_full(settings.db_path, invoice_id)
    await state.clear()

    await message.answer(
        f"❌ Накладная <b>#{invoice_id}</b> отклонена.\n"
        f"🧾 Причина: <b>{html.escape(reason)}</b>"
    )

    if inv:
        try:
            await _send_invoice_to_user(
                message,
                inv,
                "❌ <b>Ваша накладная отклонена</b>\n\n"
                f"🆔 Номер: <b>#{invoice_id}</b>\n"
                f"🧾 Причина: <b>{html.escape(reason)}</b>\n\n"
                "Вы можете отправить новую накладную в разделе «🧾 Накладные → Новая».",
            )
        except Exception:
            pass
