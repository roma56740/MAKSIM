from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp


_file_lock = asyncio.Lock()


def registration_from_message(text: str) -> dict[str, str]:
    labels = {
        "ID": "id",
        "Имя": "full_name",
        "Клиент": "client_label",
        "Компания": "company",
        "Телефон": "phone",
        "Email": "email",
        "Telegram ID": "telegram_id",
        "Роль сайта": "site_role",
        "Связаться через": "contact_label",
        "Контакт": "contact_value",
        "Дата": "date_label",
    }
    result: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        key = labels.get(label.strip())
        if key:
            result[key] = value.strip()
    client_label = result.get("client_label", "")
    result["client_type"] = "corporate" if "Корпоратив" in client_label else "private"
    result["site_role"] = "manager" if "Менеджер" in client_label else "client"
    return result


async def save_registration_decision(
    path: str,
    registration_id: str,
    status: str,
    moderator_id: int,
    source: dict[str, str] | None = None,
) -> dict[str, Any]:
    if status not in {"approved", "rejected"}:
        raise ValueError("Unsupported registration status")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    source = source or {}
    now = datetime.now(timezone.utc).isoformat()

    async with _file_lock:
        rows: list[dict[str, Any]] = []
        if target.exists():
            try:
                decoded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(decoded, list):
                    rows = [row for row in decoded if isinstance(row, dict)]
            except (OSError, json.JSONDecodeError):
                rows = []

        record = next((row for row in rows if str(row.get("id", "")) == registration_id), None)
        if record is None:
            record = {
                "id": registration_id,
                "full_name": source.get("full_name", ""),
                "phone": source.get("phone", ""),
                "email": source.get("email", ""),
                "telegram_id": source.get("telegram_id", ""),
                "site_role": source.get("site_role", "client"),
                "company": source.get("company", ""),
                "client_type": source.get("client_type", "private"),
                "contact_label": source.get("contact_label", ""),
                "contact_value": source.get("contact_value", ""),
                "created_at": now,
            }
            rows.insert(0, record)

        record["status"] = status
        record["moderated_at"] = now
        record["updated_at"] = now
        record["moderator_tg_id"] = moderator_id

        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
        return dict(record)


async def send_registration_decision(
    hook_url: str,
    secret: str,
    registration_id: str,
    action: str,
    moderator_id: int,
) -> bool | None:
    if not hook_url:
        return None
    timeout = aiohttp.ClientTimeout(total=15)
    payload = {
        "registration_id": registration_id,
        "action": action,
        "moderator": f"Telegram {moderator_id}",
    }
    headers = {"X-Site-Registration-Secret": secret}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(hook_url, data=payload, headers=headers) as response:
                if response.status != 200:
                    return False
                data = await response.json(content_type=None)
                return bool(data.get("success")) if isinstance(data, dict) else False
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False
