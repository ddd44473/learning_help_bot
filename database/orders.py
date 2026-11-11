import json
from collections.abc import Mapping, Sequence

import aiosqlite

from database.db import DB_PATH
from database.chat_archive import register_order_topic_in_tx
from database.referrals import grant_referral_reward_for_completed_order_in_tx
from database.operations import (
    assignment_actor_allowed_in_tx,
    OP_KIND_ORDER_CONFIRM,
    attach_operation_result_in_tx,
    reserve_operation_in_tx,
)
from utils.order_statuses import (
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_COMPLETED,
    ORDER_STATUS_IN_PROGRESS,
    ORDER_STATUS_NEW,
    ORDER_STATUS_WAITING_CLIENT,
)
from database.reviews import (
    add_review_text,
    create_review,
    get_all_reviews,
    get_client_review_for_order,
    get_master_rating_summary,
    get_master_reviews,
    get_masters_with_reviews,
    get_masters_with_reviews_count,
    get_review_by_order_id,
    get_reviews_by_master,
)



ASSIGNMENT_STATUS_ASSIGNED = "assigned"
ASSIGNMENT_STATUS_WAITING_SELECTION = "waiting_selection"
ASSIGNMENT_STATUS_OPEN = "open"
ASSIGNMENT_STATUS_CANCELLED = "cancelled"

FAILED_MASTER_OUTCOMES = {
    "declined",
    "negotiation_failed",
}


