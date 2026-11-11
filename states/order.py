from aiogram.fsm.state import State, StatesGroup


class OrderState(StatesGroup):
    waiting_for_order_details = State()
    waiting_for_master = State()
    waiting_for_confirmation = State()
    creating_order = State()
