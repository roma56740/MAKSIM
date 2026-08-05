from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


BTN_USER_ADMIN_CHAT = "💬 Чат с админом"
BTN_CHAT_CLOSE = "🔒 Закрыть чат"


def user_register_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📝 Регистрация")]],
        resize_keyboard=True,
        input_field_placeholder="Регистрация",
    )


def user_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Каталог"), KeyboardButton(text="📄 Моё КП")],
            [KeyboardButton(text="🧾 Сформировать КП"), KeyboardButton(text="💬 Вопросы")],
            [KeyboardButton(text="🧾 Накладные"), KeyboardButton(text="💳 Счета")],
            [KeyboardButton(text="🎁 Акции"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="ℹ️ Помощь")],
            [KeyboardButton(text=BTN_USER_ADMIN_CHAT)],  # ✅ под "Помощь"
        ],
        resize_keyboard=True,
        input_field_placeholder="Главное меню",
    )


def user_ai_chat_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🧹 Очистить диалог"), KeyboardButton(text="⬅️ В меню")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши вопрос…",
    )


def user_support_chat_kb() -> ReplyKeyboardMarkup:
    # ✅ когда пользователь в чате с админом — только одна кнопка
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CHAT_CLOSE)]],
        resize_keyboard=True,
        input_field_placeholder="Сообщение админу…",
    )


def user_back_cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def user_contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить контакт", request_contact=True)],
            [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def user_file_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏭ Пропустить")],
            [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )
