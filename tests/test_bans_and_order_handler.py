"""AUTH-01 / OBS-01 / ORD-01: транзакции и обработчики без Telegram."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import test_financial_concurrency as financial
from test_financial_concurrency import agree_price, MASTER_A_TG, CLIENT_TG
from database.access_control import ban_user, unban_user, is_banned
from database.orders import create_order, claim_open_order, assign_waiting_order_to_master
from database.masters import get_active_masters, get_active_masters_count, is_master_available

shop_db = financial.shop_db


async def test_ban_between_listing_and_assignment(shop_db):
    path, order_id = shop_db
    assert MASTER_A_TG in {row['telegram_id'] for row in await get_active_masters()}
    assert await ban_user(MASTER_A_TG, "old_name", 88001)
    assert not await is_master_available(MASTER_A_TG)
    assert MASTER_A_TG not in {row['telegram_id'] for row in await get_active_masters()}
    assert await get_active_masters_count() == 1
    with pytest.raises(ValueError):
        await create_order(1, "task", "solution", "synthetic", MASTER_A_TG, direction="Математика")
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE orders SET assigned_master_telegram_id=NULL, assignment_status='open' WHERE id=?", (order_id,))
        await db.execute("UPDATE masters SET username='new_name' WHERE telegram_id=?", (MASTER_A_TG,))
        await db.commit()
    assert await is_banned(MASTER_A_TG)
    assert not await claim_open_order(order_id, MASTER_A_TG)
    assert not await assign_waiting_order_to_master(order_id, CLIENT_TG, MASTER_A_TG)
    assert await unban_user(MASTER_A_TG, 88001)
    assert await claim_open_order(order_id, MASTER_A_TG)


async def test_assignment_returns_generation_from_its_transaction(shop_db):
    from database.orders import cancel_new_order_by_master, get_assignment_generation
    path, order_id = shop_db
    assert await cancel_new_order_by_master(order_id, MASTER_A_TG)
    assigned, captured = await assign_waiting_order_to_master(order_id, CLIENT_TG, 999002, capture_generation=True)
    assert assigned and captured == await get_assignment_generation(order_id)
    assert await cancel_new_order_by_master(order_id, 999002)
    from database.orders import set_order_open_for_free_master
    # Оба уже отказавшихся мастера исключены — добавим нового кандидата.
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO masters(telegram_id, full_name) VALUES (999003, 'Synthetic')")
        await db.execute("INSERT INTO master_directions(master_telegram_id, direction) VALUES (999003, 'Математика')")
        await db.commit()
    opened, generation = await set_order_open_for_free_master(order_id, CLIENT_TG, capture_generation=True)
    assert opened and generation == await get_assignment_generation(order_id)
    assert generation > captured


async def test_ban_preserves_active_paid_order(shop_db):
    from database.payments import pay_order_from_balance
    path, order_id = shop_db
    await agree_price(path, order_id)
    assert (await pay_order_from_balance(order_id, 1))[0] == "success"
    await ban_user(MASTER_A_TG, None, 88001)
    async with aiosqlite.connect(path) as db:
        row = await (await db.execute("SELECT status, assigned_master_telegram_id FROM orders WHERE id=?", (order_id,))).fetchone()
        assert tuple(row) == ("in_progress", MASTER_A_TG)
        assert (await (await db.execute("SELECT COUNT(*) FROM balance_transactions")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT COUNT(*) FROM ban_business_audit")).fetchone())[0] == 1


async def test_committed_ban_log_failure_is_not_failure(shop_db, monkeypatch):
    from handlers import admin_bans
    monkeypatch.setattr(admin_bans, "ADMIN_IDS", {88001})
    monkeypatch.setattr(admin_bans, "log_user_activity", AsyncMock(side_effect=RuntimeError("log failed")))
    message = SimpleNamespace(from_user=SimpleNamespace(id=88001), chat=SimpleNamespace(type="private"),
                              text=f"/ban {MASTER_A_TG}", answer=AsyncMock())
    await admin_bans.ban_command(message)
    assert await is_banned(MASTER_A_TG)
    assert message.answer.call_args.args[0].startswith("Пользователь заблокирован.")


@pytest.mark.parametrize("attempts", [2, 5, 20])
async def test_concurrent_confirm_handler_publishes_once(shop_db, monkeypatch, attempts):
    from handlers import order_form
    path, _ = shop_db
    publish = AsyncMock(return_value=42)
    monkeypatch.setattr(order_form, "create_order_assignment_topic", publish)
    monkeypatch.setattr(order_form, "log_user_activity", AsyncMock())
    draft = dict(draft_id="one-draft", confirmation_message_id=50, direction="Математика",
                 work_type="solution", description="synthetic task", assigned_master_telegram_id=MASTER_A_TG)
    barrier = asyncio.Barrier(attempts)

    async def get_data():
        await barrier.wait()
        return dict(draft)

    async def confirm():
        callback = SimpleNamespace(from_user=SimpleNamespace(id=CLIENT_TG), answer=AsyncMock(),
            message=SimpleNamespace(message_id=50, edit_reply_markup=AsyncMock(), answer=AsyncMock()))
        state = SimpleNamespace(get_data=get_data, set_state=AsyncMock(), clear=AsyncMock())
        await order_form.confirm_order(callback, state, SimpleNamespace(id=123456))

    await asyncio.gather(*(confirm() for _ in range(attempts)))
    publish.assert_awaited_once()
    assert isinstance(publish.call_args.kwargs['order_id'], int)
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM orders")).fetchone())[0] == 2


async def test_old_confirmation_rejected(shop_db):
    from handlers import order_form
    callback = SimpleNamespace(message=SimpleNamespace(message_id=51), answer=AsyncMock())
    state = SimpleNamespace(get_data=AsyncMock(return_value={"draft_id": "new", "confirmation_message_id": 50}))
    await order_form.confirm_order(callback, state, SimpleNamespace(id=123456))
    assert "устарело" in callback.answer.call_args.args[0]
