from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def save_business_connection(
    db_path: str,
    *,
    connection_id: str,
    admin_tg_id: int,
    user_chat_id: int | None,
    is_enabled: bool,
    rights: dict[str, Any] | None,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO telegram_business_connections (
                connection_id, admin_tg_id, user_chat_id,
                is_enabled, rights_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                admin_tg_id = excluded.admin_tg_id,
                user_chat_id = excluded.user_chat_id,
                is_enabled = excluded.is_enabled,
                rights_json = excluded.rights_json,
                updated_at = excluded.updated_at
            """,
            (
                connection_id,
                admin_tg_id,
                user_chat_id,
                1 if is_enabled else 0,
                json.dumps(rights or {}, ensure_ascii=False),
                _utcnow(),
            ),
        )
        await db.commit()


async def get_active_business_connection(
    db_path: str,
    admin_tg_id: int,
) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                """
                SELECT *
                FROM telegram_business_connections
                WHERE admin_tg_id = ? AND is_enabled = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (admin_tg_id,),
            )
        ).fetchone()
        return dict(row) if row else None


def _manager_filter(
    excluded_ids: Iterable[int] | None,
    reg_types: Iterable[str] | None = None,
) -> tuple[str, list[Any]]:
    excluded = sorted({int(value) for value in (excluded_ids or [])})
    groups = sorted({str(value).strip() for value in (reg_types or []) if str(value).strip()})
    clauses: list[str] = []
    params: list[Any] = []
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        clauses.append(f"tg_id NOT IN ({placeholders})")
        params.extend(excluded)
    if groups:
        placeholders = ",".join("?" for _ in groups)
        clauses.append(f"reg_type IN ({placeholders})")
        params.extend(groups)
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


async def count_approved_managers(
    db_path: str,
    excluded_ids: Iterable[int] | None = None,
    reg_types: Iterable[str] | None = None,
) -> int:
    extra_where, params = _manager_filter(excluded_ids, reg_types)
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                f"SELECT COUNT(1) FROM users WHERE status = 'approved'{extra_where}",
                tuple(params),
            )
        ).fetchone()
        return int(row[0] if row else 0)


async def list_approved_managers(
    db_path: str,
    *,
    limit: int,
    offset: int,
    excluded_ids: Iterable[int] | None = None,
    reg_types: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    extra_where, params = _manager_filter(excluded_ids, reg_types)
    params.extend([int(limit), int(offset)])
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                f"""
                SELECT tg_id, full_name, first_name, username, phone
                FROM users
                WHERE status = 'approved'{extra_where}
                ORDER BY COALESCE(full_name, first_name, username, CAST(tg_id AS TEXT)) COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                tuple(params),
            )
        ).fetchall()
        return [dict(row) for row in rows]


async def list_approved_manager_groups(
    db_path: str,
    excluded_ids: Iterable[int] | None = None,
) -> list[dict[str, Any]]:
    extra_where, params = _manager_filter(excluded_ids)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM(reg_type), ''), 'Без группы') AS reg_type,
                       COUNT(1) AS managers_count
                FROM users
                WHERE status = 'approved'{extra_where}
                GROUP BY COALESCE(NULLIF(TRIM(reg_type), ''), 'Без группы')
                ORDER BY reg_type COLLATE NOCASE
                """,
                tuple(params),
            )
        ).fetchall()
        return [dict(row) for row in rows]


async def get_manager(db_path: str, tg_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                """
                SELECT tg_id, full_name, first_name, username, phone, status
                FROM users
                WHERE tg_id = ?
                """,
                (tg_id,),
            )
        ).fetchone()
        return dict(row) if row else None


async def update_user_telegram_profile(
    db_path: str,
    tg_id: int,
    *,
    username: str | None,
    first_name: str | None,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE users
            SET username = ?, first_name = ?, updated_at = ?
            WHERE tg_id = ?
            """,
            (username, first_name, _utcnow(), tg_id),
        )
        await db.commit()
