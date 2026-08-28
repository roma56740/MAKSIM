import os
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    db_path: str
    site_registrations_path: str
    site_registration_hook_url: str
    site_registration_secret: str
    site_api_enabled: bool
    site_api_host: str
    site_api_port: int


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


def _storage_path(env_name: str, default: str, persistent_root: str) -> str:
    raw = os.getenv(env_name, default).strip() or default
    if persistent_root and not os.path.isabs(raw):
        raw = os.path.join(persistent_root, raw)
    return os.path.abspath(raw)


def prepare_storage(settings: "Settings") -> list[tuple[str, str]]:
    """Создаёт каталоги и один раз переносит комплектные данные на persistent volume."""
    project_root = Path(__file__).resolve().parent
    targets = (
        (project_root / "db" / "bot.sqlite3", Path(settings.db_path)),
        (
            project_root / "data" / "site_registrations.json",
            Path(settings.site_registrations_path),
        ),
    )
    copied: list[tuple[str, str]] = []
    for source, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == target.resolve() or target.exists() or not source.exists():
            continue
        shutil.copy2(source, target)
        copied.append((str(source), str(target)))
    return copied


def load_settings() -> "Settings":
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    persistent_root = (
        os.getenv("PERSISTENT_DATA_DIR", "").strip()
        or os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    )
    db_path = _storage_path("DB_PATH", "db/bot.sqlite3", persistent_root)
    site_registrations_path = _storage_path(
        "SITE_REGISTRATIONS_PATH",
        "data/site_registrations.json",
        persistent_root,
    )
    site_registration_hook_url = os.getenv("SITE_REGISTRATION_HOOK_URL", "").strip()
    site_registration_secret = os.getenv("SITE_REGISTRATION_SECRET", "").strip()
    if not site_registration_secret:
        site_registration_secret = hashlib.sha256(f"site-registration|{bot_token}".encode("utf-8")).hexdigest()
    site_api_enabled = os.getenv("SITE_API_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    site_api_host = os.getenv("SITE_API_HOST", "0.0.0.0").strip() or "0.0.0.0"
    raw_site_api_port = os.getenv("PORT", os.getenv("SITE_API_PORT", "8080")).strip()
    try:
        site_api_port = int(raw_site_api_port)
    except ValueError as exc:
        raise RuntimeError("PORT или SITE_API_PORT должен быть числом") from exc
    if not 1 <= site_api_port <= 65535:
        raise RuntimeError("PORT или SITE_API_PORT указан неверно")

    return Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        db_path=db_path,
        site_registrations_path=site_registrations_path,
        site_registration_hook_url=site_registration_hook_url,
        site_registration_secret=site_registration_secret,
        site_api_enabled=site_api_enabled,
        site_api_host=site_api_host,
        site_api_port=site_api_port,
    )
