from __future__ import annotations

import asyncio
from datetime import datetime
from io import BytesIO

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from openpyxl import Workbook

from callbacks import BroadcastCb
from config import Settings
from db import is_admin, list_user_ids
from keyboards.admin import admin_back_cancel_kb, admin_main_kb


router = Router()

PHOTO_CAPTION_LIMIT = 1000
DOCUMENT_CAPTION_LIMIT = 1000
TEXT_MESSAGE_LIMIT = 3900


class BroadcastForm(StatesGroup):
    content = State()
    confirm = State()


def _split_long_text(text: str, limit: int = TEXT_MESSAGE_LIMIT) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []

    chunks: list[str] = []
    current = ""

    for part in text.split("\n"):
        part = part.strip()

        if len(part) > limit:
            if current:
                chunks.append(current)
                current = ""

            for i in range(0, len(part), limit):
                chunk = part[i:i + limit].strip()
                if chunk:
                    chunks.append(chunk)
            continue

        candidate = part if not current else f"{current}\n{part}"

        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = part

    if current:
        chunks.append(current)

    return chunks


async def _answer_text_safely(message: Message, text: str) -> None:
    for chunk in _split_long_text(text):
        await message.answer(chunk)


async def _bot_send_text_safely(bot, chat_id: int, text: str) -> None:
    chunks = _split_long_text(text)
    if not chunks:
        return

    for chunk in chunks:
        await bot.send_message(chat_id=chat_id, text=chunk)


async def _answer_photo_with_safe_caption(
    message: Message,
    photo_file_id: str,
    caption: str | None = None,
) -> None:
    caption = (caption or "").strip()

    if len(caption) <= PHOTO_CAPTION_LIMIT:
        await message.answer_photo(photo_file_id, caption=caption or None)
        return

    await message.answer_photo(photo_file_id, caption="🖼 Фото для рассылки")
    await _answer_text_safely(message, caption)


async def _answer_document_with_safe_caption(
    message: Message,
    document_file_id: str,
    caption: str | None = None,
) -> None:
    caption = (caption or "").strip()

    if len(caption) <= DOCUMENT_CAPTION_LIMIT:
        await message.answer_document(document_file_id, caption=caption or None)
        return

    await message.answer_document(document_file_id, caption="📎 Файл для рассылки")
    await _answer_text_safely(message, caption)


async def _bot_send_photo_with_safe_caption(
    bot,
    chat_id: int,
    photo_file_id: str,
    caption: str | None = None,
) -> None:
    caption = (caption or "").strip()

    if len(caption) <= PHOTO_CAPTION_LIMIT:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file_id,
            caption=caption or None,
        )
        return

    await bot.send_photo(
        chat_id=chat_id,
        photo=photo_file_id,
        caption="🖼 Фото",
    )
    await _bot_send_text_safely(bot, chat_id, caption)


async def _bot_send_document_with_safe_caption(
    bot,
    chat_id: int,
    document_file_id: str,
    caption: str | None = None,
) -> None:
    caption = (caption or "").strip()

    if len(caption) <= DOCUMENT_CAPTION_LIMIT:
        await bot.send_document(
            chat_id=chat_id,
            document=document_file_id,
            caption=caption or None,
        )
        return

    await bot.send_document(
        chat_id=chat_id,
        document=document_file_id,
        caption="📎 Файл",
    )
    await _bot_send_text_safely(bot, chat_id, caption)


def _confirm_kb():
    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ Отправить", callback_data=BroadcastCb(action="send").pack()),
        InlineKeyboardButton(text="❌ Отмена", callback_data=BroadcastCb(action="cancel").pack()),
    )
    return kb.as_markup()


def _fit_sheet_columns(ws) -> None:
    for column_cells in ws.columns:
        max_len = 0
        column_letter = column_cells[0].column_letter

        for cell in column_cells:
            value = "" if cell.value is None else str(cell.value)
            if len(value) > max_len:
                max_len = len(value)

        ws.column_dimensions[column_letter].width = min(max(max_len + 2, 12), 80)


def _build_broadcast_report(
    success_ids: list[int],
    failed_rows: list[tuple[int, str]],
) -> BufferedInputFile:
    wb = Workbook()

    ws_ok = wb.active
    ws_ok.title = "Получили"
    ws_ok.append(["tg_id", "status"])
    for uid in success_ids:
        ws_ok.append([uid, "sent"])
    _fit_sheet_columns(ws_ok)

    ws_fail = wb.create_sheet("Не получили")
    ws_fail.append(["tg_id", "error"])
    for uid, error_text in failed_rows:
        ws_fail.append([uid, error_text])
    _fit_sheet_columns(ws_fail)

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"broadcast_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return BufferedInputFile(output.getvalue(), filename=filename)


