from __future__ import annotations

import json
from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from pathlib import Path

from config import Settings
from db import is_admin
from db.site_registrations import list_site_registrations, set_site_registration_status
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
    status = "approved" if action == "approve" else "rejected"
    updated = await set_site_registration_status(
        settings.db_path,
        registration_id,
        status,
        call.from_user.id,
    )
    if updated is None:
        source = registration_from_message(call.message.text if call.message else "")
        await save_registration_decision(
            settings.site_registrations_path,
            registration_id,
            status,
            call.from_user.id,
            source,
        )
    delivered = await send_registration_decision(
        settings.site_registration_hook_url,
        settings.site_registration_secret,
        registration_id,
        action,
        call.from_user.id,
    )

    result_text = "✅ Доступ к сайту открыт" if status == "approved" else "❌ Регистрация отклонена"
    if delivered is False:
        result_text += "\n\nЗапись сохранена. Повторная передача на сайт произойдёт после проверки связи."

    if call.message:
        try:
            await call.message.edit_text(f"{call.message.html_text}\n\n<b>{result_text}</b>", reply_markup=None)
        except Exception:
            await call.message.answer(result_text)
    await call.answer("Готово")
