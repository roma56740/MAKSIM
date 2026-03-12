from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite


# ---------- time helpers ----------

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- robust search helpers (unicode-safe) ----------

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:[-_][0-9A-Za-zА-Яа-яЁё]+)*", re.U)

_STOPWORDS = {
    "пожалуйста", "плиз", "пж", "можешь", "можно", "давай",
    "подбери", "подберите", "подобери", "подобрать",
    "найди", "найдите", "покажи", "покажите", "ищу", "хочу", "нужно", "нужен", "нужна", "нужны",
    "посоветуй", "посоветуйте", "подскажи", "подскажите",
    "мне", "для", "меня", "нам", "нас", "ему", "ей",
    "сам", "сама", "сами", "самое", "самый",
    "какой", "какая", "какое", "какие",
    "любой", "любое", "любая", "любые",
    "вот", "это", "эти", "тот", "та", "те",
    "пожалуй", "вообще",
}

_MAX_TOKENS_DEFAULT = 8


def _cf(s: Any) -> str:
    return str(s).casefold().strip()


def _uniq_keep_order(items: Iterable[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _tokenize_query(raw: str, *, max_tokens: int = _MAX_TOKENS_DEFAULT) -> list[str]:
    t = (raw or "").strip()
    if not t:
        return []
    t_cf = t.casefold()
    tokens = [_cf(x) for x in _WORD_RE.findall(t_cf)]
    clean: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        if tok in _STOPWORDS:
            continue
        if len(tok) < 2 and not tok.isdigit():
            continue
        clean.append(tok)

    clean = _uniq_keep_order(clean)
    if len(clean) > max_tokens:
        clean = clean[:max_tokens]
    return clean


def prepare_search_query(raw: str, max_tokens: int = _MAX_TOKENS_DEFAULT) -> str:
    toks = _tokenize_query(raw, max_tokens=max_tokens)
    if not toks:
        raw = (raw or "").strip()
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw[:120]
    return " ".join(toks)[:120]


def _build_search_text(data: dict[str, Any]) -> str:
    parts = [
        data.get("code"),
        data.get("source_pk"),
        data.get("title"),
        data.get("strength"),
        data.get("volume"),
        data.get("product_type"),
        data.get("description"),
        data.get("url"),
    ]
    joined = " ".join([str(p) for p in parts if p not in (None, "", "—")])
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined.casefold()


_schema_lock = asyncio.Lock()
_schema_ready: set[str] = set()


async def _ensure_products_search_schema(*, db_path: str, conn: aiosqlite.Connection | None = None) -> None:
    """
    Авто-миграция:
      - добавляет products.search_text TEXT
      - создаёт уникальный индекс (supplier_id, code), если его нет
      - бекфиллит search_text для существующих строк
    """
    async with _schema_lock:
        if db_path in _schema_ready:
            return

        must_close = False
        db = conn
        if db is None:
            db = await aiosqlite.connect(db_path)
            must_close = True

        try:
            # PRAGMA table_info может упасть, если таблицы нет
            try:
                cur = await db.execute("PRAGMA table_info(products);")
                cols = [r[1] for r in await cur.fetchall()]
            except Exception:
                cols = []

            if cols:
                if "search_text" not in cols:
                    await db.execute("ALTER TABLE products ADD COLUMN search_text TEXT;")
                    cols.append("search_text")

                # на всякий случай — чтобы ON CONFLICT(supplier_id, code) точно работал
                await db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_supplier_code ON products(supplier_id, code);"
                )

                # бекфилл search_text только для пустых
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    """
                    SELECT id, code, source_pk, title, strength, volume, product_type, description, url, search_text
                    FROM products
                    WHERE search_text IS NULL OR search_text = '';
                    """
                )
                rows = await cur.fetchall()
                for r in rows:
                    data = dict(r)
                    sid = int(data["id"])
                    st = _build_search_text(data)
                    await db.execute("UPDATE products SET search_text = ? WHERE id = ?;", (st, sid))

            if must_close:
                await db.commit()

            _schema_ready.add(db_path)
        finally:
            if must_close:
                await db.close()


def _build_where_and_params(tokens: list[str], alias: str = "p", *, mode: str = "and") -> tuple[str, list[Any]]:
    """
    mode:
      - "and": все токены должны встретиться (точнее)
      - "or" : достаточно любого токена (шире)
    """
    if not tokens:
        return "1=0", []

    conds = [f"instr({alias}.search_text, ?) > 0" for _ in tokens]
    joiner = " OR " if (mode or "").lower() == "or" else " AND "
    where = "(" + joiner.join(conds) + ")"
    return where, list(tokens)


def _build_score_expr(tokens: list[str], alias: str = "p") -> tuple[str, list[Any]]:
    if not tokens:
        return "0", []

    # чуть умнее: длинные токены дают больше веса
    weights = [10 + min(10, len(t or "")) for t in tokens]
    parts = [f"(CASE WHEN instr({alias}.search_text, ?) > 0 THEN {w} ELSE 0 END)" for w in weights]
    expr = "(" + " + ".join(parts) + ")"
    return expr, list(tokens)

