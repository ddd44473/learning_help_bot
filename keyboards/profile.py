from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import TOPUP_ADMIN_USERNAME


def profile_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Оформить заказ",
                    callback_data="make_order",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📁 Мои заказы",
                    callback_data="my_orders",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Пригласить друзей",
                    callback_data="referral_program",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 Пополнить баланс",
                    callback_data="balance",
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


def balance_keyboard() -> InlineKeyboardMarkup:
    buttons = []

    if TOPUP_ADMIN_USERNAME:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="💰 Пополнить баланс",
                    url=(
                        f"https://t.me/"
                        f"{TOPUP_ADMIN_USERNAME}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="💳 История транзакций",
                callback_data="balance_transactions",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="↩️ Назад",
                callback_data="profile",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=buttons,
    )

REFERRAL_RULES_URL = "https://t.me/coliseumofknowledge/15"


def referral_program_keyboard(share_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📤 Поделиться",
                    url=share_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎁 Мои бонусы",
                    callback_data="referral_bonuses",
                )
            ],
            [
                InlineKeyboardButton(
                    text="ℹ️ Как это работает",
                    callback_data="referral_how_it_works",
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


def referral_bonuses_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📚 Сделать заказ",
                    callback_data="make_order",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Пригласить друзей",
                    callback_data="referral_program",
                )
            ],
            [
                InlineKeyboardButton(
                    text="ℹ️ Как работают бонусы",
                    callback_data="referral_how_it_works",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="referral_program",
                )
            ],
        ]
    )


def referral_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📜 Правила программы",
                    url=REFERRAL_RULES_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="referral_program",
                )
            ],
        ]
    )


def referral_reward_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎁 Мои бонусы",
                    callback_data="referral_bonuses",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📚 Сделать заказ",
                    callback_data="make_order",
                )
            ],
        ]
    )


def bonus_expiry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📚 Сделать заказ",
                    callback_data="make_order",
                )
            ]
        ]
    )
