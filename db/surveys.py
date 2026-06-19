from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def init_surveys_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS surveys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                select_mode TEXT NOT NULL CHECK(select_mode IN ('single', 'multiple')),
                file_id TEXT,
                file_kind TEXT CHECK(file_kind IN ('photo', 'document')),
                status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'sent')),
                sent_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                survey_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (survey_id) REFERENCES surveys(id) ON DELETE CASCADE
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                survey_id INTEGER NOT NULL,
                tg_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(survey_id, tg_id),
                FOREIGN KEY (survey_id) REFERENCES surveys(id) ON DELETE CASCADE,
                FOREIGN KEY (tg_id) REFERENCES users(tg_id) ON DELETE CASCADE
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_response_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                response_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                UNIQUE(response_id, option_id),
                FOREIGN KEY (response_id) REFERENCES survey_responses(id) ON DELETE CASCADE,
                FOREIGN KEY (option_id) REFERENCES survey_options(id) ON DELETE CASCADE
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_temp_votes (
                survey_id INTEGER NOT NULL,
                tg_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (survey_id, tg_id, option_id),
                FOREIGN KEY (survey_id) REFERENCES surveys(id) ON DELETE CASCADE,
                FOREIGN KEY (option_id) REFERENCES survey_options(id) ON DELETE CASCADE
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS survey_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                survey_id INTEGER NOT NULL,
                tg_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('sent', 'failed')),
                error TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(survey_id, tg_id),
                FOREIGN KEY (survey_id) REFERENCES surveys(id) ON DELETE CASCADE,
                FOREIGN KEY (tg_id) REFERENCES users(tg_id) ON DELETE CASCADE
            );
            """
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_surveys_created_at ON surveys(created_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_survey_options_survey_id ON survey_options(survey_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_survey_responses_survey_id ON survey_responses(survey_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_survey_deliveries_survey_id ON survey_deliveries(survey_id);")

        await db.commit()


async def create_survey(
    db_path: str,
    question: str,
    select_mode: str,
    file_id: str | None,
    file_kind: str | None,
    created_by: int,
    options: list[str],
) -> int:
    now = _utcnow()

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            """
            INSERT INTO surveys
                (question, select_mode, file_id, file_kind, status, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'draft', ?, ?, ?)
            """,
            (question, select_mode, file_id, file_kind, created_by, now, now),
        )
        survey_id = int(cur.lastrowid)

        for index, option_text in enumerate(options, start=1):
            await db.execute(
                """
                INSERT INTO survey_options (survey_id, text, sort_order)
                VALUES (?, ?, ?)
                """,
                (survey_id, option_text, index),
            )

        await db.commit()
        return survey_id


async def mark_survey_sent(db_path: str, survey_id: int, sent_count: int, failed_count: int) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE surveys
            SET status = 'sent', sent_count = ?, failed_count = ?, sent_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (sent_count, failed_count, now, now, survey_id),
        )
        await db.commit()


async def save_survey_delivery(
    db_path: str,
    survey_id: int,
    tg_id: int,
    status: str,
    error: str | None = None,
) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO survey_deliveries (survey_id, tg_id, status, error, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(survey_id, tg_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                created_at = excluded.created_at
            """,
            (survey_id, tg_id, status, error, now),
        )
        await db.commit()


