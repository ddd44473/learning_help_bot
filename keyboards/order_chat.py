from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


EXIT_ORDER_CHAT_TEXT = "❌ Выйти из диалога"


def order_chat_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Написать по заказу",
                    callback_data=f"write_order:{order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Мои заказы",
                    callback_data="my_orders",
                )
            ],
        ]
    )


def exit_order_chat_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=EXIT_ORDER_CHAT_TEXT,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Напишите сообщение исполнителю...",
    )