from __future__ import annotations

import aiosqlite
from typing import Any, Dict, List


def _sum(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _invoice_period_expr(alias: str = "i") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        "CASE "
        f"WHEN {prefix}status IN ('approved', 'rejected') "
        f"THEN COALESCE({prefix}handled_at, {prefix}updated_at, {prefix}created_at) "
        f"ELSE {prefix}created_at "
        "END"
    )


def _invoice_period_condition(alias: str = "i") -> str:
    expr = _invoice_period_expr(alias)
    return f"{expr} >= ? AND {expr} < ?"


async def get_bot_summary(db_path: str, start_iso: str, end_iso: str) -> Dict[str, Any]:
    """
    Основные метрики по боту за период [start_iso, end_iso).

    Для накладных:
    - pending считается по дате создания;
    - approved/rejected считаются по дате решения администратора.
    """
    invoice_where = _invoice_period_condition("")
    invoice_where_i = _invoice_period_condition("i")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        users_total = (await (await db.execute("SELECT COUNT(*) AS c FROM users")).fetchone())["c"]
        users_new = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE created_at >= ? AND created_at < ?",
            (start_iso, end_iso),
        )).fetchone())["c"]

        registrations_total = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM registrations WHERE created_at >= ? AND created_at < ?",
            (start_iso, end_iso),
        )).fetchone())["c"]

        reg_counts = {"pending": 0, "approved": 0, "rejected": 0}
        async with db.execute(
            "SELECT status, COUNT(*) AS c FROM registrations "
            "WHERE created_at >= ? AND created_at < ? GROUP BY status",
            (start_iso, end_iso),
        ) as cur:
            async for row in cur:
                reg_counts[row["status"]] = row["c"]

        invoices_total = (await (await db.execute(
            f"SELECT COUNT(*) AS c FROM invoices WHERE {invoice_where}",
            (start_iso, end_iso),
        )).fetchone())["c"]

        inv_counts = {"pending": 0, "approved": 0, "rejected": 0}
        async with db.execute(
            f"SELECT status, COUNT(*) AS c FROM invoices WHERE {invoice_where} GROUP BY status",
            (start_iso, end_iso),
        ) as cur:
            async for row in cur:
                inv_counts[row["status"]] = row["c"]

        sums = await (await db.execute(
            "SELECT "
            "COALESCE(SUM(COALESCE(deal_amount,0)),0) AS deal_sum, "
            "COALESCE(SUM(COALESCE(reward_amount,0)),0) AS reward_sum "
            f"FROM invoices WHERE {invoice_where}",
            (start_iso, end_iso),
        )).fetchone()
        deal_sum = _sum(sums["deal_sum"])
        reward_sum = _sum(sums["reward_sum"])

        item_sums = await (await db.execute(
            "SELECT "
            "COALESCE(SUM(ii.quantity), 0) AS items_qty, "
            "COALESCE(SUM(ii.line_total), 0) AS items_sales, "
            "COUNT(ii.id) AS item_lines "
            "FROM invoice_items ii "
            "JOIN invoices i ON i.id = ii.invoice_id "
            f"WHERE i.status = 'approved' AND {invoice_where_i}",
            (start_iso, end_iso),
        )).fetchone()

        payouts_total = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM payouts WHERE created_at >= ? AND created_at < ?",
            (start_iso, end_iso),
        )).fetchone())["c"]

        po_counts = {"pending": 0, "paid": 0, "rejected": 0}
        async with db.execute(
            "SELECT status, COUNT(*) AS c FROM payouts "
            "WHERE created_at >= ? AND created_at < ? GROUP BY status",
            (start_iso, end_iso),
        ) as cur:
            async for row in cur:
                po_counts[row["status"]] = row["c"]

        payouts_sum = _sum((await (await db.execute(
            "SELECT COALESCE(SUM(COALESCE(amount,0)),0) AS s "
            "FROM payouts WHERE created_at >= ? AND created_at < ?",
            (start_iso, end_iso),
        )).fetchone())["s"])

        suppliers_total = (await (await db.execute("SELECT COUNT(*) AS c FROM suppliers")).fetchone())["c"]
        suppliers_new = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM suppliers WHERE created_at >= ? AND created_at < ?",
            (start_iso, end_iso),
        )).fetchone())["c"]

        products_total = (await (await db.execute("SELECT COUNT(*) AS c FROM products")).fetchone())["c"]
        products_new = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM products WHERE created_at >= ? AND created_at < ?",
            (start_iso, end_iso),
        )).fetchone())["c"]

        kp_sessions_total = (await (await db.execute("SELECT COUNT(*) AS c FROM kp_sessions")).fetchone())["c"]
        kp_sessions_new = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM kp_sessions WHERE created_at >= ? AND created_at < ?",
            (start_iso, end_iso),
        )).fetchone())["c"]

        kp_items_total = (await (await db.execute("SELECT COUNT(*) AS c FROM kp_items")).fetchone())["c"]
        kp_items_new = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM kp_items WHERE created_at >= ? AND created_at < ?",
            (start_iso, end_iso),
        )).fetchone())["c"]

        price_uploads_total = (await (await db.execute(
            "SELECT COUNT(*) AS c FROM supplier_prices WHERE uploaded_at >= ? AND uploaded_at < ?",
            (start_iso, end_iso),
        )).fetchone())["c"]

        return {
            "users_total": users_total,
            "users_new": users_new,
            "registrations_total": registrations_total,
            "registrations_pending": reg_counts.get("pending", 0),
            "registrations_approved": reg_counts.get("approved", 0),
            "registrations_rejected": reg_counts.get("rejected", 0),
            "invoices_total": invoices_total,
            "invoices_pending": inv_counts.get("pending", 0),
            "invoices_approved": inv_counts.get("approved", 0),
            "invoices_rejected": inv_counts.get("rejected", 0),
            "deal_sum": deal_sum,
            "reward_sum": reward_sum,
            "items_qty": _sum(item_sums["items_qty"]),
            "items_sales": _sum(item_sums["items_sales"]),
            "item_lines": int(item_sums["item_lines"] or 0),
            "payouts_total": payouts_total,
            "payouts_pending": po_counts.get("pending", 0),
            "payouts_paid": po_counts.get("paid", 0),
            "payouts_rejected": po_counts.get("rejected", 0),
            "payouts_sum": payouts_sum,
            "suppliers_total": suppliers_total,
            "suppliers_new": suppliers_new,
            "products_total": products_total,
            "products_new": products_new,
            "kp_sessions_total": kp_sessions_total,
            "kp_sessions_new": kp_sessions_new,
            "kp_items_total": kp_items_total,
            "kp_items_new": kp_items_new,
            "price_uploads_total": price_uploads_total,
        }


