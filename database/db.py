import os
from pathlib import Path

import aiosqlite

from database.models import (
    CREATE_ORDER_FILES_INDEXES,
    CREATE_ORDER_FILES_TABLE,
    CREATE_ORDERS_TABLE,
    CREATE_USERS_TABLE,
    CREATE_REVIEWS_INDEXES,
    CREATE_REVIEWS_TABLE,
    CREATE_MASTERS_TABLE,
    CREATE_MASTERS_INDEXES,
    CREATE_BALANCE_TRANSACTIONS_TABLE,
    CREATE_BALANCE_TRANSACTIONS_INDEXES,
    CREATE_ORDER_PRICE_OFFERS_INDEXES,
    CREATE_ORDER_PRICE_OFFERS_TABLE,
    CREATE_MASTER_RESPONSE_TIMES_INDEXES,
    CREATE_MASTER_RESPONSE_TIMES_TABLE,
    CREATE_USER_ACTIVITY_LOGS_INDEXES,
    CREATE_USER_ACTIVITY_LOGS_TABLE,
    CREATE_ORDER_MASTER_ATTEMPTS_TABLE,
    CREATE_ORDER_MASTER_ATTEMPTS_INDEXES,
    CREATE_MASTER_DIRECTIONS_TABLE,
    CREATE_MASTER_DIRECTIONS_INDEXES,
    CREATE_REFERRALS_TABLE,
    CREATE_REFERRALS_INDEXES,
    CREATE_BONUS_TRANSACTIONS_TABLE,
    CREATE_BONUS_TRANSACTIONS_INDEXES,
    CREATE_BONUS_GRANTS_TABLE,
    CREATE_BONUS_GRANTS_INDEXES,
    CREATE_BONUS_USAGE_ALLOCATIONS_TABLE,
    CREATE_BONUS_USAGE_ALLOCATIONS_INDEXES,
    CREATE_OPERATION_LOG_TABLE,
    CREATE_ORDER_TOPIC_CHECKPOINTS_TABLE,

)


BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_DIR = BASE_DIR / "database"

# Минимальный явный seam для изолированного тестирования:
# путь базы данных можно переопределить переменной окружения до
# первого импорта database.*, а рабочее поведение бота (без переменной)
# полностью не меняется. Проверяется только перед использованием:
# переменная со значением "" игнорируется.
_DB_PATH_ENV = "STUDY_BOT_DB_PATH"
_env_db_path = os.environ.get(_DB_PATH_ENV, "").strip()
DB_PATH = Path(_env_db_path) if _env_db_path else DATABASE_DIR / "database.db"


async def get_db() -> aiosqlite.Connection:
    DATABASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA foreign_keys = ON")

    return db


async def init_db() -> None:
    """
    Создаёт актуальную структуру базы данных.

    Эта функция отвечает за создание таблиц
    и безопасных индексов.

    Индексы таблицы orders здесь намеренно
    не создаются, потому что существующая база
    может ещё не содержать новые колонки,
    например assignment_status.

    Обновлением существующей базы и созданием
    актуальных индексов orders занимается
    database.migrations.
    """

    DATABASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Совместимость нужна до CREATE INDEX по новым бонусным колонкам.
    from database.migrations import migrate_legacy_bonus_schema_precheck_and_apply, table_exists

    async with aiosqlite.connect(DB_PATH, isolation_level=None) as compatibility_db:
        if await table_exists(compatibility_db, "bonus_grants") or await table_exists(compatibility_db, "bonus_usage_allocations"):
            await migrate_legacy_bonus_schema_precheck_and_apply(compatibility_db)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        # -------------------------------------------------
        # Основные сущности
        # -------------------------------------------------

        await db.execute(
            CREATE_USERS_TABLE
        )

        await db.execute(
            CREATE_MASTERS_TABLE
        )

        await db.execute(
            CREATE_MASTER_DIRECTIONS_TABLE
        )

        await db.execute(
            CREATE_ORDERS_TABLE
        )

        # -------------------------------------------------
        # Зависимые сущности
        # -------------------------------------------------

        await db.execute(
            CREATE_MASTER_RESPONSE_TIMES_TABLE
        )

        await db.execute(
            CREATE_ORDER_FILES_TABLE
        )

        await db.execute(
            CREATE_REVIEWS_TABLE
        )

        await db.execute(
            CREATE_BALANCE_TRANSACTIONS_TABLE
        )

        await db.execute(
            CREATE_ORDER_PRICE_OFFERS_TABLE
        )

        await db.execute(
            CREATE_USER_ACTIVITY_LOGS_TABLE
        )

        await db.execute(
            CREATE_ORDER_MASTER_ATTEMPTS_TABLE
        )

        await db.execute(
            CREATE_REFERRALS_TABLE
        )

        await db.execute(
            CREATE_BONUS_TRANSACTIONS_TABLE
        )

        await db.execute(
            CREATE_BONUS_GRANTS_TABLE
        )

        await db.execute(
            CREATE_BONUS_USAGE_ALLOCATIONS_TABLE
        )

        await db.execute(
            CREATE_OPERATION_LOG_TABLE
        )

        await db.execute(
            CREATE_ORDER_TOPIC_CHECKPOINTS_TABLE
        )

        # -------------------------------------------------
        # Индексы
        # -------------------------------------------------
        #
        # CREATE_ORDERS_INDEXES здесь специально
        # НЕ выполняется.
        #
        # Для старой базы новые колонки orders
        # сначала должны быть добавлены миграциями.
        # После этого run_migrations() безопасно
        # создаст актуальные индексы orders.
        # -------------------------------------------------

        await db.executescript(
            CREATE_ORDER_FILES_INDEXES
        )

        await db.executescript(
            CREATE_REVIEWS_INDEXES
        )

        await db.executescript(
            CREATE_MASTERS_INDEXES
        )

        await db.executescript(
            CREATE_MASTER_DIRECTIONS_INDEXES
        )

        await db.executescript(
            CREATE_BALANCE_TRANSACTIONS_INDEXES
        )

        await db.executescript(
            CREATE_ORDER_PRICE_OFFERS_INDEXES
        )

        await db.executescript(
            CREATE_MASTER_RESPONSE_TIMES_INDEXES
        )

        await db.executescript(
            CREATE_USER_ACTIVITY_LOGS_INDEXES
        )

        await db.executescript(
            CREATE_ORDER_MASTER_ATTEMPTS_INDEXES
        )

        await db.executescript(
            CREATE_REFERRALS_INDEXES
        )

        await db.executescript(
            CREATE_BONUS_TRANSACTIONS_INDEXES
        )

        await db.executescript(
            CREATE_BONUS_GRANTS_INDEXES
        )

        await db.executescript(
            CREATE_BONUS_USAGE_ALLOCATIONS_INDEXES
        )

        await db.commit()

    from database.access_control import init_access_control
    await init_access_control()
