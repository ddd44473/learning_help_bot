from typing import Sequence

from aiosqlite import Row
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from catalog.directions import DIRECTION_OPTIONS


def reviews_directions_keyboard() -> InlineKeyboardMarkup:
    """
    Первый уровень раздела отзывов:
    направления + два общих режима.
    """

    rows: list[list[InlineKeyboardButton]] = []

    # Длинные названия отображаем целиком: одна кнопка на строку.
    for key, title, emoji in DIRECTION_OPTIONS:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{title} {emoji}",
                    callback_data=f"reviews_direction:{key}",
                )
            ]
        )

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="👨‍💻 Все исполнители",
                    callback_data="reviews_all_masters",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Все отзывы",
                    callback_data="reviews_all",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="about_service",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def reviews_keyboard(
    current_index: int,
    total_reviews: int,
) -> InlineKeyboardMarkup:
    navigation_row = []

    if current_index > 0:
        navigation_row.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"review_page:{current_index - 1}",
            )
        )

    navigation_row.append(
        InlineKeyboardButton(
            text=f"{current_index + 1} / {total_reviews}",
            callback_data="review_page_info",
        )
    )

    if current_index < total_reviews - 1:
        navigation_row.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"review_page:{current_index + 1}",
            )
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            navigation_row,
            [
                InlineKeyboardButton(
                    text="↩️ К направлениям",
                    callback_data="reviews",
                )
            ],
        ]
    )


def reviews_masters_keyboard(
    masters: Sequence[Row],
    current_page: int,
    total_pages: int,
    filter_key: str = "all",
) -> InlineKeyboardMarkup:
    """
    Показывает исполнителей с отзывами.

    filter_key:
    - all — все исполнители;
    - ключ направления из общего каталога.
    """

    keyboard: list[
        list[InlineKeyboardButton]
    ] = []

    for master in masters:
        telegram_id = int(
            master["telegram_id"]
        )

        # В пользовательском разделе отзывов Telegram username
        # исполнителя не показываем. Клиент видит только публичное
        # имя из карточки исполнителя.
        master_name = str(
            master["full_name"]
            or "Исполнитель"
        )

        reviews_count = int(
            master["reviews_count"]
            or 0
        )
        average_rating = master[
            "average_rating"
        ]

        rating_text = (
            f"{float(average_rating):.2f}"
            if average_rating is not None
            else "—"
        )

        keyboard.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"👨‍💻 {master_name} — "
                        f"⭐ {rating_text} "
                        f"({reviews_count})"
                    ),
                    callback_data=(
                        f"reviews_master:{telegram_id}"
                    ),
                )
            ]
        )

    if total_pages > 1:
        navigation_row: list[
            InlineKeyboardButton
        ] = []

        if current_page > 0:
            navigation_row.append(
                InlineKeyboardButton(
                    text="⬅️",
                    callback_data=(
                        "reviews_masters_page:"
                        f"{filter_key}:"
                        f"{current_page - 1}"
                    ),
                )
            )

        navigation_row.append(
            InlineKeyboardButton(
                text=(
                    f"{current_page + 1} / "
                    f"{total_pages}"
                ),
                callback_data=(
                    "reviews_masters_page_info"
                ),
            )
        )

        if current_page < total_pages - 1:
            navigation_row.append(
                InlineKeyboardButton(
                    text="➡️",
                    callback_data=(
                        "reviews_masters_page:"
                        f"{filter_key}:"
                        f"{current_page + 1}"
                    ),
                )
            )

        keyboard.append(
            navigation_row
        )

    keyboard.extend(
        [
            [
                InlineKeyboardButton(
                    text="⭐ Все отзывы",
                    callback_data="reviews_all",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ К направлениям",
                    callback_data="reviews",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=keyboard
    )


def master_reviews_keyboard(
    master_telegram_id: int,
    current_index: int,
    total_reviews: int,
    source: str = "about",
) -> InlineKeyboardMarkup:
    """
    Клавиатура отзывов конкретного исполнителя.

    source:
    - about — отзывы открыты из раздела «О сервисе»;
    - order — отзывы открыты во время выбора исполнителя.
    """

    navigation_row = []

    if current_index > 0:
        navigation_row.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=(
                    f"review_master_page:"
                    f"{master_telegram_id}:"
                    f"{current_index - 1}:"
                    f"{source}"
                ),
            )
        )

    navigation_row.append(
        InlineKeyboardButton(
            text=f"{current_index + 1} / {total_reviews}",
            callback_data="review_page_info",
        )
    )

    if current_index < total_reviews - 1:
        navigation_row.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=(
                    f"review_master_page:"
                    f"{master_telegram_id}:"
                    f"{current_index + 1}:"
                    f"{source}"
                ),
            )
        )

    if source == "order":
        back_callback = (
            f"master_card:{master_telegram_id}"
        )
    else:
        back_callback = "reviews"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            navigation_row,
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data=back_callback,
                )
            ],
        ]
    )