async def list_active_user_ids(db_path: str) -> List[int]:
    """Пользователи, которым можно рассылать (status='approved')."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT tg_id FROM users WHERE status = 'approved' ORDER BY created_at"
        )).fetchall()
        return [int(r["tg_id"]) for r in rows]


async def count_user_stats(db_path: str, start_iso: str, end_iso: str) -> int:
    """Сколько пользователей было активных в периоде."""
    invoice_where = _invoice_period_condition("i")

    q = f"""
    SELECT COUNT(*) AS c
    FROM users u
    WHERE u.status = 'approved'
      AND (
        EXISTS (SELECT 1 FROM invoices i WHERE i.tg_id = u.tg_id AND {invoice_where})
        OR EXISTS (SELECT 1 FROM kp_items k WHERE k.tg_id = u.tg_id AND k.created_at >= ? AND k.created_at < ?)
        OR EXISTS (SELECT 1 FROM payouts p WHERE p.tg_id = u.tg_id AND p.created_at >= ? AND p.created_at < ?)
        OR EXISTS (SELECT 1 FROM registrations r WHERE r.tg_id = u.tg_id AND r.created_at >= ? AND r.created_at < ?)
      )
    """
    params = (start_iso, end_iso, start_iso, end_iso, start_iso, end_iso, start_iso, end_iso)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        return int((await (await db.execute(q, params)).fetchone())["c"])


async def list_user_stats(db_path: str, start_iso: str, end_iso: str, limit: int, offset: int) -> List[Dict[str, Any]]:
    """
    Статистика активных пользователей за период.
    """
    invoice_where = _invoice_period_condition("i")

    q = f"""
    SELECT
        u.tg_id,
        u.full_name,
        u.phone,
        u.reg_type,

        (SELECT COUNT(*) FROM invoices i
            WHERE i.tg_id = u.tg_id
              AND {invoice_where}) AS invoices_cnt,
        (SELECT COALESCE(SUM(COALESCE(i.deal_amount,0)),0) FROM invoices i
            WHERE i.tg_id = u.tg_id
              AND {invoice_where}) AS deal_sum,
        (SELECT COALESCE(SUM(COALESCE(i.reward_amount,0)),0) FROM invoices i
            WHERE i.tg_id = u.tg_id
              AND {invoice_where}) AS reward_sum,

        (SELECT COALESCE(SUM(ii.quantity), 0)
            FROM invoice_items ii
            JOIN invoices i ON i.id = ii.invoice_id
            WHERE i.tg_id = u.tg_id
              AND i.status = 'approved'
              AND {invoice_where}) AS items_qty,
        (SELECT COUNT(ii.id)
            FROM invoice_items ii
            JOIN invoices i ON i.id = ii.invoice_id
            WHERE i.tg_id = u.tg_id
              AND i.status = 'approved'
              AND {invoice_where}) AS item_lines,

        (SELECT COUNT(*) FROM kp_items k
            WHERE k.tg_id = u.tg_id
              AND k.created_at >= ? AND k.created_at < ?) AS kp_items_cnt,

        (SELECT COALESCE(SUM(COALESCE(p.amount,0)),0) FROM payouts p
            WHERE p.tg_id = u.tg_id
              AND p.created_at >= ? AND p.created_at < ?) AS payout_sum

    FROM users u
    WHERE u.status = 'approved'
      AND (
        EXISTS (SELECT 1 FROM invoices i WHERE i.tg_id = u.tg_id AND {invoice_where})
        OR EXISTS (SELECT 1 FROM kp_items k WHERE k.tg_id = u.tg_id AND k.created_at >= ? AND k.created_at < ?)
        OR EXISTS (SELECT 1 FROM payouts p WHERE p.tg_id = u.tg_id AND p.created_at >= ? AND p.created_at < ?)
        OR EXISTS (SELECT 1 FROM registrations r WHERE r.tg_id = u.tg_id AND r.created_at >= ? AND r.created_at < ?)
      )
    ORDER BY reward_sum DESC, invoices_cnt DESC, u.created_at ASC
    LIMIT ? OFFSET ?
    """
    params = (
        start_iso, end_iso,
        start_iso, end_iso,
        start_iso, end_iso,
        start_iso, end_iso,
        start_iso, end_iso,
        start_iso, end_iso,
        start_iso, end_iso,
        start_iso, end_iso,
        start_iso, end_iso,
        start_iso, end_iso,
        start_iso, end_iso,
        int(limit), int(offset),
    )

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(q, params)).fetchall()
        return [dict(r) for r in rows]


async def get_user_sales_summary(
    db_path: str,
    tg_id: int,
    start_iso: str,
    end_iso: str,
) -> Dict[str, Any]:
    """Персональная сводка менеджера за период [start_iso, end_iso)."""
    invoice_where = _invoice_period_condition("i")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        invoices = await (
            await db.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN i.status='pending' THEN 1 ELSE 0 END) AS pending, "
                "SUM(CASE WHEN i.status='approved' THEN 1 ELSE 0 END) AS approved, "
                "SUM(CASE WHEN i.status='rejected' THEN 1 ELSE 0 END) AS rejected, "
                "COALESCE(SUM(CASE WHEN i.status='approved' THEN COALESCE(i.deal_amount,0) ELSE 0 END),0) AS deal_sum, "
                "COALESCE(SUM(CASE WHEN i.status='approved' THEN COALESCE(i.reward_amount,0) ELSE 0 END),0) AS reward_sum "
                "FROM invoices i "
                f"WHERE i.tg_id = ? AND {invoice_where}",
                (tg_id, start_iso, end_iso),
            )
        ).fetchone()
        items = await (
            await db.execute(
                "SELECT COUNT(ii.id) AS item_lines, "
                "COALESCE(SUM(ii.quantity),0) AS items_qty, "
                "COALESCE(SUM(ii.line_total),0) AS items_sum "
                "FROM invoice_items ii JOIN invoices i ON i.id = ii.invoice_id "
                f"WHERE i.tg_id = ? AND i.status='approved' AND {invoice_where}",
                (tg_id, start_iso, end_iso),
            )
        ).fetchone()
        payouts = await (
            await db.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END),0) AS paid_sum, "
                "COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END),0) AS pending_sum "
                "FROM payouts WHERE tg_id = ? AND created_at >= ? AND created_at < ?",
                (tg_id, start_iso, end_iso),
            )
        ).fetchone()
        return {
            **(dict(invoices) if invoices else {}),
            **(dict(items) if items else {}),
            "payouts_total": int(payouts["total"] or 0) if payouts else 0,
            "paid_sum": float(payouts["paid_sum"] or 0) if payouts else 0.0,
            "pending_payout_sum": float(payouts["pending_sum"] or 0) if payouts else 0.0,
        }
