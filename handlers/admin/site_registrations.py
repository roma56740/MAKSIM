from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message
from pathlib import Path

from config import Settings
from db import is_admin
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
    path = Path(settings.site_registrations_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("[]", encoding="utf-8")
    await message.answer_document(
        FSInputFile(path, filename="site_registrations.json"),
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
