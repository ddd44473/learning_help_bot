"""DB-01: перенос бонусных таблиц исторической (legacy) схемы.

Историческая схема взята из реального коммита 23ee42f ("referral
system ready", старая ветка main) — только DDL-определения, без
каких-либо продуктивных данных.
"""

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from database.migrations import (
    BONUS_SCHEMA_MIGRATION,
    LegacyBonusSchemaError,
    run_migrations,
)

# ------------------------------------------------------------------
# DDL исторической схемы (Git: 23ee42f, старая ветка main)
# ------------------------------------------------------------------

LEGACY_USERS_DDL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    tg_username TEXT,
    tg_full_name TEXT,
    bot_nickname TEXT,
    bot_nickname_normalized TEXT UNIQUE,
    birthday TEXT,
    level INTEGER NOT NULL DEFAULT 1 CHECK (level >= 1),
    coins INTEGER NOT NULL DEFAULT 0 CHECK (coins >= 0),
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    bonus_balance INTEGER NOT NULL DEFAULT 0 CHECK (bonus_balance >= 0),
    referral_code TEXT,
    is_registered INTEGER NOT NULL DEFAULT 0
        CHECK (is_registered IN (0, 1)),
    rules_accepted INTEGER NOT NULL DEFAULT 0
        CHECK (rules_accepted IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    last_active_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours'))
);
"""

LEGACY_ORDERS_DDL = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    direction TEXT,
    subject TEXT NOT NULL,
    work_type TEXT NOT NULL,
    description TEXT NOT NULL,
    deadline TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""

# Точное историческое определение bonus_grants (23ee42f).
LEGACY_BONUS_GRANTS_DDL = """
CREATE TABLE bonus_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    original_amount INTEGER NOT NULL CHECK (original_amount > 0),
    remaining_amount INTEGER NOT NULL CHECK (remaining_amount >= 0),
    expired_amount INTEGER NOT NULL DEFAULT 0 CHECK (expired_amount >= 0),
    grant_type TEXT NOT NULL
        CHECK (grant_type IN (
            'referral_reward', 'legacy_migration', 'legacy_refund'
        )),
    source_order_id INTEGER,
    related_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    expires_at TEXT NOT NULL,
    reminder_sent_at TEXT,
    expired_processed_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,
    FOREIGN KEY (source_order_id) REFERENCES orders(id) ON DELETE SET NULL,
    FOREIGN KEY (related_user_id) REFERENCES users(id) ON DELETE SET NULL
);
"""

# Точное историческое определение bonus_usage_allocations (23ee42f).
LEGACY_BONUS_ALLOCATIONS_DDL = """
CREATE TABLE bonus_usage_allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    grant_id INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
    UNIQUE(order_id, grant_id),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (grant_id) REFERENCES bonus_grants(id) ON DELETE RESTRICT
);
"""

NOW_MOSCOW = "datetime('now', '+3 hours')"


async def test_actual_startup_order_init_then_migrations(tmp_path, use_db_path):
    from database.db import init_db
    path = tmp_path / "startup_legacy.db"
    use_db_path(path)
    await build_legacy_database(path)
    await populate_legacy_sample(path)
    await init_db()
    await run_migrations()
    await init_db()
    await run_migrations()
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute("PRAGMA integrity_check")).fetchone())[0] == "ok"
        assert await (await db.execute("PRAGMA foreign_key_check")).fetchall() == []
        assert (await (await db.execute("SELECT SUM(remaining_amount) FROM bonus_grants")).fetchone())[0] == 460


async def test_partial_current_schema_rejected_before_startup_writes(tmp_path, use_db_path):
    from database.db import init_db
    path = tmp_path / "partial_current.db"
    use_db_path(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE bonus_grants(user_id INTEGER, amount INTEGER, source_type TEXT, granted_at TEXT)")
        await db.commit()
    with pytest.raises(LegacyBonusSchemaError):
        await init_db()
    async with aiosqlite.connect(path) as db:
        tables = await (await db.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
        assert tables == [("bonus_grants",)]


async def test_partial_allocations_with_legacy_unique_fail_closed(tmp_path, use_db_path):
    from database.db import init_db
    path = tmp_path / "partial_allocations.db"
    use_db_path(path)
    await build_legacy_database(path)
    async with aiosqlite.connect(path) as db:
        await db.execute("ALTER TABLE bonus_usage_allocations ADD COLUMN restored_at TEXT")
        await db.commit()
    with pytest.raises(LegacyBonusSchemaError, match="legacy UNIQUE"):
        await init_db()
    async with aiosqlite.connect(path) as db:
        columns = await (await db.execute("PRAGMA table_info(bonus_grants)")).fetchall()
        assert "original_amount" in {row[1] for row in columns}
        assert not await (await db.execute("SELECT 1 FROM sqlite_master WHERE name='app_migrations'")).fetchall()


async def build_legacy_database(
    db_path: Path,
) -> None:
    """Создаёт одноразовую базу исторической схемы (синтетику)."""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()

    db = await aiosqlite.connect(db_path)
    await db.executescript(LEGACY_USERS_DDL)
    await db.executescript(LEGACY_ORDERS_DDL)
    await db.executescript(LEGACY_BONUS_GRANTS_DDL)
    await db.executescript(LEGACY_BONUS_ALLOCATIONS_DDL)
    await db.commit()
    await db.close()


async def populate_legacy_sample(db_path: Path) -> None:
    """Показательное историческое состояние:
    - user 1 (пригласивший): 250 бонусов за первую работу клиента 2,
      полностью потраченные; 500 legacy-баланса (400 активных,
      100 сгорело); возврат 60; отдельное начисление 50, из которых
      50 сгорело;
    - user 2: реферальный заказ 10.
    """
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA foreign_keys = ON")

    await db.execute(
        f"""
        INSERT INTO users (id, telegram_id, tg_username, balance,
                           bonus_balance, is_registered,
                           created_at, updated_at, last_active_at)
        VALUES (1, 7770001, 'legacy_client', 1500, 460, 1,
                {NOW_MOSCOW}, {NOW_MOSCOW}, {NOW_MOSCOW})
        """
    )
    await db.execute(
        f"""
        INSERT INTO users (id, telegram_id, tg_username, balance,
                           bonus_balance, is_registered,
                           created_at, updated_at, last_active_at)
        VALUES (2, 7770002, 'invited_client', 300, 0, 1,
                {NOW_MOSCOW}, {NOW_MOSCOW}, {NOW_MOSCOW})
        """
    )
    await db.execute(
        f"""
        INSERT INTO orders (id, user_id, direction, subject, work_type,
                            description, created_at, updated_at)
        VALUES (10, 2, 'Математика', 'Контрольная', 'solution', 'описание',
                {NOW_MOSCOW}, {NOW_MOSCOW})
        """
    )

    # Награда приглашившему: 250, полностью потрачено заказом 10.
    await db.execute(
        """
        INSERT INTO bonus_grants (
            id, user_id, original_amount, remaining_amount, expired_amount,
            grant_type, source_order_id, related_user_id, created_at,
            expires_at
        )
        VALUES (100, 1, 250, 250, 0, 'referral_reward', 10, 2,
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours', '+30 days'))
        """
    )
    await db.execute(
        """
        INSERT INTO bonus_usage_allocations (
            id, order_id, grant_id, amount, created_at
        )
        VALUES (200, 10, 100, 250,
                datetime('now', '+3 hours'))
        """
    )
    await db.execute(
        "UPDATE bonus_grants SET remaining_amount = 0 WHERE id = 100"
    )

    # Накопленный старый баланс: 500 всего, 400 активных, 100 сгорело.
    await db.execute(
        """
        INSERT INTO bonus_grants (
            id, user_id, original_amount, remaining_amount, expired_amount,
            grant_type, source_order_id, related_user_id, created_at,
            expires_at
        )
        VALUES (101, 1, 500, 400, 100, 'legacy_migration', NULL, NULL,
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours', '+45 days'))
        """
    )

    # Возвратное начисление старого бота, 60 активных бонусов.
    await db.execute(
        """
        INSERT INTO bonus_grants (
            id, user_id, original_amount, remaining_amount, expired_amount,
            grant_type, source_order_id, related_user_id, created_at,
            expires_at
        )
        VALUES (102, 1, 60, 60, 0, 'legacy_refund', 10, NULL,
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours', '+60 days'))
        """
    )

    # Полностью сгоревшее начисление: 50 из 50.
    await db.execute(
        """
        INSERT INTO bonus_grants (
            id, user_id, original_amount, remaining_amount, expired_amount,
            grant_type, source_order_id, related_user_id, created_at,
            expires_at, expired_processed_at
        )
        VALUES (103, 1, 50, 0, 50, 'legacy_migration', NULL, NULL,
                datetime('now', '+3 hours'),
                datetime('now', "+3 hours", "-1 day"),
                datetime('now', '+3 hours'))
        """
    )

    await db.commit()
    await db.close()


def grant_columns(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            row[1] for row in conn.execute("PRAGMA table_info(bonus_grants)")
        ]
    finally:
        conn.close()


CURRENT_GRANT_COLUMNS = {
    "id",
    "user_id",
    "amount",
    "remaining_amount",
    "source_type",
    "source_order_id",
    "related_user_id",
    "granted_at",
    "expires_at",
    "reminder_sent_at",
    "expired_at",
}


def grant_rows(db_path: Path) -> dict[int, dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            int(row["id"]): {
                "user_id": int(row["user_id"]),
                "amount": int(row["amount"]),
                "remaining": int(row["remaining_amount"]),
                "source_type": str(row["source_type"]),
                "source_order_id": row["source_order_id"],
                "related_user_id": row["related_user_id"],
                "granted_at": row["granted_at"],
                "expires_at": row["expires_at"],
                "expired_at": row["expired_at"],
            }
            for row in conn.execute(
                "SELECT * FROM bonus_grants ORDER BY id"
            )
        }
    finally:
        conn.close()


# ------------------------------------------------------------------
# Полный перенос populated legacy-базы
# ------------------------------------------------------------------


async def test_legacy_populated_migration_preserves_value(
    tmp_path, use_db_path
):
    db_path = tmp_path / "legacy_populated.db"
    await build_legacy_database(db_path)
    await populate_legacy_sample(db_path)
    use_db_path(db_path)

    await run_migrations()

    assert CURRENT_GRANT_COLUMNS <= set(grant_columns(db_path))
    rows = grant_rows(db_path)

    # Строки не потеряны и не созданы заново: идентификаторы сохранены.
    assert sorted(rows) == [100, 101, 102, 103]

    reward = rows[100]
    assert reward["amount"] == 250
    assert reward["remaining"] == 0
    assert reward["source_type"] == "referral_reward"
    assert reward["source_order_id"] == 10
    assert reward["related_user_id"] == 2
    # 250 потрачены, сгорания не было: expired_at пуст.
    assert reward["expired_at"] is None

    legacy_balance = rows[101]
    assert legacy_balance["amount"] == 500
    assert legacy_balance["remaining"] == 400
    assert legacy_balance["source_type"] == "legacy_balance"
    assert legacy_balance["source_order_id"] is None
    assert legacy_balance["related_user_id"] is None
    assert legacy_balance["expires_at"] is not None

    refund = rows[102]
    assert refund["amount"] == 60
    assert refund["remaining"] == 60
    assert refund["source_type"] == "legacy_balance"
    assert refund["source_order_id"] == 10

    expired = rows[103]
    assert expired["amount"] == 50
    assert expired["remaining"] == 0
    assert expired["expired_at"] is not None

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM bonus_usage_allocations")
        allocations = await cursor.fetchall()
        await cursor.close()
        assert len(allocations) == 1
        assert int(allocations[0]["grant_id"]) == 100
        assert int(allocations[0]["amount"]) == 250
        # Восстановлено не было: NULL без изобретения данных.
        assert allocations[0]["restored_at"] is None

        cursor = await db.execute(
            "SELECT bonus_balance FROM users WHERE id = 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        # Синхронизация кеша: 400 активных + 60 возвратных, остальное 0.
        assert int(row["bonus_balance"]) == 460

        cursor = await db.execute("PRAGMA integrity_check")
        (integrity,) = await cursor.fetchone()
        await cursor.close()
        assert integrity == "ok"

        cursor = await db.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
        await cursor.close()

        cursor = await db.execute(
            "SELECT 1 FROM app_migrations WHERE name = ?",
            (BONUS_SCHEMA_MIGRATION,),
        )
        assert await cursor.fetchone() is not None
        await cursor.close()

        # Историческое сгорание сохранено в audit-таблице.
        cursor = await db.execute(
            """
            SELECT legacy_row_pk, legacy_values
            FROM app_migrations_legacy_audit
            WHERE migration_name = ? AND table_name = 'bonus_grants'
            """,
            (BONUS_SCHEMA_MIGRATION,),
        )
        audit = {int(r[0]): r[1] for r in await cursor.fetchall()}
        await cursor.close()
        assert 103 in audit and "expired_amount" in audit[103]


async def test_repeated_migration_is_noop(tmp_path, use_db_path):
    db_path = tmp_path / "legacy_repeat.db"
    await build_legacy_database(db_path)
    await populate_legacy_sample(db_path)
    use_db_path(db_path)

    await run_migrations()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM bonus_grants"
        )
        first_count, first_sum = map(int, await cursor.fetchone())
        await cursor.close()
    await run_migrations()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM bonus_grants"
        )
        second_count, second_sum = map(int, await cursor.fetchone())
        await cursor.close()

    assert (first_count, first_sum) == (second_count, second_sum)
    assert first_count == 4
    assert first_sum == 860  # 250 + 500 + 60 + 50


# ------------------------------------------------------------------
# Прерванная миграция: откат целиком и успешный перезапуск
# ------------------------------------------------------------------


async def test_interrupted_migration_rolls_back_fully(
    tmp_path, use_db_path, monkeypatch
):
    db_path = tmp_path / "legacy_interrupted.db"
    await build_legacy_database(db_path)
    await populate_legacy_sample(db_path)
    use_db_path(db_path)

    import database.migrations as migrations_module

    original_rebuild = migrations_module._rebuild_legacy_bonus_allocations

    async def failing_rebuild(*args, **kwargs):
        raise RuntimeError("симуляция прерывания при перестройке")

    migrations_module._rebuild_legacy_bonus_allocations = failing_rebuild
    with pytest.raises(RuntimeError):
        await run_migrations()
    # Восстанавливаем вручную: monkeypatch.undo() отменил бы и
    # перенаправление DB_PATH из фикстуры use_db_path.
    migrations_module._rebuild_legacy_bonus_allocations = original_rebuild

    # База осталась в историческом состоянии (ничего не потеряно).
    assert "original_amount" in grant_columns(db_path)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM bonus_grants")
        assert int((await cursor.fetchone())[0]) == 4
        await cursor.close()

    # Перезапуск без сбоя доводит миграцию до конца.
    await run_migrations()

    assert CURRENT_GRANT_COLUMNS <= set(grant_columns(db_path))
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM bonus_grants")
        assert int((await cursor.fetchone())[0]) == 4
        await cursor.close()


# ------------------------------------------------------------------
# Неопознанная схема: fail closed, данные не изменены
# ------------------------------------------------------------------


async def test_unknown_schema_fails_without_changes(tmp_path, use_db_path):
    db_path = tmp_path / "legacy_unknown.db"
    await build_legacy_database(db_path)
    use_db_path(db_path)

    db = await aiosqlite.connect(db_path)
    await db.execute("ALTER TABLE bonus_grants ADD COLUMN strange_col TEXT")
    await db.commit()
    await db.close()

    with pytest.raises(LegacyBonusSchemaError):
        await run_migrations()

    # Схема и пустые данные не тронуты.
    assert "original_amount" in grant_columns(db_path)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM bonus_grants")
        assert int((await cursor.fetchone())[0]) == 0
        await cursor.close()


# ------------------------------------------------------------------
# Пустая legacy-база: перенос без побочных эффектов
# ------------------------------------------------------------------


async def test_empty_legacy_migration(tmp_path, use_db_path):
    db_path = tmp_path / "legacy_empty.db"
    await build_legacy_database(db_path)
    use_db_path(db_path)

    await run_migrations()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM bonus_grants")
        assert int((await cursor.fetchone())[0]) == 0
        await cursor.close()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM bonus_usage_allocations"
        )
        assert int((await cursor.fetchone())[0]) == 0
        await cursor.close()
        cursor = await db.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []
        await cursor.close()