async def create_order(
    user_id: int,
    subject: str,
    work_type: str,
    description: str,
    assigned_master_telegram_id: int | None = None,
    direction: str | None = None,
    deadline: str | None = None,
    assignment_status: str | None = None,
    *,
    dedup_key: str | None = None,
    dedup_content_hash: str | None = None,
    files: Sequence[Mapping[str, object]] | None = None,
) -> tuple[int, bool]:
    """
    Создаёт заказ.

    Для новых заказов направление используется не только как поле карточки:
    выбранный исполнитель обязан быть активным и иметь это направление.
    В режиме open также должен существовать хотя бы один активный
    исполнитель нужного направления.

    dedup_key (ORD-01): durable-идемпотентность подтверждения черновика.
    Повторное подтверждение того же черновика возвращает уже созданный
    заказ в той же транзакции вместе с резервом ключа, поэтому
    конкурирующие/повторные вызовы создают не более одного заказа.
    Новый черновик всегда создаёт собственный заказ независимо от
    совпадения текста — ключ привязан к событию подтверждения, а не
    к содержимому черновика.

    Возвращает (order_id, freshly_created).
    """

    # Explicit files (including []) finalizes a client draft atomically.
    # None preserves legacy low-level callers: absence of a checkpoint must
    # NEVER be interpreted as proof that a remote attempt did not happen.
    file_refs = _validate_order_file_refs(files) if files is not None else None

    normalized_direction = (
        direction.strip()
        if direction is not None
        and direction.strip()
        else None
    )

    if assignment_status is None:
        assignment_status = (
            ASSIGNMENT_STATUS_ASSIGNED
            if assigned_master_telegram_id is not None
            else ASSIGNMENT_STATUS_OPEN
        )

    if assignment_status not in {
        ASSIGNMENT_STATUS_ASSIGNED,
        ASSIGNMENT_STATUS_OPEN,
    }:
        raise ValueError(
            "Некорректный начальный статус назначения исполнителя."
        )

    if (
        assignment_status == ASSIGNMENT_STATUS_ASSIGNED
        and assigned_master_telegram_id is None
    ):
        raise ValueError(
            "Для закреплённого заказа необходимо указать исполнителя."
        )

    if (
        assignment_status == ASSIGNMENT_STATUS_OPEN
        and assigned_master_telegram_id is not None
    ):
        raise ValueError(
            "У свободного заказа исполнитель не должен "
            "быть назначен заранее."
        )

    if normalized_direction is None:
        raise ValueError(
            "Для нового заказа необходимо указать направление."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            dedup_fresh = True
            if dedup_key is not None:
                dedup_fresh, dedup_result_ref = (
                    await reserve_operation_in_tx(
                        db,
                        op_key=dedup_key,
                        op_kind=OP_KIND_ORDER_CONFIRM,
                        content_hash=dedup_content_hash,
                    )
                )
                if not dedup_fresh:
                    # Повторное подтверждение: возвращаем существующий
                    # заказ, а не создаём ещё один. result_ref содержит
                    # {"order_id": ...}, сохранённый первой попыткой.
                    await db.rollback()
                    existing_id = None
                    if dedup_result_ref:
                        try:
                            existing_id = int(
                                json.loads(dedup_result_ref)["order_id"]
                            )
                        except (TypeError, ValueError, KeyError):
                            existing_id = None
                    if existing_id is None:
                        raise RuntimeError(
                            "Повторное подтверждение черновика не "
                            "имеет сохранённого заказа."
                        )
                    cursor = await db.execute(
                        """
                        SELECT 1
                        FROM orders
                        WHERE
                            id = ?
                            AND user_id = ?
                        LIMIT 1
                        """,
                        (existing_id, user_id),
                    )
                    confirmed = await cursor.fetchone()
                    await cursor.close()
                    if confirmed is None:
                        raise RuntimeError(
                            "Сохранённый заказ повторно подтверждённого "
                            "черновика не найден."
                        )
                    return existing_id, False

            if assigned_master_telegram_id is not None:
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
                        AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = masters.telegram_id)
                        AND master_directions.direction = ?
                    LIMIT 1
                    """,
                    (
                        assigned_master_telegram_id,
                        normalized_direction,
                    ),
                )
                eligible = await cursor.fetchone()
                await cursor.close()

                if eligible is None:
                    raise ValueError(
                        "Выбранный исполнитель не работает "
                        "с направлением этого заказа."
                    )

            else:
                cursor = await db.execute(
                    """
                    SELECT 1
                    FROM masters
                    JOIN master_directions
                        ON master_directions.master_telegram_id =
                           masters.telegram_id
                    WHERE
                        masters.is_active = 1
                        AND masters.deleted_at IS NULL
                        AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = masters.telegram_id)
                        AND master_directions.direction = ?
                    LIMIT 1
                    """,
                    (normalized_direction,),
                )
                eligible = await cursor.fetchone()
                await cursor.close()

                if eligible is None:
                    raise ValueError(
                        "Для выбранного направления сейчас "
                        "нет доступных исполнителей."
                    )

            cursor = await db.execute(
                """
                INSERT INTO orders (
                    user_id,
                    direction,
                    subject,
                    work_type,
                    description,
                    deadline,
                    assigned_master_telegram_id,
                    assignment_status,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    datetime('now', '+3 hours'),
                    datetime('now', '+3 hours')
                )
                """,
                (
                    user_id,
                    normalized_direction,
                    subject,
                    work_type,
                    description,
                    deadline,
                    assigned_master_telegram_id,
                    assignment_status,
                ),
            )

            order_id = cursor.lastrowid
            await cursor.close()

            if order_id is None:
                raise RuntimeError(
                    "Не удалось получить ID созданного заказа."
                )

            if assigned_master_telegram_id is not None:
                await db.execute(
                    """
                    INSERT INTO order_master_attempts (
                        order_id,
                        master_telegram_id,
                        outcome,
                        reason,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        ?, ?, 'selected', 'initial_selection',
                        datetime('now', '+3 hours'),
                        datetime('now', '+3 hours')
                    )
                    ON CONFLICT(order_id, master_telegram_id)
                    DO UPDATE SET
                        outcome = 'selected',
                        reason = 'initial_selection',
                        updated_at = datetime('now', '+3 hours')
                    """,
                    (
                        int(order_id),
                        assigned_master_telegram_id,
                    ),
                )

            if file_refs is not None:
                for file_ref in file_refs:
                    await _insert_order_file_in_tx(db, int(order_id), **file_ref)
                await db.execute(
                    """INSERT INTO order_topic_checkpoints
                    (order_id, assignment_generation, group_chat_id, topic_id, state, detail)
                    VALUES (?, 0, 0, 0, 'failed', 'local_creation_pending')""",
                    (int(order_id),),
                )

            if dedup_key is not None:
                await attach_operation_result_in_tx(
                    db,
                    op_key=dedup_key,
                    result={"order_id": int(order_id)},
                )

            await db.commit()
            return int(order_id), True

        except BaseException:
            await db.rollback()
            raise


def _validate_order_file_refs(files: Sequence[Mapping[str, object]]) -> list[dict]:
    """Validate opaque Telegram refs, not binary downloads or remote ownership.

    The UI accepts a single message or an album of at most ten items. Repeated
    file IDs are legitimate repeated uploads: retain their exact order/count.
    Never accept caller-supplied order IDs, row IDs or archive filesystem paths.
    """
    if not isinstance(files, (list, tuple)) or len(files) > 10:
        raise ValueError("Некорректный список вложений заказа (не более 10).")
    result = []
    for ref in files:
        if not isinstance(ref, Mapping) or set(ref) - {'telegram_file_id', 'file_type', 'file_name'}:
            raise ValueError("Некорректные поля вложения заказа.")
        file_id, kind, name = ref.get('telegram_file_id'), ref.get('file_type'), ref.get('file_name')
        if (not isinstance(file_id, str) or not file_id or any(c.isspace() or ord(c) < 32 for c in file_id)
                or kind not in ('photo', 'document') or (name is not None and not isinstance(name, str))):
            raise ValueError("Некорректная ссылка на вложение заказа.")
        result.append(dict(telegram_file_id=file_id, file_type=kind, file_name=name))
    return result


async def _insert_order_file_in_tx(db, order_id, telegram_file_id, file_type, file_name=None):
    await db.execute(
        """INSERT INTO order_files (order_id, telegram_file_id, file_type, file_name, created_at)
        VALUES (?, ?, ?, ?, datetime('now', '+3 hours'))""",
        (order_id, telegram_file_id, file_type, file_name),
    )

async def add_order_file(
    order_id: int,
    telegram_file_id: str,
    file_type: str,
    file_name: str | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute(
            """
            INSERT INTO order_files (
                order_id,
                telegram_file_id,
                file_type,
                file_name,
                created_at
            )
            VALUES (?, ?, ?, ?, datetime('now', '+3 hours'))
            """,
            (
                order_id,
                telegram_file_id,
                file_type,
                file_name,
            ),
        )

        await db.commit()


async def get_order_files(
    order_id: int,
) -> list[aiosqlite.Row]:
    """Возвращает все файлы заказа в порядке добавления."""

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                id,
                order_id,
                telegram_file_id,
                file_type,
                file_name,
                created_at
            FROM order_files
            WHERE order_id = ?
            ORDER BY id ASC
            """,
            (order_id,),
        )

        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)



