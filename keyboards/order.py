from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from catalog.directions import DIRECTION_OPTIONS


def order_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📚 Каталог направлений",
                    callback_data="catalog_order",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👨‍🏫 Выбрать исполнителя",
                    callback_data="choose_executor",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="main_menu",
                )
            ],
        ]
    )


def catalog_directions_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    # Длинные названия отображаем целиком: одна кнопка на строку.
    for key, title, emoji in DIRECTION_OPTIONS:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{title} {emoji}",
                    callback_data=f"direction_{key}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="↩️ Назад",
                callback_data="main_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )
