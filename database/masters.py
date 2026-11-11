import aiosqlite

from catalog.directions import DIRECTION_NAMES, DIRECTION_OPTIONS
from database.db import DB_PATH


def _normalize_direction(
    direction: str | None,
) -> str | None:
    if direction is None:
        return None

    normalized = direction.strip()

    return normalized or None


def _normalize_directions(
    directions: set[str] | list[str] | tuple[str, ...],
) -> list[str]:
    normalized = sorted(
        {
            str(direction).strip()
            for direction in directions
            if str(direction).strip()
        }
    )

    unknown = [
        direction
        for direction in normalized
        if direction not in DIRECTION_NAMES
    ]

    if unknown:
        raise ValueError(
            "Неизвестные направления: "
            + ", ".join(unknown)
        )

    return normalized



CREATE_MASTER_SUCCESS = "success"
CREATE_MASTER_ALREADY_EXISTS = "already_exists"
CREATE_MASTER_DELETED = "deleted"


async def create_master_profile_with_directions(
    telegram_id: int,
    username: str | None,
    full_name: str,
    university: str | None,
    specialty_code: str | None,
    specialty_name: str | None,
    directions: set[str] | list[str] | tuple[str, ...],
) -> str:
    """
    Атомарно создаёт нового исполнителя вместе с его полной карточкой.

    Важное отличие от редактирования:
    запись masters появляется только в момент финального сохранения.
    Поэтому отменённое заполнение не оставляет в БД пустого исполнителя.

    Возвращает:
    - success
    - already_exists
    - deleted
    """

    normalized_username = (
        username.strip().lstrip("@")
        if username is not None
        and username.strip()
        else None
    )

    normalized_full_name = full_name.strip()

    normalized_university = (
        university.strip()
        if university is not None
        and university.strip()
        else None
    )

    normalized_specialty_code = (
        specialty_code.strip()
        if specialty_code is not None
        and specialty_code.strip()
        else None
    )

    normalized_specialty_name = (
        specialty_name.strip()
        if specialty_name is not None
        and specialty_name.strip()
        else None
    )

    normalized_directions = _normalize_directions(
        directions
    )

    if not normalized_full_name:
        raise ValueError(
            "Имя исполнителя не может быть пустым."
        )

    if not normalized_directions:
        raise ValueError(
            "Новому исполнителю нужно назначить "
            "хотя бы одно направление."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT deleted_at
                FROM masters
                WHERE telegram_id = ?
                LIMIT 1
                """,
                (telegram_id,),
            )

            existing = await cursor.fetchone()
            await cursor.close()

            if existing is not None:
                await db.rollback()

                if existing["deleted_at"] is not None:
                    return CREATE_MASTER_DELETED

                return CREATE_MASTER_ALREADY_EXISTS

            await db.execute(
                """
                INSERT INTO masters (
                    telegram_id,
                    username,
                    full_name,
                    university,
                    specialty_code,
                    specialty_name,
                    average_response_minutes,
                    is_active,
                    deleted_at,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?,
                    NULL,
                    1,
                    NULL,
                    datetime('now', '+3 hours'),
                    datetime('now', '+3 hours')
                )
                """,
                (
                    telegram_id,
                    normalized_username,
                    normalized_full_name,
                    normalized_university,
                    normalized_specialty_code,
                    normalized_specialty_name,
                ),
            )

            await db.executemany(
                """
                INSERT INTO master_directions (
                    master_telegram_id,
                    direction,
                    created_at
                )
                VALUES (?, ?, datetime('now', '+3 hours'))
                """,
                [
                    (
                        telegram_id,
                        direction,
                    )
                    for direction in normalized_directions
                ],
            )

            await db.commit()
            return CREATE_MASTER_SUCCESS

        except Exception:
            await db.rollback()
            raise


async def get_active_masters(
    limit: int | None = None,
    offset: int = 0,
    exclude_telegram_ids: set[int] | list[int] | tuple[int, ...] | None = None,
    direction: str | None = None,
) -> list[aiosqlite.Row]:
    """
    Возвращает активных исполнителей вместе с рейтингом.

    Если передано direction, возвращаются только исполнители,
    которым администратор назначил это направление.

    exclude_telegram_ids используется при повторном выборе исполнителя:
    исполнители, с которыми этот заказ уже завершил неудачную попытку,
    больше не показываются клиенту.
    """

    excluded = sorted(
        {
            int(value)
            for value in (exclude_telegram_ids or [])
        }
    )
    normalized_direction = _normalize_direction(
        direction
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        query = """
            SELECT
                masters.telegram_id,
                masters.username,
                masters.full_name,
                masters.university,
                masters.specialty_code,
                masters.specialty_name,
                masters.average_response_minutes,
                masters.is_active,
                masters.created_at,
                masters.updated_at,
                COUNT(reviews.id) AS reviews_count,
                AVG(reviews.rating) AS average_rating
            FROM masters
            LEFT JOIN orders
                ON orders.assigned_master_telegram_id = masters.telegram_id
            LEFT JOIN reviews
                ON reviews.order_id = orders.id
            WHERE masters.is_active = 1
                AND masters.deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = masters.telegram_id)
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

        if excluded:
            placeholders = ",".join(
                "?"
                for _ in excluded
            )
            query += (
                " AND masters.telegram_id "
                f"NOT IN ({placeholders}) "
            )
            params.extend(excluded)

        query += """
            GROUP BY masters.telegram_id
            ORDER BY
                CASE WHEN COUNT(reviews.id) > 0 THEN 0 ELSE 1 END ASC,
                AVG(reviews.rating) DESC,
                COUNT(reviews.id) DESC,
                masters.full_name COLLATE NOCASE ASC
        """

        if limit is not None:
            query += " LIMIT ? OFFSET ? "
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


