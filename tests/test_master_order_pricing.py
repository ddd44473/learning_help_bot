"""Характеризационные тесты ценового workflow исполнителя (workstream A).

Проверяются реальные aiogram filters/routing через Dispatcher, точные
callback-контракты, FSM-данные и последовательность БД/уведомлений.
"""
import aiosqlite
import pytest

from states.master_price import MasterPriceState
from test_dispatcher_phase2 import incoming, new_texts_after, routing as dispatcher_routing

routing = dispatcher_routing

CLIENT_TG = 7770001
MASTER_TG = 999001
GROUP_ID = -10088001
ORDER_ID = 1


async def prepare_order(db, *, price_status="waiting_master", master_active=1):
    await db.execute(
        "INSERT INTO users(id, telegram_id, is_registered) VALUES (1, ?, 1)",
        (CLIENT_TG,),
    )
    await db.execute(
        "INSERT INTO masters(telegram_id, full_name, is_active) VALUES (?, 'Мастер', ?)",
        (MASTER_TG, master_active),
    )
    await db.execute(
        """
        INSERT INTO orders(
            id, user_id, direction, subject, work_type, description,
            status, assigned_master_telegram_id, assignment_status,
            price_status, group_chat_id, topic_id
        ) VALUES (?, 1, 'Математика', 'Задача', 'solution', 'решить',
                  'new', ?, 'assigned', ?, ?, 1)
        """,
        (ORDER_ID, MASTER_TG, price_status, GROUP_ID),
    )
    await db.commit()


async def start_offer(routing_bundle, master=MASTER_TG, mid=10):
    dp, bot, session = routing_bundle
    await dp.feed_update(bot, incoming(
        uid=master, callback=f"offer_price:{ORDER_ID}",
        chat_type="supergroup", mid=mid,
    ))
    return session


async def offer_count(db):
    cursor = await db.execute("SELECT COUNT(*) FROM order_price_offers")
    return (await cursor.fetchone())[0]


@pytest.mark.asyncio
async def test_start_offer_sets_exact_fsm_payload(routing, db):
    await prepare_order(db)
    dp, bot, session = routing

    await start_offer(routing)

    state = dp.fsm.get_context(bot=bot, chat_id=GROUP_ID, user_id=MASTER_TG)
    assert await state.get_state() == MasterPriceState.waiting_for_price.state
    assert await state.get_data() == {
        "order_id": ORDER_ID,
        "group_chat_id": GROUP_ID,
        "topic_id": 1,
        "expected_price_status": "waiting_master",
        "client_telegram_id": CLIENT_TG,
        "direction": "Математика",
    }
    assert any("Введите стоимость выполнения" in text for text in new_texts_after(session, 0))
    assert await offer_count(db) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["1 500", "1500"])
async def test_valid_amount_creates_offer_and_notifies_client(routing, db, text):
    await prepare_order(db)
    await start_offer(routing)

    dp, bot, session = routing
    before = len(session.calls)
    await dp.feed_update(bot, incoming(
        uid=MASTER_TG, text=text, chat_type="supergroup", mid=11,
    ))

    cursor = await db.execute(
        "SELECT amount, offered_by, status FROM order_price_offers WHERE order_id=?",
        (ORDER_ID,),
    )
    assert tuple(await cursor.fetchone()) == (1500, "master", "pending")
    row = await (await db.execute(
        "SELECT price_status FROM orders WHERE id=?", (ORDER_ID,)
    )).fetchone()
    assert row[0] == "waiting_client"
    assert not await dp.fsm.get_context(
        bot=bot, chat_id=GROUP_ID, user_id=MASTER_TG
    ).get_state()
    out = new_texts_after(session, before)
    assert any("предложена клиенту" in value for value in out)
    assert any("Исполнитель предложил" in value for value in out)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected",
    [
        ("abc", "целым числом"),
        ("0", "больше нуля"),
        ("-5", "больше нуля"),
        ("1000001", "слишком большая"),
        ("   ", "целым числом"),
    ],
)
async def test_invalid_amount_is_rejected(routing, db, text, expected):
    await prepare_order(db)
    await start_offer(routing)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=MASTER_TG, text=text, chat_type="supergroup", mid=12,
    ))

    assert await offer_count(db) == 0
    assert any(expected in value for value in new_texts_after(session, before))
    assert await dp.fsm.get_context(
        bot=bot, chat_id=GROUP_ID, user_id=MASTER_TG
    ).get_state() == MasterPriceState.waiting_for_price.state


@pytest.mark.asyncio
async def test_non_text_amount_is_rejected(routing, db):
    await prepare_order(db)
    await start_offer(routing)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=MASTER_TG, text="", chat_type="supergroup", mid=13,
    ))

    assert await offer_count(db) == 0
    assert any("текстом" in value for value in new_texts_after(session, before))


@pytest.mark.asyncio
async def test_cancel_clears_state_without_offer(routing, db):
    await prepare_order(db)
    await start_offer(routing)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=MASTER_TG, text="/cancel", chat_type="supergroup", mid=14,
    ))

    assert await offer_count(db) == 0
    assert await dp.fsm.get_context(
        bot=bot, chat_id=GROUP_ID, user_id=MASTER_TG
    ).get_state() is None
    assert any("отменён" in value for value in new_texts_after(session, before))


