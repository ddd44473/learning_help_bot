"""Перехват входящих сообщений рабочей группы до маршрутизации обработчиков."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from config import MASTER_GROUP_ID
from services.chat_capture import capture_error, capture_group_message

logger = logging.getLogger(__name__)


class ChatArchiveMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if (
            isinstance(event, Message)
            and event.chat.id == MASTER_GROUP_ID
            and event.message_thread_id is not None
        ):
            try:
                await capture_group_message(event, data['bot'])
            except Exception as exc:
                logger.exception('Ошибка записи сообщения рабочей группы')
                await capture_error(event.chat.id, event.message_thread_id, str(exc))
        return await handler(event, data)
