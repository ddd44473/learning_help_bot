from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def rules_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🟢 Принимаю",
                    callback_data="accept_rules",
                )
            ]
        ]
    )

def go_to_main_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔵 Главное меню",
                    callback_data="start_main_menu",
                )
            ]
        ]
    )