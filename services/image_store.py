from __future__ import annotations

import hashlib
import os
from io import BytesIO

import aiohttp
from PIL import Image, ImageDraw

MEDIA_DIR = os.path.join("media", "products")
DEFAULT_IMAGE = os.path.join(MEDIA_DIR, "default.png")


def ensure_default_image() -> None:
    os.makedirs(MEDIA_DIR, exist_ok=True)
    if os.path.exists(DEFAULT_IMAGE):
        return

    img = Image.new("RGBA", (800, 800), (240, 240, 240, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((40, 40, 760, 760), outline=(180, 180, 180, 255), width=6)
    draw.text((290, 380), "NO IMAGE", fill=(90, 90, 90, 255))
    img.save(DEFAULT_IMAGE, "PNG")


def default_image_path() -> str:
    ensure_default_image()
    return DEFAULT_IMAGE


def _file_name(supplier_id: int, code: str) -> str:
    h = hashlib.sha1(f"{supplier_id}:{code}".encode("utf-8")).hexdigest()[:16]
    return f"{supplier_id}_{h}.jpg"


async def download_and_store_product_image(supplier_id: int, code: str, image_url: str) -> str | None:
    if not image_url:
        return None

    ensure_default_image()
    path = os.path.join(MEDIA_DIR, _file_name(supplier_id, code))

    try:
        timeout = aiohttp.ClientTimeout(total=25)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; TgBot/1.0)"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(image_url) as resp:
                resp.raise_for_status()
                data = await resp.read()

        img = Image.open(BytesIO(data)).convert("RGB")
        img.save(path, "JPEG", quality=90, optimize=True)
        return path
    except Exception:
        return None
