from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


def client_price_offer_keyboard(
    order_id: int,
    offer_id: int,
) -> InlineKeyboardMarkup:
    """
    Клавиатура ответа клиента на предложение исполнителя.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Согласиться",
                    callback_data=(
                        f"accept_price:"
                        f"{order_id}:"
                        f"{offer_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Предложить свою цену",
                    callback_data=(
                        f"counter_price:"
                        f"{order_id}:"
                        f"{offer_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отказаться",
                    callback_data=(
                        f"reject_price:"
                        f"{order_id}:"
                        f"{offer_id}"
                    ),
                )
            ],
        ]
    )


def master_counter_offer_keyboard(
    order_id: int,
    offer_id: int,
) -> InlineKeyboardMarkup:
    """
    Клавиатура ответа исполнителя
    на встречное предложение клиента.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Согласиться",
                    callback_data=(
                        f"master_accept_price:"
                        f"{order_id}:"
                        f"{offer_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Предложить новую цену",
                    callback_data=(
                        f"offer_price:{order_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отказаться",
                    callback_data=(
                        f"master_reject_price:"
                        f"{order_id}:"
                        f"{offer_id}"
                    ),
                )
            ],
        ]
    )

def order_reassignment_keyboard(
    order_id: int,
) -> InlineKeyboardMarkup:
    """
    Действия клиента после неудачной попытки
    договориться с текущим исполнителем.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👨‍💻 Выбрать другого исполнителя",
                    callback_data=f"reassign_choose:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Свободный исполнитель",
                    callback_data=f"reassign_free:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить заказ",
                    callback_data=f"reassign_cancel:{order_id}",
                )
            ],
        ]
    )
