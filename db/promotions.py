from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def create_promotion(
    db_path: str,
    *,
    kind: str,
    title: str,
    text: str | None,
    file_id: str | None,
    file_kind: str | None,
    expires_at: str | None,
    created_by: int,
    duplicated_from: int | None = None,
) -> int:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            INSERT INTO promotions (
                kind, title, text, file_id, file_kind, status,
                expires_at, archived_at, created_by, duplicated_from,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?, ?)
            """,
            (
                kind,
                title,
                text,
                file_id,
                file_kind,
                expires_at,
                created_by,
                duplicated_from,
                now,
                now,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_promotion(db_path: str, promotion_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute("SELECT * FROM promotions WHERE id = ?", (promotion_id,))
        ).fetchone()
        return dict(row) if row else None


async def count_promotions(db_path: str, status: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute(
                "SELECT COUNT(1) FROM promotions WHERE status = ?", (status,)
            )
        ).fetchone()
        return int(row[0] if row else 0)


async def list_promotions(
    db_path: str,
    *,
    status: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                """
                SELECT *
                FROM promotions
                WHERE status = ?
                ORDER BY COALESCE(archived_at, created_at) DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (status, int(limit), int(offset)),
            )
        ).fetchall()
        return [dict(row) for row in rows]


async def list_active_promotions(db_path: str, now_iso: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                """
                SELECT *
                FROM promotions
                WHERE status = 'active'
                  AND (
                    expires_at IS NULL OR TRIM(expires_at) = ''
                    OR julianday(expires_at) IS NULL
                    OR julianday(expires_at) > julianday(?)
                  )
                ORDER BY created_at DESC, id DESC
                """,
                (now_iso,),
            )
        ).fetchall()
        return [dict(row) for row in rows]


async def list_due_promotion_ids(db_path: str, now_iso: str) -> list[int]:
    async with aiosqlite.connect(db_path) as db:
        rows = await (
            await db.execute(
                """
                SELECT id
                FROM promotions
                WHERE status = 'active'
                  AND expires_at IS NOT NULL
                  AND TRIM(expires_at) <> ''
                  AND julianday(expires_at) IS NOT NULL
                  AND julianday(expires_at) <= julianday(?)
                ORDER BY expires_at ASC
                """,
                (now_iso,),
            )
        ).fetchall()
        return [int(row[0]) for row in rows]


async def archive_promotion(db_path: str, promotion_id: int) -> bool:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            UPDATE promotions
            SET status = 'archived', archived_at = ?, updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (now, now, promotion_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def add_promotion_delivery(
    db_path: str,
    promotion_id: int,
    tg_id: int,
    message_id: int,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO promotion_deliveries (
                promotion_id, tg_id, message_id, created_at, deleted_at
            ) VALUES (?, ?, ?, ?, NULL)
            """,
            (promotion_id, tg_id, message_id, _utcnow()),
        )
        await db.commit()


async def list_active_deliveries(
    db_path: str,
    promotion_id: int,
) -> list[dict[str, int]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                """
                SELECT tg_id, message_id
                FROM promotion_deliveries
                WHERE promotion_id = ? AND deleted_at IS NULL
                """,
                (promotion_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]


async def mark_delivery_deleted(
    db_path: str,
    promotion_id: int,
    tg_id: int,
    message_id: int,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE promotion_deliveries
            SET deleted_at = ?
            WHERE promotion_id = ? AND tg_id = ? AND message_id = ?
            """,
            (_utcnow(), promotion_id, tg_id, message_id),
        )
        await db.commit()
