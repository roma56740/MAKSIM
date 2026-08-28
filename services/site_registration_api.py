from __future__ import annotations

import asyncio
import hmac
import html
import logging
import secrets
from typing import Any

from aiohttp import web
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Settings
from db import list_db_admin_ids
from db.site_registrations import (
    create_site_registration,
    get_site_registration_by_token,
    list_site_registrations,
)


logger = logging.getLogger(__name__)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _authorized(request: web.Request, secret: str) -> bool:
    supplied = request.headers.get("X-Site-Registration-Secret", "").strip()
    if not supplied:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
    return bool(supplied and secret and hmac.compare_digest(supplied, secret))


async def _notify_admins(bot: Bot, settings: Settings, registration: dict[str, Any]) -> None:
    admin_ids = set(settings.admin_ids)
    admin_ids.update(await list_db_admin_ids(settings.db_path))
    if not admin_ids:
        logger.warning("Site registration saved, but no administrators are configured")
        return

    site_role = str(registration.get("site_role") or "client")
    client_label = (
        "Менеджер"
        if site_role == "manager"
        else ("Корпоративный клиент" if registration["client_type"] == "corporate" else "Частный клиент")
    )
    methods = {
        "phone": "Телефонный звонок",
        "telegram": "Telegram",
        "whatsapp": "WhatsApp",
        "email": "Email",
    }
    lines = [
        "🪪 <b>Регистрация на сайте</b>",
        "",
        f"<b>ID:</b> <code>{html.escape(str(registration['id']))}</code>",
        f"<b>Имя:</b> {html.escape(str(registration['full_name']))}",
        f"<b>Клиент:</b> {html.escape(client_label)}",
    ]
    if registration.get("company"):
        lines.append(f"<b>Компания:</b> {html.escape(str(registration['company']))}")
    lines.append(f"<b>Телефон:</b> {html.escape(str(registration['phone']))}")
    if registration.get("email"):
        lines.append(f"<b>Email:</b> {html.escape(str(registration['email']))}")
    lines.append(f"<b>Telegram ID:</b> <code>{html.escape(str(registration['telegram_id']))}</code>")
    lines.append(
        f"<b>Связаться через:</b> {html.escape(methods.get(str(registration['contact_method']), 'Не указано'))}"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Открыть доступ",
            callback_data=f"site_reg:approve:{registration['id']}",
        ),
        InlineKeyboardButton(
            text="❌ Отклонить",
            callback_data=f"site_reg:reject:{registration['id']}",
        ),
    ]])

    async def send(admin_id: int) -> None:
        try:
            await bot.send_message(admin_id, "\n".join(lines), reply_markup=markup)
        except Exception:
            logger.exception("Could not notify Telegram administrator %s", admin_id)

    await asyncio.gather(*(send(admin_id) for admin_id in sorted(admin_ids)))


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "goodwine-bot"})


async def create_registration(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    bot: Bot = request.app["bot"]
    if not _authorized(request, settings.site_registration_secret):
        return web.json_response({"success": False, "message": "Нет доступа"}, status=401)
    if request.content_length is not None and request.content_length > 32768:
        return web.json_response({"success": False, "message": "Слишком большой запрос"}, status=413)
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"success": False, "message": "Некорректные данные"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"success": False, "message": "Некорректные данные"}, status=400)

    full_name = _clean(payload.get("full_name"), 140)
    phone = _clean(payload.get("phone"), 80)
    email = _clean(payload.get("email"), 160)
    telegram_id_raw = _clean(payload.get("telegram_id"), 24)
    company = _clean(payload.get("company"), 180)
    site_role = "manager" if _clean(payload.get("site_role"), 20) == "manager" else "client"
    client_type = _clean(payload.get("client_type"), 20)
    contact_method = _clean(payload.get("contact_method"), 20)
    if len(full_name) < 2 or len(phone) < 7:
        return web.json_response({"success": False, "message": "Заполните имя и телефон"}, status=422)
    if email and ("@" not in email or "." not in email.rsplit("@", 1)[-1]):
        return web.json_response({"success": False, "message": "Проверьте электронную почту"}, status=422)
    if not telegram_id_raw.isdigit() or not 5 <= len(telegram_id_raw) <= 20:
        return web.json_response({"success": False, "message": "Укажите числовой Telegram ID"}, status=422)
    telegram_id = int(telegram_id_raw)
    if site_role == "manager":
        client_type = "manager"
    elif client_type not in {"private", "corporate"}:
        return web.json_response({"success": False, "message": "Выберите формат"}, status=422)
    if client_type == "corporate" and not company:
        return web.json_response({"success": False, "message": "Укажите компанию"}, status=422)
    if contact_method not in {"phone", "telegram", "whatsapp", "email"}:
        return web.json_response({"success": False, "message": "Выберите способ связи"}, status=422)

    registration_id = "site-" + secrets.token_hex(12)
    access_token = secrets.token_urlsafe(32)
    try:
        registration = await create_site_registration(
            settings.db_path,
            registration_id=registration_id,
            access_token=access_token,
            full_name=full_name,
            phone=phone,
            email=email,
            telegram_id=telegram_id,
            client_type=client_type,
            company=company,
            contact_method=contact_method,
            site_role=site_role,
        )
    except Exception:
        logger.exception("Could not save a site registration")
        return web.json_response(
            {"success": False, "message": "Не удалось сохранить регистрацию"},
            status=500,
        )

    task = asyncio.create_task(
        _notify_admins(bot, settings, registration),
        name=f"site-registration-notify-{registration_id}",
    )
    background_tasks: set[asyncio.Task[Any]] = request.app["background_tasks"]
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return web.json_response({
        "success": True,
        "registration_id": registration_id,
        "access_token": access_token,
        "status": "pending",
    })


async def registrations_list(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    if not _authorized(request, settings.site_registration_secret):
        return web.json_response({"success": False, "message": "Нет доступа"}, status=401)
    rows = await list_site_registrations(settings.db_path)
    return web.json_response({"success": True, "registrations": rows})


async def registration_status(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    registration_id = _clean(request.match_info.get("registration_id"), 80)
    access_token = request.headers.get("X-Site-Access-Token", "").strip()
    registration = await get_site_registration_by_token(
        settings.db_path,
        registration_id,
        access_token,
    )
    if registration is None:
        return web.json_response({"success": False, "message": "Регистрация не найдена"}, status=404)
    return web.json_response({
        "success": True,
        "registration": registration,
        "status": registration["status"],
    })


async def finish_background_tasks(app: web.Application) -> None:
    tasks: set[asyncio.Task[Any]] = app["background_tasks"]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def build_site_registration_app(bot: Bot, settings: Settings) -> web.Application:
    app = web.Application(client_max_size=32768)
    app["bot"] = bot
    app["settings"] = settings
    app["background_tasks"] = set()
    app.on_cleanup.append(finish_background_tasks)
    app.add_routes([
        web.get("/", health),
        web.get("/health", health),
        web.get("/api/site/registrations", registrations_list),
        web.post("/api/site/registrations", create_registration),
        web.get("/api/site/registrations/{registration_id}/status", registration_status),
    ])
    return app


async def start_site_registration_api(bot: Bot, settings: Settings) -> web.AppRunner | None:
    if not settings.site_api_enabled:
        logger.info("Site registration API is disabled")
        return None
    app = build_site_registration_app(bot, settings)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.site_api_host, settings.site_api_port)
    await site.start()
    logger.info("Site registration API is listening on %s:%s", settings.site_api_host, settings.site_api_port)
    return runner
