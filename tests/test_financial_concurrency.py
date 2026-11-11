"""Финансовая конкурентная регрессионная свита (§14 build_full.md).

Все сценарии — на реальном файловом SQLite (aiosqlite), synthetic data,
без Telegram и без реальных данных. Каждый тест проверяет точные
балансы, историю движений и итоговые состояния заказов.
"""

import asyncio

import aiosqlite
import pytest_asyncio

from database.payments import (
    PAYMENT_RESULT_NOT_AVAILABLE,
    PAYMENT_RESULT_SUCCESS,
    REFUND_RESULT_SUCCESS,
    cancel_paid_order_with_refund,
    pay_order_from_balance,
    refund_paid_order_for_master_reassignment,
)

PRICE = 1000

CLIENT_TG = 7770001
REFERRER_TG = 7770002
MASTER_A_TG = 999001
MASTER_B_TG = 999002

NOW = "datetime('now', '+3 hours')"


@pytest_asyncio.fixture
async def shop_db(tmp_path, use_db_path):
    """Готовая синтетическая экономика: клиент, два исполнителя,
    реферальная связь исполнителя с клиентом и согласованная цена."""
    from database.db import init_db
    from database.migrations import run_migrations
    from database.orders import create_order

    db_path = tmp_path / "fin_conc.db"
    use_db_path(db_path)
    await init_db()
    await run_migrations()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"""
            INSERT INTO users (
                id, telegram_id, tg_username, balance, bonus_balance,
                is_registered, created_at, updated_at, last_active_at
            )
            VALUES (
                1, {CLIENT_TG}, 'client', 5000, 0, 1, {NOW}, {NOW}, {NOW}
            )
            """
        )
        await db.execute(
            f"""
            INSERT INTO users (
                id, telegram_id, tg_username, balance, bonus_balance,
                is_registered, created_at, updated_at, last_active_at
            )
            VALUES (
                2, {REFERRER_TG}, 'referrer', 0, 0, 1, {NOW}, {NOW}, {NOW}
            )
            """
        )
        await db.execute(
            f"""
            INSERT INTO referrals (
                referrer_user_id, referred_user_id, created_at
            )
            VALUES (2, 1, {NOW})
            """
        )
        for telegram_id, full_name in (
            (MASTER_A_TG, "Мастер А"),
            (MASTER_B_TG, "Мастер Б"),
        ):
            await db.execute(
                f"""
                INSERT INTO masters (
                    telegram_id, full_name, is_active, created_at, updated_at
                )
                VALUES ({telegram_id}, '{full_name}', 1, {NOW}, {NOW})
                """
            )
            await db.execute(
                f"""
                INSERT INTO master_directions (
                    master_telegram_id, direction, created_at
                )
                VALUES ({telegram_id}, 'Математика', {NOW})
                """
            )
        await db.commit()

    order_id, _fresh = await create_order(
        user_id=1,
        subject="Задача",
        work_type="solution",
        description="решить",
        assigned_master_telegram_id=MASTER_A_TG,
        direction="Математика",
    )
    yield db_path, order_id


async def agree_price(db_path, order_id: int, amount: int = PRICE) -> int:
    """Согласовывает цену: предложение мастера принято клиентом."""
    from database.price_offers import (
        accept_price_offer,
    )

    offer_id = await insert_offer(
        db_path, order_id, MASTER_A_TG, amount, "waiting_client"
    )
    ok, agreed_price = await accept_price_offer(
        order_id=order_id,
        offer_id=offer_id,
        accepted_by="client",
        accepted_by_telegram_id=CLIENT_TG,
        expected_price_status="waiting_client",
    )
    assert ok and agreed_price == amount
    return offer_id


async def insert_offer(
    db_path,
    order_id: int,
    offered_by_tg: int,
    amount: int,
    next_price_status: str,
) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            f"""
            INSERT INTO order_price_offers (
                order_id, offered_by, offered_by_telegram_id, amount,
                status, created_at
            )
            VALUES (?, 'master', ?, ?, 'pending', {NOW})
            """,
            (order_id, offered_by_tg, amount),
        )
        offer_id = int(cursor.lastrowid)
        await db.execute(
            """
            UPDATE orders SET price_status = ? WHERE id = ?
            """,
            (next_price_status, order_id),
        )
        await db.commit()
        await cursor.close()
    return offer_id