async def get_active_masters_count(
    exclude_telegram_ids: set[int] | list[int] | tuple[int, ...] | None = None,
    direction: str | None = None,
) -> int:
    """
    Возвращает количество доступных активных исполнителей.

    Если direction задан, учитываются только исполнители
    с назначенным направлением.
    """

    excluded = sorted(
        {
            int(value)
            for value in (exclude_telegram_ids or [])
        }
    )
    normalized_direction = _normalize_direction(
        direction
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        query = """
            SELECT COUNT(*)
            FROM masters
            WHERE is_active = 1
                AND deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = masters.telegram_id)
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

        if excluded:
            placeholders = ",".join(
                "?"
                for _ in excluded
            )
            query += (
                " AND telegram_id "
                f"NOT IN ({placeholders})"
            )
            params.extend(excluded)

        cursor = await db.execute(
            query,
            tuple(params),
        )
        row = await cursor.fetchone()
        await cursor.close()

        return (
            0
            if row is None
            else int(row[0])
        )


async def get_master_by_telegram_id(
    telegram_id: int,
) -> aiosqlite.Row | None:
    """
    Возвращает активного исполнителя по Telegram ID.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                telegram_id,
                username,
                full_name,
                university,
                specialty_code,
                specialty_name,
                average_response_minutes,
                is_active,
                deleted_at,
                created_at,
                updated_at
            FROM masters
            WHERE
                telegram_id = ?
                AND is_active = 1
                AND deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = masters.telegram_id)
            LIMIT 1
            """,
            (telegram_id,),
        )

        master = await cursor.fetchone()
        await cursor.close()

        return master


async def get_master_by_username(
    username: str,
) -> aiosqlite.Row | None:
    """
    Возвращает исполнителя по Telegram username.
    Принимает username с символом @ или без него.
    """

    normalized_username = (
        username
        .strip()
        .lstrip("@")
        .casefold()
    )

    if not normalized_username:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                telegram_id,
                username,
                full_name,
                university,
                specialty_code,
                specialty_name,
                average_response_minutes,
                is_active,
                deleted_at,
                created_at,
                updated_at
            FROM masters
            WHERE LOWER(username) = ?
            LIMIT 1
            """,
            (normalized_username,),
        )

        master = await cursor.fetchone()
        await cursor.close()

        return master


async def get_master_directions(
    telegram_id: int,
) -> list[str]:
    """
    Возвращает все назначенные исполнителю направления
    в порядке общего каталога.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT direction
            FROM master_directions
            WHERE master_telegram_id = ?
            """,
            (telegram_id,),
        )

        rows = await cursor.fetchall()
        await cursor.close()

    values = {
        str(row[0])
        for row in rows
    }

    # Сохраняем единый порядок каталога,
    # а не алфавитный порядок SQLite.
    return [
        title
        for _, title, _ in DIRECTION_OPTIONS
        if title in values
    ]


