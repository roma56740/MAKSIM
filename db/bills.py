from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def create_bill(
    db_path: str,
    tg_id: int,
    text: str | None,
    file_id: str | None,
    file_kind: str | None,
) -> int:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            INSERT INTO bills (
                tg_id, text, file_id, file_kind, status, reason, paid_at, handled_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?)
            """,
            (tg_id, text, file_id, file_kind, now, now),
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_bill(db_path: str, bill_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def count_bills(db_path: str, status: str | None = None, tg_id: int | None = None) -> int:
    where = []
    params: list[Any] = []

    if status and status != "all":
        where.append("status = ?")
        params.append(status)

    if tg_id is not None:
        where.append("tg_id = ?")
        params.append(tg_id)

    q = "SELECT COUNT(1) FROM bills"
    if where:
        q += " WHERE " + " AND ".join(where)

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(q, tuple(params))
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def list_bills(
    db_path: str,
    status: str | None = None,
    tg_id: int | None = None,
    limit: int = 10,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = []
    params: list[Any] = []

    if status and status != "all":
        where.append("status = ?")
        params.append(status)

    if tg_id is not None:
        where.append("tg_id = ?")
        params.append(tg_id)

    q = "SELECT * FROM bills"
    if where:
        q += " WHERE " + " AND ".join(where)

    q += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, tuple(params))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def reject_bill(db_path: str, bill_id: int, admin_id: int, reason: str) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE bills
            SET status = 'rejected',
                reason = ?,
                handled_by = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (reason, admin_id, now, bill_id),
        )
        await db.commit()


async def mark_bill_paid(db_path: str, bill_id: int, admin_id: int) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE bills
            SET status = 'paid',
                reason = NULL,
                paid_at = ?,
                handled_by = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, admin_id, now, bill_id),
        )
        await db.commit()
