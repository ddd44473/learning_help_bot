from aiogram.fsm.state import (
    State,
    StatesGroup,
)


class ClientPriceState(StatesGroup):
    waiting_for_counter_price = State()