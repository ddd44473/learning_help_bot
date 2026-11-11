import aiosqlite

from database.db import DB_PATH


async def start_master_response_waiting(
    order_id: int,
    master_telegram_id: int,
) -> bool:
    """
    Запускает новый замер ожидания ответа мастера.

    Если по этому заказу и мастеру уже существует
    незакрытый замер, новый не создаётся.

    Возвращает:
    True  — новый замер создан;
    False — ожидание уже было запущено.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        try:
            await db.execute(
                "BEGIN IMMEDIATE"
            )

            cursor = await db.execute(
                """
                SELECT id
                FROM master_response_times
                WHERE
                    order_id = ?
                    AND master_telegram_id = ?
                    AND master_response_at IS NULL
                LIMIT 1
                """,
                (
                    order_id,
                    master_telegram_id,
                ),
            )

            existing = await cursor.fetchone()
            await cursor.close()

            if existing is not None:
                await db.rollback()
                return False

            await db.execute(
                """
                INSERT INTO master_response_times (
                    order_id,
                    master_telegram_id,
                    client_message_at,
                    created_at
                )
                VALUES (
                    ?, ?,
                    datetime('now', '+3 hours'),
                    datetime('now', '+3 hours')
                )
                """,
                (
                    order_id,
                    master_telegram_id,
                ),
            )

            await db.commit()

            return True

        except Exception:
            await db.rollback()
            raise


async def finish_master_response_waiting(
    order_id: int,
    master_telegram_id: int,
) -> int | None:
    """
    Завершает текущий замер ожидания ответа мастера.

    Записывает:
    - время ответа мастера;
    - продолжительность ожидания в секундах.

    Возвращает продолжительность ответа
    в секундах или None, если открытого
    замера не существует.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        try:
            await db.execute(
                "BEGIN IMMEDIATE"
            )

            cursor = await db.execute(
                """
                SELECT id
                FROM master_response_times
                WHERE
                    order_id = ?
                    AND master_telegram_id = ?
                    AND master_response_at IS NULL
                ORDER BY id ASC
                LIMIT 1
                """,
                (
                    order_id,
                    master_telegram_id,
                ),
            )

            waiting = await cursor.fetchone()
            await cursor.close()

            if waiting is None:
                await db.rollback()
                return None

            waiting_id = int(
                waiting["id"]
            )

            cursor = await db.execute(
                """
                UPDATE master_response_times
                SET
                    master_response_at =
                        datetime('now', '+3 hours'),

                    response_seconds = MAX(
                        0,
                        CAST(
                            (
                                julianday(
                                    datetime('now', '+3 hours')
                                )
                                -
                                julianday(
                                    client_message_at
                                )
                            )
                            * 86400
                            AS INTEGER
                        )
                    )

                WHERE
                    id = ?
                    AND master_response_at IS NULL
                """,
                (
                    waiting_id,
                ),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return None

            await cursor.close()

            cursor = await db.execute(
                """
                SELECT response_seconds
                FROM master_response_times
                WHERE id = ?
                LIMIT 1
                """,
                (
                    waiting_id,
                ),
            )

            row = await cursor.fetchone()
            await cursor.close()

            if (
                row is None
                or row["response_seconds"] is None
            ):
                await db.rollback()
                return None

            response_seconds = int(
                row["response_seconds"]
            )

            await db.commit()

            return response_seconds

        except Exception:
            await db.rollback()
            raise


async def get_master_average_response_seconds(
    master_telegram_id: int,
) -> int | None:
    """
    Рассчитывает среднее время ответа мастера
    по всем завершённым замерам.

    Возвращает результат в секундах.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                ROUND(
                    AVG(response_seconds)
                )
            FROM master_response_times
            WHERE
                master_telegram_id = ?
                AND response_seconds IS NOT NULL
            """,
            (
                master_telegram_id,
            ),
        )

        row = await cursor.fetchone()
        await cursor.close()

        if (
            row is None
            or row[0] is None
        ):
            return None

        return int(row[0])


async def update_master_average_response(
    master_telegram_id: int,
) -> int | None:
    """
    Пересчитывает среднее время ответа мастера
    и сохраняет его в masters.average_response_minutes.

    Возвращает новое среднее значение в минутах.
    """

    average_seconds = (
        await get_master_average_response_seconds(
            master_telegram_id
        )
    )

    if average_seconds is None:
        return None

    average_minutes = max(
        round(
            average_seconds / 60
        ),
        1,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            UPDATE masters
            SET
                average_response_minutes = ?,
                updated_at = datetime('now', '+3 hours')
            WHERE telegram_id = ?
            """,
            (
                average_minutes,
                master_telegram_id,
            ),
        )

        if cursor.rowcount <= 0:
            await cursor.close()
            await db.rollback()
            return None

        await cursor.close()
        await db.commit()

    return average_minutes