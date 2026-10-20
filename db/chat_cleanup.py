"""Persistent deletion schedule for temporary messages in managers' bot chats."""
import aiosqlite


async def init_chat_cleanup_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS manager_message_cleanup (
                bot_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sent_at REAL NOT NULL,
                delete_at REAL NOT NULL,
                PRIMARY KEY (bot_id, chat_id, message_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_manager_cleanup_due ON manager_message_cleanup(bot_id, delete_at)")
        await db.commit()


async def schedule_message(db_path, bot_id, chat_id, message_id, sent_at, delete_at):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            INSERT INTO manager_message_cleanup (bot_id, chat_id, message_id, sent_at, delete_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (bot_id, chat_id, message_id) DO NOTHING
        """, (bot_id, chat_id, message_id, sent_at, delete_at))
        await db.commit()


async def due_messages(db_path, bot_id, now, limit=100):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("""
            SELECT * FROM manager_message_cleanup
            WHERE bot_id=? AND delete_at<=? ORDER BY delete_at, chat_id, message_id LIMIT ?
        """, (bot_id, now, limit))).fetchall()
        return [dict(row) for row in rows]


async def forget_message(db_path, bot_id, chat_id, message_id):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM manager_message_cleanup WHERE bot_id=? AND chat_id=? AND message_id=?",
                         (bot_id, chat_id, message_id))
        await db.commit()


async def retry_message(db_path, bot_id, chat_id, message_id, delete_at):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE manager_message_cleanup SET delete_at=? WHERE bot_id=? AND chat_id=? AND message_id=?",
                         (delete_at, bot_id, chat_id, message_id))
        await db.commit()


async def postpone_due_messages(db_path, bot_id, now, retry_at):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE manager_message_cleanup SET delete_at=? WHERE bot_id=? AND delete_at<=?",
                         (retry_at, bot_id, now))
        await db.commit()
