from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from urllib.parse import urlsplit, urlunsplit

from callbacks import AdminRegCb, UserRegCb
from config import Settings
from db import get_registration, get_user, is_admin, upsert_registration
from db.personal_messages import update_user_telegram_profile
from keyboards.admin import admin_main_kb
from keyboards.user import (
    user_back_cancel_kb,
    user_contact_kb,
    user_file_kb,
    user_main_kb,
    user_register_kb,
)

router = Router()


class RegForm(StatesGroup):
    reg_type = State()
    full_name = State()
    phone = State()
    file = State()


async def _notify_admins_new_registration(
    message: Message,
    settings: Settings,
    tg_id: int,
    reg_type: str,
    full_name: str,
    phone: str,
) -> None:
    text = (
        "🆕 <b>Новая заявка на регистрацию</b>\n"
        f"ID: <code>{tg_id}</code>\n"
        f"Статус: {reg_type}\n"
        f"ФИО: {full_name}\n"
        f"Телефон: {phone}"
    )

    kb = InlineKeyboardBuilder()
    kb.add(
        InlineKeyboardButton(
            text="Открыть заявку",
            callback_data=AdminRegCb(action="view", tg_id=tg_id).pack(),
        )
    )

    for admin_id in settings.admin_ids:
        try:
            await message.bot.send_message(admin_id, text, reply_markup=kb.as_markup())
        except Exception:
            pass


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()

    admin_access = await is_admin(settings.db_path, message.from_user.id, settings.admin_ids)
    user = await get_user(settings.db_path, message.from_user.id)
    start_argument = (message.text or "").partition(" ")[2].strip().casefold()
    if start_argument == "site_manager":
        if not admin_access and not (user and user.get("status") == "approved"):
            await message.answer(
                "⛔ Служебная регистрация доступна только действующим сотрудникам, "
                "которым уже открыт доступ к этому боту."
            )
        else:
            builder = InlineKeyboardBuilder()
            hook_url = (settings.site_registration_hook_url or "").strip()
            if hook_url:
                parsed = urlsplit(hook_url)
                manager_url = urlunsplit((parsed.scheme, parsed.netloc, "/manager-registration.php", "", ""))
                if parsed.scheme in {"http", "https"} and parsed.netloc:
                    builder.button(text="Открыть служебную форму", url=manager_url)
            await message.answer(
                "🌐 <b>Ваш Telegram ID для регистрации менеджера:</b>\n\n"
                f"<code>{message.from_user.id}</code>\n\n"
                "Этот ID привязан к вашему подтверждённому профилю сотрудника. "
                "Скопируйте число и вставьте его в служебную форму на сайте.",
                reply_markup=builder.as_markup() if builder.buttons else None,
            )
    elif start_argument == "site":
        await message.answer(
            "🌐 Для обычной клиентской регистрации Telegram ID больше не нужен. "
            "Вернитесь на сайт и заполните короткую форму."
        )

    if admin_access:
        await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_main_kb())
        return

    if user:
        await update_user_telegram_profile(
            settings.db_path,
            message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
        )

    if user and user.get("status") == "approved":
        await message.answer("🏠 <b>Главное меню</b>", reply_markup=user_main_kb())
        return

    if user and user.get("status") == "blocked":
        await message.answer("🚫 Доступ заблокирован. Обратитесь к администратору.", reply_markup=ReplyKeyboardRemove())
        return

    if user and user.get("status") == "pending":
        await message.answer("⏳ Заявка на проверке.", reply_markup=ReplyKeyboardRemove())
        return

    if user and user.get("status") == "rejected":
        reg = await get_registration(settings.db_path, message.from_user.id)
        reason = (reg or {}).get("reason") or "(без причины)"
        await message.answer(
            f"❌ Регистрация отклонена: <b>{reason}</b>\nНажмите «📝 Регистрация», чтобы отправить заново.",
            reply_markup=user_register_kb(),
        )
        return

    await message.answer("Вы не зарегистрированы. Нажмите «📝 Регистрация».", reply_markup=user_register_kb())


@router.message(F.text == "📝 Регистрация")
async def start_registration(message: Message, state: FSMContext, settings: Settings) -> None:
    user = await get_user(settings.db_path, message.from_user.id)
    if user and user.get("status") in {"pending", "blocked"}:
        await message.answer("⏳ Сейчас регистрация недоступна.", reply_markup=ReplyKeyboardRemove())
        return

    kb = InlineKeyboardBuilder()
    kb.add(
        InlineKeyboardButton(text="ИП", callback_data=UserRegCb(action="type", value="ip").pack()),
        InlineKeyboardButton(text="Самозанятый", callback_data=UserRegCb(action="type", value="self").pack()),
    )
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=UserRegCb(action="back", value="").pack()))

    await state.set_state(RegForm.reg_type)
    await message.answer("Выберите статус:", reply_markup=kb.as_markup())


