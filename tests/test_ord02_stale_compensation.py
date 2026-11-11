"""ORD-02: генерация назначения против устаревших компенсаций.

Прецедент B (§36): компенсация сбоя создания Telegram-темы старой
попытки никогда не изменяет более новую генерацию назначения,
включая случай повторного выбора того же исполнителя.
"""


import aiosqlite
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def prepared_db(tmp_path, use_db_path):
    from database.db import init_db
    from database.migrations import run_migrations

    db_path = tmp_path / "ord02.db"
    use_db_path(db_path)
    await init_db()
    await run_migrations()

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
        for master_id, name in ((999001, "Мастер А"), (999002, "Мастер Б")):
            await db.execute(
                """
                INSERT INTO masters (
                    telegram_id, full_name, is_active,
                    created_at, updated_at
                )
                VALUES (?, ?, 1,
                        datetime('now', '+3 hours'),
                        datetime('now', '+3 hours'))
                """,
                (master_id, name),
            )
            await db.execute(
                """
                INSERT INTO master_directions (
                    master_telegram_id, direction, created_at
                )
                VALUES (?, 'Математика', datetime('now', '+3 hours'))
                """,
                (master_id,),
            )
        await db.commit()
    yield db_path


async def create_assigned_order(db_path, master_id: int) -> int:
    from database.orders import create_order

    order_id, _fresh = await create_order(
        user_id=1,
        subject="Задача",
        work_type="solution",
        description="решить",
        assigned_master_telegram_id=master_id,
        direction="Математика",
    )
    return order_id


