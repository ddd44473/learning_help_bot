import aiosqlite

from database.db import DB_PATH
from utils.order_statuses import ORDER_STATUS_COMPLETED


async def get_master_reviews_summary(
    master_telegram_id: int,
) -> tuple[int, float | None]:
    """
    Возвращает количество отзывов и среднюю оценку мастера.

    Результат:
    (
        reviews_count,
        average_rating,
    )

    Если отзывов нет:
    (
        0,
        None,
    )
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                COUNT(reviews.id) AS reviews_count,
                AVG(reviews.rating) AS average_rating
            FROM reviews
            JOIN orders
                ON orders.id = reviews.order_id
            WHERE
                orders.assigned_master_telegram_id = ?
            """,
            (master_telegram_id,),
        )

        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return 0, None

        reviews_count = int(
            row["reviews_count"] or 0
        )

        average_rating_raw = row[
            "average_rating"
        ]

        average_rating = (
            float(average_rating_raw)
            if average_rating_raw is not None
            else None
        )

        return (
            reviews_count,
            average_rating,
        )


async def get_master_rating_stats(
    master_telegram_id: int,
) -> dict:
    """
    Возвращает подробную статистику рейтинга мастера.

    Результат:
    {
        "reviews_count": 18,
        "average_rating": 4.83,
        "rating_5": 14,
        "rating_4": 3,
        "rating_3": 1,
        "rating_2": 0,
        "rating_1": 0,
    }
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            SELECT
                COUNT(reviews.id) AS reviews_count,

                AVG(reviews.rating) AS average_rating,

                SUM(
                    CASE
                        WHEN reviews.rating = 5
                        THEN 1
                        ELSE 0
                    END
                ) AS rating_5,

                SUM(
                    CASE
                        WHEN reviews.rating = 4
                        THEN 1
                        ELSE 0
                    END
                ) AS rating_4,

                SUM(
                    CASE
                        WHEN reviews.rating = 3
                        THEN 1
                        ELSE 0
                    END
                ) AS rating_3,

                SUM(
                    CASE
                        WHEN reviews.rating = 2
                        THEN 1
                        ELSE 0
                    END
                ) AS rating_2,

                SUM(
                    CASE
                        WHEN reviews.rating = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS rating_1

            FROM reviews

            JOIN orders
                ON orders.id = reviews.order_id

            WHERE
                orders.assigned_master_telegram_id = ?
            """,
            (
                master_telegram_id,
            ),
        )

        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return {
                "reviews_count": 0,
                "average_rating": None,
                "rating_5": 0,
                "rating_4": 0,
                "rating_3": 0,
                "rating_2": 0,
                "rating_1": 0,
            }

        average_rating_raw = row[
            "average_rating"
        ]

        return {
            "reviews_count": int(
                row["reviews_count"] or 0
            ),

            "average_rating": (
                float(average_rating_raw)
                if average_rating_raw is not None
                else None
            ),

            "rating_5": int(
                row["rating_5"] or 0
            ),

            "rating_4": int(
                row["rating_4"] or 0
            ),

            "rating_3": int(
                row["rating_3"] or 0
            ),

            "rating_2": int(
                row["rating_2"] or 0
            ),

            "rating_1": int(
                row["rating_1"] or 0
            ),
        }


# ---------------------------------------------------------------------------
# Persistence границы отзывов, ранее размещённая в database.orders.
# database.orders реэкспортирует эти имена для обратной совместимости.
# ---------------------------------------------------------------------------


