# handlers/admin/db_export.py
from __future__ import annotations

import datetime
from pathlib import Path
from shutil import copy2

from aiogram import Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import FSInputFile, Message

from config import Settings
from db import is_admin

router = Router()


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return await is_admin(settings.db_path, message.from_user.id, settings.admin_ids)


@router.message(IsAdmin(), Command("db"))
async def cmd_db(message: Message, settings: Settings) -> None:
    # Чтобы чат был чистым — удаляем команду
    try:
        await message.delete()
    except Exception:
        pass

    db_path = Path(settings.db_path)
    if not db_path.exists():
        await message.answer("❌ Файл базы данных не найден.")
        return

    # Делаем копию (так надёжнее, если БД занята/в работе)
    tmp_dir = Path("temp")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    copy_path = tmp_dir / f"db_backup_{ts}.sqlite"

    try:
        copy2(db_path, copy_path)
        path_to_send = copy_path
    except Exception:
        # если копия не получилась — отправим оригинал
        path_to_send = db_path

    try:
        await message.answer_document(
            FSInputFile(str(path_to_send), filename=db_path.name),
            caption="🗄️ Экспорт базы данных (SQLite).",
        )
    finally:
        # Чистим временную копию
        if path_to_send != db_path:
            try:
                path_to_send.unlink(missing_ok=True)
            except Exception:
                pass