@router.message(F.text == "❌ Отмена")
async def cancel_any(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. /start", reply_markup=ReplyKeyboardRemove())


@router.callback_query(UserRegCb.filter(F.action == "back"))
async def reg_back_inline(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Отменено. Нажмите /start")
    await call.answer()


@router.callback_query(UserRegCb.filter(F.action == "type"))
async def reg_choose_type(call: CallbackQuery, callback_data: UserRegCb, state: FSMContext) -> None:
    reg_type = "ИП" if callback_data.value == "ip" else "Самозанятый"
    await state.update_data(reg_type=reg_type)
    await state.set_state(RegForm.full_name)

    await call.message.edit_text(f"Статус: <b>{reg_type}</b>\n\nВведите ФИО:")
    await call.message.answer("Управление:", reply_markup=user_back_cancel_kb())
    await call.answer()


@router.message(RegForm.full_name, F.text == "⬅️ Назад")
async def back_from_full_name(message: Message, state: FSMContext) -> None:
    await state.set_state(RegForm.reg_type)

    kb = InlineKeyboardBuilder()
    kb.add(
        InlineKeyboardButton(text="ИП", callback_data=UserRegCb(action="type", value="ip").pack()),
        InlineKeyboardButton(text="Самозанятый", callback_data=UserRegCb(action="type", value="self").pack()),
    )
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=UserRegCb(action="back", value="").pack()))
    await message.answer("Выберите статус:", reply_markup=kb.as_markup())


@router.message(RegForm.full_name)
async def reg_full_name(message: Message, state: FSMContext) -> None:
    full_name = (message.text or "").strip()
    if len(full_name) < 5:
        await message.answer("Введите ФИО (минимум 5 символов).", reply_markup=user_back_cancel_kb())
        return

    await state.update_data(full_name=full_name)
    await state.set_state(RegForm.phone)
    await message.answer("Нажмите кнопку «📱 Отправить контакт».", reply_markup=user_contact_kb())


@router.message(RegForm.phone, F.text == "⬅️ Назад")
async def back_from_phone(message: Message, state: FSMContext) -> None:
    await state.set_state(RegForm.full_name)
    await message.answer("Введите ФИО:", reply_markup=user_back_cancel_kb())


@router.message(RegForm.phone, F.contact)
async def reg_phone_contact(message: Message, state: FSMContext) -> None:
    phone = (message.contact.phone_number or "").strip()
    if not phone:
        await message.answer("Не получилось получить номер. Нажмите кнопку ещё раз.", reply_markup=user_contact_kb())
        return

    await state.update_data(phone=phone)
    await state.set_state(RegForm.file)
    await message.answer("Отправьте фото/файл договора или нажмите «Пропустить».", reply_markup=user_file_kb())


@router.message(RegForm.phone)
async def reg_phone_text_forbidden(message: Message) -> None:
    await message.answer("Нельзя вводить номер текстом. Нажмите «📱 Отправить контакт».", reply_markup=user_contact_kb())


@router.message(RegForm.file, F.text == "⬅️ Назад")
async def back_from_file(message: Message, state: FSMContext) -> None:
    await state.set_state(RegForm.phone)
    await message.answer("Нажмите кнопку «📱 Отправить контакт».", reply_markup=user_contact_kb())


async def _submit_registration(
    message: Message,
    state: FSMContext,
    settings: Settings,
    file_id: str | None,
    file_kind: str | None,
) -> None:
    data = await state.get_data()
    await upsert_registration(
        settings.db_path,
        tg_id=message.from_user.id,
        reg_type=data["reg_type"],
        full_name=data["full_name"],
        phone=data["phone"],
        file_id=file_id,
        file_kind=file_kind,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    await state.clear()

    await message.answer(
        "✅ Заявка отправлена.\n⏳ Ожидайте проверки администратора.",
        reply_markup=ReplyKeyboardRemove(),
    )

    await _notify_admins_new_registration(
        message,
        settings,
        tg_id=message.from_user.id,
        reg_type=data["reg_type"],
        full_name=data["full_name"],
        phone=data["phone"],
    )


@router.message(RegForm.file, F.text == "⏭ Пропустить")
async def reg_skip_file(message: Message, state: FSMContext, settings: Settings) -> None:
    await _submit_registration(message, state, settings, file_id=None, file_kind=None)


@router.message(RegForm.file, F.document)
async def reg_file_document(message: Message, state: FSMContext, settings: Settings) -> None:
    await _submit_registration(
        message,
        state,
        settings,
        file_id=message.document.file_id,
        file_kind="document",
    )


@router.message(RegForm.file, F.photo)
async def reg_file_photo(message: Message, state: FSMContext, settings: Settings) -> None:
    await _submit_registration(
        message,
        state,
        settings,
        file_id=message.photo[-1].file_id,
        file_kind="photo",
    )


@router.message(RegForm.file)
async def reg_file_invalid(message: Message) -> None:
    await message.answer("Отправьте фото/файл или нажмите «Пропустить».", reply_markup=user_file_kb())