async def count_surveys(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(1) FROM surveys WHERE status = 'sent'")
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def list_surveys(db_path: str, limit: int = 8, offset: int = 0) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                s.*,
                COUNT(DISTINCT r.id) AS voters_count,
                COUNT(ro.id) AS votes_count
            FROM surveys s
            LEFT JOIN survey_responses r ON r.survey_id = s.id
            LEFT JOIN survey_response_options ro ON ro.response_id = r.id
            WHERE s.status = 'sent'
            GROUP BY s.id
            ORDER BY COALESCE(s.sent_at, s.created_at) DESC, s.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def get_survey(db_path: str, survey_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM surveys WHERE id = ?", (survey_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_survey_options(db_path: str, survey_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, survey_id, text, sort_order
            FROM survey_options
            WHERE survey_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (survey_id,),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def option_belongs_to_survey(db_path: str, survey_id: int, option_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT 1 FROM survey_options WHERE survey_id = ? AND id = ? LIMIT 1",
            (survey_id, option_id),
        )
        row = await cur.fetchone()
        return row is not None


async def user_has_response(db_path: str, survey_id: int, tg_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT 1 FROM survey_responses WHERE survey_id = ? AND tg_id = ? LIMIT 1",
            (survey_id, tg_id),
        )
        row = await cur.fetchone()
        return row is not None


async def save_single_response(db_path: str, survey_id: int, tg_id: int, option_id: int) -> bool:
    now = _utcnow()

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        cur = await db.execute(
            "SELECT id FROM survey_responses WHERE survey_id = ? AND tg_id = ? LIMIT 1",
            (survey_id, tg_id),
        )
        row = await cur.fetchone()
        if row:
            return False

        cur = await db.execute(
            "INSERT INTO survey_responses (survey_id, tg_id, created_at) VALUES (?, ?, ?)",
            (survey_id, tg_id, now),
        )
        response_id = int(cur.lastrowid)

        await db.execute(
            "INSERT INTO survey_response_options (response_id, option_id) VALUES (?, ?)",
            (response_id, option_id),
        )
        await db.execute(
            "DELETE FROM survey_temp_votes WHERE survey_id = ? AND tg_id = ?",
            (survey_id, tg_id),
        )
        await db.commit()
        return True


async def get_temp_selected_options(db_path: str, survey_id: int, tg_id: int) -> set[int]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT option_id FROM survey_temp_votes WHERE survey_id = ? AND tg_id = ?",
            (survey_id, tg_id),
        )
        rows = await cur.fetchall()
        return {int(row[0]) for row in rows}


async def toggle_temp_option(db_path: str, survey_id: int, tg_id: int, option_id: int) -> set[int]:
    now = _utcnow()

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            """
            SELECT 1 FROM survey_temp_votes
            WHERE survey_id = ? AND tg_id = ? AND option_id = ?
            LIMIT 1
            """,
            (survey_id, tg_id, option_id),
        )
        row = await cur.fetchone()

        if row:
            await db.execute(
                "DELETE FROM survey_temp_votes WHERE survey_id = ? AND tg_id = ? AND option_id = ?",
                (survey_id, tg_id, option_id),
            )
        else:
            await db.execute(
                """
                INSERT OR IGNORE INTO survey_temp_votes (survey_id, tg_id, option_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (survey_id, tg_id, option_id, now),
            )

        await db.commit()

    return await get_temp_selected_options(db_path, survey_id, tg_id)


async def finalize_multiple_response(db_path: str, survey_id: int, tg_id: int) -> tuple[bool, int]:
    now = _utcnow()

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        cur = await db.execute(
            "SELECT option_id FROM survey_temp_votes WHERE survey_id = ? AND tg_id = ? ORDER BY option_id ASC",
            (survey_id, tg_id),
        )
        rows = await cur.fetchall()
        option_ids = [int(row[0]) for row in rows]

        if not option_ids:
            return False, 0

        cur = await db.execute(
            "SELECT id FROM survey_responses WHERE survey_id = ? AND tg_id = ? LIMIT 1",
            (survey_id, tg_id),
        )
        exists = await cur.fetchone()
        if exists:
            return False, len(option_ids)

        cur = await db.execute(
            "INSERT INTO survey_responses (survey_id, tg_id, created_at) VALUES (?, ?, ?)",
            (survey_id, tg_id, now),
        )
        response_id = int(cur.lastrowid)

        for option_id in option_ids:
            await db.execute(
                "INSERT OR IGNORE INTO survey_response_options (response_id, option_id) VALUES (?, ?)",
                (response_id, option_id),
            )

        await db.execute(
            "DELETE FROM survey_temp_votes WHERE survey_id = ? AND tg_id = ?",
            (survey_id, tg_id),
        )
        await db.commit()
        return True, len(option_ids)


async def get_survey_results(db_path: str, survey_id: int) -> dict[str, Any] | None:
    survey = await get_survey(db_path, survey_id)
    if not survey:
        return None

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            """
            SELECT COUNT(1) AS voters_count
            FROM survey_responses
            WHERE survey_id = ?
            """,
            (survey_id,),
        )
        voters_row = await cur.fetchone()
        voters_count = int(dict(voters_row).get("voters_count") if voters_row else 0)

        cur = await db.execute(
            """
            SELECT COUNT(ro.id) AS choices_count
            FROM survey_response_options ro
            INNER JOIN survey_responses r ON r.id = ro.response_id
            WHERE r.survey_id = ?
            """,
            (survey_id,),
        )
        choices_row = await cur.fetchone()
        choices_count = int(dict(choices_row).get("choices_count") if choices_row else 0)

        cur = await db.execute(
            """
            SELECT
                o.id,
                o.text,
                o.sort_order,
                COUNT(ro.id) AS votes_count
            FROM survey_options o
            LEFT JOIN survey_response_options ro ON ro.option_id = o.id
            LEFT JOIN survey_responses r ON r.id = ro.response_id AND r.survey_id = o.survey_id
            WHERE o.survey_id = ?
            GROUP BY o.id
            ORDER BY o.sort_order ASC, o.id ASC
            """,
            (survey_id,),
        )
        options = [dict(row) for row in await cur.fetchall()]

        cur = await db.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS delivered,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM survey_deliveries
            WHERE survey_id = ?
            """,
            (survey_id,),
        )
        delivery_row = await cur.fetchone()
        delivery = dict(delivery_row) if delivery_row else {}

    survey["voters_count"] = voters_count
    survey["choices_count"] = choices_count
    survey["delivered_count"] = int(delivery.get("delivered") or survey.get("sent_count") or 0)
    survey["delivery_failed_count"] = int(delivery.get("failed") or survey.get("failed_count") or 0)
    survey["options"] = options
    return survey


