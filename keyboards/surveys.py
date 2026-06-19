from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks.surveys import SurveyAdminCb, SurveyUserCb


def admin_surveys_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="➕ Создать опрос",
            callback_data=SurveyAdminCb(action="create").pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📊 Результаты старых опросов",
            callback_data=SurveyAdminCb(action="list", page=1).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ В админ-меню",
            callback_data=SurveyAdminCb(action="back").pack(),
        )
    )
    return kb.as_markup()


def survey_mode_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="1️⃣ Один вариант",
            callback_data=SurveyAdminCb(action="mode", mode="single").pack(),
        ),
        InlineKeyboardButton(
            text="✅ Несколько вариантов",
            callback_data=SurveyAdminCb(action="mode", mode="multiple").pack(),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=SurveyAdminCb(action="cancel").pack(),
        )
    )
    return kb.as_markup()


def survey_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="🚀 Отправить всем",
            callback_data=SurveyAdminCb(action="send").pack(),
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=SurveyAdminCb(action="cancel").pack(),
        ),
    )
    return kb.as_markup()


def admin_surveys_list_kb(items: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    for item in items:
        title = str(item.get("question") or "Опрос").strip()
        if len(title) > 34:
            title = title[:31] + "..."

        voters = int(item.get("voters_count") or 0)
        kb.row(
            InlineKeyboardButton(
                text=f"🗳 {title} · ответов {voters}",
                callback_data=SurveyAdminCb(
                    action="open",
                    page=page,
                    survey_id=int(item["id"]),
                ).pack(),
            )
        )

    kb.row(
        InlineKeyboardButton(
            text="⬅️",
            callback_data=SurveyAdminCb(action="list", page=max(1, page - 1)).pack(),
        ),
        InlineKeyboardButton(
            text=f"{page}/{max(1, total_pages)}",
            callback_data=SurveyAdminCb(action="noop", page=page).pack(),
        ),
        InlineKeyboardButton(
            text="➡️",
            callback_data=SurveyAdminCb(action="list", page=min(total_pages, page + 1)).pack(),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ В опросы",
            callback_data=SurveyAdminCb(action="menu").pack(),
        )
    )
    return kb.as_markup()


def admin_survey_result_kb(survey_id: int, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="📄 Скачать Excel-отчёт",
            callback_data=SurveyAdminCb(action="export", page=page, survey_id=survey_id).pack(),
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️ К списку",
            callback_data=SurveyAdminCb(action="list", page=page).pack(),
        ),
        InlineKeyboardButton(
            text="🏠 В опросы",
            callback_data=SurveyAdminCb(action="menu").pack(),
        ),
    )
    return kb.as_markup()


def survey_user_kb(
    survey_id: int,
    mode: str,
    options: list[dict],
    selected_ids: set[int] | None = None,
) -> InlineKeyboardMarkup:
    selected_ids = selected_ids or set()
    kb = InlineKeyboardBuilder()

    for option in options:
        option_id = int(option["id"])
        title = str(option.get("text") or "Вариант").strip()
        if len(title) > 58:
            title = title[:55] + "..."

        if mode == "multiple":
            prefix = "✅ " if option_id in selected_ids else "▫️ "
            action = "toggle"
        else:
            prefix = ""
            action = "pick"

        kb.row(
            InlineKeyboardButton(
                text=f"{prefix}{title}",
                callback_data=SurveyUserCb(
                    action=action,
                    survey_id=survey_id,
                    option_id=option_id,
                ).pack(),
            )
        )

    if mode == "multiple":
        kb.row(
            InlineKeyboardButton(
                text="✅ Отправить ответ",
                callback_data=SurveyUserCb(action="submit", survey_id=survey_id).pack(),
            )
        )

    return kb.as_markup()


def survey_preview_kb(mode: str, options: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for option in options:
        title = option.strip()
        if len(title) > 58:
            title = title[:55] + "..."
        prefix = "▫️ " if mode == "multiple" else ""
        kb.row(
            InlineKeyboardButton(
                text=f"{prefix}{title}",
                callback_data=SurveyAdminCb(action="noop").pack(),
            )
        )

    if mode == "multiple":
        kb.row(
            InlineKeyboardButton(
                text="✅ Отправить ответ",
                callback_data=SurveyAdminCb(action="noop").pack(),
            )
        )
    return kb.as_markup()
