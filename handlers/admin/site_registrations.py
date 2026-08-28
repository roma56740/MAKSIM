from __future__ import annotations

import json
from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pathlib import Path

from config import Settings
from db import is_admin, list_db_admin_ids
from db.site_registrations import get_site_registration, list_site_registrations, set_site_registration_status
from services.site_registrations import (
    registration_from_message,
    save_registration_decision,
    send_registration_decision,
)


router = Router()


@router.message(F.text == "🌐 Регистрации сайта")
async def send_site_registrations_file(message: Message, settings: Settings) -> None:
    if not await is_admin(settings.db_path, message.from_user.id, settings.admin_ids):
        await message.answer("Нет доступа")
        return
    rows = await list_site_registrations(settings.db_path)
    path = Path(settings.site_registrations_path)
    if path.exists():
        try:
            legacy_rows = json.loads(path.read_text(encoding="utf-8"))
            known_ids = {str(row.get("id", "")) for row in rows}
            if isinstance(legacy_rows, list):
                rows.extend(
                    row for row in legacy_rows
                    if isinstance(row, dict) and str(row.get("id", "")) not in known_ids
                )
        except (OSError, json.JSONDecodeError):
            pass
    content = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
    await message.answer_document(
        BufferedInputFile(content, filename="site_registrations.json"),
        caption="🌐 <b>Регистрации на сайте</b>\nОтдельный накопительный файл со всеми решениями.",
    )


@router.callback_query(F.data.startswith("site_reg:"))
async def moderate_site_registration(call: CallbackQuery, settings: Settings) -> None:
    if not await is_admin(settings.db_path, call.from_user.id, settings.admin_ids):
        await call.answer("Нет доступа", show_alert=True)
        return

    parts = (call.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"approve", "reject"} or not parts[2]:
        await call.answer("Не удалось прочитать заявку", show_alert=True)
        return

    action, registration_id = parts[1], parts[2]
    registration = await get_site_registration(settings.db_path, registration_id)
    if registration is None and action == "approve":
        await call.answer("Регистрация не найдена в базе бота. Попросите клиента отправить форму ещё раз.", show_alert=True)
        return
    if registration is not None and action == "approve":
        telegram_id = int(registration.get("telegram_id") or 0)
        if telegram_id <= 0:
            await call.answer("В регистрации не указан Telegram ID", show_alert=True)
            return
        await set_site_registration_status(settings.db_path, registration_id, "awaiting_user", call.from_user.id)
        confirmation_markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, это я", callback_data=f"site_user:yes:{registration_id}"),
            InlineKeyboardButton(text="❌ Нет, не я", callback_data=f"site_user:no:{registration_id}"),
        ]])
        try:
            role_label = "менеджера" if str(registration.get("site_role") or "client") == "manager" else "клиента"
            await call.bot.send_message(
                telegram_id,
                "🪪 <b>Подтверждение регистрации</b>\n\n"
                f"Вы подавали заявку на доступ {role_label} к сайту «ГУДВИН КОНСАЛТИНГ»?",
                reply_markup=confirmation_markup,
            )
        except Exception:
            await set_site_registration_status(settings.db_path, registration_id, "pending", call.from_user.id)
            await call.answer(
                "Не удалось написать пользователю. Он должен сначала открыть бота и нажать START.",
                show_alert=True,
            )
            return
        result_text = "✅ Данные проверены\n\nОжидаем подтверждение пользователя в Telegram."
        if call.message:
            try:
                await call.message.edit_text(f"{call.message.html_text}\n\n<b>{result_text}</b>", reply_markup=None)
            except Exception:
                await call.message.answer(result_text)
        await call.answer("Подтверждение отправлено пользователю")
        return

    status = "rejected"
    updated = await set_site_registration_status(settings.db_path, registration_id, status, call.from_user.id)
    if updated is None:
        source = registration_from_message(call.message.text if call.message else "")
        await save_registration_decision(
            settings.site_registrations_path,
            registration_id,
            "rejected",
            call.from_user.id,
            source,
        )
    delivered = await send_registration_decision(
        settings.site_registration_hook_url,
        settings.site_registration_secret,
        registration_id,
        "reject",
        call.from_user.id,
    )

    result_text = "❌ Регистрация отклонена"
    if delivered is False:
        result_text += "\n\nЗапись сохранена. Повторная передача на сайт произойдёт после проверки связи."

    if call.message:
        try:
            await call.message.edit_text(f"{call.message.html_text}\n\n<b>{result_text}</b>", reply_markup=None)
        except Exception:
            await call.message.answer(result_text)
    await call.answer("Готово")


@router.callback_query(F.data.startswith("site_user:"))
async def confirm_site_registration_owner(call: CallbackQuery, settings: Settings) -> None:
    parts = (call.data or "").split(":", 2)
    if len(parts) != 3 or parts[1] not in {"yes", "no"} or not parts[2]:
        await call.answer("Не удалось прочитать подтверждение", show_alert=True)
        return

    action, registration_id = parts[1], parts[2]
    registration = await get_site_registration(settings.db_path, registration_id)
    if registration is None:
        await call.answer("Регистрация не найдена", show_alert=True)
        return
    if int(registration.get("telegram_id") or 0) != call.from_user.id:
        await call.answer("Это подтверждение предназначено другому пользователю", show_alert=True)
        return
    if str(registration.get("status") or "") != "awaiting_user":
        await call.answer("Это подтверждение уже обработано", show_alert=True)
        return

    status = "approved" if action == "yes" else "rejected"
    await set_site_registration_status(settings.db_path, registration_id, status, call.from_user.id)
    await send_registration_decision(
        settings.site_registration_hook_url,
        settings.site_registration_secret,
        registration_id,
        "approve" if action == "yes" else "reject",
        call.from_user.id,
    )

    if action == "yes":
        text = "✅ <b>Регистрация подтверждена</b>\n\nДоступ к сайту открыт. Вернитесь на сайт — страница обновится автоматически."
        admin_text = f"✅ Пользователь подтвердил регистрацию\nID: <code>{registration_id}</code>"
    else:
        text = "❌ Регистрация отменена. Доступ к сайту не открыт."
        admin_text = f"❌ Пользователь не подтвердил регистрацию\nID: <code>{registration_id}</code>"

    if call.message:
        try:
            await call.message.edit_text(text, reply_markup=None)
        except Exception:
            await call.message.answer(text)
    admin_ids = set(settings.admin_ids)
    admin_ids.update(await list_db_admin_ids(settings.db_path))
    for admin_id in sorted(admin_ids):
        try:
            await call.bot.send_message(admin_id, admin_text)
        except Exception:
            pass
    await call.answer("Готово")