async def master_has_direction(
    telegram_id: int,
    direction: str | None,
) -> bool:
    """
    Проверяет, назначено ли исполнителю конкретное направление.

    Для пустого направления возвращает False: новые сценарии
    выбора исполнителя должны всегда опираться на направление заказа.
    """

    normalized_direction = _normalize_direction(
        direction
    )

    if normalized_direction is None:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT 1
            FROM masters
            JOIN master_directions
                ON master_directions.master_telegram_id =
                   masters.telegram_id
            WHERE
                masters.telegram_id = ?
                AND masters.is_active = 1
                AND masters.deleted_at IS NULL
                AND master_directions.direction = ?
            LIMIT 1
            """,
            (
                telegram_id,
                normalized_direction,
            ),
        )

        row = await cursor.fetchone()
        await cursor.close()

        return row is not None


async def replace_master_directions(
    telegram_id: int,
    directions: set[str] | list[str] | tuple[str, ...],
) -> bool:
    """
    Полностью заменяет набор направлений исполнителя.

    Пустой набор допустим. Это позволяет оставить исполнителя
    без направлений до ручного назначения администратором.
    """

    normalized_directions = _normalize_directions(
        directions
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT 1
                FROM masters
                WHERE
                    telegram_id = ?
                    AND deleted_at IS NULL
                LIMIT 1
                """,
                (telegram_id,),
            )
            exists = await cursor.fetchone()
            await cursor.close()

            if exists is None:
                await db.rollback()
                return False

            await db.execute(
                """
                DELETE FROM master_directions
                WHERE master_telegram_id = ?
                """,
                (telegram_id,),
            )

            if normalized_directions:
                await db.executemany(
                    """
                    INSERT INTO master_directions (
                        master_telegram_id,
                        direction,
                        created_at
                    )
                    VALUES (?, ?, datetime('now', '+3 hours'))
                    """,
                    [
                        (
                            telegram_id,
                            direction,
                        )
                        for direction in normalized_directions
                    ],
                )

            await db.commit()
            return True

        except Exception:
            await db.rollback()
            raise


async def update_master_profile(
    telegram_id: int,
    full_name: str,
    university: str | None,
    specialty_code: str | None,
    specialty_name: str | None,
) -> bool:
    """
    Обновляет данные карточки исполнителя.

    Среднее время ответа здесь не изменяется,
    потому что оно рассчитывается автоматически
    на основе реальных диалогов с клиентами.
    """

    normalized_full_name = (
        full_name.strip()
    )

    normalized_university = (
        university.strip()
        if (
            university is not None
            and university.strip()
        )
        else None
    )

    normalized_specialty_code = (
        specialty_code.strip()
        if (
            specialty_code is not None
            and specialty_code.strip()
        )
        else None
    )

    normalized_specialty_name = (
        specialty_name.strip()
        if (
            specialty_name is not None
            and specialty_name.strip()
        )
        else None
    )

    if not normalized_full_name:
        raise ValueError(
            "Имя исполнителя не может быть пустым."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            UPDATE masters
            SET
                full_name = ?,
                university = ?,
                specialty_code = ?,
                specialty_name = ?,
                updated_at = datetime('now', '+3 hours')
            WHERE
                telegram_id = ?
                AND deleted_at IS NULL
            """,
            (
                normalized_full_name,
                normalized_university,
                normalized_specialty_code,
                normalized_specialty_name,
                telegram_id,
            ),
        )

        await db.commit()

        updated = (
            cursor.rowcount > 0
        )

        await cursor.close()

        return updated


async def update_master_profile_with_directions(
    telegram_id: int,
    full_name: str,
    university: str | None,
    specialty_code: str | None,
    specialty_name: str | None,
    directions: set[str] | list[str] | tuple[str, ...],
) -> bool:
    """
    Атомарно сохраняет карточку исполнителя и весь набор направлений.
    """

    normalized_full_name = full_name.strip()
    normalized_university = (
        university.strip()
        if university is not None
        and university.strip()
        else None
    )
    normalized_specialty_code = (
        specialty_code.strip()
        if specialty_code is not None
        and specialty_code.strip()
        else None
    )
    normalized_specialty_name = (
        specialty_name.strip()
        if specialty_name is not None
        and specialty_name.strip()
        else None
    )
    normalized_directions = _normalize_directions(
        directions
    )

    if not normalized_full_name:
        raise ValueError(
            "Имя исполнителя не может быть пустым."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                UPDATE masters
                SET
                    full_name = ?,
                    university = ?,
                    specialty_code = ?,
                    specialty_name = ?,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    telegram_id = ?
                    AND deleted_at IS NULL
                """,
                (
                    normalized_full_name,
                    normalized_university,
                    normalized_specialty_code,
                    normalized_specialty_name,
                    telegram_id,
                ),
            )

            updated = cursor.rowcount > 0
            await cursor.close()

            if not updated:
                await db.rollback()
                return False

            await db.execute(
                """
                DELETE FROM master_directions
                WHERE master_telegram_id = ?
                """,
                (telegram_id,),
            )

            if normalized_directions:
                await db.executemany(
                    """
                    INSERT INTO master_directions (
                        master_telegram_id,
                        direction,
                        created_at
                    )
                    VALUES (?, ?, datetime('now', '+3 hours'))
                    """,
                    [
                        (
                            telegram_id,
                            direction,
                        )
                        for direction in normalized_directions
                    ],
                )

            await db.commit()
            return True

        except Exception:
            await db.rollback()
            raise


