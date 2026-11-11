from aiogram.fsm.state import State, StatesGroup


class OrderChatState(StatesGroup):
    active = State()


class ReviewState(StatesGroup):
    waiting_for_comment = State()