async def fetch_one(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
    return row[0] if row is not None else None


async def fetch_all(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        rows = [dict(row) for row in await cursor.fetchall()]
        await cursor.close()
    return rows


async def test_late_failure_of_old_attempt_spares_new_assignment(
    prepared_db,
):
    db_path = prepared_db
    from database.orders import (
        assign_waiting_order_to_master,
        get_assignment_generation,
        release_assignment_after_topic_failure,
    )

    order_id = await create_assigned_order(db_path, 999001)
    generation_a = await get_assignment_generation(order_id)
    assert generation_a == 0  # исходное закрепление при создании

    # Реальный путь перевыбора: сначала высвобождение А
    # (например, отказ исполнителя/сбой), затем закрепление Б.
    from database.orders import cancel_new_order_by_master

    released_by_decline = await cancel_new_order_by_master(
        order_id=order_id,
        master_telegram_id=999001,
    )
    assert released_by_decline is True

    assigned = await assign_waiting_order_to_master(
        order_id=order_id,
        client_telegram_id=7770001,
        master_telegram_id=999002,
    )
    assert assigned is True
    generation_b = await get_assignment_generation(order_id)
    assert generation_b > generation_a

    # Поздний сбой Telegram-операции старой попытки (А) должен
    # быть силен только в своей генерации: заказ Б не страдает.
    released = await release_assignment_after_topic_failure(
        order_id=order_id,
        master_telegram_id=999001,
        captured_assignment_generation=generation_a,
    )
    assert released is False

    master_now = await fetch_one(
        db_path,
        "SELECT assigned_master_telegram_id FROM orders WHERE id = ?",
        (order_id,),
    )
    assert master_now == 999002
    assignment_status = await fetch_one(
        db_path,
        "SELECT assignment_status FROM orders WHERE id = ?",
        (order_id,),
    )
    assert assignment_status == "assigned"
    group_chat_id = await fetch_one(
        db_path,
        "SELECT COALESCE(group_chat_id, 0) FROM orders WHERE id = ?",
        (order_id,),
    )
    card_message_id = await fetch_one(
        db_path,
        "SELECT COALESCE(order_card_message_id, 0) FROM orders WHERE id = ?",
        (order_id,),
    )
    assert group_chat_id == 0
    assert card_message_id == 0


async def test_compensation_releases_exactly_current_generation(
    prepared_db,
):
    db_path = prepared_db
    from database.orders import (
        get_assignment_generation,
        release_assignment_after_topic_failure,
    )

    order_id = await create_assigned_order(db_path, 999001)
    generation = await get_assignment_generation(order_id)

    released = await release_assignment_after_topic_failure(
        order_id=order_id,
        master_telegram_id=999001,
        captured_assignment_generation=generation,
    )
    assert released is True

    row = (await fetch_all(
        db_path,
        """
        SELECT assigned_master_telegram_id, assignment_status,
               assignment_generation
        FROM orders WHERE id = ?
        """,
        (order_id,),
    ))[0]
    assert row["assigned_master_telegram_id"] is None
    assert row["assignment_status"] == "waiting_selection"
    # Генерация выросла после высвобождения.
    assert int(row["assignment_generation"]) > generation

    # Повтор той же компенсации больше не действует (иная генерация).
    released_again = await release_assignment_after_topic_failure(
        order_id=order_id,
        master_telegram_id=999001,
        captured_assignment_generation=generation,
    )
    assert released_again is False


async def test_same_master_reselect_is_protected_aba(prepared_db):
    """Сценарий А-отказ-выбор Б-снова А: поздний сбой первой попытки А
    не выбивает повтора А."""
    db_path = prepared_db
    from database.orders import (
        assign_waiting_order_to_master,
        get_assignment_generation,
        release_assignment_after_topic_failure,
    )

    order_id = await create_assigned_order(db_path, 999001)
    gen_a1 = await get_assignment_generation(order_id)

    released = await release_assignment_after_topic_failure(
        order_id=order_id,
        master_telegram_id=999001,
        captured_assignment_generation=gen_a1,
    )
    assert released is True

    # Клиент снова выбирает того же исполнителя А.
    assigned = await assign_waiting_order_to_master(
        order_id=order_id,
        client_telegram_id=7770001,
        master_telegram_id=999001,
    )
    assert assigned is True
    gen_a2 = await get_assignment_generation(order_id)
    assert gen_a2 > gen_a1

    # Финальная защита: сбой попытки 1 после повторного закрепления.
    released_stale = await release_assignment_after_topic_failure(
        order_id=order_id,
        master_telegram_id=999001,
        captured_assignment_generation=gen_a1,
    )
    assert released_stale is False
    master_now = await fetch_one(
        db_path,
        "SELECT assigned_master_telegram_id FROM orders WHERE id = ?",
        (order_id,),
    )
    assert master_now == 999001


async def test_compensation_requires_captured_generation(prepared_db):
    from database.orders import release_assignment_after_topic_failure

    with pytest.raises(ValueError):
        await release_assignment_after_topic_failure(
            order_id=1,
            master_telegram_id=999001,
            captured_assignment_generation=None,
        )


async def test_open_order_compensation_respects_generation(prepared_db):
    """Свободный заказ: компенсация сбоя темы действует только пока
    режим open остаётся той же генерации (никто не забрал заказ)."""
    db_path = prepared_db
    from database.orders import (
        claim_open_order,
        create_order,
        get_assignment_generation,
        release_assignment_after_topic_failure,
    )

    order_id, _fresh = await create_order(
        user_id=1,
        subject="Задача",
        work_type="solution",
        description="решить",
        direction="Математика",
        assignment_status="open",
    )
    generation_open = await get_assignment_generation(order_id)
    assert generation_open == 0

    released = await release_assignment_after_topic_failure(
        order_id=order_id,
        master_telegram_id=None,
        captured_assignment_generation=generation_open,
    )
    assert released is True

    status = await fetch_one(
        db_path,
        "SELECT assignment_status FROM orders WHERE id = ?",
        (order_id,),
    )
    assert status == "open"

    # Исполнитель забирает свободный заказ после сбоя: gen растёт.
    claimed = await claim_open_order(order_id, 999001)
    assert claimed is True
    generation_claimed = await get_assignment_generation(order_id)
    assert generation_claimed > generation_open

    # Поздняя компенсация клиентской попытки не должна вышибать B.
    released_stale = await release_assignment_after_topic_failure(
        order_id=order_id,
        master_telegram_id=None,
        captured_assignment_generation=generation_open,
    )
    assert released_stale is False
    master_now = await fetch_one(
        db_path,
        "SELECT assigned_master_telegram_id FROM orders WHERE id = ?",
        (order_id,),
    )
    assert master_now == 999001