async def create_review(
    order_id: int,
    client_telegram_id: int,
    rating: int,
) -> bool:
    """
    Создаёт оценку для завершённого заказа.

    Проверки выполняются внутри одного INSERT ... SELECT:
    - заказ существует;
    - заказ принадлежит нажавшему клиенту;
    - заказ завершён;
    - у заказа назначен мастер;
    - для заказа ещё нет отзыва.
    """

    if rating not in {1, 2, 3, 4, 5}:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO reviews (
                order_id,
                client_user_id,
                master_telegram_id,
                rating,
                created_at
            )
            SELECT
                orders.id,
                orders.user_id,
                orders.assigned_master_telegram_id,
                ?,
                datetime('now', '+3 hours')
            FROM orders
            JOIN users
                ON users.id = orders.user_id
            WHERE
                orders.id = ?
                AND users.telegram_id = ?
                AND orders.status = ?
                AND orders.assigned_master_telegram_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1
                    FROM reviews
                    WHERE reviews.order_id = orders.id
                )
            """,
            (
                rating,
                order_id,
                client_telegram_id,
                ORDER_STATUS_COMPLETED,
            ),
        )

        await db.commit()
        return cursor.rowcount > 0

async def get_review_by_order_id(
    order_id: int,
) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT *
            FROM reviews
            WHERE order_id = ?
            LIMIT 1
            """,
            (order_id,),
        )

        return await cursor.fetchone()

async def get_client_review_for_order(
    order_id: int,
    client_telegram_id: int,
) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT reviews.*
            FROM reviews
            JOIN users
                ON users.id = reviews.client_user_id
            WHERE
                reviews.order_id = ?
                AND users.telegram_id = ?
            LIMIT 1
            """,
            (
                order_id,
                client_telegram_id,
            ),
        )

        return await cursor.fetchone()

async def add_review_text(
    order_id: int,
    client_telegram_id: int,
    review_text: str,
) -> bool:
    """
    Добавляет текст к уже созданной оценке. Текст может добавить
    только клиент этого заказа и только один раз.
    """

    normalized_text = review_text.strip()

    if not normalized_text:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            UPDATE reviews
            SET review_text = ?
            WHERE
                order_id = ?
                AND review_text IS NULL
                AND client_user_id = (
                    SELECT id
                    FROM users
                    WHERE telegram_id = ?
                )
            """,
            (
                normalized_text,
                order_id,
                client_telegram_id,
            ),
        )

        await db.commit()
        return cursor.rowcount > 0

async def get_all_reviews(
    limit: int = 20,
    offset: int = 0,
) -> list[aiosqlite.Row]:
    safe_limit = max(1, min(limit, 100))
    safe_offset = max(0, offset)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                reviews.id,
                reviews.order_id,
                reviews.rating,
                reviews.review_text,
                reviews.created_at,
                users.bot_nickname AS client_bot_nickname,
                masters.full_name AS master_name,
                orders.direction,
                orders.subject,
                orders.work_type
            FROM reviews
            JOIN orders
                ON orders.id = reviews.order_id
            JOIN users
                ON users.id = reviews.client_user_id
            LEFT JOIN masters
                ON masters.telegram_id = reviews.master_telegram_id
            ORDER BY reviews.created_at DESC, reviews.id DESC
            LIMIT ? OFFSET ?
            """,
            (
                safe_limit,
                safe_offset,
            ),
        )

        return list(await cursor.fetchall())

async def get_reviews_by_master(
    master_telegram_id: int,
    limit: int = 20,
    offset: int = 0,
) -> list[aiosqlite.Row]:
    safe_limit = max(1, min(limit, 100))
    safe_offset = max(0, offset)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                reviews.id,
                reviews.order_id,
                reviews.rating,
                reviews.review_text,
                reviews.created_at,
                reviews.master_telegram_id,
                users.bot_nickname AS client_bot_nickname,
                masters.full_name AS master_name,
                orders.direction,
                orders.subject,
                orders.work_type
            FROM reviews
            JOIN orders
                ON orders.id = reviews.order_id
            JOIN users
                ON users.id = reviews.client_user_id
            LEFT JOIN masters
                ON masters.telegram_id = reviews.master_telegram_id
            WHERE reviews.master_telegram_id = ?
            ORDER BY reviews.created_at DESC, reviews.id DESC
            LIMIT ? OFFSET ?
            """,
            (
                master_telegram_id,
                safe_limit,
                safe_offset,
            ),
        )

        reviews = await cursor.fetchall()
        await cursor.close()

        return list(reviews)