@pytest.mark.asyncio
async def test_foreign_master_cannot_use_pending_state(routing, db):
    await prepare_order(db)
    await start_offer(routing)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=888888, text="1500", chat_type="supergroup", mid=15,
    ))

    assert await offer_count(db) == 0
    assert any("другим исполнителем" in value for value in new_texts_after(session, before))
    # Чужой пользователь имеет собственный FSM-ключ; состояние мастера
    # не должно быть использовано для создания предложения.
    assert await offer_count(db) == 0


@pytest.mark.asyncio
async def test_inactive_master_is_denied_by_shared_access_guard(routing, db):
    await prepare_order(db, master_active=0)
    dp, bot, session = routing
    before = len(session.calls)

    await start_offer(routing)

    replies = [
        call for call in session.calls[before:]
        if call.__api_method__ == "answerCallbackQuery"
    ]
    assert replies and replies[-1].show_alert is True
    assert "активная карточка" in replies[-1].text
    assert await offer_count(db) == 0


@pytest.mark.asyncio
async def test_start_offer_outside_master_group_is_rejected(routing, db):
    await prepare_order(db)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=MASTER_TG, callback=f"offer_price:{ORDER_ID}", mid=16,
    ))

    replies = [
        call for call in session.calls[before:]
        if call.__api_method__ == "answerCallbackQuery"
    ]
    assert replies and "рабочей группе" in replies[-1].text
    assert await offer_count(db) == 0


async def insert_client_offer(db, amount=1200):
    cursor = await db.execute(
        """
        INSERT INTO order_price_offers(
            order_id, offered_by, offered_by_telegram_id, amount, status
        ) VALUES (?, 'client', ?, ?, 'pending')
        """,
        (ORDER_ID, CLIENT_TG, amount),
    )
    await db.execute(
        "UPDATE orders SET price_status='waiting_master_response' WHERE id=?",
        (ORDER_ID,),
    )
    await db.commit()
    return int(cursor.lastrowid)


@pytest.mark.asyncio
async def test_accept_counter_price_commits_agreed_price_and_payment_screen(routing, db):
    await prepare_order(db, price_status="waiting_master_response")
    offer_id = await insert_client_offer(db)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=MASTER_TG, callback=f"master_accept_price:{ORDER_ID}:{offer_id}",
        chat_type="supergroup", mid=17,
    ))

    order = await (await db.execute(
        "SELECT agreed_price, price_status, status FROM orders WHERE id=?",
        (ORDER_ID,),
    )).fetchone()
    assert tuple(order) == (1200, "agreed", "new")
    offer = await (await db.execute(
        "SELECT status FROM order_price_offers WHERE id=?", (offer_id,)
    )).fetchone()
    assert offer[0] == "accepted"
    out = new_texts_after(session, before)
    assert any("Встречная цена принята" in value for value in out)
    assert any("Исполнитель согласился на вашу стоимость" in value for value in out)

    # Повторная кнопка не меняет финансовое состояние.
    await dp.feed_update(bot, incoming(
        uid=MASTER_TG, callback=f"master_accept_price:{ORDER_ID}:{offer_id}",
        chat_type="supergroup", mid=18,
    ))
    assert tuple(await (await db.execute(
        "SELECT agreed_price FROM orders WHERE id=?", (ORDER_ID,)
    )).fetchone()) == (1200,)


@pytest.mark.asyncio
async def test_reject_counter_price_returns_order_to_client_selection(routing, db):
    await prepare_order(db, price_status="waiting_master_response")
    offer_id = await insert_client_offer(db)
    dp, bot, session = routing
    before = len(session.calls)

    await dp.feed_update(bot, incoming(
        uid=MASTER_TG, callback=f"master_reject_price:{ORDER_ID}:{offer_id}",
        chat_type="supergroup", mid=19,
    ))

    order = await (await db.execute(
        """
        SELECT status, price_status, assignment_status,
               assigned_master_telegram_id, group_chat_id, topic_id
        FROM orders WHERE id=?
        """,
        (ORDER_ID,),
    )).fetchone()
    assert tuple(order) == ("new", "waiting_master", "waiting_selection", None, None, None)
    offer = await (await db.execute(
        "SELECT status FROM order_price_offers WHERE id=?", (offer_id,)
    )).fetchone()
    assert offer[0] == "rejected"
    attempt = await (await db.execute(
        "SELECT outcome, reason FROM order_master_attempts WHERE order_id=?",
        (ORDER_ID,),
    )).fetchone()
    assert tuple(attempt) == ("negotiation_failed", "master_rejected_client_price")
    out = new_texts_after(session, before)
    assert any("Встречная цена отклонена" in value for value in out)
    assert any("не согласился на вашу встречную цену" in value for value in out)


@pytest.mark.asyncio
async def test_wrong_master_cannot_accept_or_reject(routing, db):
    await prepare_order(db, price_status="waiting_master_response")
    offer_id = await insert_client_offer(db)
    dp, bot, _ = routing

    await dp.feed_update(bot, incoming(
        uid=888888, callback=f"master_accept_price:{ORDER_ID}:{offer_id}",
        chat_type="supergroup", mid=20,
    ))
    assert tuple(await (await db.execute(
        "SELECT agreed_price FROM orders WHERE id=?", (ORDER_ID,)
    )).fetchone()) == (None,)

    await dp.feed_update(bot, incoming(
        uid=888888, callback=f"master_reject_price:{ORDER_ID}:{offer_id}",
        chat_type="supergroup", mid=21,
    ))
    assert tuple(await (await db.execute(
        "SELECT status FROM order_price_offers WHERE id=?", (offer_id,)
    )).fetchone()) == ("pending",)
