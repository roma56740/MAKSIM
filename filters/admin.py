from __future__ import annotations

from typing import Any

from aiogram.filters import BaseFilter

from config import Settings
from db import is_admin


class IsAdmin(BaseFilter):
    async def __call__(self, event: Any, settings: Settings) -> bool:
        tg_id = getattr(getattr(event, "from_user", None), "id", None)
        if tg_id is None:
            return False
        return await is_admin(settings.db_path, int(tg_id), settings.admin_ids)


class NotAdmin(BaseFilter):
    async def __call__(self, event: Any, settings: Settings) -> bool:
        tg_id = getattr(getattr(event, "from_user", None), "id", None)
        if tg_id is None:
            return True
        return not await is_admin(settings.db_path, int(tg_id), settings.admin_ids)