async def get_master_reviews(
    master_telegram_id: int,
    limit: int = 20,
    offset: int = 0,
) -> list[aiosqlite.Row]:
    safe_limit = max(1, min(limit, 100))
    safe_offset = max(0, offset)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                reviews.*,
                orders.direction,
                orders.subject,
                orders.work_type
            FROM reviews
            JOIN orders
                ON orders.id = reviews.order_id
            WHERE reviews.master_telegram_id = ?
            ORDER BY reviews.created_at DESC, reviews.id DESC
            LIMIT ? OFFSET ?
            """,
            (
                master_telegram_id,
                safe_limit,
                safe_offset,
            ),
        )

        return list(await cursor.fetchall())

async def get_master_rating_summary(
    master_telegram_id: int,
) -> aiosqlite.Row:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                COUNT(*) AS reviews_count,
                ROUND(AVG(rating), 2) AS average_rating
            FROM reviews
            WHERE master_telegram_id = ?
            """,
            (master_telegram_id,),
        )

        row = await cursor.fetchone()

        if row is None:
            raise RuntimeError(
                "Не удалось получить статистику отзывов мастера."
            )

        return row

async def get_masters_with_reviews(
    limit: int = 10,
    offset: int = 0,
    direction: str | None = None,
) -> list[aiosqlite.Row]:
    """
    Возвращает активных исполнителей,
    у которых есть хотя бы один отзыв.

    Если direction задан, список дополнительно ограничивается
    исполнителями, которым назначено выбранное направление.
    Сами отзывы выбранного исполнителя затем показываются все.
    """

    normalized_direction = (
        direction.strip()
        if direction is not None
        and direction.strip()
        else None
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        query = """
            SELECT
                masters.telegram_id,
                masters.username,
                masters.full_name,
                masters.is_active,
                COUNT(reviews.id) AS reviews_count,
                ROUND(
                    AVG(reviews.rating),
                    2
                ) AS average_rating
            FROM masters
            INNER JOIN reviews
                ON reviews.master_telegram_id =
                   masters.telegram_id
            WHERE masters.is_active = 1
        """

        params: list[object] = []

        if normalized_direction is not None:
            query += """
                AND EXISTS (
                    SELECT 1
                    FROM master_directions
                    WHERE
                        master_directions.master_telegram_id =
                            masters.telegram_id
                        AND master_directions.direction = ?
                )
            """
            params.append(
                normalized_direction
            )

        query += """
            GROUP BY
                masters.telegram_id,
                masters.username,
                masters.full_name,
                masters.is_active
            ORDER BY
                average_rating DESC,
                reviews_count DESC,
                masters.full_name COLLATE NOCASE ASC
            LIMIT ?
            OFFSET ?
        """

        params.extend(
            [
                limit,
                offset,
            ]
        )

        cursor = await db.execute(
            query,
            tuple(params),
        )

        masters = await cursor.fetchall()
        await cursor.close()

        return list(masters)

async def get_masters_with_reviews_count(
    direction: str | None = None,
) -> int:
    """
    Возвращает количество активных исполнителей,
    у которых есть хотя бы один отзыв.

    Если direction задан, учитываются только исполнители
    с этим назначенным направлением.
    """

    normalized_direction = (
        direction.strip()
        if direction is not None
        and direction.strip()
        else None
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        query = """
            SELECT COUNT(
                DISTINCT masters.telegram_id
            )
            FROM masters
            INNER JOIN reviews
                ON reviews.master_telegram_id =
                   masters.telegram_id
            WHERE masters.is_active = 1
        """

        params: list[object] = []

        if normalized_direction is not None:
            query += """
                AND EXISTS (
                    SELECT 1
                    FROM master_directions
                    WHERE
                        master_directions.master_telegram_id =
                            masters.telegram_id
                        AND master_directions.direction = ?
                )
            """
            params.append(
                normalized_direction
            )

        cursor = await db.execute(
            query,
            tuple(params),
        )

        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return 0

        return int(row[0])