async def update_order_topic(
    order_id: int,
    group_chat_id: int,
    topic_id: int,
) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            UPDATE orders
            SET
                group_chat_id = ?,
                topic_id = ?,
                updated_at = datetime('now', '+3 hours')
            WHERE id = ?
            """,
            (
                group_chat_id,
                topic_id,
                order_id,
            ),
        )

        if cursor.rowcount > 0:
            await register_order_topic_in_tx(
                db, int(order_id), int(group_chat_id), int(topic_id)
            )
        await db.commit()
        return cursor.rowcount > 0

async def update_order_card_message_id(
    order_id: int,
    order_card_message_id: int,
) -> bool:
    """
    Сохраняет ID сообщения с основной карточкой заказа
    в рабочей теме мастеров.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            UPDATE orders
            SET
                order_card_message_id = ?,
                updated_at = datetime('now', '+3 hours')
            WHERE id = ?
            """,
            (
                order_card_message_id,
                order_id,
            ),
        )

        await db.commit()

        return cursor.rowcount > 0


async def update_order_status(
    order_id: int,
    old_status: str,
    new_status: str,
    *,
    actor_telegram_id: int | None = None,
    expected_assignment_generation: int | None = None,
) -> bool:
    """
    Обновляет статус заказа только в том случае, если текущий
    статус в базе совпадает с old_status.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute("BEGIN IMMEDIATE")
        if actor_telegram_id is not None and not await assignment_actor_allowed_in_tx(
            db, order_id, actor_telegram_id, expected_assignment_generation
        ):
            await db.rollback()
            return False

        cursor = await db.execute(
            """
            UPDATE orders
            SET
                status = ?,
                updated_at = datetime('now', '+3 hours')
            WHERE
                id = ?
                AND status = ?
            """,
            (
                new_status,
                order_id,
                old_status,
            ),
        )

        await db.commit()
        return cursor.rowcount > 0


