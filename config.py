import os
import hashlib
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    db_path: str
    site_registrations_path: str
    site_registration_hook_url: str
    site_registration_secret: str


def _parse_admin_ids(raw: str) -> set[int]:
    raw = raw.strip()
    if not raw:
        return set()

    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as e:
            raise RuntimeError(f"ADMIN_IDS содержит не число: {part!r}") from e
    return ids


def load_settings() -> "Settings":
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    raw_db_path = os.getenv("DB_PATH", "db/bot.sqlite3").strip() or "db/bot.sqlite3"
    db_path = os.path.abspath(raw_db_path)

    raw_registrations_path = os.getenv("SITE_REGISTRATIONS_PATH", "data/site_registrations.json").strip() or "data/site_registrations.json"
    site_registrations_path = os.path.abspath(raw_registrations_path)
    site_registration_hook_url = os.getenv("SITE_REGISTRATION_HOOK_URL", "").strip()
    site_registration_secret = os.getenv("SITE_REGISTRATION_SECRET", "").strip()
    if not site_registration_secret:
        site_registration_secret = hashlib.sha256(f"site-registration|{bot_token}".encode("utf-8")).hexdigest()

    return Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        db_path=db_path,
        site_registrations_path=site_registrations_path,
        site_registration_hook_url=site_registration_hook_url,
        site_registration_secret=site_registration_secret,
    )
