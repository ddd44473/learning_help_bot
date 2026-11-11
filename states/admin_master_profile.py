from aiogram.fsm.state import State, StatesGroup


class AdminMasterProfileState(StatesGroup):
    # Полное заполнение через /create_master.
    waiting_for_full_name = State()
    waiting_for_university = State()
    waiting_for_specialty_code = State()
    waiting_for_specialty_name = State()
    waiting_for_directions = State()

    # Редактирование одного поля из /edit_master.
    editing_field_value = State()
    editing_directions = State()

    # Поиск исполнителя в административном каталоге /masters.
    searching_master = State()

    # Полное повторное заполнение из карточки управления.
    refill_full_name = State()
    refill_university = State()
    refill_specialty_code = State()
    refill_specialty_name = State()
    refill_directions = State()