MASTER_STATUS_SUCCESS = "success"
MASTER_STATUS_NOT_FOUND = "not_found"
MASTER_STATUS_DELETED = "deleted"
MASTER_STATUS_ACTIVE_ORDERS = "active_orders"
MASTER_STATUS_NO_DIRECTIONS = "no_directions"
MASTER_STATUS_UNCHANGED = "unchanged"


async def get_master_by_telegram_id_any(
    telegram_id: int,
) -> aiosqlite.Row | None:
    """
    Возвращает исполнителя независимо от статуса.

    Нужен административному редактору: администратор должен
    видеть и активного, и неактивного, и soft-deleted исполнителя.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                telegram_id,
                username,
                full_name,
                university,
                specialty_code,
                specialty_name,
                average_response_minutes,
                is_active,
                deleted_at,
                created_at,
                updated_at
            FROM masters
            WHERE telegram_id = ?
            LIMIT 1
            """,
            (telegram_id,),
        )

        row = await cursor.fetchone()
        await cursor.close()
        return row


async def is_master_available(
    telegram_id: int,
) -> bool:
    """
    True только для действующего активного исполнителя.

    Используется не только при выборе заказа, но и перед
    рабочими действиями/сообщениями в теме заказа.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT 1
            FROM masters
            WHERE
                telegram_id = ?
                AND is_active = 1
                AND deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = masters.telegram_id)
            LIMIT 1
            """,
            (telegram_id,),
        )

        row = await cursor.fetchone()
        await cursor.close()
        return row is not None


async def get_master_active_orders_count(
    telegram_id: int,
) -> int:
    """
    Возвращает число незавершённых заказов, закреплённых
    за исполнителем.

    Такие заказы блокируют отключение и удаление исполнителя.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE
                assigned_master_telegram_id = ?
                AND status IN (
                    'new',
                    'in_progress',
                    'waiting_client'
                )
            """,
            (telegram_id,),
        )

        row = await cursor.fetchone()
        await cursor.close()

        return 0 if row is None else int(row[0])


async def update_master_field(
    telegram_id: int,
    field_name: str,
    value: str | None,
) -> bool:
    """
    Обновляет одно редактируемое поле карточки.

    Название колонки выбирается только из whitelist.
    Удалённую карточку редактировать нельзя.
    """

    allowed_fields = {
        "full_name",
        "university",
        "specialty_code",
        "specialty_name",
    }

    if field_name not in allowed_fields:
        raise ValueError(
            f"Поле {field_name!r} нельзя редактировать."
        )

    normalized = (
        value.strip()
        if value is not None and value.strip()
        else None
    )

    if field_name == "full_name" and not normalized:
        raise ValueError(
            "Имя исполнителя не может быть пустым."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            f"""
            UPDATE masters
            SET
                {field_name} = ?,
                updated_at = datetime('now', '+3 hours')
            WHERE
                telegram_id = ?
                AND deleted_at IS NULL
            """,
            (
                normalized,
                telegram_id,
            ),
        )

        await db.commit()
        updated = cursor.rowcount > 0
        await cursor.close()
        return updated


