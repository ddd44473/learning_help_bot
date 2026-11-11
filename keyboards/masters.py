from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiosqlite import Row


def masters_selection_keyboard(
    masters: Sequence[Row],
    current_page: int,
    total_pages: int,
    *,
    is_reassign: bool = False,
) -> InlineKeyboardMarkup:
    """
    Показывает компактный список исполнителей
    с рейтингом и средним временем ответа.

    При наличии нескольких страниц добавляет
    кнопки навигации.

    В контексте перевыбора существующего заказа
    (is_reassign=True) не показывает кнопку
    «Свободный исполнитель», так как callback
    assignment_free принадлежит только новому черновику.
    """

    buttons: list[
        list[InlineKeyboardButton]
    ] = []

    # Исполнители текущей страницы.
    for master in masters:
        telegram_id = int(
            master["telegram_id"]
        )

        full_name = str(
            master["full_name"]
            or "Без имени"
        )

        reviews_count = int(
            master["reviews_count"]
            or 0
        )

        average_rating = master[
            "average_rating"
        ]

        average_response_minutes = master[
            "average_response_minutes"
        ]

        if (
            reviews_count > 0
            and average_rating is not None
        ):
            rating_text = (
                f"⭐ {float(average_rating):.2f} "
                f"({reviews_count})"
            )
        else:
            rating_text = "⭐ Новый исполнитель"

        if average_response_minutes is not None:
            response_text = (
                f"⏱ ~{int(average_response_minutes)} мин"
            )
        else:
            response_text = (
                "⏱ нет данных"
            )

        title = (
            f"👨‍💻 {full_name} — "
            f"{rating_text} · "
            f"{response_text}"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=title,
                    callback_data=(
                        f"master_card:"
                        f"{telegram_id}"
                    ),
                )
            ]
        )

    # «Свободный исполнитель» — только для нового черновика заказа.
    # В контексте перевыбора существующего заказа callback
    # assignment_free не принадлежит этому этапу, поэтому кнопка скрыта.
    if not is_reassign:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🟢 Свободный исполнитель",
                    callback_data="assignment_free",
                )
            ]
        )

    # Навигация между страницами.
    if total_pages > 1:
        navigation_row: list[
            InlineKeyboardButton
        ] = []

        if current_page > 0:
            navigation_row.append(
                InlineKeyboardButton(
                    text="⬅️",
                    callback_data=(
                        f"masters_page:"
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
                    "masters_page_info"
                ),
            )
        )

        if current_page < total_pages - 1:
            navigation_row.append(
                InlineKeyboardButton(
                    text="➡️",
                    callback_data=(
                        f"masters_page:"
                        f"{current_page + 1}"
                    ),
                )
            )

        buttons.append(
            navigation_row
        )

    # Отмена оформления заказа.
    buttons.append(
        [
            InlineKeyboardButton(
                text="❌ Отменить оформление",
                callback_data="cancel_order",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons,
    )


def master_card_keyboard(
    telegram_id: int,
) -> InlineKeyboardMarkup:
    """
    Клавиатура карточки исполнителя.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Отзывы",
                    callback_data=(
                        f"master_reviews:{telegram_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Выбрать исполнителя",
                    callback_data=(
                        f"select_master:{telegram_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ К списку исполнителей",
                    callback_data="masters_list",
                )
            ],
        ]
    )