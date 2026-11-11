"""Global guard: rate limiting, duplicate callbacks and persistent user bans.

Installed on Dispatcher.update BEFORE message/callback middlewares, thus applies to
all types of incoming updates, including group messages and FSM handlers.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from config import ADMIN_IDS
from database.access_control import is_banned

logger = logging.getLogger(__name__)

# Bounds memory usage in long-running processes. These are per process, not a
# distributed DDoS mitigation system. Financial actions must also be atomic.
MAX_TRACKED_USERS = 5000
WINDOW = 10.0
MAX_EVENTS_PER_WINDOW = 35
MAX_CALLBACKS_PER_WINDOW = 20
DUPLICATE_CALLBACK_SECONDS = 0.8


class SecurityMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._recent: OrderedDict[int, deque[float]] = OrderedDict()
        self._callbacks: OrderedDict[int, deque[float]] = OrderedDict()
        self._last_callback: OrderedDict[tuple[int, str], float] = OrderedDict()

    @staticmethod
    def _actor(update: Update):
        incoming = update.event
        return incoming, (getattr(incoming, 'from_user', None)
                          or getattr(incoming, 'user', None))

    @staticmethod
    def _within_limit(history: OrderedDict, identity: int, now: float, maximum: int) -> bool:
        values = history.setdefault(identity, deque())
        history.move_to_end(identity)
        while values and now - values[0] > WINDOW:
            values.popleft()
        values.append(now)
        while len(history) > MAX_TRACKED_USERS:
            history.popitem(last=False)
        return len(values) <= maximum

    def _allowed_rate(self, user_id: int, incoming: Any) -> bool:
        now = time.monotonic()
        if not self._within_limit(self._recent, user_id, now, MAX_EVENTS_PER_WINDOW):
            return False
        if isinstance(incoming, CallbackQuery):
            if not self._within_limit(self._callbacks, user_id, now, MAX_CALLBACKS_PER_WINDOW):
                return False
            key = user_id, (incoming.data or '')
            previous = self._last_callback.get(key)
            self._last_callback[key] = now
            self._last_callback.move_to_end(key)
            while len(self._last_callback) > 12000:
                self._last_callback.popitem(last=False)
            if previous is not None and now - previous < DUPLICATE_CALLBACK_SECONDS:
                return False
        return True

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)
        incoming, actor = self._actor(event)
        if actor is None or getattr(actor, 'is_bot', False):
            return await handler(event, data)

        uid = int(actor.id)
        if uid not in ADMIN_IDS:
            if not self._allowed_rate(uid, incoming):
                if isinstance(incoming, CallbackQuery):
                    try:
                        await incoming.answer('Слишком частые нажатия. Подождите немного.', show_alert=False)
                    except Exception:
                        logger.debug('Не удалось ответить на ограниченный callback', exc_info=True)
                return None
            try:
                blocked = await is_banned(uid)
            except Exception:
                # Fail closed: do not allow banned accounts through on a DB error.
                logger.exception('Проверка бана недоступна для Telegram ID %s', uid)
                return None
            if blocked:
                if isinstance(incoming, CallbackQuery):
                    try:
                        await incoming.answer('Доступ к боту заблокирован.', show_alert=True)
                    except Exception:
                        logger.debug('Не удалось ответить заблокированному пользователю', exc_info=True)
                # No replies in chats: prevents spam/amplification.
                return None
        return await handler(event, data)
