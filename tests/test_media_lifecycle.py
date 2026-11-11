"""REL-02: задержанный finalizer не меняет отменённый/новый FSM."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation


@pytest.mark.parametrize("replacement", ["cancel", "new_draft", "exception", "task_cancel"])
async def test_album_finalizer_obeys_session(monkeypatch, replacement):
    from handlers import order_form
    from states.order import OrderState

    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=123456, chat_id=88101, user_id=88101))
    await state.set_state(OrderState.waiting_for_order_details)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delay(_seconds):
        entered.set()
        await release.wait()

    monkeypatch.setattr(order_form, "asyncio", SimpleNamespace(sleep=delay, create_task=asyncio.create_task))
    finish = AsyncMock(side_effect=RuntimeError("synthetic finish failure") if replacement == "exception" else None)
    monkeypatch.setattr(order_form, "finish_order_details_messages", finish)
    msg = SimpleNamespace(chat=SimpleNamespace(id=88101), from_user=SimpleNamespace(id=88101),
                          media_group_id="synthetic-album", answer=AsyncMock())
    await order_form.process_order_details(msg, state)
    key = (88101, 88101, "synthetic-album")
    task = order_form._pending_media_groups[key]["task"]
    await entered.wait()
    if replacement == "task_cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert key not in order_form._pending_media_groups
        finish.assert_not_called()
        return
    if replacement == "cancel":
        await state.clear()
    elif replacement == "new_draft":
        await state.clear()
        await state.set_state(OrderState.waiting_for_order_details)
        await state.update_data(details_session_id="different-draft", description="do not touch")
    release.set()
    await task
    assert key not in order_form._pending_media_groups
    if replacement == "exception":
        finish.assert_awaited_once()
    else:
        finish.assert_not_called()
    if replacement == "new_draft":
        assert (await state.get_data())["description"] == "do not touch"


async def test_dispatcher_uses_event_isolation():
    from bot import dp
    assert isinstance(dp.fsm.events_isolation, SimpleEventIsolation)
