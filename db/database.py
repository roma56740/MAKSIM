import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from db.ai_user_chat import init_ai_user_chat_db
import aiosqlite
from db.support_chat import create_tables as create_support_chat_tables


from db.ai_instructions import init_ai_db

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def init_db(db_path: str) -> None:
    Path(os.path.dirname(db_path) or ".").mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON;")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                full_name TEXT,
                phone TEXT,
                reg_type TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        cur = await db.execute("PRAGMA table_info(users)")
        user_cols = [r[1] for r in await cur.fetchall()]
        if "username" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN username TEXT;")
        if "first_name" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN first_name TEXT;")
        # --------------------- BILLS (счета на оплату) ---------------------
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,

                text TEXT,
                file_id TEXT,
                file_kind TEXT,

                status TEXT NOT NULL DEFAULT 'pending', -- pending/paid/rejected
                reason TEXT,

                paid_at TEXT,
                handled_by INTEGER,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                FOREIGN KEY (tg_id) REFERENCES users(tg_id) ON DELETE CASCADE
            );
            """
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_bills_tg_id ON bills(tg_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bills_status ON bills(status);")

        # --- миграции для уже существующих БД (если таблица bills была создана раньше без колонок) ---
        cur = await db.execute("PRAGMA table_info(bills)")
        bills_cols = [r[1] for r in await cur.fetchall()]
        if bills_cols:
            if "reason" not in bills_cols:
                await db.execute("ALTER TABLE bills ADD COLUMN reason TEXT;")
            if "paid_at" not in bills_cols:
                await db.execute("ALTER TABLE bills ADD COLUMN paid_at TEXT;")
            if "handled_by" not in bills_cols:
                await db.execute("ALTER TABLE bills ADD COLUMN handled_by INTEGER;")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                tg_id INTEGER PRIMARY KEY,
                reg_type TEXT NOT NULL,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                file_id TEXT,
                file_kind TEXT,
                status TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                tg_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            """
        )

        # --------------------- SUPPLIERS ---------------------
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                website TEXT,
                email TEXT,
                phone TEXT,
                description TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        # --- миграции для уже существующих БД ---
        cur = await db.execute("PRAGMA table_info(suppliers)")
        s_cols = [r[1] for r in await cur.fetchall()]
        if s_cols:
            if "email" not in s_cols:
                await db.execute("ALTER TABLE suppliers ADD COLUMN email TEXT;")
            if "phone" not in s_cols:
                await db.execute("ALTER TABLE suppliers ADD COLUMN phone TEXT;")
            if "description" not in s_cols:
                await db.execute("ALTER TABLE suppliers ADD COLUMN description TEXT;")

        # --------------------- INVOICES / DEALS (накладные) ---------------------
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                supplier_id INTEGER,

                deal_amount REAL,
                reward_amount REAL,

                file_id TEXT,
                file_kind TEXT,
                comment TEXT,

                status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
                reason TEXT,
                handled_at TEXT,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                FOREIGN KEY (tg_id) REFERENCES users(tg_id) ON DELETE CASCADE,
                FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
            );
            """
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoices_tg_id ON invoices(tg_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);")

        cur = await db.execute("PRAGMA table_info(invoices)")
        invoice_cols = [r[1] for r in await cur.fetchall()]
        if invoice_cols:
            if "handled_at" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN handled_at TEXT;")
            if "source_file_name" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN source_file_name TEXT;")
            if "source_mime_type" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN source_mime_type TEXT;")
            if "analysis_status" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN analysis_status TEXT NOT NULL DEFAULT 'pending';")
            if "analysis_json" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN analysis_json TEXT;")
            if "analysis_error" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN analysis_error TEXT;")
            if "analyzed_at" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN analyzed_at TEXT;")
            if "invoice_number" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN invoice_number TEXT;")
            if "invoice_number_key" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN invoice_number_key TEXT;")
            if "invoice_date" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN invoice_date TEXT;")
            if "company_name" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN company_name TEXT;")
            if "company_key" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN company_key TEXT;")
            if "document_total" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN document_total REAL;")
            if "duplicate_of_id" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN duplicate_of_id INTEGER;")
            if "duplicate_warning_json" not in invoice_cols:
                await db.execute("ALTER TABLE invoices ADD COLUMN duplicate_warning_json TEXT;")

            await db.execute(
                """
                UPDATE invoices
                SET handled_at = updated_at
                WHERE handled_at IS NULL
                  AND status IN ('approved', 'rejected');
                """
            )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoices_handled_at ON invoices(handled_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoices_analysis_status ON invoices(analysis_status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoices_number_key ON invoices(invoice_number_key);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoices_date_total ON invoices(invoice_date, document_total);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoices_company_key ON invoices(company_key);")

        # --------------------- INVOICE ITEMS (товары из накладных) ---------------------
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS invoice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                product_key TEXT,
                quantity REAL NOT NULL,
                unit_price REAL NOT NULL,
                line_total REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
            );
            """
        )
        cur = await db.execute("PRAGMA table_info(invoice_items)")
        invoice_item_cols = [r[1] for r in await cur.fetchall()]
        if "product_key" not in invoice_item_cols:
            await db.execute("ALTER TABLE invoice_items ADD COLUMN product_key TEXT;")

        # SQLite LOWER/NOCASE не приводит кириллицу к одному регистру.
        # Нормализуем ключ Python-методом casefold, включая старые строки.
        rows = await (await db.execute(
            "SELECT id, product_name FROM invoice_items "
            "WHERE product_key IS NULL OR TRIM(product_key) = ''"
        )).fetchall()
        for item_id, product_name in rows:
            product_key = " ".join(str(product_name or "").split()).casefold()
            await db.execute(
                "UPDATE invoice_items SET product_key = ? WHERE id = ?",
                (product_key, item_id),
            )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice_id ON invoice_items(invoice_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoice_items_product_name ON invoice_items(product_name);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invoice_items_product_key ON invoice_items(product_key);")

        # --------------------- PROMOTIONS / SPECIAL OFFERS ---------------------
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS promotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'promotion',
                title TEXT NOT NULL,
                text TEXT,
                file_id TEXT,
                file_kind TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT,
                archived_at TEXT,
                created_by INTEGER,
                duplicated_from INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (duplicated_from) REFERENCES promotions(id) ON DELETE SET NULL
            );
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_promotions_status ON promotions(status);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_promotions_expires_at ON promotions(expires_at);")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS promotion_deliveries (
                promotion_id INTEGER NOT NULL,
                tg_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT,
                PRIMARY KEY (promotion_id, tg_id, message_id),
                FOREIGN KEY (promotion_id) REFERENCES promotions(id) ON DELETE CASCADE
            );
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_promotion_deliveries_active ON promotion_deliveries(promotion_id, deleted_at);")

        # --------------------- TELEGRAM BUSINESS CONNECTIONS ---------------------
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_business_connections (
                connection_id TEXT PRIMARY KEY,
                admin_tg_id INTEGER NOT NULL,
                user_chat_id INTEGER,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                rights_json TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_business_connections_admin ON telegram_business_connections(admin_tg_id, is_enabled);")

        # --------------------- PAYOUTS (выплаты) ---------------------
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,

                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', -- pending/paid/rejected/canceled
                period_start TEXT,
                period_end TEXT,
                comment TEXT,

                paid_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                FOREIGN KEY (tg_id) REFERENCES users(tg_id) ON DELETE CASCADE
            );
            """
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_payouts_tg_id ON payouts(tg_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_payouts_status ON payouts(status);")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_prices (
                supplier_id INTEGER PRIMARY KEY,
                tg_file_id TEXT NOT NULL,
                file_name TEXT,
                uploaded_by INTEGER,
                uploaded_at TEXT NOT NULL,
                FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS kp_sessions (
                tg_id INTEGER PRIMARY KEY,
                supplier_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
            );
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS kp_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                supplier_id INTEGER NOT NULL,
                product_id INTEGER,

                title TEXT,
                description TEXT,
                price REAL,
                final_price REAL,
                url TEXT,

                image_url TEXT,
                image_path TEXT,
                qty INTEGER NOT NULL DEFAULT 1,
                extra_json TEXT,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE SET NULL
            );
            """
        )

        await db.execute("CREATE INDEX IF NOT EXISTS idx_kp_items_tg_id ON kp_items(tg_id);")

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER NOT NULL,

                source_pk TEXT,
                code TEXT NOT NULL,

                -- НОВЫЕ ПОЛЯ под ваш прайс
                title TEXT,
                strength TEXT,
                volume REAL,

                -- СТАРЫЕ (оставляем для совместимости со старым выводом)
                description TEXT,
                price REAL,
                discount_percent REAL,
                final_price REAL,
                product_type TEXT,
                stock_qty INTEGER,
                url TEXT,

                image_url TEXT,
                image_path TEXT,
                extra_json TEXT,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                UNIQUE (supplier_id, code),
                FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE
            );
            """
        )

        # --- миграции для уже существующих БД ---
        cur = await db.execute("PRAGMA table_info(products)")
        cols = [r[1] for r in await cur.fetchall()]

        # старые миграции
        if "image_path" not in cols:
            await db.execute("ALTER TABLE products ADD COLUMN image_path TEXT;")

        # новые колонки под новый Excel
        if "title" not in cols:
            await db.execute("ALTER TABLE products ADD COLUMN title TEXT;")
        if "strength" not in cols:
            await db.execute("ALTER TABLE products ADD COLUMN strength TEXT;")
        if "volume" not in cols:
            await db.execute("ALTER TABLE products ADD COLUMN volume REAL;")

        # --------------------- AI INSTRUCTIONS ---------------------
        await init_ai_db(conn=db)
        # --------------------- AI USER CHAT ---------------------
        await init_ai_user_chat_db(conn=db)

        await db.commit()

    # Открываем отдельное соединение только после фиксации основной миграции,
    # чтобы SQLite не блокировал базу при первом запуске/обновлении.
    await create_support_chat_tables(db_path)


async def is_admin(db_path: str, tg_id: int, env_admin_ids: set[int]) -> bool:
    if tg_id in env_admin_ids:
        return True

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE tg_id = ? LIMIT 1", (tg_id,))
        row = await cur.fetchone()
        return row is not None


async def list_db_admin_ids(db_path: str) -> list[int]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT tg_id FROM admins ORDER BY tg_id ASC")
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


async def add_admin(db_path: str, tg_id: int) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins (tg_id, created_at) VALUES (?, ?)",
            (tg_id, now),
        )
        await db.commit()


async def remove_admin(db_path: str, tg_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM admins WHERE tg_id = ?", (tg_id,))
        await db.commit()


async def list_user_ids(db_path: str, exclude_statuses: Iterable[str] = ("blocked",)) -> list[int]:
    excl = list(exclude_statuses)
    placeholders = ",".join(["?"] * len(excl)) if excl else ""
    query = "SELECT tg_id FROM users"
    params: list[Any] = []

    if excl:
        query += f" WHERE status NOT IN ({placeholders})"
        params.extend(excl)

    query += " ORDER BY tg_id ASC"

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(query, tuple(params))
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


async def get_user(db_path: str, tg_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_registration(db_path: str, tg_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM registrations WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def upsert_registration(
    db_path: str,
    tg_id: int,
    reg_type: str,
    full_name: str,
    phone: str,
    file_id: str | None,
    file_kind: str | None,
    username: str | None = None,
    first_name: str | None = None,
) -> None:
    now = _utcnow()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO registrations (
                tg_id, reg_type, full_name, phone, file_id, file_kind, status, reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                reg_type = excluded.reg_type,
                full_name = excluded.full_name,
                phone = excluded.phone,
                file_id = excluded.file_id,
                file_kind = excluded.file_kind,
                status = 'pending',
                reason = NULL,
                updated_at = excluded.updated_at
            """,
            (tg_id, reg_type, full_name, phone, file_id, file_kind, now, now),
        )

        await db.execute(
            """
            INSERT INTO users (
                tg_id, full_name, phone, reg_type, status,
                username, first_name, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                full_name = excluded.full_name,
                phone = excluded.phone,
                reg_type = excluded.reg_type,
                status = 'pending',
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = excluded.updated_at
            """,
            (tg_id, full_name, phone, reg_type, username, first_name, now, now),
        )

        await db.commit()