async def change_master_active_status(
    telegram_id: int,
    make_active: bool,
) -> tuple[str, int]:
    """
    Безопасно меняет active/inactive.

    Отключение запрещено при активных заказах.
    Активация запрещена удалённому исполнителю и исполнителю,
    которому не назначено ни одного направления.

    Возвращает (result, active_orders_count).
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT
                    is_active,
                    deleted_at
                FROM masters
                WHERE telegram_id = ?
                LIMIT 1
                """,
                (telegram_id,),
            )

            master = await cursor.fetchone()
            await cursor.close()

            if master is None:
                await db.rollback()
                return MASTER_STATUS_NOT_FOUND, 0

            if master["deleted_at"] is not None:
                await db.rollback()
                return MASTER_STATUS_DELETED, 0

            current_active = bool(
                int(master["is_active"])
            )

            if current_active == bool(make_active):
                await db.rollback()
                return MASTER_STATUS_UNCHANGED, 0

            if make_active:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*)
                    FROM master_directions
                    WHERE master_telegram_id = ?
                    """,
                    (telegram_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()

                directions_count = (
                    0 if row is None else int(row[0])
                )

                if directions_count <= 0:
                    await db.rollback()
                    return MASTER_STATUS_NO_DIRECTIONS, 0

            else:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*)
                    FROM orders
                    WHERE
                        assigned_master_telegram_id = ?
                        AND status IN (
                            'new',
                            'in_progress',
                            'waiting_client'
                        )
                    """,
                    (telegram_id,),
                )

                row = await cursor.fetchone()
                await cursor.close()

                active_orders = (
                    0 if row is None else int(row[0])
                )

                if active_orders > 0:
                    await db.rollback()
                    return (
                        MASTER_STATUS_ACTIVE_ORDERS,
                        active_orders,
                    )

            cursor = await db.execute(
                """
                UPDATE masters
                SET
                    is_active = ?,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    telegram_id = ?
                    AND deleted_at IS NULL
                """,
                (
                    int(make_active),
                    telegram_id,
                ),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return MASTER_STATUS_NOT_FOUND, 0

            await cursor.close()
            await db.commit()

            return MASTER_STATUS_SUCCESS, 0

        except Exception:
            await db.rollback()
            raise


async def soft_delete_master(
    telegram_id: int,
) -> tuple[str, int]:
    """
    Soft delete исполнителя.

    Строка masters, направления, старые заказы и отзывы сохраняются.
    Удаление запрещено, пока за исполнителем есть активные заказы.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT deleted_at
                FROM masters
                WHERE telegram_id = ?
                LIMIT 1
                """,
                (telegram_id,),
            )

            master = await cursor.fetchone()
            await cursor.close()

            if master is None:
                await db.rollback()
                return MASTER_STATUS_NOT_FOUND, 0

            if master["deleted_at"] is not None:
                await db.rollback()
                return MASTER_STATUS_DELETED, 0

            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM orders
                WHERE
                    assigned_master_telegram_id = ?
                    AND status IN (
                        'new',
                        'in_progress',
                        'waiting_client'
                    )
                """,
                (telegram_id,),
            )

            row = await cursor.fetchone()
            await cursor.close()

            active_orders = (
                0 if row is None else int(row[0])
            )

            if active_orders > 0:
                await db.rollback()
                return (
                    MASTER_STATUS_ACTIVE_ORDERS,
                    active_orders,
                )

            cursor = await db.execute(
                """
                UPDATE masters
                SET
                    is_active = 0,
                    deleted_at = datetime('now', '+3 hours'),
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    telegram_id = ?
                    AND deleted_at IS NULL
                """,
                (telegram_id,),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return MASTER_STATUS_NOT_FOUND, 0

            await cursor.close()
            await db.commit()

            return MASTER_STATUS_SUCCESS, 0

        except Exception:
            await db.rollback()
            raise


async def set_master_active(
    telegram_id: int,
    is_active: bool,
) -> bool:
    """
    Совместимая обёртка для старого API.

    Использует те же безопасные правила, что и административный редактор.
    """

    result, _ = await change_master_active_status(
        telegram_id=telegram_id,
        make_active=is_active,
    )

    return result in {
        MASTER_STATUS_SUCCESS,
        MASTER_STATUS_UNCHANGED,
    }

ADMIN_MASTER_STATUS_ACTIVE = "active"
ADMIN_MASTER_STATUS_INACTIVE = "inactive"
ADMIN_MASTER_STATUS_DELETED = "deleted"

ADMIN_MASTER_STATUS_FILTERS = {
    ADMIN_MASTER_STATUS_ACTIVE,
    ADMIN_MASTER_STATUS_INACTIVE,
    ADMIN_MASTER_STATUS_DELETED,
}


def _build_admin_master_filter(
    *,
    status_filter: str | None,
    direction: str | None,
) -> tuple[str, list[object]]:
    """
    Формирует только внутреннюю SQL-часть административного каталога.

    Значения статусов проверяются whitelist'ом, а направление всегда
    передаётся параметром SQLite, поэтому пользовательский ввод
    не попадает в SQL напрямую.
    """

    clauses: list[str] = []
    params: list[object] = []

    if status_filter is not None:
        if status_filter not in ADMIN_MASTER_STATUS_FILTERS:
            raise ValueError(
                f"Неизвестный фильтр статуса: {status_filter}"
            )

        if status_filter == ADMIN_MASTER_STATUS_ACTIVE:
            clauses.append(
                "masters.deleted_at IS NULL "
                "AND masters.is_active = 1"
            )

        elif status_filter == ADMIN_MASTER_STATUS_INACTIVE:
            clauses.append(
                "masters.deleted_at IS NULL "
                "AND masters.is_active = 0"
            )

        elif status_filter == ADMIN_MASTER_STATUS_DELETED:
            clauses.append(
                "masters.deleted_at IS NOT NULL"
            )

    normalized_direction = _normalize_direction(
        direction
    )

    if normalized_direction is not None:
        if normalized_direction not in DIRECTION_NAMES:
            raise ValueError(
                f"Неизвестное направление: {normalized_direction}"
            )

        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM master_directions
                WHERE
                    master_directions.master_telegram_id =
                        masters.telegram_id
                    AND master_directions.direction = ?
            )
            """
        )
        params.append(
            normalized_direction
        )

    if not clauses:
        return "", params

    return (
        " WHERE " + " AND ".join(
            f"({clause})"
            for clause in clauses
        ),
        params,
    )