async def confirm_order_completion_by_client(
    order_id: int,
    client_telegram_id: int,
) -> bool:
    """
    Финально завершает заказ только по подтверждению его клиента.

    Переход waiting_client -> completed и возможное начисление
    реферального бонуса выполняются в одной транзакции.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT
                    orders.id,
                    orders.user_id,
                    orders.status,
                    orders.agreed_price,
                    orders.assigned_master_telegram_id
                FROM orders
                JOIN users
                    ON users.id = orders.user_id
                WHERE
                    orders.id = ?
                    AND users.telegram_id = ?
                LIMIT 1
                """,
                (order_id, client_telegram_id),
            )
            order = await cursor.fetchone()
            await cursor.close()

            if (
                order is None
                or str(order["status"]) != ORDER_STATUS_WAITING_CLIENT
                or order["assigned_master_telegram_id"] is None
            ):
                await db.rollback()
                return False

            cursor = await db.execute(
                """
                UPDATE orders
                SET
                    status = ?,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    id = ?
                    AND status = ?
                    AND user_id = ?
                    AND assigned_master_telegram_id IS NOT NULL
                """,
                (
                    ORDER_STATUS_COMPLETED,
                    order_id,
                    ORDER_STATUS_WAITING_CLIENT,
                    int(order["user_id"]),
                ),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return False
            await cursor.close()

            agreed_price = order["agreed_price"]
            if agreed_price is not None:
                await grant_referral_reward_for_completed_order_in_tx(
                    db,
                    order_id=order_id,
                    referred_user_id=int(order["user_id"]),
                    agreed_price=int(agreed_price),
                )

            await db.commit()
            return True

        except Exception:
            await db.rollback()
            raise


async def return_order_to_work_by_client(
    order_id: int,
    client_telegram_id: int,
) -> bool:
    """
    Возвращает готовую работу исполнителю на доработку.

    Переход разрешён исключительно:
    waiting_client -> in_progress.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            UPDATE orders
            SET
                status = ?,
                updated_at = datetime('now', '+3 hours')
            WHERE
                id = ?
                AND status = ?
                AND user_id = (
                    SELECT id
                    FROM users
                    WHERE telegram_id = ?
                    LIMIT 1
                )
                AND assigned_master_telegram_id IS NOT NULL
            """,
            (
                ORDER_STATUS_IN_PROGRESS,
                order_id,
                ORDER_STATUS_WAITING_CLIENT,
                client_telegram_id,
            ),
        )

        await db.commit()

        return cursor.rowcount > 0


async def assign_order_to_master(
    order_id: int,
    master_telegram_id: int,
) -> bool:
    """
    Атомарно закрепляет новый заказ за мастером и переводит его
    в статус in_progress.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            UPDATE orders
            SET
                assigned_master_telegram_id = ?,
                status = ?,
                assignment_generation = assignment_generation + 1,
                updated_at = datetime('now', '+3 hours')
            WHERE
                id = ?
                AND status = ?
                AND assigned_master_telegram_id IS NULL
                AND EXISTS (
                    SELECT 1 FROM masters m
                    JOIN master_directions d ON d.master_telegram_id = m.telegram_id
                    WHERE m.telegram_id = ? AND m.is_active = 1 AND m.deleted_at IS NULL
                    AND d.direction = orders.direction
                    AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = m.telegram_id)
                )
            """,
            (
                master_telegram_id,
                ORDER_STATUS_IN_PROGRESS,
                order_id,
                ORDER_STATUS_NEW,
                master_telegram_id,
            ),
        )

        await db.commit()
        return cursor.rowcount > 0


async def get_order_by_id(
    order_id: int,
) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT *
            FROM orders
            WHERE id = ?
            LIMIT 1
            """,
            (order_id,),
        )

        return await cursor.fetchone()


async def get_user_order(
    order_id: int,
    user_id: int,
) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT *
            FROM orders
            WHERE
                id = ?
                AND user_id = ?
            LIMIT 1
            """,
            (
                order_id,
                user_id,
            ),
        )

        return await cursor.fetchone()


