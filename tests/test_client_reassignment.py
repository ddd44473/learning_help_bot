"""Характеризационные тесты перевыбора/переназначения исполнителя (workstream C)."""
import aiosqlite
import pytest

from states.order import OrderState
from test_dispatcher_phase2 import incoming, new_texts_after, routing as dispatcher_routing

routing = dispatcher_routing

CLIENT_TG = 7770001
OTHER_CLIENT_TG = 7770002
MASTER_TG = 999001
GROUP_ID = -10088001
ORDER_ID = 1


async def prepare_order(db, *, assignment_status="waiting_selection", direction="Математика"):
    await db.execute(
        "INSERT INTO users(id, telegram_id, is_registered) VALUES (1, ?, 1)",
        (CLIENT_TG,),
    )
    await db.execute(
        "INSERT INTO users(id, telegram_id, is_registered) VALUES (2, ?, 1)",
        (OTHER_CLIENT_TG,),
    )
    await db.execute(
        "INSERT INTO masters(telegram_id, full_name, is_active) VALUES (?, 'Мастер', 1)",
        (MASTER_TG,),
    )
    await db.execute(
        "INSERT INTO master_directions(master_telegram_id, direction) VALUES (?, ?)",
        (MASTER_TG, "Математика"),
    )
    await db.execute(
        """
        INSERT INTO orders(
            id, user_id, direction, subject, work_type, description,
            status, assignment_status, price_status, group_chat_id, topic_id
        ) VALUES (?, 1, ?, 'Задача', 'solution', 'решить',
                  'new', ?, 'waiting_master', ?, 1)
        """,
        (ORDER_ID, direction, assignment_status, GROUP_ID),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_reassignment_entry_sets_waiting_for_master_state(routing, db):
    await prepare_order(db)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=CLIENT_TG, callback=f"reassign_choose:{ORDER_ID}", mid=30,
    ))

    state = dp.fsm.get_context(bot=bot, chat_id=CLIENT_TG, user_id=CLIENT_TG)
    assert await state.get_state() == OrderState.waiting_for_master.state
    assert await state.get_data() == {
        "reassign_order_id": ORDER_ID,
        "assignment_mode": "specific",
        "direction": "Математика",
    }
    out = new_texts_after(session, before)
    assert any("Выберите другого исполнителя" in value for value in out)


@pytest.mark.asyncio
async def test_reassignment_entry_rejects_foreign_client(routing, db):
    await prepare_order(db)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=OTHER_CLIENT_TG, callback=f"reassign_choose:{ORDER_ID}", mid=31,
    ))

    state = dp.fsm.get_context(
        bot=bot, chat_id=OTHER_CLIENT_TG, user_id=OTHER_CLIENT_TG
    )
    assert await state.get_state() is None
    replies = [
        call for call in session.calls[before:]
        if call.__api_method__ == "answerCallbackQuery"
    ]
    assert replies and "недоступен для перевыбора" in replies[-1].text


@pytest.mark.asyncio
async def test_free_master_mode_publishes_once_with_captured_generation(routing, db, monkeypatch):
    await prepare_order(db)
    from handlers import client_reassignment

    captured = {}

    async def publish(*, bot, order_id, expected_assignment_generation):
        captured["order_id"] = order_id
        captured["generation"] = expected_assignment_generation
        return 42

    monkeypatch.setattr(client_reassignment, "create_order_assignment_topic", publish)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=CLIENT_TG, callback=f"reassign_free:{ORDER_ID}", mid=32,
    ))

    order = await (await db.execute(
        "SELECT assignment_status, assignment_generation, price_status FROM orders WHERE id=?",
        (ORDER_ID,),
    )).fetchone()
    assert order[0] == "open"
    assert captured == {"order_id": ORDER_ID, "generation": order[1]}
    log = await (await db.execute(
        "SELECT action, details FROM user_activity_logs WHERE entity_id=?",
        (ORDER_ID,),
    )).fetchone()
    assert log[0] == "assignment_mode_changed"
    assert "free_master" in log[1]
    assert any("открыт для свободного исполнителя" in value for value in new_texts_after(session, before))


@pytest.mark.asyncio
async def test_free_master_topic_failure_compensates_generation(routing, db, monkeypatch):
    await prepare_order(db)
    from handlers import client_reassignment

    async def fail_publish(**kwargs):
        raise RuntimeError("synthetic topic failure")

    monkeypatch.setattr(client_reassignment, "create_order_assignment_topic", fail_publish)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=CLIENT_TG, callback=f"reassign_free:{ORDER_ID}", mid=33,
    ))

    order = await (await db.execute(
        "SELECT assignment_status, assigned_master_telegram_id, group_chat_id, topic_id "
        "FROM orders WHERE id=?",
        (ORDER_ID,),
    )).fetchone()
    # Свободный режим остаётся доступным для повторного выбора, но
    # незавершённая тема и назначение очищены компенсацией.
    assert tuple(order) == ("open", None, None, None)
    replies = [
        call for call in session.calls[before:]
        if call.__api_method__ == "answerCallbackQuery"
    ]
    assert replies and "возвращён к выбору исполнителя" in replies[-1].text


@pytest.mark.asyncio
async def test_free_master_mode_requires_waiting_selection(routing, db):
    await prepare_order(db, assignment_status="assigned")
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=CLIENT_TG, callback=f"reassign_free:{ORDER_ID}", mid=34,
    ))

    replies = [
        call for call in session.calls[before:]
        if call.__api_method__ == "answerCallbackQuery"
    ]
    assert replies and "нельзя перевести" in replies[-1].text


@pytest.mark.asyncio
async def test_reassign_cancel_cancels_unpaid_order_and_clears_state(routing, db):
    await prepare_order(db)
    dp, bot, session = routing
    state = dp.fsm.get_context(bot=bot, chat_id=CLIENT_TG, user_id=CLIENT_TG)
    await state.set_state(OrderState.waiting_for_master)
    await state.set_data({"reassign_order_id": ORDER_ID})
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=CLIENT_TG, callback=f"reassign_cancel:{ORDER_ID}", mid=35,
    ))

    order = await (await db.execute(
        "SELECT status, assignment_status, price_status FROM orders WHERE id=?",
        (ORDER_ID,),
    )).fetchone()
    assert tuple(order) == ("cancelled", "cancelled", "rejected")
    assert await state.get_state() is None
    assert any("отменён" in value for value in new_texts_after(session, before))


@pytest.mark.asyncio
async def test_reassign_cancel_rejects_paid_order(routing, db):
    await prepare_order(db)
    dp, bot, _ = routing
    await db.execute("UPDATE orders SET status='in_progress' WHERE id=?", (ORDER_ID,))
    await db.commit()
    before_count = (await (await db.execute("SELECT COUNT(*) FROM orders")).fetchone())[0]

    await dp.feed_update(bot, incoming(
        uid=CLIENT_TG, callback=f"reassign_cancel:{ORDER_ID}", mid=36,
    ))

    order = await (await db.execute(
        "SELECT status FROM orders WHERE id=?", (ORDER_ID,)
    )).fetchone()
    assert order[0] == "in_progress"
    assert before_count == 1
