from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _money(v: float | int | None) -> str:
    if v is None:
        v = 0.0
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except Exception:
        return str(v)


def _status_badge(status: str | None) -> str:
    s = (status or "").lower()
    if s == "approved":
        return "🟢 Одобрен"
    if s == "pending":
        return "🟡 На проверке"
    if s == "rejected":
        return "🔴 Отклонён"
    if s == "blocked":
        return "⛔️ Заблокирован"
    return f"⚪️ {status or '—'}"


async def get_profile_analytics(db_path: str, tg_id: int) -> dict[str, Any]:
    """
    Возвращает все данные для красивого профиля:
    user + registration + баланс + статистика по накладным/выплатам + текущее КП.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # --- user ---
        cur = await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        user = await cur.fetchone()
        user_d = dict(user) if user else None

        # --- registration ---
        cur = await db.execute("SELECT * FROM registrations WHERE tg_id = ?", (tg_id,))
        reg = await cur.fetchone()
        reg_d = dict(reg) if reg else None

        # --- KP session (текущий) ---
        cur = await db.execute(
            """
            SELECT ks.tg_id, ks.supplier_id, ks.created_at, ks.updated_at, s.name AS supplier_name
            FROM kp_sessions ks
            LEFT JOIN suppliers s ON s.id = ks.supplier_id
            WHERE ks.tg_id = ?
            """,
            (tg_id,),
        )
        kp_sess = await cur.fetchone()
        kp_sess_d = dict(kp_sess) if kp_sess else None

        # Позиции в текущем КП (если знаем supplier_id — считаем в рамках него)
        kp_items_count = 0
        if kp_sess_d and kp_sess_d.get("supplier_id"):
            cur = await db.execute(
                "SELECT COUNT(1) AS c FROM kp_items WHERE tg_id = ? AND supplier_id = ?",
                (tg_id, int(kp_sess_d["supplier_id"])),
            )
        else:
            cur = await db.execute("SELECT COUNT(1) AS c FROM kp_items WHERE tg_id = ?", (tg_id,))
        row = await cur.fetchone()
        kp_items_count = int(row["c"] if row and row["c"] is not None else 0)

        # --- invoices stats ---
        cur = await db.execute(
            """
            SELECT
              COUNT(1) AS total,
              SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved,
              SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,

              COALESCE(SUM(CASE WHEN status='approved' THEN COALESCE(deal_amount,0) ELSE 0 END),0) AS approved_deal_sum,
              COALESCE(SUM(CASE WHEN status='approved' THEN COALESCE(reward_amount,0) ELSE 0 END),0) AS approved_reward_sum,
              COALESCE(SUM(CASE WHEN status='pending' THEN COALESCE(deal_amount,0) ELSE 0 END),0) AS pending_deal_sum,

              MAX(CASE WHEN status='approved' THEN updated_at ELSE NULL END) AS last_approved_at
            FROM invoices
            WHERE tg_id = ?
            """,
            (tg_id,),
        )
        inv = await cur.fetchone()
        inv_d = dict(inv) if inv else {}

        # --- payouts stats ---
        cur = await db.execute(
            """
            SELECT
              COUNT(1) AS total,
              SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END) AS paid,
              SUM(CASE WHEN status IN ('rejected','canceled') THEN 1 ELSE 0 END) AS canceled,

              COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END),0) AS paid_sum,
              COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END),0) AS pending_sum,

              MAX(CASE WHEN status='paid' THEN paid_at ELSE NULL END) AS last_paid_at
            FROM payouts
            WHERE tg_id = ?
            """,
            (tg_id,),
        )
        pay = await cur.fetchone()
        pay_d = dict(pay) if pay else {}

        # --- balance ---
        earned = float(inv_d.get("approved_reward_sum") or 0.0)
        paid = float(pay_d.get("paid_sum") or 0.0)
        available = earned - paid

        return {
            "user": user_d,
            "registration": reg_d,
            "kp_session": kp_sess_d,
            "kp_items_count": kp_items_count,
            "invoices": inv_d,
            "payouts": pay_d,
            "balance": {
                "earned": earned,
                "paid": paid,
                "available": available,
                "pending_payouts": float(pay_d.get("pending_sum") or 0.0),
            },
            "format": {
                "money": _money,
                "badge": _status_badge,
            },
        }
