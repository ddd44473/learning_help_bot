from aiogram.fsm.state import (
    State,
    StatesGroup,
)


class MasterPriceState(StatesGroup):
    waiting_for_price = State()