async def search_products_by_text(
    db_path: str,
    supplier_id: int,
    raw_query: str,
    limit: int,
    offset: int,
    *,
    in_stock_only: bool = False,
    mode: str = "and",
) -> tuple[int, list[dict[str, Any]]]:
    tokens = _tokenize_query(raw_query)
    if not tokens:
        return 0, []

    await _ensure_products_search_schema(db_path=db_path)

    where_sql, where_params = _build_where_and_params(tokens, alias="p", mode=mode)
    score_expr, score_params = _build_score_expr(tokens, alias="p")

    stock_clause = "AND COALESCE(p.stock_qty,0) > 0" if in_stock_only else ""

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            f"""
            SELECT COUNT(1)
            FROM products p
            WHERE p.supplier_id = ?
              {stock_clause}
              AND {where_sql}
            """,
            [int(supplier_id)] + where_params,
        )
        row = await cur.fetchone()
        await cur.close()

        total = int(row[0] or 0) if row else 0
        if total <= 0:
            return 0, []

        cur = await db.execute(
            f"""
            SELECT p.*, {score_expr} AS _score
            FROM products p
            WHERE p.supplier_id = ?
              {stock_clause}
              AND {where_sql}
            ORDER BY _score DESC, COALESCE(p.stock_qty,0) DESC, p.id DESC
            LIMIT ? OFFSET ?
            """,
            score_params + [int(supplier_id)] + where_params + [int(limit), int(offset)],
        )
        rows = await cur.fetchall()
        await cur.close()

        return total, [dict(r) for r in rows]
# -------------------- supplier_prices --------------------

async def upsert_supplier_price(
    db_path: str,
    supplier_id: int,
    tg_file_id: str,
    file_name: str | None,
    uploaded_by: int | None,
) -> None:
    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO supplier_prices (supplier_id, tg_file_id, file_name, uploaded_by, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(supplier_id) DO UPDATE SET
                tg_file_id = excluded.tg_file_id,
                file_name = excluded.file_name,
                uploaded_by = excluded.uploaded_by,
                uploaded_at = excluded.uploaded_at
            """,
            (supplier_id, tg_file_id, file_name, uploaded_by, now),
        )
        await db.commit()


async def get_supplier_price(db_path: str, supplier_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM supplier_prices WHERE supplier_id = ?", (supplier_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


# -------------------- products: basic --------------------

async def count_products(db_path: str, supplier_id: int) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("SELECT COUNT(1) FROM products WHERE supplier_id = ?", (supplier_id,))
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def list_products(db_path: str, supplier_id: int, limit: int, offset: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM products
            WHERE supplier_id = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (supplier_id, int(limit), int(offset)),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_product(db_path: str, product_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_product_by_code(db_path: str, supplier_id: int, code: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM products WHERE supplier_id = ? AND code = ?",
            (supplier_id, code),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def _get_products_columns(db: aiosqlite.Connection) -> list[str]:
    cur = await db.execute("PRAGMA table_info(products);")
    rows = await cur.fetchall()
    return [r[1] for r in rows]


async def upsert_product_by_code(
    db_path: str,
    supplier_id: int,
    code: str,
    data: dict[str, Any],
    overwrite: bool,
) -> tuple[str, int]:
    """
    overwrite=False:
      - если товар уже существует -> skipped

    overwrite=True:
      - обновляем существующий товар по переданным ключам (отсутствующие ключи НЕ трогаем)
    """
    existing = await get_product_by_code(db_path, supplier_id, code)
    if existing and not overwrite:
        return ("skipped", int(existing["id"]))

    merged = dict(existing or {})
    # ВАЖНО: overwrite=True — обновляем только ключи, которые реально пришли в data.
    # (если ключ отсутствует в data — старое значение сохраняем)
    merged.update(data)

    extra_json = merged.get("extra_json")
    if isinstance(extra_json, (dict, list)):
        extra_json = json.dumps(extra_json, ensure_ascii=False)

    payload: dict[str, Any] = {
        "source_pk": merged.get("source_pk"),
        "code": code,

        "title": merged.get("title"),
        "strength": merged.get("strength"),
        "volume": merged.get("volume"),

        "description": merged.get("description"),
        "price": merged.get("price"),
        "discount_percent": merged.get("discount_percent"),
        "final_price": merged.get("final_price"),
        "product_type": merged.get("product_type"),
        "stock_qty": merged.get("stock_qty"),
        "url": merged.get("url"),
        "image_url": merged.get("image_url"),
        "image_path": merged.get("image_path"),
        "extra_json": extra_json,
    }

    payload["search_text"] = _build_search_text(payload)

    now = _utcnow()
    created_at = (existing.get("created_at") if existing else None) or now

    async with aiosqlite.connect(db_path) as db:
        await _ensure_products_search_schema(db_path=db_path, conn=db)

        cols_db = await _get_products_columns(db)
        cols_set = set(cols_db)

        # данные для вставки
        row_data: dict[str, Any] = {"supplier_id": int(supplier_id), **payload}
        if "created_at" in cols_set:
            row_data["created_at"] = created_at
        if "updated_at" in cols_set:
            row_data["updated_at"] = now

        # фиксированный порядок (чтобы было предсказуемо)
        preferred_order = [
            "supplier_id",
            "source_pk", "code",
            "title", "strength", "volume",
            "description", "price", "discount_percent", "final_price",
            "product_type", "stock_qty", "url",
            "image_url", "image_path", "extra_json",
            "search_text",
            "created_at", "updated_at",
        ]

        insert_cols = [c for c in preferred_order if c in row_data and c in cols_set]
        values = [row_data[c] for c in insert_cols]
        placeholders = ", ".join(["?"] * len(insert_cols))

        # обновляем всё, кроме supplier_id/code/created_at
        update_cols = [c for c in insert_cols if c not in {"supplier_id", "code", "created_at"}]
        set_clause = ", ".join([f"{c} = excluded.{c}" for c in update_cols])

        sql = f"""
            INSERT INTO products ({", ".join(insert_cols)})
            VALUES ({placeholders})
            ON CONFLICT(supplier_id, code) DO UPDATE SET
                {set_clause}
        """

        await db.execute(sql, values)
        await db.commit()

    prod = await get_product_by_code(db_path, supplier_id, code)
    return ("updated" if existing else "created", int(prod["id"]) if prod else 0)


async def update_product_field(db_path: str, product_id: int, field: str, value: Any) -> None:
    allowed = {
        "source_pk", "code", "title", "strength", "volume",
        "description", "price", "discount_percent", "final_price",
        "product_type", "stock_qty", "url",
        "image_url", "image_path", "extra_json",
        "search_text",
    }
    if field not in allowed:
        raise ValueError("Field not allowed")

    now = _utcnow()
    async with aiosqlite.connect(db_path) as db:
        await _ensure_products_search_schema(db_path=db_path, conn=db)

        await db.execute(f"UPDATE products SET {field} = ?, updated_at = ? WHERE id = ?", (value, now, product_id))

        # пересчёт search_text, если меняли не его
        if field != "search_text":
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT code, source_pk, title, strength, volume, product_type, description, url
                FROM products WHERE id = ?;
                """,
                (product_id,),
            )
            row = await cur.fetchone()
            if row:
                st = _build_search_text(dict(row))
                await db.execute("UPDATE products SET search_text = ?, updated_at = ? WHERE id = ?;", (st, now, product_id))

        await db.commit()


