import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from config import load_settings
from db import init_db
from db.surveys import init_surveys_db
from db.site_registrations import init_site_registrations_db
from services.image_store import ensure_default_image
from services.site_registration_api import start_site_registration_api

# user
from handlers.user.pending_block import router as pending_router
from handlers.user.catalog import router as user_catalog_router
from handlers.start import router as start_router
from handlers.user.kp_build import router as kp_build_router
from handlers.user.profile import router as user_profile_router
from handlers.user.invoices import router as user_invoices_router
from handlers.user.bills import router as user_bills_router  # ✅ НОВОЕ
from handlers.user.surveys import router as user_surveys_router
from handlers.user.promotions import router as user_promotions_router
from handlers.user.sales_report import router as user_sales_report_router

# admin
from handlers.admin.db_export import router as admin_db_export_router
from handlers.admin.invoices_panel import router as admin_invoices_panel_router
from handlers.admin.invoices import router as admin_invoices_router
from handlers.admin.analytics import router as admin_analytics_router
from handlers.admin.users import router as admin_users_router
from handlers.admin.broadcast import router as admin_broadcast_router
from handlers.admin.admins import router as admin_admins_router
from handlers.admin.suppliers import router as admin_suppliers_router
from handlers.admin.prices import router as admin_prices_router
from handlers.admin.products import router as admin_products_router
from handlers.admin.registration import router as admin_reg_router
from handlers.admin.bills import router as admin_bills_router  # ✅ НОВОЕ
from handlers.admin.surveys import router as admin_surveys_router
from handlers.admin.promotions import router as admin_promotions_router
from handlers.admin.personal_messages import router as admin_personal_messages_router
from handlers.admin.site_registrations import router as admin_site_registrations_router
from handlers.user.ai_chat import router as user_ai_chat_router
from handlers.admin.ai import router as admin_ai_router  # ✅ ИИ

# new
from handlers.user.admin_chat import router as user_admin_chat_router
from handlers.admin.support_chats import router as admin_support_chats_router
from services.promotions import promotion_expiry_worker

async def main() -> None:
    settings = load_settings()
    logging.info("DB_PATH = %s", settings.db_path)

    await init_db(settings.db_path)
    await init_surveys_db(settings.db_path)
    await init_site_registrations_db(settings.db_path)
    ensure_default_image()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )

    dp = Dispatcher()
    dp["settings"] = settings

    # user (важно: pending первым)
    dp.include_router(pending_router)
    dp.include_router(user_catalog_router)
    dp.include_router(kp_build_router)
    dp.include_router(user_profile_router)
    dp.include_router(user_invoices_router)
    dp.include_router(user_bills_router)  # ✅ НОВОЕ
    dp.include_router(user_admin_chat_router)      # ✅ чат с админом
    dp.include_router(user_surveys_router)         # ✅ ответы на опросы
    dp.include_router(user_promotions_router)
    dp.include_router(user_sales_report_router)

    # admin
    dp.include_router(admin_site_registrations_router)
    dp.include_router(admin_invoices_panel_router)
    dp.include_router(admin_invoices_router)
    dp.include_router(admin_bills_router)  # ✅ НОВОЕ
    dp.include_router(admin_analytics_router)
    dp.include_router(admin_users_router)
    dp.include_router(admin_broadcast_router)
    dp.include_router(admin_surveys_router)  # ✅ опросы
    dp.include_router(admin_promotions_router)
    dp.include_router(admin_personal_messages_router)
    dp.include_router(admin_admins_router)
    dp.include_router(admin_suppliers_router)
    dp.include_router(admin_prices_router)
    dp.include_router(admin_products_router)
    dp.include_router(admin_reg_router)
    dp.include_router(admin_db_export_router)  # ✅ /db
    dp.include_router(admin_support_chats_router)  # ✅ чаты

    dp.include_router(admin_ai_router)  # ✅ ИИ
    dp.include_router(user_ai_chat_router)

    # start/регистрация
    dp.include_router(start_router)

    expiry_task = asyncio.create_task(
        promotion_expiry_worker(bot, settings.db_path),
        name="promotion-expiry-worker",
    )
    api_runner = await start_site_registration_api(bot, settings)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        expiry_task.cancel()
        await asyncio.gather(expiry_task, return_exceptions=True)
        if api_runner is not None:
            await api_runner.cleanup()


if __name__ == "__main__":
    import os

    level = logging.DEBUG if os.getenv("DEBUG", "0") == "1" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    # чтобы не спамило httpx в INFO (по желанию)
    logging.getLogger("httpx").setLevel(logging.WARNING if level == logging.INFO else logging.INFO)

    asyncio.run(main())
