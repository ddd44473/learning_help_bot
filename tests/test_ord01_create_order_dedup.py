"""ORD-01: dedup подтверждения черновика.

Прецедент B-подобный §36: одно логическое подтверждение черновика
создаёт не более одного заказа; другой черновик с тем же содержимым
остаётся независимым.
"""

import asyncio

import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def prepared_db(tmp_path, use_db_path):
    from database.db import init_db
    from database.migrations import run_migrations

    db_path = tmp_path / "ord01.db"
    use_db_path(db_path)
    await init_db()
    await run_migrations()

    # Синтетические пользователи и исполнители: заказ создаётся
    # в режиме assigned или open.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO users (
                id, telegram_id, tg_username, balance, is_registered,
                created_at, updated_at, last_active_at
            )
            VALUES (
                1, 7770001, 'client', 1000, 1,
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours')
            )
            """
        )
        await db.execute(
            """
            INSERT INTO masters (
                telegram_id, full_name, is_active,
                created_at, updated_at
            )
            VALUES (
                999001, 'Мастер А', 1,
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours')
            )
            """
        )
        await db.execute(
            """
            INSERT INTO master_directions (
                master_telegram_id, direction, created_at
            )
            VALUES (999001, 'Математика', datetime('now', '+3 hours'))
            """
        )
        await db.commit()
    yield db_path


def dedup_key_for(chat_id: int, message_id: int) -> str:
    return f"order_confirm:{chat_id}:{message_id}"


ORDER_KWARGS = dict(
    user_id=1,
    subject="Задача",
    work_type="solution",
    description="решить",
)


async def fetch_many(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def test_same_draft_key_creates_one_order_open(prepared_db, use_db_path):
    db_path = prepared_db
    from database.orders import create_order

    key = dedup_key_for(42, 100)

    order_id_first, fresh_first = await create_order(
        **ORDER_KWARGS,
        direction="Математика",
        assignment_status="open",
        dedup_key=key,
    )
    assert fresh_first is True

    order_id_second, fresh_second = await create_order(
        **ORDER_KWARGS,
        direction="Математика",
        assignment_status="open",
        dedup_key=key,
    )
    assert fresh_second is False
    assert order_id_second == order_id_first

    orders = await fetch_many(db_path, "SELECT id FROM orders")
    assert [row["id"] for row in orders] == [order_id_first]


async def test_same_draft_key_concurrent_creates_one_order(
    prepared_db,
):
    db_path = prepared_db
    from database.orders import create_order

    key = dedup_key_for(42, 101)
    workers = 5
    barrier = asyncio.Barrier(workers)

    async def confirm():
        await barrier.wait()
        return await create_order(
            **ORDER_KWARGS,
            direction="Математика",
            assignment_status="open",
            dedup_key=key,
        )

    results = await asyncio.gather(*(confirm() for _ in range(workers)))
    order_ids = {order_id for order_id, _ in results}
    assert len(order_ids) == 1

    orders = await fetch_many(db_path, "SELECT id FROM orders")
    assert len(orders) == 1
    # Телями попыток создания заказов выполняется ровно одна строка.
    assert orders[0]["id"] in order_ids


async def test_distinct_draft_keys_are_independent(prepared_db):
    db_path = prepared_db
    from database.orders import create_order

    id_a, fresh_a = await create_order(
        **ORDER_KWARGS,
        direction="Математика",
        assignment_status="open",
        dedup_key=dedup_key_for(42, 200),
    )
    # Новой черновик — то же содержимое, другое событие подтверждения.
    id_b, fresh_b = await create_order(
        **ORDER_KWARGS,
        direction="Математика",
        assignment_status="open",
        dedup_key=dedup_key_for(42, 201),
    )
    assert fresh_a and fresh_b
    assert id_a != id_b
    orders = await fetch_many(db_path, "SELECT id FROM orders ORDER BY id")
    assert [row["id"] for row in orders] == [id_a, id_b]


async def test_failure_of_first_attempt_does_not_consume_key(prepared_db):
    db_path = prepared_db
    from database.orders import create_order

    import database.orders as orders_module

    key = dedup_key_for(42, 300)
    original_attach = orders_module.attach_operation_result_in_tx

    async def failing_attach(*args, **kwargs):
        raise RuntimeError("симуляция сбоя после создания заказа")

    orders_module.attach_operation_result_in_tx = failing_attach
    try:
        with pytest.raises(RuntimeError):
            await create_order(
                **ORDER_KWARGS,
                direction="Математика",
                assignment_status="open",
                dedup_key=key,
            )
    finally:
        orders_module.attach_operation_result_in_tx = original_attach

    # Ни заказа, ни резерва не осталось.
    orders = await fetch_many(db_path, "SELECT id FROM orders")
    assert orders == []
    reserves = await fetch_many(
        db_path, "SELECT * FROM operation_log WHERE op_key = ?", (key,)
    )
    assert reserves == []

    # Повтор работает как первая попытка.
    order_id, fresh = await create_order(
        **ORDER_KWARGS,
        direction="Математика",
        assignment_status="open",
        dedup_key=key,
    )
    assert fresh is True
    orders = await fetch_many(db_path, "SELECT id FROM orders")
    assert [row["id"] for row in orders] == [order_id]


async def test_dedup_with_assigned_master(prepared_db):
    db_path = prepared_db
    from database.orders import create_order

    key = dedup_key_for(42, 400)
    first, fresh1 = await create_order(
        **ORDER_KWARGS,
        assigned_master_telegram_id=999001,
        direction="Математика",
        dedup_key=key,
    )
    second, fresh2 = await create_order(
        **ORDER_KWARGS,
        assigned_master_telegram_id=999001,
        direction="Математика",
        dedup_key=key,
    )
    assert fresh1 is True and fresh2 is False
    assert first == second

    attempts = await fetch_many(
        db_path,
        """
        SELECT * FROM order_master_attempts
        WHERE order_id = ?
        """,
        (first,),
    )
    # Уникальность попыток не нарушилась повтором подтверждения.
    assert len(attempts) == 1
    assert int(attempts[0]["master_telegram_id"]) == 999001