async def delete_product(db_path: str, product_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await db.commit()


# -------------------- products: SEARCH (supplier scoped) --------------------

async def count_products_search(db_path: str, supplier_id: int, query: str) -> int:
    await _ensure_products_search_schema(db_path=db_path)
    tokens = _tokenize_query(query)
    if not tokens:
        return 0

    where, params = _build_where_and_params(tokens, alias="p")
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            f"""
            SELECT COUNT(1)
            FROM products p
            WHERE p.supplier_id = ?
              AND {where}
            """,
            (int(supplier_id), *params),
        )
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def search_products(db_path: str, supplier_id: int, query: str, limit: int, offset: int) -> list[dict[str, Any]]:
    await _ensure_products_search_schema(db_path=db_path)
    tokens = _tokenize_query(query)
    if not tokens:
        return []

    where, w_params = _build_where_and_params(tokens, alias="p")
    score_expr, s_params = _build_score_expr(tokens, alias="p")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT p.*,
                   {score_expr} AS score
            FROM products p
            WHERE p.supplier_id = ?
              AND {where}
            ORDER BY score DESC,
                     CAST(COALESCE(p.stock_qty, 0) AS INTEGER) DESC,
                     p.id DESC
            LIMIT ? OFFSET ?
            """,
            (*s_params, int(supplier_id), *w_params, int(limit), int(offset)),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# -------------------- products: GLOBAL SEARCH (для ИИ-чата) --------------------

async def count_products_global_search(db_path: str, query: str) -> int:
    await _ensure_products_search_schema(db_path=db_path)
    tokens = _tokenize_query(query)
    if not tokens:
        return 0

    where, params = _build_where_and_params(tokens, alias="p")
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            f"""
            SELECT COUNT(1)
            FROM products p
            WHERE {where}
            """,
            (*params,),
        )
        row = await cur.fetchone()
        return int(row[0] if row else 0)


async def search_products_global(db_path: str, query: str, limit: int, offset: int) -> list[dict[str, Any]]:
    await _ensure_products_search_schema(db_path=db_path)
    tokens = _tokenize_query(query)
    if not tokens:
        return []

    where, w_params = _build_where_and_params(tokens, alias="p")
    score_expr, s_params = _build_score_expr(tokens, alias="p")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT p.*,
                   s.name AS supplier_name,
                   {score_expr} AS score
            FROM products p
            LEFT JOIN suppliers s ON s.id = p.supplier_id
            WHERE {where}
            ORDER BY score DESC,
                     CAST(COALESCE(p.stock_qty, 0) AS INTEGER) DESC,
                     p.id DESC
            LIMIT ? OFFSET ?
            """,
            (*s_params, *w_params, int(limit), int(offset)),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
