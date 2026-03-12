from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup


_PRICE_META_PROPS = (
    "product:price:amount",
    "product:price",
    "og:price:amount",
    "og:price",
    "twitter:data1",  # иногда "1990 RUB"
)

_CURRENCY_META_PROPS = (
    "product:price:currency",
    "og:price:currency",
    "price:currency",
)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("\xa0", " ").replace(" ", "")
    # 1 234,56 / 1234.56
    s = s.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _first_not_empty(values: Iterable[str | None]) -> str | None:
    for v in values:
        if v and str(v).strip():
            return str(v).strip()
    return None


def _extract_price_from_jsonld(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    def walk(obj: Any) -> Iterable[dict]:
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for x in obj:
                yield from walk(x)

    price: float | None = None
    currency: str | None = None

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        # JSON-LD может быть list/dict
        for obj in walk(data):
            # schema.org Product / Offer
            offers = obj.get("offers")
            if offers is None:
                continue

            # offers: dict или list
            offer_list = offers if isinstance(offers, list) else [offers]
            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue

                p = (
                    offer.get("price")
                    or offer.get("lowPrice")
                    or offer.get("highPrice")
                )
                c = offer.get("priceCurrency") or offer.get("currency")

                p_f = _to_float(p)
                if p_f is not None and price is None:
                    price = p_f
                if c and currency is None:
                    currency = str(c).strip()

            if price is not None:
                return price, currency

    return price, currency


def _extract_price_from_meta(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    def meta(prop: str) -> str | None:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return tag.get("content") if tag and tag.get("content") else None

    price_raw = _first_not_empty(meta(p) for p in _PRICE_META_PROPS)
    cur_raw = _first_not_empty(meta(p) for p in _CURRENCY_META_PROPS)

    price = _to_float(price_raw)
    currency = cur_raw.strip() if cur_raw else None

    # twitter:data1 иногда "1990 RUB"
    if currency is None and price_raw:
        m = re.search(r"\b(RUB|USD|EUR)\b", price_raw, flags=re.I)
        if m:
            currency = m.group(1).upper()

    return price, currency


def _extract_price_from_text(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    # Берём ограниченный кусок текста, чтобы не ловить мусор
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s{2,}", " ", text)
    text = text[:12000]

    # 12 990 ₽ / 12990 руб / 199.50 EUR
    pattern = re.compile(
        r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(₽|руб\.?|р\.|RUB|€|EUR|\$|USD)\b",
        flags=re.I,
    )
    m = pattern.search(text)
    if not m:
        return None, None

    price = _to_float(m.group(1))
    cur = m.group(2).upper()
    if cur in {"РУБ", "РУБ.", "Р.", "₽"}:
        cur = "RUB"
    if cur == "€":
        cur = "EUR"
    if cur == "$":
        cur = "USD"
    return price, cur


def extract_page_data(html: str, base_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")

    def meta(prop: str) -> str | None:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return tag.get("content") if tag and tag.get("content") else None

    title = meta("og:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None
    if not title:
        title = soup.title.get_text(" ", strip=True) if soup.title else None

    image = meta("og:image")
    if image and base_url:
        image = urljoin(base_url, image)

    # если нет og:image — ищем img
    if not image:
        candidates: list[str] = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if not src:
                continue
            src = urljoin(base_url, src) if base_url else src
            s = src.lower()
            if any(x in s for x in ["logo", "icon", "sprite", "placeholder"]):
                continue
            candidates.append(src)

        preferred = [c for c in candidates if any(x in c.lower() for x in ["catalog", "product", "upload", "images"])]
        image = (preferred[0] if preferred else (candidates[0] if candidates else None))

    # --- price extraction (meta -> jsonld -> text) ---
    price, currency = _extract_price_from_meta(soup)
    if price is None:
        p2, c2 = _extract_price_from_jsonld(soup)
        price = price or p2
        currency = currency or c2
    if price is None:
        p3, c3 = _extract_price_from_text(soup)
        price = price or p3
        currency = currency or c3

    chars: list[str] = []
    for t in soup.find_all(["table", "ul", "ol"], limit=10):
        txt = t.get_text("\n", strip=True)
        if txt and len(txt) > 30:
            chars.append(txt)

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text[:12000]

    return {
        "title": title,
        "image_url": image,
        "meta_description": meta("description") or meta("og:description"),
        "chars_blocks": chars[:5],
        "text": text,
        "url": base_url,
        "price": price,
        "currency": currency,
    }