async def get_assignment_generation(
    order_id: int,
) -> int | None:
    """
    Возвращает текущую генерацию назначения заказа (ORD-02).

    Генерация монотонно растёт при каждом изменении закрепления:
    новое закрепление исполнителя, отказ или высвобождение заказа.
    Вызывающий код фиксирует значение перед длительной Telegram-
    операцией и передаёт его в компенсацию, которая действует
    только внутри той же генерации (exact compare-and-set).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT assignment_generation
            FROM orders
            WHERE id = ?
            LIMIT 1
            """,
            (order_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row is not None else None


async def get_order_by_topic_id(
    group_chat_id: int,
    topic_id: int,
) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                orders.*,
                users.telegram_id AS client_telegram_id
            FROM orders
            JOIN users
                ON users.id = orders.user_id
            WHERE
                orders.group_chat_id = ?
                AND orders.topic_id = ?
            LIMIT 1
            """,
            (
                group_chat_id,
                topic_id,
            ),
        )

        return await cursor.fetchone()


async def get_order_with_client(
    order_id: int,
) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                orders.*,
                users.telegram_id AS client_telegram_id
            FROM orders
            JOIN users
                ON users.id = orders.user_id
            WHERE orders.id = ?
            LIMIT 1
            """,
            (order_id,),
        )

        return await cursor.fetchone()


async def get_order_status(
    order_id: int,
) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT status
            FROM orders
            WHERE id = ?
            LIMIT 1
            """,
            (order_id,),
        )

        row = await cursor.fetchone()
        return None if row is None else str(row["status"])


async def get_active_orders_by_user_id(
    user_id: int,
    limit: int = 10,
    offset: int = 0,
) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            SELECT *
            FROM orders
            WHERE
                user_id = ?
                AND status IN (
                    'new',
                    'in_progress',
                    'waiting_client'
                )
            ORDER BY
                created_at DESC,
                id DESC
            LIMIT ?
            OFFSET ?
            """,
            (
                user_id,
                limit,
                offset,
            ),
        )

        orders = await cursor.fetchall()
        await cursor.close()

        return list(orders)


async def get_active_orders_count_by_user_id(
    user_id: int,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE
                user_id = ?
                AND status IN (
                    'new',
                    'in_progress',
                    'waiting_client'
                )
            """,
            (
                user_id,
            ),
        )

        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return 0

        return int(row[0])

async def get_completed_orders_by_user_id(
    user_id: int,
    limit: int = 10,
    offset: int = 0,
) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            SELECT
                orders.*,
                reviews.id AS review_id,
                reviews.rating AS review_rating,
                reviews.review_text AS review_text
            FROM orders
            LEFT JOIN reviews
                ON reviews.order_id = orders.id
            WHERE
                orders.user_id = ?
                AND orders.status IN (
                    'completed',
                    'closed'
                )
            ORDER BY
                orders.updated_at DESC,
                orders.id DESC
            LIMIT ?
            OFFSET ?
            """,
            (
                user_id,
                limit,
                offset,
            ),
        )

        rows = await cursor.fetchall()
        await cursor.close()

        return list(rows)


async def get_completed_orders_count_by_user_id(
    user_id: int,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE
                user_id = ?
                AND status IN (
                    'completed',
                    'closed'
                )
            """,
            (
                user_id,
            ),
        )

        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return 0

        return int(row[0])

















async def save_or_update_master(
    telegram_id: int,
    username: str | None,
    full_name: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        await db.execute(
            """
            INSERT INTO masters (
                telegram_id,
                username,
                full_name,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 1, datetime('now', '+3 hours'), datetime('now', '+3 hours'))

            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                is_active = 1,
                updated_at = datetime('now', '+3 hours')
            """,
            (
                telegram_id,
                username,
                full_name,
            ),
        )

        await db.commit()




async def _upsert_master_attempt(
    db: aiosqlite.Connection,
    order_id: int,
    master_telegram_id: int,
    outcome: str,
    reason: str | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO order_master_attempts (
            order_id,
            master_telegram_id,
            outcome,
            reason,
            created_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?,
            datetime('now', '+3 hours'),
            datetime('now', '+3 hours')
        )
        ON CONFLICT(order_id, master_telegram_id)
        DO UPDATE SET
            outcome = excluded.outcome,
            reason = excluded.reason,
            updated_at = datetime('now', '+3 hours')
        """,
        (
            order_id,
            master_telegram_id,
            outcome,
            reason,
        ),
    )


async def get_excluded_master_ids(
    order_id: int,
) -> set[int]:
    """
    Возвращает мастеров, с которыми по этому заказу
    уже не удалось продолжить работу.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT master_telegram_id
            FROM order_master_attempts
            WHERE
                order_id = ?
                AND outcome IN ('declined', 'negotiation_failed')
            """,
            (order_id,),
        )

        rows = await cursor.fetchall()
        await cursor.close()
        return {int(row[0]) for row in rows}


