from __future__ import annotations

import aiosqlite
from datetime import datetime
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


async def init_ai_user_chat_db(*, db_path: Optional[str] = None, conn: Optional[aiosqlite.Connection] = None) -> None:
    must_close = False
    if conn is None:
        if not db_path:
            raise RuntimeError("db_path is required")
        conn = await aiosqlite.connect(db_path)
        must_close = True

    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_dialog_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('system','user','assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_dialog_messages_tg_id_id ON ai_dialog_messages(tg_id, id);"
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_product_search_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_product_search_sessions_tg_id_id ON ai_product_search_sessions(tg_id, id);"
        )

        if must_close:
            await conn.commit()
    finally:
        if must_close:
            await conn.close()


async def add_dialog_message(*, db_path: str, tg_id: int, role: str, content: str) -> None:
    content = (content or "").strip()
    if not content:
        return
    async with aiosqlite.connect(db_path) as db:
        await init_ai_user_chat_db(conn=db)
        await db.execute(
            "INSERT INTO ai_dialog_messages (tg_id, role, content, created_at) VALUES (?, ?, ?, ?);",
            (tg_id, role, content, _now_iso()),
        )
        await db.commit()


async def get_dialog_history(*, db_path: str, tg_id: int, limit: int = 20) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await init_ai_user_chat_db(conn=db)
        cur = await db.execute(
            """
            SELECT role, content
            FROM ai_dialog_messages
            WHERE tg_id = ?
            ORDER BY id DESC
            LIMIT ?;
            """,
            (tg_id, int(limit)),
        )
        rows = await cur.fetchall()
        items = [dict(r) for r in rows]
        items.reverse()
        return items


async def clear_dialog_history(*, db_path: str, tg_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await init_ai_user_chat_db(conn=db)
        await db.execute("DELETE FROM ai_dialog_messages WHERE tg_id = ?;", (tg_id,))
        await db.commit()


async def create_search_session(*, db_path: str, tg_id: int, query: str) -> int:
    q = (query or "").strip() or "товары"
    async with aiosqlite.connect(db_path) as db:
        await init_ai_user_chat_db(conn=db)
        cur = await db.execute(
            "INSERT INTO ai_product_search_sessions (tg_id, query, created_at) VALUES (?, ?, ?);",
            (tg_id, q, _now_iso()),
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_search_session(*, db_path: str, session_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await init_ai_user_chat_db(conn=db)
        cur = await db.execute("SELECT * FROM ai_product_search_sessions WHERE id = ?;", (int(session_id),))
        row = await cur.fetchone()
        return dict(row) if row else None