async def get_admin_masters_stats() -> dict[str, int]:
    """
    Сводка для /masters.

    Удалённые исполнители считаются отдельно и не попадают
    в активных/неактивных.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                COUNT(*) AS total_count,

                SUM(
                    CASE
                        WHEN deleted_at IS NULL
                             AND is_active = 1
                        THEN 1
                        ELSE 0
                    END
                ) AS active_count,

                SUM(
                    CASE
                        WHEN deleted_at IS NULL
                             AND is_active = 0
                        THEN 1
                        ELSE 0
                    END
                ) AS inactive_count,

                SUM(
                    CASE
                        WHEN deleted_at IS NOT NULL
                        THEN 1
                        ELSE 0
                    END
                ) AS deleted_count
            FROM masters
            """
        )

        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return {
                "total": 0,
                "active": 0,
                "inactive": 0,
                "deleted": 0,
            }

        return {
            "total": int(
                row["total_count"] or 0
            ),
            "active": int(
                row["active_count"] or 0
            ),
            "inactive": int(
                row["inactive_count"] or 0
            ),
            "deleted": int(
                row["deleted_count"] or 0
            ),
        }


async def get_admin_masters_count(
    *,
    status_filter: str | None = None,
    direction: str | None = None,
) -> int:
    """
    Количество исполнителей для административного каталога.

    В отличие от клиентского выбора может учитывать
    неактивных и soft-deleted исполнителей.
    """

    where_sql, params = _build_admin_master_filter(
        status_filter=status_filter,
        direction=direction,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            f"""
            SELECT COUNT(*)
            FROM masters
            {where_sql}
            """,
            tuple(params),
        )

        row = await cursor.fetchone()
        await cursor.close()

        return 0 if row is None else int(row[0])


async def get_admin_masters(
    *,
    limit: int = 6,
    offset: int = 0,
    status_filter: str | None = None,
    direction: str | None = None,
) -> list[aiosqlite.Row]:
    """
    Страница административного каталога исполнителей.

    На уровне БД limit дополнительно ограничен, чтобы даже ошибка
    в handler не привела к огромному Telegram-сообщению/клавиатуре.
    """

    safe_limit = max(
        1,
        min(
            int(limit),
            20,
        ),
    )
    safe_offset = max(
        0,
        int(offset),
    )

    where_sql, params = _build_admin_master_filter(
        status_filter=status_filter,
        direction=direction,
    )

    params.extend(
        [
            safe_limit,
            safe_offset,
        ]
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            f"""
            SELECT
                masters.telegram_id,
                masters.username,
                masters.full_name,
                masters.university,
                masters.specialty_code,
                masters.specialty_name,
                masters.average_response_minutes,
                masters.is_active,
                masters.deleted_at,
                masters.created_at,
                masters.updated_at
            FROM masters
            {where_sql}
            ORDER BY
                CASE
                    WHEN masters.deleted_at IS NOT NULL THEN 2
                    WHEN masters.is_active = 0 THEN 1
                    ELSE 0
                END ASC,
                masters.full_name COLLATE NOCASE ASC,
                masters.telegram_id ASC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        )

        rows = await cursor.fetchall()
        await cursor.close()

        return list(rows)
