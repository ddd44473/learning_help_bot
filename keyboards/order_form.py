from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from catalog.directions import DIRECTION_OPTIONS


def work_types_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Контрольная работа",
                    callback_data="work_type:Контрольная работа",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📄 Реферат",
                    callback_data="work_type:Реферат",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧪 Лабораторная работа",
                    callback_data="work_type:Лабораторная работа",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📚 Курсовая работа",
                    callback_data="work_type:Курсовая работа",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Другое",
                    callback_data="work_type:Другое",
                )
            ],
        ]
    )


def skip_deadline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ Пропустить",
                    callback_data="skip_deadline",
                )
            ]
        ]
    )


def files_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Завершить добавление",
                    callback_data="finish_files",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏭ Без файлов",
                    callback_data="skip_files",
                )
            ],
        ]
    )


def confirm_order_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Создать заказ",
                    callback_data="confirm_order",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data="cancel_order",
                )
            ],
        ]
    )

def master_assignment_mode_keyboard() -> InlineKeyboardMarkup:
    """
    Выбор способа назначения исполнителя при создании заказа.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👨‍💻 Выбрать исполнителя",
                    callback_data="assignment_specific",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Свободный исполнитель",
                    callback_data="assignment_free",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧭 Изменить направление",
                    callback_data="assignment_change_direction",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить оформление",
                    callback_data="cancel_order",
                )
            ],
        ]
    )


def unavailable_masters_keyboard(
    *,
    allow_direction_change: bool = True,
) -> InlineKeyboardMarkup:
    """
    Навигация из ситуации, когда для направления
    нет доступных исполнителей.
    """

    rows: list[list[InlineKeyboardButton]] = []

    if allow_direction_change:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧭 Выбрать другое направление",
                    callback_data="assignment_change_direction",
                )
            ]
        )

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="↩️ К способу назначения",
                    callback_data="assignment_back",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить оформление",
                    callback_data="cancel_order",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def change_order_direction_keyboard() -> InlineKeyboardMarkup:
    """
    Меняет только направление уже заполненного заказа.
    Остальные данные FSM не сбрасываются.
    """

    rows: list[list[InlineKeyboardButton]] = []

    # Длинные названия отображаем целиком: одна кнопка на строку.
    for key, title, emoji in DIRECTION_OPTIONS:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{title} {emoji}",
                    callback_data=f"assignment_direction:{key}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="↩️ К способу назначения",
                callback_data="assignment_back",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )
