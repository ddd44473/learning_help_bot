import aiosqlite

from catalog.directions import LEGACY_DIRECTION_TITLES

from database.db import DB_PATH
from database.models import (
    CREATE_USER_ACTIVITY_LOGS_INDEXES,
    CREATE_USER_ACTIVITY_LOGS_TABLE,
    CREATE_ORDER_MASTER_ATTEMPTS_TABLE,
    CREATE_ORDER_MASTER_ATTEMPTS_INDEXES,
    CREATE_ORDERS_INDEXES,
    CREATE_MASTER_DIRECTIONS_TABLE,
    CREATE_MASTER_DIRECTIONS_INDEXES,
    CREATE_MASTERS_TABLE,
    CREATE_USERS_INDEXES,
    CREATE_REFERRALS_TABLE,
    CREATE_REFERRALS_INDEXES,
    CREATE_BONUS_TRANSACTIONS_TABLE,
    CREATE_BONUS_TRANSACTIONS_INDEXES,
    CREATE_BONUS_GRANTS_TABLE,
    CREATE_BONUS_GRANTS_INDEXES,
    CREATE_BONUS_USAGE_ALLOCATIONS_TABLE,
    CREATE_BONUS_USAGE_ALLOCATIONS_INDEXES,
    CREATE_OPERATION_LOG_TABLE,
    CREATE_OPERATION_LOG_INDEX,
    CREATE_ORDER_TOPIC_CHECKPOINTS_TABLE,
    CREATE_ARCHIVE_DOWNLOAD_JOBS_TABLE,
    CREATE_ARCHIVE_DOWNLOAD_JOBS_INDEX,
)


MOSCOW_TIME_MIGRATION = "timestamps_to_moscow_v1"
BONUS_GRANTS_MIGRATION = "bonus_grants_v1"
DIRECTION_TITLES_MIGRATION = "direction_titles_2026_10_v1"

TIMESTAMP_COLUMNS = {
    "users": ("created_at", "updated_at", "last_active_at"),
    "orders": ("created_at", "updated_at"),
    "order_files": ("created_at",),
    "reviews": ("created_at",),
    "masters": ("deleted_at", "created_at", "updated_at"),
    "master_directions": ("created_at",),
    "balance_transactions": ("created_at",),
    "order_price_offers": ("created_at",),
    "master_response_times": (
        "client_message_at",
        "master_response_at",
        "created_at",
    ),
    "user_activity_logs": ("created_at",),
    "order_master_attempts": ("created_at", "updated_at"),
    "referrals": ("reward_granted_at", "created_at"),
    "bonus_transactions": ("created_at",),
}


async def migrate_existing_timestamps_to_moscow(
    db: aiosqlite.Connection,
) -> None:
    """
    Один раз переводит исторические UTC timestamp-значения на МСК.

    До этой версии SQLite CURRENT_TIMESTAMP сохранял UTC. После миграции
    приложение хранит timestamp-поля непосредственно по московскому времени.
    Запись в app_migrations делает преобразование идемпотентным.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )

    cursor = await db.execute(
        "SELECT 1 FROM app_migrations WHERE name = ? LIMIT 1",
        (MOSCOW_TIME_MIGRATION,),
    )
    already_applied = await cursor.fetchone()
    await cursor.close()

    if already_applied is not None:
        return

    for table_name, columns in TIMESTAMP_COLUMNS.items():
        if not await table_exists(db, table_name):
            continue

        for column_name in columns:
            if not await column_exists(db, table_name, column_name):
                continue

            await db.execute(
                f"""
                UPDATE {table_name}
                SET {column_name} = datetime({column_name}, '+3 hours')
                WHERE {column_name} IS NOT NULL
                """
            )

    await db.execute(
        """
        INSERT INTO app_migrations (name, applied_at)
        VALUES (?, datetime('now', '+3 hours'))
        """,
        (MOSCOW_TIME_MIGRATION,),
    )


async def migrate_legacy_bonus_balances_to_grants(
    db: aiosqlite.Connection,
) -> None:
    """
    Один раз переносит старый агрегированный bonus_balance в начисления
    с отдельным 60-дневным сроком, чтобы существующие бонусы не пропали.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )

    cursor = await db.execute(
        "SELECT 1 FROM app_migrations WHERE name = ? LIMIT 1",
        (BONUS_GRANTS_MIGRATION,),
    )
    already_applied = await cursor.fetchone()
    await cursor.close()
    if already_applied is not None:
        return

    cursor = await db.execute(
        """
        SELECT
            users.id,
            users.bonus_balance,
            COALESCE(SUM(
                CASE
                    WHEN bonus_grants.remaining_amount > 0
                         AND bonus_grants.expires_at > datetime('now', '+3 hours')
                    THEN bonus_grants.remaining_amount
                    ELSE 0
                END
            ), 0) AS tracked_balance
        FROM users
        LEFT JOIN bonus_grants ON bonus_grants.user_id = users.id
        GROUP BY users.id, users.bonus_balance
        """
    )
    rows = await cursor.fetchall()
    await cursor.close()

    for row in rows:
        user_id = int(row[0])
        legacy_balance = int(row[1] or 0)
        tracked_balance = int(row[2] or 0)
        amount_to_migrate = max(legacy_balance - tracked_balance, 0)

        if amount_to_migrate <= 0:
            continue

        await db.execute(
            """
            INSERT INTO bonus_grants (
                user_id, amount, remaining_amount, source_type,
                granted_at, expires_at
            )
            VALUES (
                ?, ?, ?, 'legacy_balance',
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours', '+60 days')
            )
            """,
            (user_id, amount_to_migrate, amount_to_migrate),
        )

    await db.execute(
        """
        INSERT INTO app_migrations (name, applied_at)
        VALUES (?, datetime('now', '+3 hours'))
        """,
        (BONUS_GRANTS_MIGRATION,),
    )