async def fetch_one(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
    return row[0] if row is not None else None


async def cash_movements(db_path, user_id=1):
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            SELECT
                -IFNULL(SUM(amount), 0) AS held,
                COUNT(*) AS rows
            FROM balance_transactions
            WHERE user_id = ? AND transaction_type IN ('payment', 'refund')
            """,
            (user_id,),
        )
        (held, rows) = await cursor.fetchone()
        await cursor.close()
        return int(held or 0), int(rows)


# ------------------------------------------------------------------
# Конкурентная оплата одного заказа
# ------------------------------------------------------------------


async def test_two_concurrent_payments_only_one_moves_money(shop_db):
    db_path, order_id = shop_db
    await agree_price(db_path, order_id)

    barrier = asyncio.Barrier(2)

    async def attempt(use_bonus: bool):
        await barrier.wait()
        return await pay_order_from_balance(
            order_id, 1, use_bonus=use_bonus
        )

    result_a, result_b = await asyncio.gather(
        attempt(True), attempt(False)
    )
    outcomes = [result_a[0], result_b[0]]
    assert sorted(outcomes) == [
        PAYMENT_RESULT_NOT_AVAILABLE,
        PAYMENT_RESULT_SUCCESS,
    ]

    held, rows = await cash_movements(db_path)
    # Скидка 250 ₽ приглашённого: деньгами 750, ровно одна запись.
    assert held == PRICE - 250 and rows == 1
    assert await fetch_one(
        db_path, "SELECT status FROM orders WHERE id = ?", (order_id,)
    ) == "in_progress"
    assert await fetch_one(
        db_path, "SELECT balance FROM users WHERE id = 1"
    ) == 5000 - (PRICE - 250)


async def test_bonus_cannot_exceed_15_percent_cap(shop_db):
    db_path, order_id = shop_db
    from database.payments import _payment_amounts

    amounts = _payment_amounts(
        price=1000,
        balance=999999,
        bonus_balance=100000,
        referral_eligible=False,
    )
    assert amounts["max_bonus_available"] == 150
    assert amounts["cash_with_bonus"] == 850


async def test_no_double_bonus_spend_concurrently(shop_db):
    """Две конкурентные попытки оплаты бонусами: суммарное списание
    не превосходит начисление."""
    db_path, order_id = shop_db

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"""
            INSERT INTO bonus_grants (
                user_id, amount, remaining_amount, source_type,
                source_order_id, granted_at, expires_at, related_user_id
            )
            VALUES (
                1, 200, 200, 'referral_reward', NULL, {NOW},
                datetime('now', '+3 hours', '+10 days'), 2
            )
            """
        )
        await db.commit()

    await agree_price(db_path, order_id)

    barrier = asyncio.Barrier(2)

    async def attempt():
        await barrier.wait()
        return await pay_order_from_balance(order_id, 1, use_bonus=True)

    results = await asyncio.gather(attempt(), attempt())
    outcomes = [r[0] for r in results]
    assert sorted(outcomes) == [
        PAYMENT_RESULT_NOT_AVAILABLE,
        PAYMENT_RESULT_SUCCESS,
    ]

    # Бонусы списаны не более 15% (150) и не более одного раза.
    grants = await fetch_one(
        db_path,
        "SELECT remaining_amount FROM bonus_grants",
    )
    assert grants == 200 - 150
    allocations = await fetch_one(
        db_path,
        "SELECT COUNT(*) FROM bonus_usage_allocations",
    )
    assert allocations == 1

    held, rows = await cash_movements(db_path)
    # 1000 - 250 скидка - 150 бонусами = 600 деньгами.
    assert held == 600
    assert rows == 1


# ------------------------------------------------------------------
# Конкурентные возвраты
# ------------------------------------------------------------------


async def test_two_concurrent_refunds_refund_once(shop_db):
    db_path, order_id = shop_db
    await agree_price(db_path, order_id)

    ok_pay, _balance, _cash = await pay_order_from_balance(order_id, 1)
    assert ok_pay == PAYMENT_RESULT_SUCCESS

    barrier = asyncio.Barrier(2)

    async def attempt():
        await barrier.wait()
        return await cancel_paid_order_with_refund(order_id)

    refund_a, refund_b = await asyncio.gather(attempt(), attempt())
    outcomes = [refund_a[0], refund_b[0]]
    assert REFUND_RESULT_SUCCESS in outcomes
    assert len(set(outcomes)) == 2  # один успех, второй — отказ

    held, rows = await cash_movements(db_path)
    assert held == 0  # оплата и возврат компенсировались
    assert rows == 2  # одна оплата + один возврат
    assert await fetch_one(
        db_path, "SELECT status FROM orders WHERE id = ?", (order_id,)
    ) == "cancelled"
    assert await fetch_one(
        db_path, "SELECT balance FROM users WHERE id = 1"
    ) == 5000


async def test_payment_vs_client_cancellation_mutually_exclusive(shop_db):
    """Оплата и отмена неоплаченного заказа не могут произойти одновременно:
    ровно одна операция определяет финал."""
    db_path, order_id = shop_db
    await agree_price(db_path, order_id)

    from database.orders import cancel_unpaid_order_by_client

    barrier = asyncio.Barrier(2)

    async def do_pay():
        await barrier.wait()
        return await pay_order_from_balance(order_id, 1)

    async def do_cancel():
        await barrier.wait()
        return await cancel_unpaid_order_by_client(
            order_id, CLIENT_TG
        )

    pay_result, cancel_result = await asyncio.gather(do_pay(), do_cancel())

    status = await fetch_one(
        db_path, "SELECT status FROM orders WHERE id = ?", (order_id,)
    )
    held, rows = await cash_movements(db_path)
    final_balance = await fetch_one(
        db_path, "SELECT balance FROM users WHERE id = 1"
    )

    if status == "in_progress":
        assert pay_result[0] == PAYMENT_RESULT_SUCCESS
        assert cancel_result is False
        assert held == PRICE - 250 and rows == 1
        assert final_balance == 5000 - (PRICE - 250)
    else:
        assert status == "cancelled"
        assert cancel_result is True
        assert pay_result[0] != PAYMENT_RESULT_SUCCESS
        assert held == 0 and rows == 0
        assert final_balance == 5000


async def test_completion_vs_cancellation(shop_db):
    db_path, order_id = shop_db
    await agree_price(db_path, order_id)
    assert (await pay_order_from_balance(order_id, 1))[0] == PAYMENT_RESULT_SUCCESS
    # Переводим в waiting_client: работа сдана.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE orders SET status = 'waiting_client' WHERE id = ?",
            (order_id,),
        )
        await db.commit()

    from database.orders import confirm_order_completion_by_client

    barrier = asyncio.Barrier(2)

    async def do_complete():
        await barrier.wait()
        return await confirm_order_completion_by_client(
            order_id, CLIENT_TG
        )

    async def do_refund_cancel():
        await barrier.wait()
        return await cancel_paid_order_with_refund(order_id)

    completed, refund = await asyncio.gather(
        do_complete(), do_refund_cancel()
    )

    status = await fetch_one(
        db_path, "SELECT status FROM orders WHERE id = ?", (order_id,)
    )
    held, _rows = await cash_movements(db_path)
    grant_exists = await fetch_one(
        db_path,
        "SELECT COUNT(*) FROM bonus_grants WHERE user_id = 2",
    )

    if status == "completed":
        assert completed is True
        assert refund[0] != REFUND_RESULT_SUCCESS
        assert held == PRICE - 250
        # Реферальная награда 250 запрошена единожды при завершении.
        assert grant_exists == 1
    else:
        assert status == "cancelled"
        assert refund[0] == REFUND_RESULT_SUCCESS
        assert held == 0
        assert grant_exists == 0


# ------------------------------------------------------------------
# Два исполнителя претендуют на свободный заказ
# ------------------------------------------------------------------


async def test_two_masters_claim_free_order_once(shop_db):
    db_path, order_id = shop_db

    from database.orders import (
        claim_open_order,
    )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE orders
            SET assigned_master_telegram_id = NULL,
                assignment_status = 'open'
            WHERE id = ?
            """,
            (order_id,),
        )
        await db.commit()

    barrier = asyncio.Barrier(2)

    async def attempt(master_tg):
        await barrier.wait()
        return await claim_open_order(order_id, master_tg)

    claim_a, claim_b = await asyncio.gather(
        attempt(MASTER_A_TG), attempt(MASTER_B_TG)
    )
    assert claim_a != claim_b  # ровно один успел
    assigned = await fetch_one(
        db_path,
        "SELECT assigned_master_telegram_id FROM orders WHERE id = ?",
        (order_id,),
    )
    assert assigned in (MASTER_A_TG, MASTER_B_TG)


