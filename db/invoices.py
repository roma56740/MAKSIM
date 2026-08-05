from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _product_key(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


async def create_invoice(
    db_path: str,
    tg_id: int,
    file_id: str,
    file_kind: str,
) -> int:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            INSERT INTO invoices (
                tg_id, supplier_id, deal_amount, reward_amount,
                file_id, file_kind, comment,
                status, reason,
                created_at, updated_at
            ) VALUES (?, NULL, NULL, NULL, ?, ?, NULL, 'pending', NULL, ?, ?)
            """,
            (tg_id, file_id, file_kind, now, now),
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_invoice_full(db_path: str, invoice_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
              i.*,
              s.name AS supplier_name,
              u.full_name AS user_full_name,
              u.phone AS user_phone,
              u.status AS user_status
            FROM invoices i
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            LEFT JOIN users u ON u.tg_id = i.tg_id
            WHERE i.id = ?
            """,
            (invoice_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_invoice_items(db_path: str, invoice_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (
            await db.execute(
                """
                SELECT id, invoice_id, product_name, product_key, quantity, unit_price, line_total, created_at
                FROM invoice_items
                WHERE invoice_id = ?
                ORDER BY id ASC
                """,
                (invoice_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]


async def count_user_invoices_by_status(db_path: str, tg_id: int) -> dict[str, int]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
              SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved,
              SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
              COUNT(1) AS total
            FROM invoices
            WHERE tg_id = ?
            """,
            (tg_id,),
        )
        r = await cur.fetchone()
        if not r:
            return {"pending": 0, "approved": 0, "rejected": 0, "total": 0}
        return {k: int(r[k] or 0) for k in ("pending", "approved", "rejected", "total")}


async def count_invoices_for_user(db_path: str, tg_id: int, status: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(1) FROM invoices WHERE tg_id = ? AND status = ?",
            (tg_id, status),
        )
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def list_invoices_for_user(
    db_path: str,
    tg_id: int,
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
              s.name AS supplier_name,
              (SELECT COALESCE(SUM(ii.quantity), 0) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS items_qty,
              (SELECT COUNT(1) FROM invoice_items ii WHERE ii.invoice_id = i.id) AS items_count
            FROM invoices i
            LEFT JOIN suppliers s ON s.id = i.supplier_id
            WHERE i.tg_id = ? AND i.status = ?
            ORDER BY i.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (tg_id, status, limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def approve_invoice(
    db_path: str,
    invoice_id: int,
    deal_amount: float,
    reward_amount: float,
    items: list[dict[str, Any]] | None = None,
) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        for item in items or []:
            quantity = float(item["quantity"])
            unit_price = float(item["unit_price"])
            line_total = round(quantity * unit_price, 2)
            await db.execute(
                """
                INSERT INTO invoice_items (
                    invoice_id, product_name, product_key, quantity, unit_price, line_total, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice_id,
                    str(item["product_name"]).strip(),
                    _product_key(str(item["product_name"])),
                    quantity,
                    unit_price,
                    line_total,
                    now,
                ),
            )
        await db.execute(
            """
            UPDATE invoices
            SET
                status='approved',
                deal_amount=?,
                reward_amount=?,
                reason=NULL,
                handled_at=?,
                updated_at=?
            WHERE id = ?
            """,
            (deal_amount, reward_amount, now, now, invoice_id),
        )
        await db.commit()


async def reject_invoice(db_path: str, invoice_id: int, reason: str) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        await db.execute(
            """
            UPDATE invoices
            SET
                status='rejected',
                deal_amount=NULL,
                reward_amount=NULL,
                reason=?,
                handled_at=?,
                updated_at=?
            WHERE id = ?
            """,
            (reason, now, now, invoice_id),
        )
        await db.commit()


async def request_invoice_recheck(
    db_path: str,
    invoice_id: int,
    comment: str,
) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
        await db.execute(
            """
            UPDATE invoices
            SET
                status='pending',
                comment=?,
                deal_amount=NULL,
                reward_amount=NULL,
                reason=NULL,
                handled_at=NULL,
                updated_at=?
            WHERE id = ?
            """,
            (comment, now, invoice_id),
        )
        await db.commit()
