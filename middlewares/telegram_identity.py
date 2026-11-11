import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from database.telegram_identity import sync_telegram_username


logger = logging.getLogger(__name__)


class TelegramIdentitySyncMiddleware(BaseMiddleware):
    """
    Обновляет Telegram username при любой обычной активности пользователя.

    Middleware подключается к сообщениям, отредактированным сообщениям и
    callback query. Поэтому он работает как в личном чате с ботом, так и для
    сообщений/inline-кнопок исполнителя в рабочей группе.
    """

    async def __call__(
        self,
        handler: Callable[
            [TelegramObject, dict[str, Any]],
            Awaitable[Any],
        ],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_user = None

        if isinstance(event, (Message, CallbackQuery)):
            telegram_user = event.from_user

        if (
            telegram_user is not None
            and not telegram_user.is_bot
        ):
            try:
                await sync_telegram_username(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                )
            except Exception:
                # Ошибка синхронизации метаданных не должна блокировать
                # основное действие пользователя в боте.
                logger.exception(
                    "Не удалось синхронизировать Telegram username "
                    "для user_id=%s",
                    telegram_user.id,
                )

        return await handler(event, data)
