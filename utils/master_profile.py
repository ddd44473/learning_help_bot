"""Представление исполнителя: публичная карточка и mention рабочей группы.

Чистые функции имени/mention и клавиатур отделены от async-сборщика,
который только читает профиль, направления и рейтинг из БД.
Без Router, Bot, FSM, изменения БД или отправки сообщений.
"""
import html
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database.masters import get_master_by_telegram_id_any, get_master_directions
from database.reviews import get_master_rating_stats


def master_public_name(
    master: Any,
    fallback_name: str | None = None,
) -> str:
    """Имя для заказчика, без обращения к полям username / Telegram ID."""
    if master is not None:
        value = str(
            master["full_name"]
            or ""
        ).strip()

        if value:
            return value

    normalized_fallback = str(
        fallback_name
        or ""
    ).strip()

    return (
        normalized_fallback
        or "Исполнитель"
    )


def master_group_mention(
    master: Any,
    telegram_id: int,
    fallback_name: str | None = None,
) -> str:
    """@mention либо HTML mention по ID — только для рабочей группы."""
    if master is not None:
        username = str(
            master["username"]
            or ""
        ).strip().lstrip("@")

        if username:
            return (
                f"@{html.escape(username)}"
            )

    safe_name = html.escape(
        master_public_name(
            master,
            fallback_name,
        )
    )

    return (
        f'<a href="tg://user?id={int(telegram_id)}">'
        f"{safe_name}</a>"
    )


def format_public_response_time(
    minutes: int | None,
) -> str:
    if minutes is None:
        return "Пока нет данных"

    if minutes < 60:
        return f"Около {minutes} мин."

    hours, remaining = divmod(
        minutes,
        60,
    )

    if remaining == 0:
        return f"Около {hours} ч."

    return (
        f"Около {hours} ч. "
        f"{remaining} мин."
    )


def claimed_master_client_keyboard(
    *,
    order_id: int,
    master_telegram_id: int,
) -> InlineKeyboardMarkup:
    """Клиентская навигация после взятия свободного заказа исполнителем."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👨‍💻 Профиль исполнителя",
                    callback_data=(
                        "claimed_master_profile:"
                        f"{master_telegram_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Отзывы исполнителя",
                    callback_data=(
                        f"reviews_master:"
                        f"{master_telegram_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Написать по заказу",
                    callback_data=(
                        f"write_order:{order_id}"
                    ),
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


def claimed_master_profile_keyboard(
    master_telegram_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Посмотреть отзывы",
                    callback_data=(
                        f"reviews_master:"
                        f"{master_telegram_id}"
                    ),
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


async def build_claimed_master_profile_text(
    master_telegram_id: int,
) -> str | None:
    """Публичная карточка: Telegram username и ID намеренно не выводятся."""
    master = await get_master_by_telegram_id_any(
        master_telegram_id
    )

    if (
        master is None
        or master["deleted_at"] is not None
    ):
        return None

    directions = await get_master_directions(
        master_telegram_id
    )

    rating_stats = await get_master_rating_stats(
        master_telegram_id
    )

    full_name = html.escape(
        str(
            master["full_name"]
            or "Исполнитель"
        )
    )

    university = html.escape(
        str(
            master["university"]
            or "Не указано"
        )
    )

    specialty_code = html.escape(
        str(
            master["specialty_code"]
            or "Код не указан"
        )
    )

    specialty_name = html.escape(
        str(
            master["specialty_name"]
            or "Название не указано"
        )
    )

    response_time = html.escape(
        format_public_response_time(
            master[
                "average_response_minutes"
            ]
        )
    )

    if directions:
        directions_text = "\n".join(
            f"• {html.escape(direction)}"
            for direction in directions
        )
    else:
        directions_text = "Не указаны"

    reviews_count = int(
        rating_stats.get(
            "reviews_count",
            0,
        )
    )

    average_rating = rating_stats.get(
        "average_rating"
    )

    if average_rating is None:
        rating_text = (
            "⭐ Рейтинг пока не сформирован\n"
            "💬 Отзывов пока нет"
        )
    else:
        rating_text = (
            f"⭐ <b>{float(average_rating):.2f} / 5.00</b>\n"
            f"💬 Отзывов: <b>{reviews_count}</b>"
        )

    return (
        "👨‍💻 <b>Профиль исполнителя</b>\n\n"
        f"👤 <b>{full_name}</b>\n\n"
        f"🎓 <b>Университет</b>\n"
        f"{university}\n\n"
        f"📚 <b>Специальность</b>\n"
        f"{specialty_code} — {specialty_name}\n\n"
        f"🧭 <b>Направления</b>\n"
        f"{directions_text}\n\n"
        f"⏱ <b>Среднее время ответа</b>\n"
        f"{response_time}\n\n"
        f"{rating_text}"
    )
