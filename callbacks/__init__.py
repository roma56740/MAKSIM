# Реэкспорт старых callback-data (чтобы работали импорты: from callbacks import ...)
from .common import *  # noqa

# Новый функционал счетов
from .bills import UBillsCb, ABillsCb

# Если хочешь строгий экспорт — можно позже заменить star-import на явный список __all__


from aiogram.filters.callback_data import CallbackData


class AdminUsersCb(CallbackData, prefix="ausers"):
    action: str  # page/view/filter/back
    page: int = 1
    tg_id: int = 0
    status: str = "all"