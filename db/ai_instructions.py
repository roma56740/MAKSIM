from __future__ import annotations

import aiosqlite
from datetime import datetime
from typing import Any, Optional


AI_KIND_SEARCH = "search"
AI_KIND_DIALOG = "dialog"

AI_KINDS = {AI_KIND_SEARCH, AI_KIND_DIALOG}


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _resolve_db_path(db_path: Optional[str]) -> str:
    """
    Мы специально делаем максимально совместимо:
    - если db_path передали — используем его
    - если не передали — попробуем достать из db.database (как у тебя обычно)
    """
    if db_path:
        return db_path

    try:
        from . import database as _database  # type: ignore
        for attr in ("DB_PATH", "_DB_PATH", "db_path"):
            v = getattr(_database, attr, None)
            if isinstance(v, str) and v:
                return v
        getter = getattr(_database, "get_db_path", None)
        if callable(getter):
            v = getter()
            if isinstance(v, str) and v:
                return v
    except Exception:
        pass

    raise RuntimeError("db_path is required (не удалось определить путь к БД автоматически).")


async def init_ai_db(*, db_path: Optional[str] = None, conn: Optional[aiosqlite.Connection] = None) -> None:
    must_close = False
    if conn is None:
        path = _resolve_db_path(db_path)
        conn = await aiosqlite.connect(path)
        must_close = True

    try:
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_instructions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('search','dialog')),
                text TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT
            );
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_instructions_kind ON ai_instructions(kind);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_instructions_created_at ON ai_instructions(created_at);")

        # ✅ Коммитим только если init_ai_db само открыло соединение
        if must_close:
            await conn.commit()
    finally:
        if must_close:
            await conn.close()


async def create_ai_instruction(
    *,
    db_path: Optional[str] = None,
    kind: str,
    text: str,
    created_by: Optional[int] = None,
) -> int:
    if kind not in AI_KINDS:
        raise ValueError("Invalid kind")
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty text")

    path = _resolve_db_path(db_path)
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await init_ai_db(conn=db)

        cur = await db.execute(
            """
            INSERT INTO ai_instructions(kind, text, created_by, created_at)
            VALUES(?,?,?,?);
            """,
            (kind, text, created_by, _now_iso()),
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_ai_instruction(*, db_path: Optional[str] = None, instr_id: int) -> Optional[dict[str, Any]]:
    path = _resolve_db_path(db_path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await init_ai_db(conn=db)
        cur = await db.execute("SELECT * FROM ai_instructions WHERE id=?;", (instr_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_ai_instruction(
    *,
    db_path: Optional[str] = None,
    instr_id: int,
    new_text: str,
) -> bool:
    new_text = (new_text or "").strip()
    if not new_text:
        raise ValueError("Empty text")

    path = _resolve_db_path(db_path)
    async with aiosqlite.connect(path) as db:
        await init_ai_db(conn=db)
        cur = await db.execute(
            "UPDATE ai_instructions SET text=?, updated_at=? WHERE id=?;",
            (new_text, _now_iso(), instr_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_ai_instruction(*, db_path: Optional[str] = None, instr_id: int) -> bool:
    path = _resolve_db_path(db_path)
    async with aiosqlite.connect(path) as db:
        await init_ai_db(conn=db)
        cur = await db.execute("DELETE FROM ai_instructions WHERE id=?;", (instr_id,))
        await db.commit()
        return cur.rowcount > 0


async def count_ai_instructions(*, db_path: Optional[str] = None, kind: str) -> int:
    if kind not in AI_KINDS:
        raise ValueError("Invalid kind")

    path = _resolve_db_path(db_path)
    async with aiosqlite.connect(path) as db:
        await init_ai_db(conn=db)
        cur = await db.execute("SELECT COUNT(1) FROM ai_instructions WHERE kind=?;", (kind,))
        (n,) = await cur.fetchone()
        return int(n)


async def list_ai_instructions(
    *,
    db_path: Optional[str] = None,
    kind: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    if kind not in AI_KINDS:
        raise ValueError("Invalid kind")

    path = _resolve_db_path(db_path)
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await init_ai_db(conn=db)
        cur = await db.execute(
            """
            SELECT id, kind, text, created_by, created_at, updated_at
            FROM ai_instructions
            WHERE kind=?
            ORDER BY id DESC
            LIMIT ? OFFSET ?;
            """,
            (kind, int(limit), int(offset)),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
