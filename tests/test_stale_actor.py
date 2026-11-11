"""AUTH-STALE: DB проверяет исполнителя и генерацию после задержки."""
import aiosqlite
import pytest

import test_financial_concurrency as financial
from test_financial_concurrency import agree_price, MASTER_A_TG, MASTER_B_TG
from database.orders import get_order_by_id, update_order_status
from database.payments import pay_order_from_balance, cancel_paid_order_with_refund
from database.payments import refund_paid_order_for_master_reassignment
from database.orders import cancel_new_order_by_master

shop_db = financial.shop_db


@pytest.mark.parametrize("same_master", [False, True])
async def test_stale_actor_cannot_change_or_refund_new_generation(shop_db, same_master):
    path, order_id = shop_db
    await agree_price(path, order_id)
    assert (await pay_order_from_balance(order_id, 1))[0] == "success"
    captured = await get_order_by_id(order_id)
    current_master = MASTER_A_TG if same_master else MASTER_B_TG
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE orders SET assigned_master_telegram_id=?, assignment_generation=assignment_generation+1 WHERE id=?", (current_master, order_id))
        await db.commit()
    # Прежняя авторизация A больше недействительна, включая A→A.
    assert not await update_order_status(order_id, "in_progress", "waiting_client", actor_telegram_id=MASTER_A_TG,
                                         expected_assignment_generation=captured['assignment_generation'])
    result = await cancel_paid_order_with_refund(order_id, actor_telegram_id=MASTER_A_TG,
                                                expected_assignment_generation=captured['assignment_generation'])
    assert result[0] == "not_available"
    result = await refund_paid_order_for_master_reassignment(order_id, MASTER_A_TG,
                expected_assignment_generation=captured['assignment_generation'])
    assert result[0] == "not_available"
    assert not await cancel_new_order_by_master(order_id, MASTER_A_TG,
                expected_assignment_generation=captured['assignment_generation'])
    current = await get_order_by_id(order_id)
    assert current['status'] == 'in_progress'
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM balance_transactions")).fetchone())[0] == 1
        assert (await (await db.execute("SELECT balance FROM users WHERE id=1")).fetchone())[0] == 4250
    assert await update_order_status(order_id, "in_progress", "waiting_client", actor_telegram_id=current_master,
                                     expected_assignment_generation=current['assignment_generation'])


async def test_admin_still_needs_current_generation(shop_db):
    path, order_id = shop_db
    await agree_price(path, order_id)
    await pay_order_from_balance(order_id, 1)
    current = await get_order_by_id(order_id)
    assert await update_order_status(order_id, "in_progress", "waiting_client", actor_telegram_id=88001,
                                     expected_assignment_generation=current['assignment_generation'])
