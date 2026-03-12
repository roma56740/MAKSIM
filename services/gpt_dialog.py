from __future__ import annotations

import os
from typing import Any
from openai import AsyncOpenAI

from db.ai_instructions import AI_KIND_DIALOG, list_ai_instructions


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is not None:
        return _client
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не задан в .env")
    _client = AsyncOpenAI(api_key=api_key)
    return _client


def _model_name() -> str:
    return (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"


async def build_dialog_system_prompt(*, db_path: str) -> str:
    instr = await list_ai_instructions(db_path=db_path, kind=AI_KIND_DIALOG, limit=200, offset=0)
    admin_rules = "\n".join([f"- {i['text'].strip()}" for i in instr if (i.get("text") or "").strip()])
    if not admin_rules:
        admin_rules = "- Отвечай кратко, по делу, на русском языке.\n- Если не уверен — уточни."

    # ✅ ключевое: тег поиска ТОЛЬКО когда реально про товары
    return (
        "Ты — менеджер-консультант в Telegram-боте.\n"
        "Следуй правилам администратора ниже. Всегда общайся дружелюбно, по делу.\n\n"
        "ПРАВИЛА АДМИНА:\n"
        f"{admin_rules}\n\n"
        "ПРО ТОВАРЫ/ПОДБОР:\n"
        "Если пользователь ЯВНО просит подобрать/найти товары или говорит что ему интересны товары "
        "(например: 'найди', 'подбери', 'интересуют', 'хочу купить', 'покажи варианты', "
        "и в сообщении есть признаки товара: бренд/название/код/крепость mg/объём ml/тип/вкус/поставщик), "
        "то:\n"
        "1) Сначала ответь как менеджер (1–3 предложения: что понял и что сейчас покажешь).\n"
        "2) В САМОЙ ПОСЛЕДНЕЙ строке добавь спец-тег:\n"
        "[[PRODUCT_SEARCH:краткий_запрос]]\n"
        "где краткий_запрос 3–8 слов (без кавычек).\n"
        "ВАЖНО: если это привет/спасибо/обычный вопрос НЕ про товары — НИКОГДА не добавляй этот тег.\n"
    )


async def ask_gpt_dialog(*, db_path: str, history: list[dict[str, Any]], user_text: str) -> str:
    system_prompt = await build_dialog_system_prompt(db_path=db_path)
    messages = [{"role": "system", "content": system_prompt}]

    for m in history[-20:]:
        role = m.get("role")
        content = m.get("content")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": str(content)})

    messages.append({"role": "user", "content": user_text})

    client = _get_client()
    resp = await client.chat.completions.create(
        model=_model_name(),
        messages=messages,
        temperature=0.35,
    )
    return (resp.choices[0].message.content or "").strip()
