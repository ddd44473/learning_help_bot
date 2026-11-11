from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from utils.order_statuses import (
    ORDER_STATUS_IN_PROGRESS,
    ORDER_STATUS_NEW,
    ORDER_STATUS_TITLES,
    ORDER_STATUS_WAITING_CLIENT,
)


def master_order_status_keyboard(
    order_id: int,
    current_status: str,
    assignment_status: str = "assigned",
) -> InlineKeyboardMarkup:
    """
    Рабочая клавиатура исполнителя.

    Исполнитель не может самостоятельно завершить заказ.
    Кнопка «Работа готова» только отправляет работу клиенту
    на подтверждение.
    """

    status_title = ORDER_STATUS_TITLES.get(
        current_status,
        current_status,
    )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"Статус: {status_title}",
                callback_data="master_status_info",
            )
        ]
    ]

    if current_status == ORDER_STATUS_NEW:
        if assignment_status == "open":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="🙋 Взять заказ",
                        callback_data=(
                            f"claim_free_order:{order_id}"
                        ),
                    )
                ]
            )

        elif assignment_status == "assigned":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="💰 Предложить стоимость",
                        callback_data=(
                            f"offer_price:{order_id}"
                        ),
                    )
                ]
            )

            rows.append(
                [
                    InlineKeyboardButton(
                        text="❌ Отказаться от заказа",
                        callback_data=(
                            f"master_decline_order:{order_id}"
                        ),
                    )
                ]
            )

    elif current_status == ORDER_STATUS_IN_PROGRESS:
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Отказаться от заказа",
                    callback_data=(
                        f"master_decline_order:{order_id}"
                    ),
                )
            ]
        )

        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Работа готова",
                    callback_data=(
                        f"master_status:{order_id}:"
                        f"{ORDER_STATUS_WAITING_CLIENT}"
                    ),
                )
            ]
        )

    elif current_status == ORDER_STATUS_WAITING_CLIENT:
        # Последнее слово теперь за клиентом.
        # Здесь намеренно нет действий исполнителя.
        pass

    return InlineKeyboardMarkup(
        inline_keyboard=rows,
    )
