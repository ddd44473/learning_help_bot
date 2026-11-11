from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from utils.order_statuses import (
    ORDER_STATUS_WAITING_CLIENT,
)


def my_orders_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📌 Активные заказы",
                    callback_data="active_orders",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Завершённые заказы",
                    callback_data="completed_orders",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="profile",
                )
            ],
        ]
    )


def active_orders_keyboard(
    orders,
    current_page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []

    for order in orders:
        subject = str(
            order["subject"] or "Не указан"
        )

        if len(subject) > 25:
            subject = subject[:25] + "…"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"📋 Заказ №{order['id']} "
                        f"— {subject}"
                    ),
                    callback_data=(
                        f"view_active_order:"
                        f"{order['id']}"
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
                        f"active_orders_page:"
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
                    "active_orders_page_info"
                ),
            )
        )

        if current_page < total_pages - 1:
            navigation_row.append(
                InlineKeyboardButton(
                    text="➡️",
                    callback_data=(
                        f"active_orders_page:"
                        f"{current_page + 1}"
                    ),
                )
            )

        buttons.append(
            navigation_row
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="↩️ Назад",
                callback_data="my_orders",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons,
    )


def completion_confirmation_keyboard(
    order_id: int,
) -> InlineKeyboardMarkup:
    """
    Клавиатура сообщения, которое клиент получает,
    когда исполнитель сообщает о готовности работы.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить завершение",
                    callback_data=(
                        f"confirm_completion:{order_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Вернуть в работу",
                    callback_data=(
                        f"return_completion:{order_id}"
                    ),
                )
            ],
        ]
    )


def active_order_details_keyboard(
    order_id: int,
    current_status: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if current_status == ORDER_STATUS_WAITING_CLIENT:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить завершение",
                        callback_data=(
                            f"confirm_completion:{order_id}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔄 Вернуть в работу",
                        callback_data=(
                            f"return_completion:{order_id}"
                        ),
                    )
                ],
            ]
        )

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="💬 Написать по заказу",
                    callback_data=f"write_order:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📌 К активным заказам",
                    callback_data="active_orders",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="main_menu",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def review_rating_keyboard(
    order_id: int,
) -> InlineKeyboardMarkup:
    """
    Оценка появляется только после клиентского
    подтверждения завершения заказа.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{rating} ⭐",
                    callback_data=(
                        f"review_rating:{order_id}:{rating}"
                    ),
                )
                for rating in range(1, 6)
            ]
        ]
    )


def completed_orders_keyboard(
    orders,
    current_page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []

    for order in orders:
        subject = str(
            order["subject"] or "Без предмета"
        )

        if len(subject) > 25:
            subject = subject[:25] + "…"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"✅ Заказ №{order['id']} "
                        f"— {subject}"
                    ),
                    callback_data=(
                        f"view_completed_order:"
                        f"{order['id']}"
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
                        f"completed_orders_page:"
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
                    "completed_orders_page_info"
                ),
            )
        )

        if current_page < total_pages - 1:
            navigation_row.append(
                InlineKeyboardButton(
                    text="➡️",
                    callback_data=(
                        f"completed_orders_page:"
                        f"{current_page + 1}"
                    ),
                )
            )

        buttons.append(
            navigation_row
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="↩️ Назад",
                callback_data="my_orders",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons,
    )


def completed_order_details_keyboard(
    order_id: int,
    has_review: bool,
) -> InlineKeyboardMarkup:
    buttons = []

    if not has_review:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⭐ Оставить отзыв",
                    callback_data=f"leave_review:{order_id}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="✅ К завершённым заказам",
                callback_data="completed_orders",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="🏠 Главное меню",
                callback_data="main_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons,
    )
