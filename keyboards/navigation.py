from aiogram.types import ReplyKeyboardMarkup

from config import Settings
from db import is_admin
from keyboards.admin import admin_main_kb
from keyboards.user import user_main_kb


async def main_menu_kb(settings: Settings, tg_id: int) -> ReplyKeyboardMarkup:
    """Choose the recipient's current menu, including database administrators."""
    if await is_admin(settings.db_path, tg_id, settings.admin_ids):
        return admin_main_kb()
    return user_main_kb()
