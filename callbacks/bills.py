from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class UBillsCb(CallbackData, prefix="ubill"):
    action: str            # menu/list/open/back
    status: str = "all"    # all/pending/paid/rejected
    page: int = 1
    bill_id: int = 0


class ABillsCb(CallbackData, prefix="abill"):
    action: str            # menu/list/open/reject/pay/pay_confirm/back
    page: int = 1
    bill_id: int = 0
