from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_ADMIN_CHATS = "💬 Чаты"
BTN_CHAT_CLOSE = "🔒 Закрыть чат"
BTN_ADMIN_USERS = "👥 Все пользователи"


def admin_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Модерация регистрации"), KeyboardButton(text="🤖 ИИ")],
            [KeyboardButton(text="🧾 Накладные"), KeyboardButton(text="💳 Счета")],
            [KeyboardButton(text="🏢 Поставщики"), KeyboardButton(text="📦 Excel-прайсы")],
            [KeyboardButton(text="📊 Аналитика"), KeyboardButton(text=BTN_ADMIN_USERS)],
            [KeyboardButton(text="📢 Рассылка"), KeyboardButton(text=BTN_ADMIN_CHATS)],
            [KeyboardButton(text="👥 Админы")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Админ-меню",
    )


def admin_support_chat_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CHAT_CLOSE)]],
        resize_keyboard=True,
        input_field_placeholder="Ответ пользователю…",
    )


def admin_reg_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🆕 Заявки"), KeyboardButton(text="⬅️ Назад")],
            [KeyboardButton(text="✅ Одобренные"), KeyboardButton(text="❌ Отклонённые")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Модерация регистрации",
    )


def admin_back_cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def admin_skip_back_cancel_kb(skip_text: str = "⏭ Пропустить") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=skip_text)],
            [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def admin_ai_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 Посмотреть инструкции"), KeyboardButton(text="➕ Инструкция для поиска")],
            [KeyboardButton(text="➕ Инструкция для диалога"), KeyboardButton(text="⬅️ В админ-меню")],
        ],
        resize_keyboard=True,
        input_field_placeholder="ИИ-инструкции",
    )


def admin_ai_cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Отмена"), KeyboardButton(text="⬅️ В админ-меню")],
        ],
        resize_keyboard=True,
    )
