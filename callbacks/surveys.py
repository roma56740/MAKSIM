from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class SurveyAdminCb(CallbackData, prefix="asv"):
    action: str                 # menu/create/mode/send/cancel/list/open/export/back/noop
    page: int = 1
    survey_id: int = 0
    mode: str = ""             # single/multiple


class SurveyUserCb(CallbackData, prefix="usv"):
    action: str                 # pick/toggle/submit
    survey_id: int
    option_id: int = 0
