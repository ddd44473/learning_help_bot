from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Сделать заказ",
                    callback_data="make_order",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 Профиль",
                    callback_data="profile",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏛 О сервисе",
                    callback_data="about_service",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛟 Помощь",
                    callback_data="help_settings",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📂 Дополнительно",
                    callback_data="extra_sections",
                )
            ],
        ]
    )