# ------------------------------------------------------------------
# Возмещение при перевыборе исполнителя + цикл pay/refund/pay
# ------------------------------------------------------------------


async def test_reassignment_refund_cycle_conserves_money(shop_db):
    db_path, order_id = shop_db
    await agree_price(db_path, order_id)
    ok_pay = (await pay_order_from_balance(order_id, 1))[0]
    assert ok_pay == PAYMENT_RESULT_SUCCESS

    ok_refund, balance_after_refund, _cash = (
        await refund_paid_order_for_master_reassignment(
            order_id, MASTER_A_TG
        )
    )
    assert ok_refund == REFUND_RESULT_SUCCESS
    assert balance_after_refund == 5000

    # Новая цена согласована вторым оффером и снова оплачена.
    await insert_offer(
        db_path, order_id, MASTER_A_TG, PRICE, "waiting_master"
    )
    await agree_price(db_path, order_id, PRICE)

    ok_pay2, balance_after_pay2, _cash2 = await pay_order_from_balance(
        order_id, 1
    )
    assert ok_pay2 == PAYMENT_RESULT_SUCCESS
    assert balance_after_pay2 == 5000 - (PRICE - 250)

    held, rows = await cash_movements(db_path)
    # оплата 750, возврат 750, повторная оплата 750: ровно одна
    # действующая оплата и три записи.
    assert held == PRICE - 250
    assert rows == 3

    # После второго закрепления исполнителя A заказ оплачен заново:
    # экранное состояние не зависело от устаревших компенсаций.
    assert await fetch_one(
        db_path, "SELECT cash_paid_amount FROM orders WHERE id = ?",
        (order_id,),
    ) == PRICE - 250


