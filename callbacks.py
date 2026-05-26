from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class AdminRegCb(CallbackData, prefix="areg"):
    action: str  # view/approve/reject/block/back
    tg_id: int


class UserRegCb(CallbackData, prefix="ureg"):
    action: str  # type/back
    value: str


class AdminsCb(CallbackData, prefix="admins"):
    action: str  # page/add/remove/confirm/cancel/back
    page: int
    tg_id: int


class AdminUsersCb(CallbackData, prefix="ausers"):
    action: str            # list/view/filter/back/menu
    page: int = 1
    tg_id: int = 0
    status: str = "all"    # all/approved/pending/rejected/blocked


class BroadcastCb(CallbackData, prefix="bcast"):
    action: str  # send/cancel


class SuppliersCb(CallbackData, prefix="supp"):
    # page/view/add/edit_name/edit_site/edit_email/edit_phone/edit_desc/del/confirm/menu/list
    action: str
    page: int
    supplier_id: int


# --- Excel-прайсы / товары ---
class PricesCb(CallbackData, prefix="price"):
    action: str  # page/view/upload/mode/back/list
    page: int
    supplier_id: int
    mode: str  # add_missing | upsert | ""


class ProductsCb(CallbackData, prefix="prod"):
    action: str  # page/view/edit/del/confirm/list
    page: int
    supplier_id: int
    product_id: int
    field: str  # description/price/discount_percent/final_price/product_type/stock_qty/url/code/source_pk


# --- User: каталог / КП ---
class UserCatalogCb(CallbackData, prefix="ucat"):
    action: str  # supp_page/supp_open/prod_page/prod_open/add/site_add/back
    page: int
    supplier_id: int
    product_id: int


class UserKpCb(CallbackData, prefix="ukp"):
    action: str  # view/page/del/clear/build
    page: int
    item_id: int


class UBillsCb(CallbackData, prefix="ubill"):
    action: str            # menu/list/open/back
    status: str = "all"    # all/pending/paid/rejected
    page: int = 1
    bill_id: int = 0


class ABillsCb(CallbackData, prefix="abill"):
    action: str            # menu/list/open/reject/pay/pay_confirm/back
    page: int = 1
    bill_id: int = 0