async def cancel_new_order_by_master(
    order_id: int,
    master_telegram_id: int,
    decline_reason: str | None = None,
    *,
    expected_assignment_generation: int | None = None,
) -> bool:
    """
    Освобождает новый заказ после отказа назначенного мастера.

    Заказ НЕ отменяется: он остаётся new и переводится в
    waiting_selection, чтобы клиент мог выбрать другого мастера,
    включить режим «Свободный мастер» или отменить заказ сам.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            if expected_assignment_generation is not None and not await assignment_actor_allowed_in_tx(
                db, order_id, master_telegram_id, expected_assignment_generation
            ):
                await db.rollback()
                return False

            cursor = await db.execute(
                """
                UPDATE orders
                SET
                    assigned_master_telegram_id = NULL,
                    assignment_status = ?,
                    price_status = 'waiting_master',
                    agreed_price = NULL,
                    group_chat_id = NULL,
                    topic_id = NULL,
                    order_card_message_id = NULL,
                    assignment_generation = assignment_generation + 1,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    id = ?
                    AND assigned_master_telegram_id = ?
                    AND assignment_status = ?
                    AND status = ?
                """,
                (
                    ASSIGNMENT_STATUS_WAITING_SELECTION,
                    order_id,
                    master_telegram_id,
                    ASSIGNMENT_STATUS_ASSIGNED,
                    ORDER_STATUS_NEW,
                ),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return False

            await cursor.close()

            await db.execute(
                """
                UPDATE order_price_offers
                SET status = 'rejected'
                WHERE order_id = ? AND status = 'pending'
                """,
                (order_id,),
            )

            await _upsert_master_attempt(
                db,
                order_id,
                master_telegram_id,
                "declined",
                (
                    decline_reason.strip()
                    if decline_reason and decline_reason.strip()
                    else "master_declined_order"
                ),
            )

            await db.commit()
            return True

        except Exception:
            await db.rollback()
            raise


async def assign_waiting_order_to_master(
    order_id: int,
    client_telegram_id: int,
    master_telegram_id: int,
    *,
    capture_generation: bool = False,
) -> bool | tuple[bool, int | None]:
    """
    Атомарно закрепляет существующий новый заказ за новым исполнителем.

    Допускается после failed negotiation или из режима open.
    Кандидат должен:
    - быть активным;
    - иметь направление заказа;
    - не находиться среди уже неудачных попыток этого заказа.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                UPDATE orders
                SET
                    assigned_master_telegram_id = ?,
                    assignment_status = ?,
                    price_status = 'waiting_master',
                    agreed_price = NULL,
                    group_chat_id = NULL,
                    topic_id = NULL,
                    order_card_message_id = NULL,
                    assignment_generation = assignment_generation + 1,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    id = ?
                    AND user_id = (
                        SELECT id FROM users WHERE telegram_id = ?
                    )
                    AND status = ?
                    AND assignment_status IN (?, ?)
                    AND assigned_master_telegram_id IS NULL
                    AND NOT EXISTS (
                        SELECT 1
                        FROM order_master_attempts attempts
                        WHERE
                            attempts.order_id = orders.id
                            AND attempts.master_telegram_id = ?
                            AND attempts.outcome IN (
                                'declined',
                                'negotiation_failed'
                            )
                    )
                    AND EXISTS (
                        SELECT 1
                        FROM masters candidate
                        JOIN master_directions directions
                            ON directions.master_telegram_id =
                               candidate.telegram_id
                        WHERE
                            candidate.telegram_id = ?
                            AND candidate.is_active = 1
                            AND candidate.deleted_at IS NULL
                            AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = candidate.telegram_id)
                            AND directions.direction = orders.direction
                    )
                """,
                (
                    master_telegram_id,
                    ASSIGNMENT_STATUS_ASSIGNED,
                    order_id,
                    client_telegram_id,
                    ORDER_STATUS_NEW,
                    ASSIGNMENT_STATUS_WAITING_SELECTION,
                    ASSIGNMENT_STATUS_OPEN,
                    master_telegram_id,
                    master_telegram_id,
                ),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return (False, None) if capture_generation else False

            await cursor.close()

            await _upsert_master_attempt(
                db,
                order_id,
                master_telegram_id,
                "selected",
                "client_reselected",
            )

            cursor = await db.execute("SELECT assignment_generation FROM orders WHERE id=?", (order_id,))
            generation = int((await cursor.fetchone())[0])
            await cursor.close()
            await db.commit()
            return (True, generation) if capture_generation else True

        except Exception:
            await db.rollback()
            raise

async def set_order_open_for_free_master(
    order_id: int,
    client_telegram_id: int,
    *,
    capture_generation: bool = False,
) -> bool | tuple[bool, int | None]:
    """
    Переводит заказ в режим «Свободный исполнитель».

    Открытие возможно только если существует хотя бы один активный
    исполнитель нужного направления, который ещё не был исключён
    из этого заказа.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            UPDATE orders
            SET
                assigned_master_telegram_id = NULL,
                assignment_status = ?,
                price_status = 'waiting_master',
                agreed_price = NULL,
                group_chat_id = NULL,
                topic_id = NULL,
                order_card_message_id = NULL,
                assignment_generation = assignment_generation + 1,
                updated_at = datetime('now', '+3 hours')
            WHERE
                id = ?
                AND user_id = (
                    SELECT id FROM users WHERE telegram_id = ?
                )
                AND status = ?
                AND assignment_status = ?
                AND assigned_master_telegram_id IS NULL
                AND EXISTS (
                    SELECT 1
                    FROM masters candidate
                    JOIN master_directions directions
                        ON directions.master_telegram_id =
                           candidate.telegram_id
                    WHERE
                        candidate.is_active = 1
                        AND candidate.deleted_at IS NULL
                        AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = candidate.telegram_id)
                        AND directions.direction = orders.direction
                        AND NOT EXISTS (
                            SELECT 1
                            FROM order_master_attempts attempts
                            WHERE
                                attempts.order_id = orders.id
                                AND attempts.master_telegram_id =
                                    candidate.telegram_id
                                AND attempts.outcome IN (
                                    'declined',
                                    'negotiation_failed'
                                )
                        )
                )
            """,
            (
                ASSIGNMENT_STATUS_OPEN,
                order_id,
                client_telegram_id,
                ORDER_STATUS_NEW,
                ASSIGNMENT_STATUS_WAITING_SELECTION,
            ),
        )

        changed = cursor.rowcount > 0
        await cursor.close()
        generation = None
        if changed and capture_generation:
            cursor = await db.execute("SELECT assignment_generation FROM orders WHERE id=?", (order_id,))
            generation = int((await cursor.fetchone())[0])
            await cursor.close()
        await db.commit()
        return (changed, generation) if capture_generation else changed

async def claim_open_order(
    order_id: int,
    master_telegram_id: int,
) -> bool:
    """
    Первый подходящий исполнитель атомарно забирает свободный заказ.

    Помимо активности и отсутствия прошлой неудачной попытки,
    исполнитель обязан иметь направление этого заказа.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                UPDATE orders
                SET
                    assigned_master_telegram_id = ?,
                    assignment_status = ?,
                    price_status = 'waiting_master',
                    assignment_generation = assignment_generation + 1,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    id = ?
                    AND status = ?
                    AND assignment_status = ?
                    AND assigned_master_telegram_id IS NULL
                    AND EXISTS (
                        SELECT 1
                        FROM masters candidate
                        JOIN master_directions directions
                            ON directions.master_telegram_id =
                               candidate.telegram_id
                        WHERE
                            candidate.telegram_id = ?
                            AND candidate.is_active = 1
                            AND candidate.deleted_at IS NULL
                            AND NOT EXISTS (SELECT 1 FROM banned_telegram_users b WHERE b.telegram_id = candidate.telegram_id)
                            AND directions.direction = orders.direction
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM order_master_attempts attempts
                        WHERE
                            attempts.order_id = orders.id
                            AND attempts.master_telegram_id = ?
                            AND attempts.outcome IN (
                                'declined',
                                'negotiation_failed'
                            )
                    )
                """,
                (
                    master_telegram_id,
                    ASSIGNMENT_STATUS_ASSIGNED,
                    order_id,
                    ORDER_STATUS_NEW,
                    ASSIGNMENT_STATUS_OPEN,
                    master_telegram_id,
                    master_telegram_id,
                ),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return False

            await cursor.close()

            await _upsert_master_attempt(
                db,
                order_id,
                master_telegram_id,
                "claimed",
                "free_master_claim",
            )

            await db.commit()
            return True

        except Exception:
            await db.rollback()
            raise

async def cancel_unpaid_order_by_client(
    order_id: int,
    client_telegram_id: int,
) -> bool:
    """
    Окончательно отменяет неоплаченный заказ по решению клиента.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                UPDATE orders
                SET
                    status = ?,
                    assignment_status = ?,
                    price_status = 'rejected',
                    assigned_master_telegram_id = NULL,
                    assignment_generation = assignment_generation + 1,
                    agreed_price = NULL,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    id = ?
                    AND user_id = (
                        SELECT id FROM users WHERE telegram_id = ?
                    )
                    AND status = ?
                """,
                (
                    ORDER_STATUS_CANCELLED,
                    ASSIGNMENT_STATUS_CANCELLED,
                    order_id,
                    client_telegram_id,
                    ORDER_STATUS_NEW,
                ),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return False

            await cursor.close()

            await db.execute(
                """
                UPDATE order_price_offers
                SET status = 'rejected'
                WHERE order_id = ? AND status = 'pending'
                """,
                (order_id,),
            )

            await db.commit()
            return True

        except Exception:
            await db.rollback()
            raise


async def release_assignment_after_topic_failure(
    order_id: int,
    master_telegram_id: int | None,
    captured_assignment_generation: int | None = None,
) -> bool:
    """
    Возвращает заказ к выбору исполнителя, если Telegram-тему
    для нового назначения создать не удалось (ORD-02).

    Компенсация действует строго внутри зафиксированной генерации
    назначения (exact compare-and-set): она меняет заказ только если
    текущий статус и закрепление совпадают с генерацией, в которой
    произошёл сбой. Поздний сбой старой попытки после перевыбора
    (или после повтора выбора того же исполнителя) не затрагивает
    более новую генерацию.

    Возвращает True, если генерация была высвобождена.
    """
    if captured_assignment_generation is None:
        raise ValueError(
            "Компенсация сбоя создания темы требует зафиксированной "
            "генерации назначения (captured_assignment_generation)."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        db.row_factory = aiosqlite.Row

        try:
            await db.execute("BEGIN IMMEDIATE")

            if master_telegram_id is not None:
                cursor = await db.execute(
                    """
                    UPDATE orders
                    SET
                        assigned_master_telegram_id = NULL,
                        assignment_status = ?,
                        price_status = 'waiting_master',
                        agreed_price = NULL,
                        group_chat_id = NULL,
                        topic_id = NULL,
                        order_card_message_id = NULL,
                        assignment_generation = assignment_generation + 1,
                        updated_at = datetime('now', '+3 hours')
                    WHERE
                        id = ?
                        AND status = ?
                        AND assigned_master_telegram_id = ?
                        AND assignment_generation = ?
                        AND topic_id IS NULL
                        AND NOT EXISTS (SELECT 1 FROM order_topic_checkpoints c WHERE c.order_id=orders.id AND c.assignment_generation=orders.assignment_generation)
                    """,
                    (
                        ASSIGNMENT_STATUS_WAITING_SELECTION,
                        order_id,
                        ORDER_STATUS_NEW,
                        master_telegram_id,
                        captured_assignment_generation,
                    ),
                )
            else:
                cursor = await db.execute(
                    """
                    UPDATE orders
                    SET
                        group_chat_id = NULL,
                        topic_id = NULL,
                        order_card_message_id = NULL,
                        assignment_generation = assignment_generation + 1,
                        updated_at = datetime('now', '+3 hours')
                    WHERE
                        id = ?
                        AND status = ?
                        AND assignment_status = ?
                        AND assigned_master_telegram_id IS NULL
                        AND assignment_generation = ?
                        AND topic_id IS NULL
                        AND NOT EXISTS (SELECT 1 FROM order_topic_checkpoints c WHERE c.order_id=orders.id AND c.assignment_generation=orders.assignment_generation)
                    """,
                    (
                        order_id,
                        ORDER_STATUS_NEW,
                        ASSIGNMENT_STATUS_OPEN,
                        captured_assignment_generation,
                    ),
                )

            released = cursor.rowcount > 0
            await cursor.close()

            if released and master_telegram_id is not None:
                await db.execute(
                    """
                    DELETE FROM order_master_attempts
                    WHERE
                        order_id = ?
                        AND master_telegram_id = ?
                        AND outcome IN ('selected', 'claimed')
                    """,
                    (order_id, master_telegram_id),
                )

            await db.commit()
            return released

        except Exception:
            await db.rollback()
            raise
