from aiogram.fsm.state import StatesGroup, State

class SupportChatState(StatesGroup):
    user_chat = State()
    admin_chat = State()