async def migrate_direction_titles(db: aiosqlite.Connection) -> None:
    """Однократно обновляет старые названия без потери заказов и профилей.

    Привязки исполнителей уникальны по (telegram_id, direction):
    INSERT OR IGNORE исключает конфликт, если у исполнителя уже есть
    новая запись, а старую удаляем только после попытки переноса.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    cursor = await db.execute(
        "SELECT 1 FROM app_migrations WHERE name = ? LIMIT 1",
        (DIRECTION_TITLES_MIGRATION,),
    )
    applied = await cursor.fetchone()
    await cursor.close()
    if applied:
        return

    for old_title, new_title in LEGACY_DIRECTION_TITLES.items():
        await db.execute(
            "UPDATE orders SET direction = ? WHERE direction = ?",
            (new_title, old_title),
        )
        await db.execute(
            """
            INSERT OR IGNORE INTO master_directions (
                master_telegram_id, direction, created_at
            )
            SELECT master_telegram_id, ?, created_at
            FROM master_directions WHERE direction = ?
            """,
            (new_title, old_title),
        )
        await db.execute(
            "DELETE FROM master_directions WHERE direction = ?",
            (old_title,),
        )

    await db.execute(
        """
        INSERT INTO app_migrations (name, applied_at)
        VALUES (?, datetime('now', '+3 hours'))
        """,
        (DIRECTION_TITLES_MIGRATION,),
    )


BONUS_SCHEMA_MIGRATION = "bonus_schema_legacy_to_current_v1"

# Классификация строго по полному набору колонок: лишняя/неизвестная
# колонка в финансовой таблице означает неопознанную схему — миграция
# обязана отказаться продолжать (fail closed), а не угадывать.
_LEGACY_GRANTS_REQUIRED = {
    "id",
    "user_id",
    "original_amount",
    "remaining_amount",
    "expired_amount",
    "grant_type",
    "source_order_id",
    "related_user_id",
    "created_at",
    "expires_at",
    "reminder_sent_at",
    "expired_processed_at",
}
_CURRENT_GRANTS_REQUIRED = {
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

_LEGACY_ALLOCATIONS_REQUIRED = {
    "id",
    "order_id",
    "grant_id",
    "amount",
    "created_at",
}
_CURRENT_ALLOCATIONS_REQUIRED = {
    "order_id",
    "grant_id",
    "amount",
    "created_at",
    "restored_at",
}


class LegacyBonusSchemaError(RuntimeError):
    """
    Неизвестный или противоречивый вид схемы бонусных таблиц.

    Миграция намеренно отказывается угадывать: продолжать работу
    с неопознанной схемой опасно для финансовых данных. База при
    этой ошибке не изменяется (ошибка выбрасывается до записи).
    """


async def _bonus_grants_shape(db) -> str:
    if not await table_exists(db, "bonus_grants"):
        # Таблицы пока нет: она будет создана в актуальном виде
        # самим развёртыванием схемы; переносить нечего.
        return "current"
    columns = {
        row[1]
        for row in (
            await (await db.execute("PRAGMA table_info(bonus_grants)")).fetchall()
        )
    }
    if columns == _CURRENT_GRANTS_REQUIRED:
        return "current"
    if columns == _LEGACY_GRANTS_REQUIRED:
        return "legacy"
    raise LegacyBonusSchemaError(
        "Неопознанная схема bonus_grants: колонки "
        f"{sorted(columns)} не совпадают ни с актуальной, ни с "
        "исторической (legacy) структурой. Продолжение миграции "
        "остановлено; данные не изменены."
    )


async def _bonus_allocations_shape(db) -> str:
    if not await table_exists(db, "bonus_usage_allocations"):
        return "current"
    columns = {
        row[1]
        for row in (
            await (
                await db.execute("PRAGMA table_info(bonus_usage_allocations)")
            ).fetchall()
        )
    }
    if columns == (_CURRENT_ALLOCATIONS_REQUIRED | {"id"}):
        if await _table_has_unique_constraint(db, "bonus_usage_allocations", ("order_id", "grant_id")):
            raise LegacyBonusSchemaError(
                "Частичная схема bonus_usage_allocations: restored_at уже есть, но legacy UNIQUE(order_id, grant_id) ещё действует. Требуется отдельная сверка."
            )
        return "current"
    if columns == _LEGACY_ALLOCATIONS_REQUIRED:
        return "legacy"
    raise LegacyBonusSchemaError(
        "Неопознанная схема bonus_usage_allocations: колонки "
        f"{sorted(columns)} не совпадают ни с актуальной, ни с "
        "исторической (legacy) структурой. Данные не изменены."
    )


async def _table_has_unique_constraint(
    db,
    table_name: str,
    columns: tuple[str, ...],
) -> bool:
    """Проверяет наличие UNIQUE-ограничения ровно на данных колонках."""
    cursor = await db.execute(f"PRAGMA index_list({table_name})")
    indexes = await cursor.fetchall()
    await cursor.close()

    for index in indexes:
        # columns: seq, name, unique, origin, partial; origin 'u' —
        # ограничение UNIQUE объявлено прямо в DDL таблицы.
        if len(index) >= 5 and bool(index[2]):
            idx_name = str(index[1])
            info_cursor = await db.execute(
                f"PRAGMA index_info({idx_name})"
            )
            idx_columns = tuple(str(row[2]) for row in await info_cursor.fetchall())
            await info_cursor.close()
            if idx_columns == columns:
                return True
    return False


async def _rebuild_legacy_bonus_grants(db) -> None:
    """Переносит legacy bonus_grants в актуальную схему без потери истории."""
    import json

    # Новая таблица строится с актуальными DDL-константами: идентичность
    # структуры гарантируется определением в database.models, а не ручным
    # переписыванием колонок.
    migrated_ddl = CREATE_BONUS_GRANTS_TABLE.replace(
        "CREATE TABLE IF NOT EXISTS bonus_grants",
        "CREATE TABLE __bonus_grants_migrated",
    )
    if "IF NOT EXISTS" in migrated_ddl:
        raise RuntimeError("DDL новой таблицы bonus_grants некорректен.")

    await db.execute(migrated_ddl)

    # Маппинг колонок подтверждён по историческому коду:
    # original_amount == amount (целый размер начисления),
    # created_at == granted_at (время начисления),
    # expired_processed_at == expired_at (время сгорания),
    # grant_type: 'legacy_migration' и 'legacy_refund' — исторические
    # типы накопленного баланса и возвратного начисления; в актуальной
    # схеме это один provenance-тип 'legacy_balance'
    # (retention: source_order_id/related_user_id сохраняются).
    await db.execute(
        """
        INSERT INTO __bonus_grants_migrated (
            id, user_id, amount, remaining_amount, source_type,
            source_order_id, related_user_id, granted_at,
            expires_at, reminder_sent_at, expired_at
        )
        SELECT
            id, user_id, original_amount, remaining_amount,
            CASE
                WHEN grant_type = 'referral_reward' THEN 'referral_reward'
                ELSE 'legacy_balance'
            END,
            source_order_id, related_user_id, created_at,
            expires_at, reminder_sent_at, expired_processed_at
        FROM bonus_grants
        ORDER BY id ASC
        """
    )

    cursor = await db.execute(
        "SELECT COUNT(*) FROM __bonus_grants_migrated"
    )
    migrated_count = int((await cursor.fetchone())[0])
    await cursor.close()
    cursor = await db.execute("SELECT COUNT(*) FROM bonus_grants")
    legacy_count = int((await cursor.fetchone())[0])
    await cursor.close()

    if migrated_count != legacy_count:
        raise LegacyBonusSchemaError(
            "Число строк после переноса bonus_grants не совпадает: "
            f"{migrated_count} из {legacy_count}. Транзакция будет "
            "откачена, данные не изменены."
        )

    cursor = await db.execute(
        """
        SELECT
            COALESCE(SUM(original_amount), 0),
            COALESCE(SUM(remaining_amount), 0),
            COALESCE(SUM(expired_amount), 0)
        FROM bonus_grants
        """
    )
    legacy_amount_sum, legacy_remaining_sum, legacy_expired_sum = map(
        int, (await cursor.fetchone())
    )
    await cursor.close()

    cursor = await db.execute(
        """
        SELECT
            COALESCE(SUM(amount), 0),
            COALESCE(SUM(remaining_amount), 0)
        FROM __bonus_grants_migrated
        """
    )
    new_amount_sum, new_remaining_sum = map(
        int, (await cursor.fetchone())
    )
    await cursor.close()

    if legacy_amount_sum != new_amount_sum or (
        legacy_remaining_sum != new_remaining_sum
    ):
        raise LegacyBonusSchemaError(
            "Контрольная сумма бонусов после переноса не совпадает: "
            f"amount {legacy_amount_sum}->{new_amount_sum}, "
            f"remaining {legacy_remaining_sum}->{new_remaining_sum}. "
            "Транзакция будет откачена, данные не изменены."
        )

    # Финансовые инварианты актуальной схемы, которые не проверялись
    # старой версией: remaining <= amount. Нарушение запрещает migration.
    cursor = await db.execute(
        """
        SELECT COUNT(*) FROM __bonus_grants_migrated
        WHERE remaining_amount > amount
        """
    )
    over_remaining = int((await cursor.fetchone())[0])
    await cursor.close()
    if over_remaining > 0:
        raise LegacyBonusSchemaError(
            f"Найдено {over_remaining} начислений с remaining_amount "
            "> original_amount. Перенос остановлен, данные не изменены; "
            "требуется ручная сверка истории бонусов администратором."
        )

    # legacy expired_amount не имеет актуального аналога
    # (сгорание фиксируется фактом remaining=0 + expired_at). Чтобы
    # не терять исторические сведения, суммарное значение сохраняется
    # в audit-таблице миграции по каждому начислению.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_migrations_legacy_audit (
            migration_name TEXT NOT NULL,
            table_name TEXT NOT NULL,
            legacy_row_pk INTEGER NOT NULL,
            legacy_values TEXT NOT NULL,
            migrated_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
            PRIMARY KEY (migration_name, table_name, legacy_row_pk)
        )
        """
    )

    cursor = await db.execute(
        "SELECT id, original_amount, expired_amount, grant_type "
        "FROM bonus_grants ORDER BY id ASC"
    )
    legacy_rows = await cursor.fetchall()
    await cursor.close()

    expected_audit = {}
    for row in legacy_rows:
        legacy_id, original_amount, expired_amount, grant_type = (
            int(row[0]), int(row[1]), int(row[2]), str(row[3]),
        )
        expected_audit[legacy_id] = {
            "original_amount": original_amount,
            "expired_amount": expired_amount,
            "grant_type": grant_type,
        }
        await db.execute(
            """
            INSERT OR IGNORE INTO app_migrations_legacy_audit (
                migration_name, table_name, legacy_row_pk, legacy_values
            )
            VALUES (?, 'bonus_grants', ?, ?)
            """,
            (
                BONUS_SCHEMA_MIGRATION,
                legacy_id,
                json.dumps(
                    {
                        "original_amount": original_amount,
                        "expired_amount": expired_amount,
                        "grant_type": grant_type,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    cursor = await db.execute(
        "SELECT legacy_row_pk, legacy_values FROM app_migrations_legacy_audit WHERE migration_name=? AND table_name='bonus_grants'",
        (BONUS_SCHEMA_MIGRATION,),
    )
    audit_rows = await cursor.fetchall()
    await cursor.close()
    actual_audit = {int(row[0]): json.loads(row[1]) for row in audit_rows}
    if actual_audit != expected_audit or sum(row["expired_amount"] for row in actual_audit.values()) != legacy_expired_sum:
        raise LegacyBonusSchemaError("Исторический audit бонусов не совпадает с исходной схемой; перенос отменён.")

    await db.execute("DROP TABLE bonus_grants")
    await db.execute(
        "ALTER TABLE __bonus_grants_migrated RENAME TO bonus_grants"
    )


async def _rebuild_legacy_bonus_allocations(db) -> None:
    """Переносит legacy bonus_usage_allocations (без restored_at,
    с UNIQUE(order_id, grant_id)) в актуальную схему."""
    migrated_ddl = CREATE_BONUS_USAGE_ALLOCATIONS_TABLE.replace(
        "CREATE TABLE IF NOT EXISTS bonus_usage_allocations",
        "CREATE TABLE __bonus_usage_allocations_migrated",
    )
    if "IF NOT EXISTS" in migrated_ddl:
        raise RuntimeError(
            "DDL новой таблицы bonus_usage_allocations некорректен."
        )

    await db.execute(migrated_ddl)

    # Legacy-строки — невосстановленные списания: restore-логика старой
    # версии УДАЛЯЛА строки, поэтому restored_at для переноса равен NULL
    # без какого-либо изобретения данных.
    await db.execute(
        """
        INSERT INTO __bonus_usage_allocations_migrated (
            id, order_id, grant_id, amount, created_at
        )
        SELECT id, order_id, grant_id, amount, created_at
        FROM bonus_usage_allocations
        ORDER BY id ASC
        """
    )

    cursor = await db.execute(
        "SELECT COUNT(*) FROM __bonus_usage_allocations_migrated"
    )
    migrated_count = int((await cursor.fetchone())[0])
    await cursor.close()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM bonus_usage_allocations"
    )
    legacy_count = int((await cursor.fetchone())[0])
    await cursor.close()

    if migrated_count != legacy_count:
        raise LegacyBonusSchemaError(
            "Число строк после переноса bonus_usage_allocations не "
            f"совпадает: {migrated_count} из {legacy_count}. Данные "
            "не изменены."
        )

    cursor = await db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM bonus_usage_allocations"
    )
    legacy_sum = int((await cursor.fetchone())[0])
    await cursor.close()
    cursor = await db.execute(
        "SELECT COALESCE(SUM(amount), 0) "
        "FROM __bonus_usage_allocations_migrated"
    )
    new_sum = int((await cursor.fetchone())[0])
    await cursor.close()

    if legacy_sum != new_sum:
        raise LegacyBonusSchemaError(
            f"Контрольная сумма списаний не совпадает {legacy_sum}!={new_sum}; "
            "данные не изменены."
        )

    await db.execute("DROP TABLE bonus_usage_allocations")
    await db.execute(
        "ALTER TABLE __bonus_usage_allocations_migrated "
        "RENAME TO bonus_usage_allocations"
    )


async def migrate_legacy_bonus_schema(db) -> None:
    """
    Переводит бонусные таблицы исторической схемы в актуальную структуру.

    Поведение строго согласовано с фактическими состояниями баз:
    - актуальная схема: полная no-op;
    - историческая схема: перенос с сохранением id, сумм, дат,
      provenance и зачислённых списаний; транзакция атомарна;
    - прерванное состояние: возможно только целиком —

      либо применено, либо нет (SQLite транзакционность DDL+DML);
    - неизвестная схема: LegacyBonusSchemaError без изменений данных.
    Никакие начисления не создаются и не сгорают при переносе.
    """
    # app_migrations создаётся здесь, потому что precheck выполняется
    # до основной транзакции миграций и до других функций-миграций.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS app_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )

    grants_shape = await _bonus_grants_shape(db)
    allocations_shape = await _bonus_allocations_shape(db)

    if grants_shape == "current" and allocations_shape == "current":
        cursor = await db.execute(
            "SELECT 1 FROM app_migrations WHERE name = ? LIMIT 1",
            (BONUS_SCHEMA_MIGRATION,),
        )
        already = await cursor.fetchone()
        await cursor.close()
        if already is None:
            # Актуальная схема без маркера: фиксируем применённое
            # состояние один раз для дальнейших проверок целостности.
            await db.execute(
                """
                INSERT INTO app_migrations (name, applied_at)
                VALUES (?, datetime('now', '+3 hours'))
                """,
                (BONUS_SCHEMA_MIGRATION,),
            )
        return

    if grants_shape == "legacy":
        await _rebuild_legacy_bonus_grants(db)

    if allocations_shape == "legacy":
        await _rebuild_legacy_bonus_allocations(db)

    # Финальная валидация структуры и внешних ключей ДО установки
    # маркера: маркер ставится только после успешного переноса.
    cursor = await db.execute("PRAGMA foreign_key_check")
    fk_violations = await cursor.fetchall()
    await cursor.close()
    if fk_violations:
        raise LegacyBonusSchemaError(
            f"PRAGMA foreign_key_check нашёл {len(fk_violations)} "
            "нарушений после переноса бонусных таблиц. Данные не изменены."
        )

    cursor = await db.execute(
        """
        INSERT INTO app_migrations (name, applied_at)
        VALUES (?, datetime('now', '+3 hours'))
        """,
        (BONUS_SCHEMA_MIGRATION,),
    )
    await cursor.close()

async def column_exists(
    db: aiosqlite.Connection,
    table_name: str,
    column_name: str,
) -> bool:
    """
    Проверяет наличие колонки в таблице.
    """

    cursor = await db.execute(
        f"PRAGMA table_info({table_name})"
    )

    columns = await cursor.fetchall()
    await cursor.close()

    return any(
        column[1] == column_name
        for column in columns
    )

async def table_exists(
    db: aiosqlite.Connection,
    table_name: str,
) -> bool:
    """
    Проверяет наличие таблицы в базе данных.
    """

    cursor = await db.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE
            type = 'table'
            AND name = ?
        LIMIT 1
        """,
        (
            table_name,
        ),
    )

    result = await cursor.fetchone()
    await cursor.close()

    return result is not None


async def migrate_legacy_bonus_schema_precheck_and_apply(
    db: aiosqlite.Connection,
) -> None:
    """
    Безопасный вход для переноса бонусных таблиц в run_migrations.

    Актуальная схема: no-op (маркер фиксируется один раз).
    Историческая схема: перестройка выполняется в отдельной явной
    транзакции со временно отключённым FOREIGN KEY (требование
    SQLite для многошаговой перестройки таблиц), с проверкой
    PRAGMA foreign_key_check до фиксации и возвратом режима FK.
    Любая ошибка полностью откатывает перенос.
    """
    grants_shape = await _bonus_grants_shape(db)
    allocations_shape = await _bonus_allocations_shape(db)

    needs_rebuild = (
        grants_shape == "legacy" or allocations_shape == "legacy"
    )

    if not needs_rebuild:
        await migrate_legacy_bonus_schema(db)
        return

    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.execute("BEGIN IMMEDIATE")
        await migrate_legacy_bonus_schema(db)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


async def _run_ddl_script(
    db: aiosqlite.Connection,
    script: str,
) -> None:
    """
    Выполняет простой DDL-скрипт по одному оператору.

    sqlite3.executescript() НЕ является безопасным внутри транзакции:
    он неявно фиксирует открытую транзакцию перед выполнением. Для
    атомарных миграций все операторы выполняются по отдельности,
    чтобы остаться внутри одной управляемой транзакции. Проектные
    DDL-скрипты содержат только CREATE ... IF NOT EXISTS без строковых
    литералов, поэтому разделение по ';' безопасно.
    """
    for raw_statement in script.split(";"):
        statement = raw_statement.strip()
        if not statement or statement.startswith("--"):
            continue
        await db.execute(statement)


async def run_migrations() -> None:
    """
    Обновляет существующую базу данных
    до актуальной структуры.

    Новая база создаётся через init_db(),
    а эта функция нужна для совместимости
    с базами, созданными предыдущими версиями бота.

    Атомарность (DB-02): все структурные изменения, конвертация
    timestamps и маркеры выполняются в одной явной транзакции.
    Прерванная миграция откатывается целиком и безопасно перезапускается
    при следующем старте; маркеры ставятся только после успешного
    выполнения соответствующих преобразований. Неизвестная/противоречивая
    схема не переделывается: работа останавливается с явной ошибкой
    (fail closed) и без изменения данных.
    """

    async with aiosqlite.connect(DB_PATH, isolation_level=None) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        # =================================================
        # DB-01: актуализация бонусных таблиц.
        # Отдельная транзакция до BEGIN IMMEDIATE, потому что
        # перестройка таблиц с внешними ключами требует отключенного
        # FOREIGN_KEYS на время операции.
        # =================================================

        await migrate_legacy_bonus_schema_precheck_and_apply(db)

        await db.execute("BEGIN IMMEDIATE")

        # =================================================
        # USERS
        # =================================================

        if not await column_exists(
            db,
            table_name="users",
            column_name="balance",
        ):
            await db.execute(
                """
                ALTER TABLE users
                ADD COLUMN balance INTEGER NOT NULL DEFAULT 0
                    CHECK (balance >= 0)
                """
            )

        if not await column_exists(
            db,
            table_name="users",
            column_name="rules_accepted",
        ):
            await db.execute(
                """
                ALTER TABLE users
                ADD COLUMN rules_accepted INTEGER NOT NULL DEFAULT 0
                    CHECK (rules_accepted IN (0, 1))
                """
            )


        if not await column_exists(
            db,
            table_name="users",
            column_name="bonus_balance",
        ):
            await db.execute(
                """
                ALTER TABLE users
                ADD COLUMN bonus_balance INTEGER NOT NULL DEFAULT 0
                    CHECK (bonus_balance >= 0)
                """
            )

        if not await column_exists(
            db,
            table_name="users",
            column_name="referral_code",
        ):
            await db.execute(
                """
                ALTER TABLE users
                ADD COLUMN referral_code TEXT
                """
            )

        await _run_ddl_script(
            db,
            CREATE_USERS_INDEXES
        )

        # =================================================
        # ORDERS
        # =================================================

        if not await column_exists(
            db,
            table_name="orders",
            column_name="updated_at",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN updated_at TEXT
                """
            )

        if not await column_exists(
            db,
            table_name="orders",
            column_name="group_chat_id",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN group_chat_id INTEGER
                """
            )

        if not await column_exists(
            db,
            table_name="orders",
            column_name="topic_id",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN topic_id INTEGER
                """
            )

        if not await column_exists(
            db,
            table_name="orders",
            column_name="assigned_master_telegram_id",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN assigned_master_telegram_id INTEGER
                """
            )

        if not await column_exists(
            db,
            table_name="orders",
            column_name="agreed_price",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN agreed_price INTEGER
                    CHECK (
                        agreed_price IS NULL
                        OR agreed_price > 0
                    )
                """
            )

        if not await column_exists(
            db,
            table_name="orders",
            column_name="price_status",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN price_status TEXT
                    NOT NULL
                    DEFAULT 'waiting_master'
                    CHECK (
                        price_status IN (
                            'waiting_master',
                            'waiting_client',
                            'waiting_master_response',
                            'agreed',
                            'rejected'
                        )
                    )
                """
            )

        if not await column_exists(
            db,
            table_name="orders",
            column_name="order_card_message_id",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN order_card_message_id INTEGER
                """
            )

        if not await column_exists(
            db,
            table_name="orders",
            column_name="assignment_status",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN assignment_status TEXT
                    NOT NULL
                    DEFAULT 'assigned'
                    CHECK (
                        assignment_status IN (
                            'assigned',
                            'waiting_selection',
                            'open',
                            'cancelled'
                        )
                    )
                """
            )

            # Старые незакреплённые новые заказы
            # считаем заказами в режиме свободного мастера.
            await db.execute(
                """
                UPDATE orders
                SET assignment_status = 'open'
                WHERE
                    status = 'new'
                    AND assigned_master_telegram_id IS NULL
                """
            )


        if not await column_exists(
            db,
            table_name="orders",
            column_name="cash_paid_amount",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN cash_paid_amount INTEGER
                    CHECK (
                        cash_paid_amount IS NULL
                        OR cash_paid_amount >= 0
                    )
                """
            )

        if not await column_exists(
            db,
            table_name="orders",
            column_name="bonus_used",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN bonus_used INTEGER NOT NULL DEFAULT 0
                    CHECK (bonus_used >= 0)
                """
            )

        if not await column_exists(
            db,
            table_name="orders",
            column_name="referral_discount",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN referral_discount INTEGER NOT NULL DEFAULT 0
                    CHECK (referral_discount >= 0)
                """
            )

        # ORD-02: монотонная генерация назначения заказа. Историческим
        # заказам достаточно значения по умолчанию — новые назначения
        # после обновления всегда увеличивают счётчик.
        if not await column_exists(
            db,
            table_name="orders",
            column_name="assignment_generation",
        ):
            await db.execute(
                """
                ALTER TABLE orders
                ADD COLUMN assignment_generation
                    INTEGER NOT NULL DEFAULT 0
                """
            )

        await _run_ddl_script(
            db,
            CREATE_ORDERS_INDEXES
        )

        # =================================================
        # MASTERS
        # =================================================

        # Предварительное условие DB-02: колонки мастеров проверяются
        # только когда таблица существует. Историческая/частичная база
        # может не содержать masters — создаём её в актуальном виде.
        if not await table_exists(
            db,
            table_name="masters",
        ):
            await db.execute(
                CREATE_MASTERS_TABLE
            )

        if not await column_exists(
            db,
            table_name="masters",
            column_name="is_active",
        ):
            await db.execute(
                """
                ALTER TABLE masters
                ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
                    CHECK (is_active IN (0, 1))
                """
            )

        if not await column_exists(
            db,
            table_name="masters",
            column_name="university",
        ):
            await db.execute(
                """
                ALTER TABLE masters
                ADD COLUMN university TEXT
                """
            )

        if not await column_exists(
            db,
            table_name="masters",
            column_name="specialty_code",
        ):
            await db.execute(
                """
                ALTER TABLE masters
                ADD COLUMN specialty_code TEXT
                """
            )

        if not await column_exists(
            db,
            table_name="masters",
            column_name="specialty_name",
        ):
            await db.execute(
                """
                ALTER TABLE masters
                ADD COLUMN specialty_name TEXT
                """
            )

        if not await column_exists(
            db,
            table_name="masters",
            column_name="average_response_minutes",
        ):
            await db.execute(
                """
                ALTER TABLE masters
                ADD COLUMN average_response_minutes INTEGER
                    CHECK (
                        average_response_minutes IS NULL
                        OR average_response_minutes >= 0
                    )
                """
            )

        if not await column_exists(
            db,
            table_name="masters",
            column_name="deleted_at",
        ):
            await db.execute(
                """
                ALTER TABLE masters
                ADD COLUMN deleted_at TEXT
                """
            )

        # =================================================
        # MASTER DIRECTIONS
        # =================================================

        # Связь many-to-many между исполнителями и направлениями.
        # Для существующих исполнителей строки специально НЕ создаются:
        # направления назначаются администратором вручную.
        if not await table_exists(
            db,
            table_name="master_directions",
        ):
            await db.execute(
                CREATE_MASTER_DIRECTIONS_TABLE
            )

        await _run_ddl_script(
            db,
            CREATE_MASTER_DIRECTIONS_INDEXES
        )

        # =================================================
        # ORDER MASTER ATTEMPTS
        # =================================================

        if not await table_exists(
            db,
            table_name="order_master_attempts",
        ):
            await db.execute(
                CREATE_ORDER_MASTER_ATTEMPTS_TABLE
            )

        await _run_ddl_script(
            db,
            CREATE_ORDER_MASTER_ATTEMPTS_INDEXES
        )


        # =================================================
        # REFERRALS / BONUS BALANCE
        # =================================================

        if not await table_exists(
            db,
            table_name="referrals",
        ):
            await db.execute(
                CREATE_REFERRALS_TABLE
            )

        if not await table_exists(
            db,
            table_name="bonus_transactions",
        ):
            await db.execute(
                CREATE_BONUS_TRANSACTIONS_TABLE
            )

        if not await table_exists(
            db,
            table_name="bonus_grants",
        ):
            await db.execute(
                CREATE_BONUS_GRANTS_TABLE
            )

        if not await table_exists(
            db,
            table_name="bonus_usage_allocations",
        ):
            await db.execute(
                CREATE_BONUS_USAGE_ALLOCATIONS_TABLE
            )

        await _run_ddl_script(
            db,
            CREATE_REFERRALS_INDEXES
        )

        await _run_ddl_script(
            db,
            CREATE_BONUS_TRANSACTIONS_INDEXES
        )

        await _run_ddl_script(
            db,
            CREATE_BONUS_GRANTS_INDEXES
        )

        await _run_ddl_script(
            db,
            CREATE_BONUS_USAGE_ALLOCATIONS_INDEXES
        )

        await migrate_legacy_bonus_balances_to_grants(db)

        # =================================================
        # IDEMPOTENCY / TOPIC CHECKPOINTS (ORD-01, FIN-01, REL-01)
        # =================================================

        if not await table_exists(
            db,
            table_name="operation_log",
        ):
            await db.execute(
                CREATE_OPERATION_LOG_TABLE
            )

        await _run_ddl_script(
            db,
            CREATE_OPERATION_LOG_INDEX
        )

        if not await table_exists(
            db,
            table_name="order_topic_checkpoints",
        ):
            await db.execute(
                CREATE_ORDER_TOPIC_CHECKPOINTS_TABLE
            )

        if not await column_exists(db, "order_topic_checkpoints", "assignment_generation"):
            await db.execute("ALTER TABLE order_topic_checkpoints ADD COLUMN assignment_generation INTEGER NOT NULL DEFAULT -1")

        # =================================================
        # ARC-01: durable задачи скачивания вложений архива.
        # =================================================

        if not await table_exists(
            db,
            table_name="chat_archive_download_jobs",
        ):
            await db.execute(
                CREATE_ARCHIVE_DOWNLOAD_JOBS_TABLE
            )

        await _run_ddl_script(
            db,
            CREATE_ARCHIVE_DOWNLOAD_JOBS_INDEX
        )
        for name, ddl in {
            "lease_token": "TEXT", "lease_until": "REAL NOT NULL DEFAULT 0",
            "next_attempt_at": "REAL NOT NULL DEFAULT 0", "reserved_bytes": "INTEGER NOT NULL DEFAULT 0",
            "content_sha256": "TEXT", "content_size": "INTEGER", "relative_path": "TEXT",
        }.items():
            if not await column_exists(db, "chat_archive_download_jobs", name):
                await db.execute(f"ALTER TABLE chat_archive_download_jobs ADD COLUMN {name} {ddl}")

        # =================================================
        # USER ACTIVITY LOGS
        # =================================================

        if not await table_exists(
            db,
            table_name="user_activity_logs",
        ):
            await db.execute(
                CREATE_USER_ACTIVITY_LOGS_TABLE
            )

        # Индексы имеют IF NOT EXISTS,
        # поэтому их безопасно проверять
        # при каждом запуске миграций.
        await _run_ddl_script(
            db,
            CREATE_USER_ACTIVITY_LOGS_INDEXES
        )

        # Все необходимые таблицы/колонки уже существуют. Теперь можно
        # один раз перевести накопленные UTC timestamp-значения на МСК.
        await migrate_existing_timestamps_to_moscow(db)

        # Обновляем направления только после подготовки основных таблиц.
        await migrate_direction_titles(db)

        cursor = await db.execute("PRAGMA foreign_key_check")
        violation = await cursor.fetchone()
        await cursor.close()
        if violation is not None:
            raise LegacyBonusSchemaError("Нарушение внешних ключей перед фиксацией основного этапа миграции.")
        await db.commit()
