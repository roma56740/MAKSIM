from __future__ import annotations

import json
import os
import re
from typing import Any

import aiohttp
from openai import AsyncOpenAI

from .page_extract import extract_page_data


MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-2024-08-06")


def _client() -> AsyncOpenAI | None:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None
    return AsyncOpenAI(api_key=key)


def _strip_json(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


async def fetch_html(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=25)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TgBot/1.0)"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()


async def enrich_from_url(url: str) -> dict[str, Any]:
    html = await fetch_html(url)
    page = extract_page_data(html, url)

    def fallback() -> dict[str, Any]:
        desc_parts = []
        if page.get("title"):
            desc_parts.append(f"Название: {page['title']}")
        if page.get("meta_description"):
            desc_parts.append(f"\nОписание: {page['meta_description']}")
        if page.get("chars_blocks"):
            desc_parts.append("\nХарактеристики:\n" + "\n\n".join(page["chars_blocks"][:2]))

        price = page.get("price")
        currency = page.get("currency")

        return {
            "title": page.get("title"),
            "description": "\n".join(desc_parts).strip() or None,
            "product_type": None,
            "image_url": page.get("image_url"),
            "price": price,
            "final_price": price,
            "currency": currency,
            "extra_json": json.dumps({"page": page}, ensure_ascii=False),
        }

    client = _client()
    if not client:
        return fallback()

    prompt = {
        "page_title": page.get("title"),
        "meta_description": page.get("meta_description"),
        "chars_blocks": page.get("chars_blocks"),
        "text": page.get("text"),
        "url": url,
        "image_url": page.get("image_url"),
        "page_price_hint": page.get("price"),
        "page_currency_hint": page.get("currency"),
    }

    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты парсер карточки товара. Верни строго JSON без markdown.\n"
                        "Нужно извлечь название, тип, описание, цену.\n"
                        "Если цена указана диапазоном — выбери минимальную.\n"
                        "Цена должна быть числом (например 12990.0), currency строкой (RUB/EUR/USD или null)."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Извлеки максимум данных о товаре.\n\n"
                        "Верни JSON строго по схеме:\n"
                        "{\n"
                        '  "title": string|null,\n'
                        '  "product_type": string|null,\n'
                        '  "image_url": string|null,\n'
                        '  "description": string|null,\n'
                        '  "price": number|null,\n'
                        '  "final_price": number|null,\n'
                        '  "currency": string|null,\n'
                        '  "attributes": { "key": "value" },\n'
                        '  "extra_text": string|null\n'
                        "}\n\n"
                        f"Данные страницы: {json.dumps(prompt, ensure_ascii=False)[:16000]}"
                    ),
                },
            ],
        )

        content = resp.choices[0].message.content or "{}"
        data = json.loads(_strip_json(content))

        desc = (data.get("description") or "").strip()
        attrs = data.get("attributes") or {}
        if isinstance(attrs, dict) and attrs:
            attrs_lines = "\n".join([f"- {k}: {v}" for k, v in attrs.items()])
            if attrs_lines and attrs_lines not in desc:
                desc = (desc + "\n\nХарактеристики:\n" + attrs_lines).strip()

        if data.get("extra_text"):
            desc = (desc + "\n\nДополнительно:\n" + str(data["extra_text"]).strip()).strip()

        price = _to_float(data.get("price"))
        final_price = _to_float(data.get("final_price"))

        # если модель не нашла цену — используем hint из page_extract
        if price is None:
            price = _to_float(page.get("price"))
        if final_price is None:
            final_price = price

        currency = (data.get("currency") or page.get("currency"))
        currency = str(currency).strip().upper() if currency else None

        extra = {"page": page, "gpt_raw": data}

        return {
            "title": data.get("title") or page.get("title"),
            "description": desc or None,
            "product_type": data.get("product_type"),
            "image_url": data.get("image_url") or page.get("image_url"),
            "price": price,
            "final_price": final_price,
            "currency": currency,
            "extra_json": json.dumps(extra, ensure_ascii=False),
        }

    except Exception:
        return fallback()
