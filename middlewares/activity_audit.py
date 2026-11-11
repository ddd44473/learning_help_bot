"""Record inbound user actions without storing private messages or documents."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from database.activity_logs import log_user_activity

logger = logging.getLogger(__name__)


class ActivityAuditMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            incoming = event.event
            actor = getattr(incoming, 'from_user', None) or getattr(incoming, 'user', None)
            if actor is not None and not actor.is_bot:
                details: dict[str, object] = {'update_id': event.update_id}
                action = 'incoming_event'
                if isinstance(incoming, CallbackQuery):
                    action = 'button_pressed'
                    # Callback data is not user message text; do not include secret
                    # contents or arbitrarily long values in activity logs.
                    details['button'] = (incoming.data or '')[:64]
                elif isinstance(incoming, Message):
                    action = 'message_received'
                    details['message_type'] = incoming.content_type
                    if incoming.text and incoming.text.startswith('/'):
                        action = 'command_received'
                        details['command'] = incoming.text.split()[0][:64]
                else:
                    details['update_type'] = event.event_type
                try:
                    await log_user_activity(
                        telegram_id=actor.id,
                        action=action,
                        details=details,
                    )
                except Exception:
                    # Log failure must not disrupt a paid order or a message.
                    logger.exception('Не удалось записать событие активности %s', event.update_id)
        return await handler(event, data)