async def test_referral_reward_exactly_once(shop_db):
    """Награда за первое завершение начисляется ровно один раз."""
    db_path, order_id = shop_db
    await agree_price(db_path, order_id)
    ok_pay = (await pay_order_from_balance(order_id, 1))[0]
    assert ok_pay == PAYMENT_RESULT_SUCCESS

    from database.orders import confirm_order_completion_by_client
    from database.referrals import grant_referral_reward_for_completed_order_in_tx

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE orders SET status = 'waiting_client' WHERE id = ?",
            (order_id,),
        )
        await db.commit()

    assert await confirm_order_completion_by_client(order_id, CLIENT_TG)

    # Повторная попытка той же транзакции награды должна быть отвергнута.
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        assert await grant_referral_reward_for_completed_order_in_tx(
            db,
            order_id=order_id,
            referred_user_id=1,
            agreed_price=PRICE,
        ) is None
        await db.rollback()

    grants = await fetch_one(
        db_path,
        "SELECT COUNT(*) FROM bonus_grants WHERE user_id = 2",
    )
    rewards = await fetch_one(
        db_path,
        "SELECT COUNT(*) FROM bonus_transactions WHERE user_id = 2 "
        "AND transaction_type = 'referral_reward'",
    )
    assert grants == 1 and rewards == 1
    assert await fetch_one(
        db_path,
        "SELECT bonus_balance FROM users WHERE id = 2",
    ) == 250

    # Следующий заказ приглашённого тоже должен завершаться штатно,
    # без повторной награды и без исключения из reward no-op.
    from database.orders import create_order
    second_id, _ = await create_order(1, "synthetic", "solution", "second order", MASTER_A_TG, direction="Математика")
    await agree_price(db_path, second_id)
    assert (await pay_order_from_balance(second_id, 1))[0] == PAYMENT_RESULT_SUCCESS
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE orders SET status='waiting_client' WHERE id=?", (second_id,))
        await db.commit()
    assert await confirm_order_completion_by_client(second_id, CLIENT_TG)
    assert await fetch_one(db_path, "SELECT COUNT(*) FROM bonus_grants WHERE user_id=2") == 1


async def test_two_concurrent_price_acceptances(shop_db):
    from database.price_offers import accept_price_offer
    path, order_id = shop_db
    offer = await insert_offer(path, order_id, MASTER_A_TG, PRICE, "waiting_client")
    barrier = asyncio.Barrier(2)

    async def accept():
        await barrier.wait()
        return await accept_price_offer(order_id, offer, "client", CLIENT_TG, "waiting_client")

    results = await asyncio.gather(accept(), accept())
    assert sum(bool(row[0]) for row in results) == 1
    assert await fetch_one(path, "SELECT agreed_price FROM orders WHERE id=?", (order_id,)) == PRICE
    assert await fetch_one(path, "SELECT COUNT(*) FROM balance_transactions") == 0


