from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import SUPPORT_URL


MANAGER_URL = SUPPORT_URL
FAQ_POST_URL = "https://t.me/coliseumofknowledge/12"


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❓ Часто задаваемые вопросы",
                    url=FAQ_POST_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Написать менеджеру",
                    url=MANAGER_URL,
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


def faq_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Написать менеджеру",
                    url=MANAGER_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="help_faq_back",
                )
            ],
        ]
    )
