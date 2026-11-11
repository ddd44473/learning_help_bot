"""Общая граница доступа для рабочих действий исполнителя.

Вынесено из handlers.master_order, чтобы ценовые workflow и основной
модуль использовали одну и ту же проверку без циклического импорта.

logger не определён намеренно: guard не пишет собственных событий,
только отвечает пользователю и возвращает решение.
"""

from aiogram.types import CallbackQuery

from config import ADMIN_IDS
from database.masters import is_master_available


def _is_admin(telegram_id: int) -> bool:
    return int(telegram_id) in ADMIN_IDS

async def _ensure_operational_master_callback(
    callback: CallbackQuery,
    *,
    allow_admin: bool = False,
) -> bool:
    """
    По умолчанию действие доступно только активному исполнителю.

    Для общих действий с заказом allow_admin=True даёт администратору
    доступ без назначения его исполнителем этого заказа.
    """

    if (
        allow_admin
        and _is_admin(
            callback.from_user.id
        )
    ):
        return True

    if await is_master_available(
        callback.from_user.id
    ):
        return True

    await callback.answer(
        "⛔ Рабочее действие недоступно. "
        "Для исполнителя требуется активная карточка.",
        show_alert=True,
    )
    return False