async def get_survey_export_rows(db_path: str, survey_id: int) -> dict[str, Any] | None:
    survey = await get_survey_results(db_path, survey_id)
    if not survey:
        return None

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            """
            SELECT
                r.tg_id,
                COALESCE(u.full_name, '') AS full_name,
                COALESCE(u.phone, '') AS phone,
                r.created_at,
                GROUP_CONCAT(o.text, '; ') AS selected_options
            FROM survey_responses r
            LEFT JOIN users u ON u.tg_id = r.tg_id
            LEFT JOIN survey_response_options ro ON ro.response_id = r.id
            LEFT JOIN survey_options o ON o.id = ro.option_id
            WHERE r.survey_id = ?
            GROUP BY r.id
            ORDER BY r.created_at ASC
            """,
            (survey_id,),
        )
        responses = [dict(row) for row in await cur.fetchall()]

        cur = await db.execute(
            """
            SELECT
                d.tg_id,
                COALESCE(u.full_name, '') AS full_name,
                COALESCE(u.phone, '') AS phone,
                d.status,
                COALESCE(d.error, '') AS error,
                d.created_at
            FROM survey_deliveries d
            LEFT JOIN users u ON u.tg_id = d.tg_id
            LEFT JOIN survey_responses r ON r.survey_id = d.survey_id AND r.tg_id = d.tg_id
            WHERE d.survey_id = ? AND r.id IS NULL
            ORDER BY d.status ASC, d.tg_id ASC
            """,
            (survey_id,),
        )
        not_answered = [dict(row) for row in await cur.fetchall()]

    return {
        "survey": survey,
        "responses": responses,
        "not_answered": not_answered,
    }
