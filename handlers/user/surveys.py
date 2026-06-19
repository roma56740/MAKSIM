from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery

from callbacks.surveys import SurveyUserCb
from config import Settings
from db.surveys import (
    finalize_multiple_response,
    get_survey,
    get_survey_options,
    get_temp_selected_options,
    option_belongs_to_survey,
    save_single_response,
    toggle_temp_option,
    user_has_response,
)
from keyboards.surveys import survey_user_kb

router = Router()


def _survey_closed_text() -> str:
    return "Опрос уже недоступен."


async def _answer_thanks(call: CallbackQuery) -> None:
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    try:
        await call.message.answer("✅ <b>Спасибо за ответ!</b>\nВаш голос сохранён.")
    except Exception:
        pass

    await call.answer("Спасибо за ответ!")


@router.callback_query(SurveyUserCb.filter(F.action == "pick"))
async def survey_single_pick(
    call: CallbackQuery,
    callback_data: SurveyUserCb,
    settings: Settings,
) -> None:
    survey = await get_survey(settings.db_path, callback_data.survey_id)
    if not survey or survey.get("status") != "sent" or survey.get("select_mode") != "single":
        await call.answer(_survey_closed_text(), show_alert=True)
        return

    if await user_has_response(settings.db_path, callback_data.survey_id, call.from_user.id):
        await call.answer("Вы уже прошли этот опрос.", show_alert=True)
        return

    ok_option = await option_belongs_to_survey(
        settings.db_path,
        callback_data.survey_id,
        callback_data.option_id,
    )
    if not ok_option:
        await call.answer(_survey_closed_text(), show_alert=True)
        return

    saved = await save_single_response(
        settings.db_path,
        callback_data.survey_id,
        call.from_user.id,
        callback_data.option_id,
    )
    if not saved:
        await call.answer("Вы уже прошли этот опрос.", show_alert=True)
        return

    await _answer_thanks(call)


@router.callback_query(SurveyUserCb.filter(F.action == "toggle"))
async def survey_multiple_toggle(
    call: CallbackQuery,
    callback_data: SurveyUserCb,
    settings: Settings,
) -> None:
    survey = await get_survey(settings.db_path, callback_data.survey_id)
    if not survey or survey.get("status") != "sent" or survey.get("select_mode") != "multiple":
        await call.answer(_survey_closed_text(), show_alert=True)
        return

    if await user_has_response(settings.db_path, callback_data.survey_id, call.from_user.id):
        await call.answer("Вы уже прошли этот опрос.", show_alert=True)
        return

    ok_option = await option_belongs_to_survey(
        settings.db_path,
        callback_data.survey_id,
        callback_data.option_id,
    )
    if not ok_option:
        await call.answer(_survey_closed_text(), show_alert=True)
        return

    selected_ids = await toggle_temp_option(
        settings.db_path,
        callback_data.survey_id,
        call.from_user.id,
        callback_data.option_id,
    )
    options = await get_survey_options(settings.db_path, callback_data.survey_id)

    try:
        await call.message.edit_reply_markup(
            reply_markup=survey_user_kb(
                survey_id=callback_data.survey_id,
                mode="multiple",
                options=options,
                selected_ids=selected_ids,
            )
        )
    except Exception:
        pass

    await call.answer("Выбор обновлён")


@router.callback_query(SurveyUserCb.filter(F.action == "submit"))
async def survey_multiple_submit(
    call: CallbackQuery,
    callback_data: SurveyUserCb,
    settings: Settings,
) -> None:
    survey = await get_survey(settings.db_path, callback_data.survey_id)
    if not survey or survey.get("status") != "sent" or survey.get("select_mode") != "multiple":
        await call.answer(_survey_closed_text(), show_alert=True)
        return

    if await user_has_response(settings.db_path, callback_data.survey_id, call.from_user.id):
        await call.answer("Вы уже прошли этот опрос.", show_alert=True)
        return

    selected_ids = await get_temp_selected_options(
        settings.db_path,
        callback_data.survey_id,
        call.from_user.id,
    )
    if not selected_ids:
        await call.answer("Выберите хотя бы один вариант.", show_alert=True)
        return

    saved, _count = await finalize_multiple_response(
        settings.db_path,
        callback_data.survey_id,
        call.from_user.id,
    )
    if not saved:
        await call.answer("Вы уже прошли этот опрос.", show_alert=True)
        return

    await _answer_thanks(call)
