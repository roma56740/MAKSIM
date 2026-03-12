from __future__ import annotations

import re
from datetime import datetime, timezone

import aiosqlite
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Settings
from db import is_admin
from db.invoices import get_invoice_full, approve_invoice, reject_invoice


router = Router()


class AdminInvoice(StatesGroup):
    waiting_reward = State()
    waiting_reject_reason = State()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_amount(text: str) -> float | None:
    t = text.strip().replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    return float(t)


def _money(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(v)


async def _ensure_admin(cbq_or_msg, settings: Settings) -> bool:
    tg_id = cbq_or_msg.from_user.id
    return await is_admin(settings.db_path, tg_id, settings.admin_ids)


async def _set_invoice_pending(db_path: str, invoice_id: int) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE invoices
            SET
                status='pending',
                reward_amount=NULL,
                reason=NULL,
                updated_at=?
            WHERE id = ?
            """,
            (now, invoice_id),
        )
        await db.commit()


async def _send_invoice_to_user(message: Message, inv: dict, caption: str, reply_markup=None) -> None:
    if (inv.get("file_kind") or "").lower() == "photo":
        await message.bot.send_photo(
            int(inv["tg_id"]),
            inv["file_id"],
            caption=caption,
            reply_markup=reply_markup,
        )
    else:
        await message.bot.send_document(
            int(inv["tg_id"]),
            inv["file_id"],
            caption=caption,
            reply_markup=reply_markup,
        )


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
    await state.set_state(AdminInvoice.waiting_reward)
    await state.update_data(invoice_id=invoice_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="ainv:cancel")
    kb.adjust(1)

    await cbq.message.answer(
        "✅ <b>Принятие накладной</b>\n\n"
        f"🆔 <b>#{invoice_id}</b>\n"
        "🎁 Введите сумму <b>вознаграждения</b> (числом), которая будет начислена в баланс пользователя.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("ainv:reject:"))
async def admin_invoice_reject(cbq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
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
    await state.set_state(AdminInvoice.waiting_reject_reason)
    await state.update_data(invoice_id=invoice_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="ainv:cancel")
    kb.adjust(1)

    await cbq.message.answer(
        "❌ <b>Отклонение накладной</b>\n\n"
        f"🆔 <b>#{invoice_id}</b>\n"
        "🧾 Напишите <b>причину</b> отклонения (коротко и по делу).",
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
        caption = (
            "🟡 <b>Статус накладной изменён</b>\n\n"
            f"🆔 Номер: <b>#{invoice_id}</b>\n"
            "Накладная снова находится <b>на проверке</b>."
        )
        await _send_invoice_to_user(message=cbq.message, inv=inv, caption=caption)
    except Exception:
        pass


@router.callback_query(F.data == "ainv:cancel")
async def admin_invoice_cancel(cbq: CallbackQuery, state: FSMContext) -> None:
    await cbq.answer("Отменено")
    await state.clear()


@router.message(AdminInvoice.waiting_reward)
async def admin_reward_input(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await _ensure_admin(message, settings):
        return

    data = await state.get_data()
    invoice_id = int(data["invoice_id"])

    reward = _parse_amount(message.text or "")
    if reward is None:
        await message.answer("⚠️ Введите число (например <b>750</b> или <b>750,50</b>).")
        return

    await approve_invoice(settings.db_path, invoice_id, reward_amount=reward)
    inv = await get_invoice_full(settings.db_path, invoice_id)
    await state.clear()

    await message.answer(
        "✅ <b>Готово!</b>\n"
        f"Накладная <b>#{invoice_id}</b> принята.\n"
        f"🎁 Начислено: <b>{_money(reward)}</b>"
    )

    if inv:
        try:
            caption = (
                "✅ <b>Ваша накладная принята!</b>\n\n"
                f"🆔 Номер: <b>#{invoice_id}</b>\n"
                f"🎁 Вознаграждение: <b>{_money(reward)}</b>\n\n"
                "Баланс обновлён 💰\n\n"
                "Если есть расхождение, нажмите кнопку ниже."
            )

            kb = InlineKeyboardBuilder()
            kb.button(text="✍️ Попросить пересчёт", callback_data=f"uinv:recheck:{invoice_id}")
            kb.adjust(1)

            await _send_invoice_to_user(
                message=message,
                inv=inv,
                caption=caption,
                reply_markup=kb.as_markup(),
            )
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
        f"🧾 Причина: <b>{reason}</b>"
    )

    if inv:
        try:
            caption = (
                "❌ <b>Ваша накладная отклонена</b>\n\n"
                f"🆔 Номер: <b>#{invoice_id}</b>\n"
                f"🧾 Причина: <b>{reason}</b>\n\n"
                "Вы можете отправить новую накладную в разделе «🧾 Накладные → Новая»."
            )

            await _send_invoice_to_user(message=message, inv=inv, caption=caption)
        except Exception:
            pass