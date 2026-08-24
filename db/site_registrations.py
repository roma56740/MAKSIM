from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def init_site_registrations_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS site_registrations (
                id TEXT PRIMARY KEY,
                access_token_hash TEXT NOT NULL,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT,
                telegram_id INTEGER,
                client_type TEXT NOT NULL,
                company TEXT,
                contact_method TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                moderator_tg_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                moderated_at TEXT
            )
            """
        )
        columns_cursor = await db.execute("PRAGMA table_info(site_registrations)")
        columns = {str(row[1]) for row in await columns_cursor.fetchall()}
        if "telegram_id" not in columns:
            await db.execute("ALTER TABLE site_registrations ADD COLUMN telegram_id INTEGER")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_site_registrations_status ON site_registrations(status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_site_registrations_phone ON site_registrations(phone)"
        )
        await db.commit()


async def create_site_registration(
    db_path: str,
    *,
    registration_id: str,
    access_token: str,
    full_name: str,
    phone: str,
    email: str,
    telegram_id: int,
    client_type: str,
    company: str,
    contact_method: str,
) -> dict[str, Any]:
    now = _utcnow()
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO site_registrations (
                id, access_token_hash, full_name, phone, email, telegram_id,
                client_type, company, contact_method, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                registration_id,
                token_hash,
                full_name,
                phone,
                email,
                telegram_id,
                client_type,
                company,
                contact_method,
                now,
                now,
            ),
        )
        await db.commit()
    result = await get_site_registration(db_path, registration_id)
    if result is None:
        raise RuntimeError("Registration was not saved")
    return result


async def get_site_registration(db_path: str, registration_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, full_name, phone, email, telegram_id, client_type, company,
                   contact_method, status, moderator_tg_id,
                   created_at, updated_at, moderated_at
            FROM site_registrations
            WHERE id = ?
            LIMIT 1
            """,
            (registration_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


async def get_site_registration_by_token(
    db_path: str,
    registration_id: str,
    access_token: str,
) -> dict[str, Any] | None:
    if not access_token:
        return None
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM site_registrations WHERE id = ? LIMIT 1",
            (registration_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    expected = str(row["access_token_hash"] or "")
    actual = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected, actual):
        return None
    result = dict(row)
    result.pop("access_token_hash", None)
    return result


async def set_site_registration_status(
    db_path: str,
    registration_id: str,
    status: str,
    moderator_tg_id: int,
) -> dict[str, Any] | None:
    if status not in {"pending", "awaiting_user", "approved", "rejected"}:
        raise ValueError("Unsupported registration status")
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            UPDATE site_registrations
            SET status = ?, moderator_tg_id = ?, moderated_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, moderator_tg_id, now, now, registration_id),
        )
        await db.commit()
        if cursor.rowcount < 1:
            return None
    return await get_site_registration(db_path, registration_id)


async def list_site_registrations(db_path: str, limit: int = 10000) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 10000))
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, full_name, phone, email, telegram_id, client_type, company,
                   contact_method, status, moderator_tg_id,
                   created_at, updated_at, moderated_at
            FROM site_registrations
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]
