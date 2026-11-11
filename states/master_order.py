from aiogram.fsm.state import State, StatesGroup


class MasterOrderState(StatesGroup):
    waiting_for_decline_reason = State()
