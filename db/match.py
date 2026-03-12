from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

import aiosqlite


def _norm(s: str | None) -> str:
    if not s:
        return ""
    t = s.strip().lower()
    t = t.replace("ё", "е")
    # унификация разделителей
    t = t.replace(",", ".")
    # убираем мусорные символы
    t = re.sub(r"[^\w\s.%/+-]", " ", t, flags=re.U)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_volume_l(text: str) -> float | None:
    """
    Пытаемся вытащить объем из текста:
    0.7 л / 0,7l / 700 ml / 700мл / 1л
    Возвращаем литры.
    """
    t = _norm(text)
    # 700 ml
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ml|мл)\b", t)
    if m:
        try:
            ml = float(m.group(1))
            return ml / 1000.0
        except Exception:
            pass
    # 0.7 l
    m = re.search(r"(\d+(?:\.\d+)?)\s*(l|л)\b", t)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    return None


def _extract_strength(text: str) -> str | None:
    """
    Пытаемся вытащить крепость: 35% / 40 %
    """
    t = _norm(text)
    m = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", t)
    if m:
        return m.group(1)
    return None


def _tokens_for_sql(s: str, max_tokens: int = 6) -> list[str]:
    """
    Берём “сильные” токены для первичного SQL-отбора кандидатов.
    """
    t = _norm(s)
    parts = [p for p in re.split(r"\s+", t) if len(p) >= 3]
    # убираем частые мусор-токены
    stop = {"the", "and", "with", "без", "для", "в", "на", "по", "шт"}
    parts = [p for p in parts if p not in stop]
    # уникализируем с сохранением порядка
    seen = set()
    out = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= max_tokens:
            break
    return out


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(a=a, b=b).ratio()


@dataclass
class MatchResult:
    product: dict[str, Any]
    score: float


async def _fetch_candidates(
    db_path: str,
    supplier_id: int,
    query_text: str,
    limit: int = 300,
) -> list[dict[str, Any]]:
    toks = _tokens_for_sql(query_text)
    # если токенов мало — всё равно попробуем по всему supplier (ограничено limit)
    where = "1=1"
    params: list[Any] = [supplier_id]

    if toks:
        conds = []
        for tok in toks:
            like = f"%{tok}%"
            conds.append(
                "(lower(coalesce(title,'')) LIKE ? OR "
                " lower(coalesce(description,'')) LIKE ? OR "
                " lower(coalesce(code,'')) LIKE ? OR "
                " lower(coalesce(source_pk,'')) LIKE ?)"
            )
            params.extend([like, like, like, like])
        where = "(" + " OR ".join(conds) + ")"

    q = f"""
    SELECT
        id, supplier_id, code, source_pk,
        title, strength, volume,
        description, product_type,
        price, discount_percent, final_price,
        stock_qty, url, image_url, image_path, extra_json
    FROM products
    WHERE supplier_id = ? AND {where}
    LIMIT ?
    """
    params.append(limit)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, tuple(params))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


def _build_candidate_text(p: dict[str, Any]) -> str:
    # Собираем строку для сравнения
    parts = []
    for k in ("title", "description", "code", "source_pk", "strength", "product_type"):
        v = (p.get(k) or "")
        if v:
            parts.append(str(v))
    vol = p.get("volume")
    if vol is not None and vol != "":
        parts.append(f"{vol}l")
    return _norm(" ".join(parts))


async def find_best_product_match(
    db_path: str,
    supplier_id: int,
    *,
    title: str | None,
    strength: str | None = None,
    volume_l: float | None = None,
    extra_text: str | None = None,
    min_score: float = 0.68,
    suggestions: int = 5,
) -> tuple[MatchResult | None, list[MatchResult]]:
    """
    Возвращает:
      - лучший матч (или None)
      - список подсказок (top-N по score)
    """
    base = " ".join([x for x in [title or "", strength or "", extra_text or ""] if x]).strip()
    base_norm = _norm(base)

    # если объём не передали — попробуем вытащить из названия
    if volume_l is None and title:
        volume_l = _extract_volume_l(title)

    # если крепость не передали — попробуем вытащить из названия/extra
    if strength is None:
        strength = _extract_strength(base)

    candidates = await _fetch_candidates(db_path, supplier_id, base, limit=300)
    scored: list[MatchResult] = []

    for p in candidates:
        cand_text = _build_candidate_text(p)
        score = _ratio(base_norm, cand_text)

        # бонус за объем (если совпал)
        try:
            v = p.get("volume")
            if volume_l is not None and v is not None and v != "":
                vv = float(v)
                if abs(vv - float(volume_l)) <= 0.02:
                    score += 0.10
        except Exception:
            pass

        # бонус за крепость, если совпала цифра
        try:
            if strength:
                st = _norm(str(strength))
                cst = _norm(str(p.get("strength") or p.get("product_type") or ""))
                if st and (st in cst or cst in st):
                    score += 0.06
        except Exception:
            pass

        # бонус, если код/артикул явно встречается в запросе
        qn = base_norm
        for kk in ("code", "source_pk"):
            kv = _norm(str(p.get(kk) or ""))
            if kv and kv in qn:
                score += 0.12
                break

        if score > 1.0:
            score = 1.0

        scored.append(MatchResult(product=p, score=score))

    scored.sort(key=lambda x: x.score, reverse=True)
    top = scored[: max(suggestions, 1)]

    best = top[0] if top else None
    if best and best.score >= float(min_score):
        return best, top
    return None, top