async def test_price_accept_vs_reject(shop_db):
    from database.price_offers import accept_price_offer, reject_price_negotiation
    path, order_id = shop_db
    offer = await insert_offer(path, order_id, MASTER_A_TG, PRICE, "waiting_client")
    barrier = asyncio.Barrier(2)

    async def accept():
        await barrier.wait()
        return (await accept_price_offer(order_id, offer, "client", CLIENT_TG, "waiting_client"))[0]

    async def reject():
        await barrier.wait()
        return await reject_price_negotiation(order_id, offer, "client", CLIENT_TG, "waiting_client")

    accepted, rejected = await asyncio.gather(accept(), reject())
    assert bool(accepted) != bool(rejected)
    price = await fetch_one(path, "SELECT agreed_price FROM orders WHERE id=?", (order_id,))
    master = await fetch_one(path, "SELECT assigned_master_telegram_id FROM orders WHERE id=?", (order_id,))
    if accepted:
        assert price == PRICE and master == MASTER_A_TG
    else:
        assert price is None and master is None
    assert await fetch_one(path, "SELECT COUNT(*) FROM balance_transactions") == 0


# ------------------------------------------------------------------
# Конкурентный бонус: spend после сгорания и восстановление
# ------------------------------------------------------------------


async def test_bonus_spend_after_expiry_fails_cleanly(shop_db):
    db_path, order_id = shop_db
    # Просроченное начисление.
    await agree_price(db_path, order_id)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"""
            INSERT INTO bonus_grants (
                user_id, amount, remaining_amount, source_type,
                source_order_id, granted_at, expires_at
            )
            VALUES (
                1, 500, 500, 'legacy_balance', NULL, {NOW},
                datetime('now', '+3 hours', '-10 days')
            )
            """
        )
        await db.commit()

    result = await pay_order_from_balance(order_id, 1, use_bonus=True)
    # Просроченные бонусы не учитываются: оплата только деньгами.
    assert result[0] == PAYMENT_RESULT_SUCCESS
    consumed = await fetch_one(
        db_path, "SELECT COUNT(*) FROM bonus_usage_allocations"
    )
    assert consumed == 0


async def test_refund_preserves_original_bonus_expiry(shop_db):
    """Детализированное списание возвращается с прежним сроком;
    новые 60 дней разрешены только legacy-fallback без allocations."""
    db_path, order_id = shop_db
    await agree_price(db_path, order_id)

    # Сначала действующее начисление; срок сдвигаем SQL после оплаты.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"""
            INSERT INTO bonus_grants (
                user_id, amount, remaining_amount, source_type,
                source_order_id, granted_at, expires_at
            )
            VALUES (
                1, 150, 150, 'referral_reward', NULL, {NOW},
                datetime('now', '+3 hours', '+60 days')
            )
            """
        )
        await db.commit()

    ok_pay, _balance, _cash = await pay_order_from_balance(
        order_id, 1, use_bonus=True
    )
    assert ok_pay == PAYMENT_RESULT_SUCCESS

    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE bonus_grants SET expires_at=datetime('now', '+3 hours', '-1 day') WHERE user_id=1")
        await db.commit()

    ok_refund, _balance2, _cash2 = await cancel_paid_order_with_refund(
        order_id
    )
    assert ok_refund == REFUND_RESULT_SUCCESS

    # Старое начисление: remaining 0 + bonus-транзакция возврата.
    restored_any = await fetch_one(
        db_path,
        """
        SELECT COUNT(*) FROM bonus_grants
        WHERE user_id = 1 AND source_type = 'legacy_balance'
        """,
    )
    refund_tx = await fetch_one(
        db_path,
        "SELECT COUNT(*) FROM bonus_transactions WHERE user_id = 1 "
        "AND transaction_type = 'refund'",
    )
    use_bonus_val = await fetch_one(
        db_path, "SELECT bonus_used FROM orders WHERE id = ?", (order_id,)
    )
    assert use_bonus_val == 150
    assert restored_any == 0
    assert refund_tx == 1
    assert await fetch_one(db_path, "SELECT bonus_balance FROM users WHERE id=1") == 0
    assert await fetch_one(db_path, "SELECT remaining_amount FROM bonus_grants WHERE user_id=1") == 0
    assert await fetch_one(db_path, "SELECT COUNT(*) FROM bonus_usage_allocations WHERE restored_at IS NOT NULL") == 1
