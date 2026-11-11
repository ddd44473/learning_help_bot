"""Финализатор альбома блокируется по изоляции FSM Dispatcher-приёмника.

Phase 3 C1: handlers.order_form больше не импортирует глобальный dp.
Изоляция событий FSM инжектируется из workflow-data Dispatcher-а, который
реально принял update (main.py: dp["fsm_events_isolation"]). Ниже:
1) unit-проверка, что injected-изоляция используется с тем же ключом;
2) интеграционная проверка через настоящий Dispatcher: изоляция
принимающего Dispatcher-а используется и обычными обработчиками
(обёртка update), и финализатором альбома, а глобальная bot.dp.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation

from test_dispatcher_phase2 import cloned_router


class RecordingIsolation:
    """Подмена изоляции: фиксирует каждый lock(key) и возвращает пустой CM."""

    def __init__(self):
        self.locked_keys = []

    def lock(self, key):
        self.locked_keys.append(key)

        @asynccontextmanager
        async def cm():
            yield

        return cm()


def _album_message(uid, mid=10):
    """Для unit-вызовов: handler читает только chat.id/from_user.id/media_group_id."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=uid),
        from_user=SimpleNamespace(id=uid),
        media_group_id="synthetic-album",
        answer=AsyncMock(),
        message_id=mid,
    )


def _typed_album_message(uid, mid=10):
    """Полноценный aiogram Message для интеграционного Dispatcher-теста."""
    user = User(id=uid, is_bot=False, first_name="Synthetic")
    return Message(
        message_id=mid,
        date=datetime.now(timezone.utc),
        from_user=user,
        chat=Chat(id=uid, type="private"),
        media_group_id="synthetic-album",
    )


async def test_injected_isolation_locks_with_state_key(monkeypatch):
    from handlers import order_form
    from states.order import OrderState

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=123456, chat_id=88201, user_id=88201),
    )
    await state.set_state(OrderState.waiting_for_order_details)
    isolation = RecordingIsolation()
    finish = AsyncMock()
    monkeypatch.setattr(order_form, "finish_order_details_messages", finish)

    msg = _album_message(88201)
    await order_form.process_order_details(
        msg, state, fsm_events_isolation=isolation
    )
    key = (88201, 88201, "synthetic-album")
    await order_form._pending_media_groups[key]["task"]
    assert isolation.locked_keys == [state.key]
    finish.assert_awaited_once()
    await state.clear()


async def test_receiver_dispatcher_isolation_is_used(monkeypatch):
    """Граница: глобальная bot.dp изоляция никогда не используется."""
    from bot import dp as global_dp
    from handlers import order_form
    from states.order import OrderState

    original_lock = global_dp.fsm.events_isolation.lock
    global_dp.fsm.events_isolation.lock = (
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("Album finalizer touched the global dp isolation")
        )
    )
    try:
        dp = Dispatcher(events_isolation=SimpleEventIsolation())
        dp["fsm_events_isolation"] = dp.fsm.events_isolation
        dp.include_router(cloned_router(order_form))
        bot = Bot("123456:synthetic-test-token")

        receiving = dp.fsm.events_isolation
        recording = RecordingIsolation()

        def record_lock(key):
            return recording.lock(key)

        monkeypatch.setattr(receiving, "lock", record_lock)
        finish = AsyncMock()
        monkeypatch.setattr(order_form, "finish_order_details_messages", finish)

        state = FSMContext(
            storage=dp.fsm.storage,
            key=StorageKey(bot_id=bot.id, chat_id=88301, user_id=88301),
        )
        await state.set_state(OrderState.waiting_for_order_details)

        await dp.feed_update(bot, Update(update_id=1, message=_typed_album_message(88301)))
        key = (88301, 88301, "synthetic-album")
        await order_form._pending_media_groups[key]["task"]
        # Двойная блокировка одним ключом: Dispatcher (FSM-обёртка
        # обработчика) и финализатор используют одну и ту же изоляцию.
        assert recording.locked_keys == [state.key, state.key]
        finish.assert_awaited_once()
    finally:
        global_dp.fsm.events_isolation.lock = original_lock


async def test_without_injection_finalizer_still_completes(monkeypatch):
    """Характеризация деградации: прямой unit-вызов не блокируется сам по себе."""
    from handlers import order_form
    from states.order import OrderState

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=123456, chat_id=88401, user_id=88401),
    )
    await state.set_state(OrderState.waiting_for_order_details)
    finish = AsyncMock()
    monkeypatch.setattr(order_form, "finish_order_details_messages", finish)
    msg = _album_message(88401)
    await order_form.process_order_details(msg, state)
    key = (88401, 88401, "synthetic-album")
    await order_form._pending_media_groups[key]["task"]
    assert key not in order_form._pending_media_groups
    await state.clear()