@router.message(F.text == "📢 Рассылка")
async def broadcast_start(message: Message, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        return

    await state.clear()
    await state.set_state(BroadcastForm.content)

    await message.answer(
        "📢 <b>Рассылка всем пользователям</b>\n\n"
        "Отправьте <b>одно</b> сообщение:\n"
        "• текст\n"
        "• фото (можно с подписью)\n"
        "• файл (можно с подписью)\n\n"
        "Управление: «⬅️ Назад» / «❌ Отмена»",
        reply_markup=admin_back_cancel_kb(),
    )


@router.message(BroadcastForm.content, F.text == "❌ Отмена")
@router.message(BroadcastForm.confirm, F.text == "❌ Отмена")
async def broadcast_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Отменено", reply_markup=admin_main_kb())


@router.message(BroadcastForm.content, F.text == "⬅️ Назад")
@router.message(BroadcastForm.confirm, F.text == "⬅️ Назад")
async def broadcast_back(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())


@router.message(BroadcastForm.content, F.photo)
async def broadcast_take_photo(message: Message, state: FSMContext) -> None:
    await state.update_data(
        kind="photo",
        file_id=message.photo[-1].file_id,
        text=message.caption or "",
    )

    await message.answer("👁 <b>Предпросмотр</b>:")
    await _answer_photo_with_safe_caption(
        message,
        message.photo[-1].file_id,
        message.caption or "",
    )
    await message.answer("Подтвердите отправку:", reply_markup=_confirm_kb())

    await state.set_state(BroadcastForm.confirm)


@router.message(BroadcastForm.content, F.document)
async def broadcast_take_document(message: Message, state: FSMContext) -> None:
    await state.update_data(
        kind="document",
        file_id=message.document.file_id,
        text=message.caption or "",
    )

    await message.answer("👁 <b>Предпросмотр</b>:")
    await _answer_document_with_safe_caption(
        message,
        message.document.file_id,
        message.caption or "",
    )
    await message.answer("Подтвердите отправку:", reply_markup=_confirm_kb())

    await state.set_state(BroadcastForm.confirm)


@router.message(BroadcastForm.content, F.text)
async def broadcast_take_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте текст/фото/файл.")
        return

    await state.update_data(kind="text", file_id=None, text=text)

    await message.answer("👁 <b>Предпросмотр</b>:")
    await _answer_text_safely(message, text)
    await message.answer("Подтвердите отправку:", reply_markup=_confirm_kb())

    await state.set_state(BroadcastForm.confirm)


@router.message(BroadcastForm.content)
async def broadcast_invalid(message: Message) -> None:
    await message.answer("Отправьте текст/фото/файл одним сообщением.")


@router.callback_query(BroadcastCb.filter(F.action == "cancel"))
async def broadcast_cancel_cb(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass

    await call.message.answer("❌ Отменено", reply_markup=admin_main_kb())
    await call.answer()


@router.callback_query(BroadcastCb.filter(F.action == "send"))
async def broadcast_send(call: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    data = await state.get_data()
    kind = data.get("kind")
    file_id = data.get("file_id")
    text = data.get("text") or ""

    user_ids = await list_user_ids(settings.db_path, exclude_statuses=("blocked",))

    ok = 0
    fail = 0
    success_ids: list[int] = []
    failed_rows: list[tuple[int, str]] = []

    status_msg = await call.message.answer(
        f"⏳ Отправляю… получателей: <b>{len(user_ids)}</b>"
    )

    for uid in user_ids:
        try:
            if kind == "text":
                await _bot_send_text_safely(call.bot, uid, text)

            elif kind == "photo":
                await _bot_send_photo_with_safe_caption(
                    call.bot,
                    uid,
                    file_id,
                    text,
                )

            elif kind == "document":
                await _bot_send_document_with_safe_caption(
                    call.bot,
                    uid,
                    file_id,
                    text,
                )

            else:
                fail += 1
                failed_rows.append((uid, f"unsupported kind: {kind}"))
                await asyncio.sleep(0.05)
                continue

            ok += 1
            success_ids.append(uid)

        except Exception as e:
            fail += 1
            failed_rows.append((uid, str(e)[:500]))

        await asyncio.sleep(0.05)

    await state.clear()

    try:
        await call.message.delete()
    except Exception:
        pass

    await status_msg.edit_text(
        "✅ <b>Рассылка завершена</b>\n\n"
        f"Успешно: <b>{ok}</b>\n"
        f"Ошибки: <b>{fail}</b>",
    )

    try:
        report_file = _build_broadcast_report(success_ids, failed_rows)

        await call.bot.send_document(
            chat_id=call.message.chat.id,
            document=report_file,
            caption=(
                "📄 <b>Отчёт по рассылке</b>\n\n"
                f"Получили: <b>{ok}</b>\n"
                f"Не получили: <b>{fail}</b>"
            ),
        )
    except Exception as e:
        await call.bot.send_message(
            chat_id=call.message.chat.id,
            text=(
                "⚠️ Не удалось сформировать Excel-отчёт.\n"
                f"Ошибка: <code>{str(e)[:500]}</code>"
            ),
        )

    await call.bot.send_message(
        chat_id=call.message.chat.id,
        text="🛠 <b>Админ-панель</b>",
        reply_markup=admin_main_kb(),
    )
    await call.answer()