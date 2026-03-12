import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

import aiosqlite


Role = Literal["user", "admin", "system"]
Status = Literal["active", "closed"]


@dataclass
class ThreadRow:
    id: int
    user_id: int
    admin_id: Optional[int]
    status: Status
    created_at: str
    updated_at: str
    closed_at: Optional[str]


@dataclass
class MessageRow:
    id: int
    thread_id: int
    sender_role: Role
    sender_id: int
    text: str
    created_at: str


@dataclass
class SupportUserRow:
    tg_id: int
    full_name: Optional[str]
    phone: Optional[str]
    status: Optional[str]
    updated_at: Optional[str]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def create_tables(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS support_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                admin_id INTEGER,
                status TEXT NOT NULL CHECK (status IN ('active','closed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER NOT NULL,
                sender_role TEXT NOT NULL CHECK (sender_role IN ('user','admin','system')),
                sender_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES support_threads(id) ON DELETE CASCADE
            )
            """
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_support_threads_user ON support_threads(user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_support_threads_status ON support_threads(status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_support_threads_updated ON support_threads(updated_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_support_messages_thread ON support_messages(thread_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_support_messages_created ON support_messages(created_at);")

        await db.commit()


async def create_thread(db_path: str, user_id: int) -> int:
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            """
            INSERT INTO support_threads (user_id, admin_id, status, created_at, updated_at, closed_at)
            VALUES (?, NULL, 'active', ?, ?, NULL)
            """,
            (user_id, now, now),
        )
        await db.commit()
        return int(cur.lastrowid)


async def set_thread_admin(db_path: str, thread_id: int, admin_id: int) -> None:
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE support_threads SET admin_id=?, updated_at=? WHERE id=?",
            (admin_id, now, thread_id),
        )
        await db.commit()


async def touch_thread(db_path: str, thread_id: int) -> None:
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE support_threads SET updated_at=? WHERE id=?", (now, thread_id))
        await db.commit()


async def close_thread(db_path: str, thread_id: int) -> Optional[ThreadRow]:
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE support_threads
            SET status='closed', closed_at=?, updated_at=?
            WHERE id=? AND status='active'
            """,
            (now, now, thread_id),
        )
        await db.commit()

    return await get_thread(db_path, thread_id)


async def get_thread(db_path: str, thread_id: int) -> Optional[ThreadRow]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM support_threads WHERE id=?", (thread_id,))).fetchone()
        if not row:
            return None
        return ThreadRow(
            id=row["id"],
            user_id=row["user_id"],
            admin_id=row["admin_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            closed_at=row["closed_at"],
        )


async def add_message(db_path: str, thread_id: int, sender_role: Role, sender_id: int, text: str) -> int:
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            """
            INSERT INTO support_messages (thread_id, sender_role, sender_id, text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (thread_id, sender_role, sender_id, text, now),
        )
        await db.execute("UPDATE support_threads SET updated_at=? WHERE id=?", (now, thread_id))
        await db.commit()
        return int(cur.lastrowid)


async def list_threads_for_user(db_path: str, user_id: int, page: int, per_page: int = 8):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        total = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM support_threads WHERE user_id=?",
            (user_id,),
        )).fetchone())["c"]

        pages = max(1, math.ceil(total / per_page)) if total else 1
        page = max(0, min(page, pages - 1))
        offset = page * per_page

        rows = await (await db.execute(
            """
            SELECT * FROM support_threads
            WHERE user_id=?
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, per_page, offset),
        )).fetchall()

        return [ThreadRow(
            id=r["id"], user_id=r["user_id"], admin_id=r["admin_id"], status=r["status"],
            created_at=r["created_at"], updated_at=r["updated_at"], closed_at=r["closed_at"]
        ) for r in rows], total, pages, page


async def list_threads_for_admin(db_path: str, status: Status, page: int, per_page: int = 10):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        total = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM support_threads WHERE status=?",
            (status,),
        )).fetchone())["c"]

        pages = max(1, math.ceil(total / per_page)) if total else 1
        page = max(0, min(page, pages - 1))
        offset = page * per_page

        rows = await (await db.execute(
            """
            SELECT * FROM support_threads
            WHERE status=?
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (status, per_page, offset),
        )).fetchall()

        return [ThreadRow(
            id=r["id"], user_id=r["user_id"], admin_id=r["admin_id"], status=r["status"],
            created_at=r["created_at"], updated_at=r["updated_at"], closed_at=r["closed_at"]
        ) for r in rows], total, pages, page


async def list_messages_newest_page(db_path: str, thread_id: int, page_from_newest: int, per_page: int = 10):
    """
    page_from_newest=0 -> самая свежая страница (последние сообщения)
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        total = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM support_messages WHERE thread_id=?",
            (thread_id,),
        )).fetchone())["c"]

        pages = max(1, math.ceil(total / per_page)) if total else 1
        page_from_newest = max(0, min(page_from_newest, pages - 1))

        start = max(total - (page_from_newest + 1) * per_page, 0)
        limit = min(per_page, total - start) if total else per_page

        rows = await (await db.execute(
            """
            SELECT * FROM support_messages
            WHERE thread_id=?
            ORDER BY id ASC
            LIMIT ? OFFSET ?
            """,
            (thread_id, limit, start),
        )).fetchall()

        msgs = [MessageRow(
            id=r["id"], thread_id=r["thread_id"], sender_role=r["sender_role"], sender_id=r["sender_id"],
            text=r["text"], created_at=r["created_at"]
        ) for r in rows]

        return msgs, total, pages, page_from_newest


async def list_users_for_admin_picker(db_path: str, page: int, per_page: int = 10):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        total = (await (await db.execute(
            """
            SELECT COUNT(*) AS c
            FROM users
            WHERE COALESCE(status, '') != 'blocked'
            """
        )).fetchone())["c"]

        pages = max(1, math.ceil(total / per_page)) if total else 1
        page = max(0, min(page, pages - 1))
        offset = page * per_page

        rows = await (await db.execute(
            """
            SELECT
                tg_id,
                full_name,
                phone,
                status,
                updated_at
            FROM users
            WHERE COALESCE(status, '') != 'blocked'
            ORDER BY
                CASE WHEN status = 'approved' THEN 0 ELSE 1 END,
                COALESCE(updated_at, created_at) DESC,
                tg_id DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        )).fetchall()

        users = [SupportUserRow(
            tg_id=int(r["tg_id"]),
            full_name=r["full_name"],
            phone=r["phone"],
            status=r["status"],
            updated_at=r["updated_at"],
        ) for r in rows]

        return users, total, pages, page