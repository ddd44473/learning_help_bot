from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import SUPPORT_URL


SERVICE_RULES_URL = "https://t.me/coliseumofknowledge/10"
MANAGER_URL = SUPPORT_URL


def about_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📜 Правила сервиса",
                    url=SERVICE_RULES_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Отзывы клиентов",
                    callback_data="reviews",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛡️ Гарантии и порядок работы",
                    callback_data="guarantees",
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


def guarantees_keyboard() -> InlineKeyboardMarkup:
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
                    callback_data="guarantees_back",
                )
            ],
        ]
    )