async def list_registrations(
    db_path: str,
    status: str,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM registrations
            WHERE status = ?
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (status, limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def approve_registration(db_path: str, tg_id: int) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE registrations
            SET status = 'approved', reason = NULL, updated_at = ?
            WHERE tg_id = ?
            """,
            (now, tg_id),
        )
        await db.execute(
            """
            UPDATE users
            SET status = 'approved', updated_at = ?
            WHERE tg_id = ?
            """,
            (now, tg_id),
        )
        await db.commit()


async def reject_registration(db_path: str, tg_id: int, reason: str) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE registrations
            SET status = 'rejected', reason = ?, updated_at = ?
            WHERE tg_id = ?
            """,
            (reason, now, tg_id),
        )
        await db.execute(
            """
            UPDATE users
            SET status = 'rejected', updated_at = ?
            WHERE tg_id = ?
            """,
            (now, tg_id),
        )
        await db.commit()


async def block_user(db_path: str, tg_id: int) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE users
            SET status = 'blocked', updated_at = ?
            WHERE tg_id = ?
            """,
            (now, tg_id),
        )
        await db.commit()


async def count_admin_users(db_path: str, status: str = "all") -> int:
    where = ""
    params: list[Any] = []

    if status != "all":
        where = "WHERE status = ?"
        params.append(status)

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(f"SELECT COUNT(1) FROM users {where}", tuple(params))
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def list_admin_users(
    db_path: str,
    status: str = "all",
    limit: int = 8,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = ""
    params: list[Any] = []

    if status != "all":
        where = "WHERE u.status = ?"
        params.append(status)

    params.extend([int(limit), int(offset)])

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT
                u.tg_id,
                u.full_name,
                u.phone,
                u.reg_type,
                u.status,
                u.created_at,
                u.updated_at,

                (SELECT COUNT(1) FROM invoices i WHERE i.tg_id = u.tg_id) AS invoices_total,
                (SELECT COALESCE(SUM(CASE WHEN i.status='approved' THEN COALESCE(i.reward_amount,0) ELSE 0 END),0)
                    FROM invoices i WHERE i.tg_id = u.tg_id) AS reward_sum,
                (SELECT COUNT(1) FROM bills b WHERE b.tg_id = u.tg_id) AS bills_total,
                (SELECT COUNT(1) FROM kp_items k WHERE k.tg_id = u.tg_id) AS kp_items_total,
                (SELECT COUNT(1) FROM support_threads st WHERE st.user_id = u.tg_id AND st.status='active') AS active_chats
            FROM users u
            {where}
            ORDER BY
                CASE u.status
                    WHEN 'pending' THEN 1
                    WHEN 'approved' THEN 2
                    WHEN 'rejected' THEN 3
                    WHEN 'blocked' THEN 4
                    ELSE 5
                END,
                u.updated_at DESC,
                u.created_at DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_admin_user_analytics(db_path: str, tg_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        user = await (await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))).fetchone()
        if not user:
            return None

        registration = await (await db.execute(
            "SELECT * FROM registrations WHERE tg_id = ?",
            (tg_id,),
        )).fetchone()

        invoices = await (await db.execute(
            """
            SELECT
                COUNT(1) AS total,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
                COALESCE(SUM(CASE WHEN status='approved' THEN COALESCE(deal_amount,0) ELSE 0 END),0) AS approved_deal_sum,
                COALESCE(SUM(CASE WHEN status='approved' THEN COALESCE(reward_amount,0) ELSE 0 END),0) AS approved_reward_sum,
                COALESCE(SUM(CASE WHEN status='pending' THEN COALESCE(deal_amount,0) ELSE 0 END),0) AS pending_deal_sum,
                MAX(COALESCE(handled_at, updated_at, created_at)) AS last_at
            FROM invoices
            WHERE tg_id = ?
            """,
            (tg_id,),
        )).fetchone()

        payouts = await (await db.execute(
            """
            SELECT
                COUNT(1) AS total,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS paid,
                SUM(CASE WHEN status IN ('rejected','canceled') THEN 1 ELSE 0 END) AS rejected,
                COALESCE(SUM(CASE WHEN status='paid' THEN COALESCE(amount,0) ELSE 0 END),0) AS paid_sum,
                COALESCE(SUM(CASE WHEN status='pending' THEN COALESCE(amount,0) ELSE 0 END),0) AS pending_sum,
                MAX(COALESCE(paid_at, updated_at, created_at)) AS last_at
            FROM payouts
            WHERE tg_id = ?
            """,
            (tg_id,),
        )).fetchone()

        bills = await (await db.execute(
            """
            SELECT
                COUNT(1) AS total,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS paid,
                SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
                MAX(COALESCE(paid_at, updated_at, created_at)) AS last_at
            FROM bills
            WHERE tg_id = ?
            """,
            (tg_id,),
        )).fetchone()

        kp = await (await db.execute(
            """
            SELECT
                COUNT(1) AS items_total,
                COALESCE(SUM(COALESCE(qty,1)),0) AS qty_total,
                COALESCE(SUM(COALESCE(final_price, price, 0) * COALESCE(qty,1)),0) AS sum_total,
                COUNT(DISTINCT supplier_id) AS suppliers_total,
                MAX(updated_at) AS last_at
            FROM kp_items
            WHERE tg_id = ?
            """,
            (tg_id,),
        )).fetchone()

        kp_session = await (await db.execute(
            """
            SELECT
                ks.tg_id,
                ks.supplier_id,
                ks.created_at,
                ks.updated_at,
                s.name AS supplier_name
            FROM kp_sessions ks
            LEFT JOIN suppliers s ON s.id = ks.supplier_id
            WHERE ks.tg_id = ?
            """,
            (tg_id,),
        )).fetchone()

        ai = await (await db.execute(
            """
            SELECT
                COUNT(1) AS messages_total,
                SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS user_messages,
                SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) AS assistant_messages,
                MAX(created_at) AS last_at
            FROM ai_dialog_messages
            WHERE tg_id = ?
            """,
            (tg_id,),
        )).fetchone()

        ai_searches = await (await db.execute(
            """
            SELECT COUNT(1) AS total, MAX(created_at) AS last_at
            FROM ai_product_search_sessions
            WHERE tg_id = ?
            """,
            (tg_id,),
        )).fetchone()

        support = await (await db.execute(
            """
            SELECT
                COUNT(1) AS threads_total,
                SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active_threads,
                SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_threads,
                MAX(updated_at) AS last_at
            FROM support_threads
            WHERE user_id = ?
            """,
            (tg_id,),
        )).fetchone()

        support_messages = await (await db.execute(
            """
            SELECT COUNT(1) AS messages_total, MAX(sm.created_at) AS last_at
            FROM support_messages sm
            INNER JOIN support_threads st ON st.id = sm.thread_id
            WHERE st.user_id = ?
            """,
            (tg_id,),
        )).fetchone()

        inv_d = dict(invoices) if invoices else {}
        pay_d = dict(payouts) if payouts else {}

        earned = float(inv_d.get("approved_reward_sum") or 0.0)
        paid = float(pay_d.get("paid_sum") or 0.0)

        return {
            "user": dict(user),
            "registration": dict(registration) if registration else None,
            "invoices": inv_d,
            "payouts": pay_d,
            "bills": dict(bills) if bills else {},
            "kp": dict(kp) if kp else {},
            "kp_session": dict(kp_session) if kp_session else None,
            "ai": dict(ai) if ai else {},
            "ai_searches": dict(ai_searches) if ai_searches else {},
            "support": dict(support) if support else {},
            "support_messages": dict(support_messages) if support_messages else {},
            "balance": {
                "earned": earned,
                "paid": paid,
                "available": earned - paid,
                "pending_payouts": float(pay_d.get("pending_sum") or 0.0),
            },
        }


# --------------------- SUPPLIERS ---------------------

async def count_suppliers(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(1) FROM suppliers")
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def list_suppliers(db_path: str, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, name, website, email, phone, description, created_at, updated_at
            FROM suppliers
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_supplier(db_path: str, supplier_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, name, website, email, phone, description, created_at, updated_at
            FROM suppliers WHERE id = ?
            """,
            (supplier_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def create_supplier(
    db_path: str,
    name: str,
    website: str | None,
    email: str | None,
    phone: str | None,
    description: str | None,
) -> int:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """
            INSERT INTO suppliers (name, website, email, phone, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, website, email, phone, description, now, now),
        )
        await db.commit()
        return int(cur.lastrowid)


async def update_supplier(
    db_path: str,
    supplier_id: int,
    name: str | None = None,
    website: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    description: str | None = None,
) -> None:
    now = _utcnow()

    fields: list[str] = []
    params: list[Any] = []

    if name is not None:
        fields.append("name = ?")
        params.append(name)

    if website is not None:
        fields.append("website = ?")
        params.append(website)

    if email is not None:
        fields.append("email = ?")
        params.append(email)

    if phone is not None:
        fields.append("phone = ?")
        params.append(phone)

    if description is not None:
        fields.append("description = ?")
        params.append(description)

    fields.append("updated_at = ?")
    params.append(now)

    params.append(supplier_id)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"UPDATE suppliers SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )
        await db.commit()


async def delete_supplier(db_path: str, supplier_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))
        await db.commit()
