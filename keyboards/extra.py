from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import SUPPORT_URL


MANAGER_URL = SUPPORT_URL


def extra_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👨‍💻 Стать исполнителем",
                    callback_data="become_executor",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ Другие услуги",
                    callback_data="additional_services",
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


def become_executor_keyboard() -> InlineKeyboardMarkup:
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
                    callback_data="become_executor_back",
                )
            ],
        ]
    )
