from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _web_code(url: str) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"WEB-{h}"


async def get_kp_session(db_path: str, tg_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM kp_sessions WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_kp_supplier(db_path: str, tg_id: int, supplier_id: int | None) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM kp_sessions WHERE tg_id = ?", (int(tg_id),))
        await db.commit()


async def count_kp_items(db_path: str, tg_id: int) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(1) FROM kp_items WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def list_kp_items(db_path: str, tg_id: int, limit: int, offset: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT *
            FROM kp_items
            WHERE tg_id = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (tg_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def clear_kp(db_path: str, tg_id: int) -> None:
    tg_id = int(tg_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM kp_items WHERE tg_id = ?", (tg_id,))
        await db.execute("DELETE FROM kp_sessions WHERE tg_id = ?", (tg_id,))
        await db.commit()

async def remove_kp_item(db_path: str, tg_id: int, item_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM kp_items WHERE tg_id = ? AND id = ?", (tg_id, item_id))
        await db.commit()

    # если корзина опустела — убрать и компанию (session)
    if await count_kp_items(db_path, tg_id) == 0:
        await clear_kp(db_path, tg_id)


async def add_kp_product(db_path: str, tg_id: int, supplier_id: int, product_id: int) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        # если уже есть — увеличим qty
        cur = await db.execute(
            """
            SELECT id, qty FROM kp_items
            WHERE tg_id = ? AND supplier_id = ? AND product_id = ?
            LIMIT 1
            """,
            (tg_id, supplier_id, product_id),
        )
        row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE kp_items SET qty = ?, updated_at = ? WHERE id = ?",
                (int(row[1]) + 1, now, int(row[0])),
            )
            await db.commit()
            return

        # снимок данных товара на момент добавления
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products WHERE id = ? LIMIT 1", (product_id,))
        p = await cur.fetchone()
        if not p:
            return
        p = dict(p)

        title = (p.get("description") or "").strip() or (p.get("code") or f"#{product_id}")

        await db.execute(
            """
            INSERT INTO kp_items (
                tg_id, supplier_id, product_id, title, description, price, final_price, url,
                image_url, image_path, qty, extra_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                tg_id,
                supplier_id,
                product_id,
                title,
                p.get("description"),
                p.get("price"),
                p.get("final_price"),
                p.get("url"),
                p.get("image_url"),
                p.get("image_path"),
                p.get("extra_json"),
                now,
                now,
            ),
        )
        await db.commit()

 

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def add_kp_web_item(
    db_path: str,
    tg_id: int,
    supplier_id: int,
    url: str,
    title: str | None,
    description: str | None,
    product_type: str | None,
    image_url: str | None,
    image_path: str | None,
    extra_json: str | None,
    price: float | None = None,
    final_price: float | None = None,
) -> int:
    now = _utcnow()

    # product_type у нас нет отдельной колонкой в kp_items — аккуратно положим в extra_json
    extra_obj: dict[str, Any] = {}
    if extra_json:
        try:
            extra_obj = json.loads(extra_json)
            if not isinstance(extra_obj, dict):
                extra_obj = {"raw": extra_json}
        except Exception:
            extra_obj = {"raw": extra_json}

    if product_type:
        extra_obj.setdefault("product_type", product_type)

    extra_out = json.dumps(extra_obj, ensure_ascii=False)

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            INSERT INTO kp_items (
                tg_id, supplier_id, product_id,
                title, description,
                price, final_price,
                url,
                image_url, image_path,
                qty, extra_json,
                created_at, updated_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                tg_id,
                supplier_id,
                title,
                description,
                price,
                final_price,
                url,
                image_url,
                image_path,
                extra_out,
                now,
                now,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)
