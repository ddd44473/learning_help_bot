"""Административный экспорт истории действий пользователя."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def user_logs_period_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    """Отдельные форматы для удобства на телефоне и ПК.

    Старые callback ``user_logs:period:telegram_id`` остаются рабочими
    и по-прежнему выгружают CSV.
    """
    labels = (
        ("day", "24 часа"),
        ("week", "7 дней"),
        ("all", "Всё время"),
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📱 HTML · {label}",
                    callback_data=f"user_logs:{period}:html:{telegram_id}",
                ),
                InlineKeyboardButton(
                    text=f"📊 CSV · {label}",
                    callback_data=f"user_logs:{period}:csv:{telegram_id}",
                ),
            ]
            for period, label in labels
        ]
    )
