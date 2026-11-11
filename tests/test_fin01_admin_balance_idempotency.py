"""FIN-01: durable-идемпотентность административных финансовых операций.

Прецедент A (§36 build_full.md): одно и то же Telegram-событие,
обработанное дважды (последовательно, конкурентно или после
«перезапуска»), двигает деньги ровно один раз.
"""

import asyncio
import hashlib

import aiosqlite
import pytest
import pytest_asyncio

from database.balance import (
    change_user_balance,
    change_user_balance_idempotent,
)


@pytest_asyncio.fixture
async def prepared_db(tmp_path, use_db_path):
    """Чистая схема на отдельном тестовом файле + перенаправление DB_PATH."""
    from database.db import init_db
    from database.migrations import run_migrations

    db_path = tmp_path / "fin01.db"
    use_db_path(db_path)
    await init_db()
    await run_migrations()
    yield db_path


async def seed_user(db_path, *, user_id=1, telegram_id=777, balance=1000):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO users (
                id, telegram_id, tg_username, balance, is_registered,
                created_at, updated_at, last_active_at
            )
            VALUES (
                ?, ?, 'client', ?, 1,
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours')
            )
            """,
            (user_id, telegram_id, balance),
        )
        await db.commit()


async def fetch(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def fetch_many(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(row) for row in rows]


async def count_balance_rows(db_path, user_id=1):
    return await fetch_many(
        db_path,
        "SELECT * FROM balance_transactions WHERE user_id = ? ORDER BY id",
        (user_id,),
    )


def op_key_for(chat_id: int, message_id: int, action: str) -> str:
    return f"admin_balance:{action}:{chat_id}:{message_id}"


def content_hash_for(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


async def test_same_event_twice_moves_money_once(prepared_db, use_db_path):
    db_path = prepared_db
    await seed_user(db_path)

    op_key = op_key_for(100, 500, "give")
    content_hash = content_hash_for("/give_money @client 300")

    ok_first, balance_first, duplicate_first = (
        await change_user_balance_idempotent(
            op_key,
            content_hash,
            user_id=1,
            amount=300,
            transaction_type="admin_deposit",
            admin_telegram_id=42,
        )
    )
    assert (ok_first, balance_first, duplicate_first) == (True, 1300, False)

    ok_second, balance_second, duplicate_second = (
        await change_user_balance_idempotent(
            op_key,
            content_hash,
            user_id=1,
            amount=300,
            transaction_type="admin_deposit",
            admin_telegram_id=42,
        )
    )
    assert (ok_second, balance_second, duplicate_second) == (
        True,
        1300,
        True,
    )

    transactions = await count_balance_rows(db_path)
    # Ровно одна запись о движении средств.
    assert len(transactions) == 1
    assert int(transactions[0]["amount"]) == 300

    users = await fetch(db_path, "SELECT balance FROM users WHERE id = 1")
    assert int(users[0]["balance"]) == 1300


async def test_same_event_twice_concurrently(prepared_db, use_db_path):
    db_path = prepared_db
    await seed_user(db_path)

    op_key = op_key_for(100, 600, "give")
    content_hash = content_hash_for("/give_money @client 250")

    async def make_call():
        return await change_user_balance_idempotent(
            op_key,
            content_hash,
            user_id=1,
            amount=250,
            transaction_type="admin_deposit",
            admin_telegram_id=42,
        )

    task_a = asyncio.create_task(make_call())
    task_b = asyncio.create_task(make_call())
    results = await asyncio.gather(task_a, task_b)

    applied = sum(1 for ok, _, _ in results if ok)
    duplicates = sum(1 for _, _, dup in results if dup)
    assert applied == 2  # один реальный + один повтор с сохранённым итогом
    assert duplicates == 1
    balances = {balance for _, balance, _ in results if balance is not None}
    assert balances == {1250}

    transactions = await count_balance_rows(db_path)
    assert len(transactions) == 1
    assert int(transactions[0]["amount"]) == 250


async def test_distinct_events_same_amount_both_apply(prepared_db):
    db_path = prepared_db
    await seed_user(db_path)

    ok1, _, _ = await change_user_balance_idempotent(
        op_key_for(100, 700, "give"),
        content_hash_for("/give_money @client 100"),
        user_id=1,
        amount=100,
        transaction_type="admin_deposit",
        admin_telegram_id=42,
    )
    ok2, _, _ = await change_user_balance_idempotent(
        op_key_for(100, 701, "give"),
        content_hash_for("/give_money @client 100"),
        user_id=1,
        amount=100,
        transaction_type="admin_deposit",
        admin_telegram_id=42,
    )
    assert (ok1, ok2) == (True, True)

    transactions = await count_balance_rows(db_path)
    assert len(transactions) == 2


async def test_edited_command_rejected_as_new_event(prepared_db):
    db_path = prepared_db
    await seed_user(db_path)

    op_key = op_key_for(100, 800, "give")
    await change_user_balance_idempotent(
        op_key,
        content_hash_for("/give_money @client 100"),
        user_id=1,
        amount=100,
        transaction_type="admin_deposit",
        admin_telegram_id=42,
    )

    # Тот же ключ события, но отредактированное содержимое сообщения:
    # не является повтором того же события — отказ, деньги не двигаются.
    with pytest.raises(RuntimeError):
        await change_user_balance_idempotent(
            op_key,
            content_hash_for("/give_money @client 999"),
            user_id=1,
            amount=999,
            transaction_type="admin_deposit",
            admin_telegram_id=42,
        )

    transactions = await count_balance_rows(db_path)
    assert len(transactions) == 1


async def test_failed_operation_does_not_consume_idempotency(
    prepared_db,
):
    db_path = prepared_db
    await seed_user(db_path, balance=50)

    op_key = op_key_for(100, 900, "withdraw")

    # Недостаточно средств: отрицательный итог запрещён.
    ok_fail, _, dup_fail = await change_user_balance_idempotent(
        op_key,
        content_hash_for("/take_money @client 300"),
        user_id=1,
        amount=-300,
        transaction_type="admin_withdrawal",
        admin_telegram_id=42,
    )
    assert ok_fail is False
    assert dup_fail is False

    transactions = await count_balance_rows(db_path)
    assert transactions == []

    # Повтор «того же события» с иным содержимым: резерв неудачной
    # попытки был откачен вместе с ней, поэтому ключ свободен.
    ok_retry, balance_retry, duplicate_retry = (
        await change_user_balance_idempotent(
            op_key,
            content_hash_for("/take_money @client 40"),
            user_id=1,
            amount=-40,
            transaction_type="admin_withdrawal",
            admin_telegram_id=42,
        )
    )
    assert (ok_retry, duplicate_retry) == (True, False)
    assert balance_retry == 10


async def test_replay_after_simulated_restart(prepared_db, use_db_path):
    """После «перезапуска» (все соединения переоткрыты) повтор того же
    события возвращает сохранённый результат: деньги не движутся."""
    db_path = prepared_db
    await seed_user(db_path)

    op_key = op_key_for(100, 1000, "give")
    content_hash = content_hash_for("/give_money @client 300")

    await change_user_balance_idempotent(
        op_key,
        content_hash,
        user_id=1,
        amount=300,
        transaction_type="admin_deposit",
        admin_telegram_id=42,
    )
    # На всякий случай подтверждаем, что use_db_path применён
    # (перезапуск симулируется новыми соединениями на каждый вызов).
    assert use_db_path is not None

    ok_replay, balance_replay, dup_replay = (
        await change_user_balance_idempotent(
            op_key,
            content_hash,
            user_id=1,
            amount=300,
            transaction_type="admin_deposit",
            admin_telegram_id=42,
        )
    )
    assert (ok_replay, balance_replay, dup_replay) == (True, 1300, True)
    assert len(await count_balance_rows(db_path)) == 1


async def test_non_idempotent_path_unchanged(prepared_db):
    db_path = prepared_db
    await seed_user(db_path)

    ok1, b1 = await change_user_balance(
        1, 50, "admin_deposit", admin_telegram_id=42
    )
    ok2, b2 = await change_user_balance(
        1, 50, "admin_deposit", admin_telegram_id=42
    )
    assert (ok1, b1) == (True, 1050)
    assert (ok2, b2) == (True, 1100)
    assert len(await count_balance_rows(db_path)) == 2


async def test_ledger_invariant_balance_chain(prepared_db):
    """Инвариант §34.2: balance_after == balance_before + amount."""
    db_path = prepared_db
    await seed_user(db_path)

    for index, amount in enumerate((300, -50, 10)):
        await change_user_balance_idempotent(
            op_key_for(100, 2000 + index, "mixed"),
            content_hash_for(f"op {index}"),
            user_id=1,
            amount=amount,
            transaction_type=(
                "admin_deposit" if amount > 0 else "admin_withdrawal"
            ),
            admin_telegram_id=42,
        )

    for row in await count_balance_rows(db_path):
        assert int(row["balance_after"]) == (
            int(row["balance_before"]) + int(row["amount"])
        )
        assert int(row["balance_after"]) >= 0
