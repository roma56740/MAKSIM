from __future__ import annotations

from typing import Any
import aiosqlite


async def count_invoices_by_status(db_path: str, status: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(1) FROM invoices WHERE status = ?", (status,))
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def list_invoices_by_status(
    db_path: str,
    status: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
              i.*,
              u.full_name AS user_full_name,
              u.phone AS user_phone
            FROM invoices i
            LEFT JOIN users u ON u.tg_id = i.tg_id
            WHERE i.status = ?
            ORDER BY i.updated_at DESC, i.id DESC
            LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
