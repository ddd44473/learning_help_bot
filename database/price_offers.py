import aiosqlite

from database.db import DB_PATH

from utils.order_statuses import (
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_NEW,
)


PRICE_STATUS_WAITING_MASTER = "waiting_master"
PRICE_STATUS_WAITING_CLIENT = "waiting_client"
PRICE_STATUS_WAITING_MASTER_RESPONSE = (
    "waiting_master_response"
)
PRICE_STATUS_AGREED = "agreed"
PRICE_STATUS_REJECTED = "rejected"

OFFERED_BY_MASTER = "master"
OFFERED_BY_CLIENT = "client"

OFFER_STATUS_PENDING = "pending"
OFFER_STATUS_ACCEPTED = "accepted"
OFFER_STATUS_REJECTED = "rejected"
OFFER_STATUS_SUPERSEDED = "superseded"


async def create_price_offer(
    order_id: int,
    offered_by: str,
    offered_by_telegram_id: int,
    amount: int,
    expected_current_price_status: str,
    new_price_status: str,
) -> int | None:
    """
    Атомарно создаёт новое ценовое предложение.

    Дополнительно проверяет права инициатора:
    - клиент может предлагать цену только по своему заказу;
    - мастер может предлагать цену только по заказу,
      который закреплён за ним.

    Предыдущее ожидающее предложение становится superseded.

    Возвращает ID нового предложения либо None,
    если:
    - заказ не найден;
    - инициатор не имеет доступа к заказу;
    - состояние переговоров уже изменилось;
    - заказ больше не находится в статусе new.
    """

    if offered_by not in {
        OFFERED_BY_MASTER,
        OFFERED_BY_CLIENT,
    }:
        raise ValueError(
            "Некорректная сторона ценового предложения."
        )

    if amount <= 0:
        raise ValueError(
            "Стоимость должна быть больше нуля."
        )

    allowed_new_statuses = {
        PRICE_STATUS_WAITING_CLIENT,
        PRICE_STATUS_WAITING_MASTER_RESPONSE,
    }

    if new_price_status not in allowed_new_statuses:
        raise ValueError(
            "Некорректное новое состояние переговоров."
        )

    # Проверяем, что переход состояния соответствует стороне.
    if (
        offered_by == OFFERED_BY_MASTER
        and new_price_status
        != PRICE_STATUS_WAITING_CLIENT
    ):
        raise ValueError(
            "После предложения мастера "
            "должен ожидаться ответ клиента."
        )

    if (
        offered_by == OFFERED_BY_CLIENT
        and new_price_status
        != PRICE_STATUS_WAITING_MASTER_RESPONSE
    ):
        raise ValueError(
            "После предложения клиента "
            "должен ожидаться ответ мастера."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        try:
            await db.execute(
                "BEGIN IMMEDIATE"
            )

            # -------------------------------------------------
            # 1. Проверяем права инициатора
            # -------------------------------------------------

            if offered_by == OFFERED_BY_CLIENT:
                cursor = await db.execute(
                    """
                    SELECT orders.id
                    FROM orders
                    JOIN users
                        ON users.id = orders.user_id
                    WHERE
                        orders.id = ?
                        AND users.telegram_id = ?
                        AND orders.status = 'new'
                        AND orders.price_status = ?
                        AND orders.agreed_price IS NULL
                    LIMIT 1
                    """,
                    (
                        order_id,
                        offered_by_telegram_id,
                        expected_current_price_status,
                    ),
                )

            else:
                cursor = await db.execute(
                    """
                    SELECT id
                    FROM orders
                    WHERE
                        id = ?
                        AND assigned_master_telegram_id = ?
                        AND status = 'new'
                        AND price_status = ?
                        AND agreed_price IS NULL
                    LIMIT 1
                    """,
                    (
                        order_id,
                        offered_by_telegram_id,
                        expected_current_price_status,
                    ),
                )

            allowed_order = await cursor.fetchone()
            await cursor.close()

            if allowed_order is None:
                await db.rollback()
                return None

            # -------------------------------------------------
            # 2. Меняем состояние переговоров
            # -------------------------------------------------

            if offered_by == OFFERED_BY_CLIENT:
                cursor = await db.execute(
                    """
                    UPDATE orders
                    SET
                        price_status = ?,
                        updated_at = datetime('now', '+3 hours')
                    WHERE
                        id = ?
                        AND user_id = (
                            SELECT id
                            FROM users
                            WHERE telegram_id = ?
                        )
                        AND status = 'new'
                        AND price_status = ?
                        AND agreed_price IS NULL
                    """,
                    (
                        new_price_status,
                        order_id,
                        offered_by_telegram_id,
                        expected_current_price_status,
                    ),
                )

            else:
                cursor = await db.execute(
                    """
                    UPDATE orders
                    SET
                        price_status = ?,
                        updated_at = datetime('now', '+3 hours')
                    WHERE
                        id = ?
                        AND assigned_master_telegram_id = ?
                        AND status = 'new'
                        AND price_status = ?
                        AND agreed_price IS NULL
                    """,
                    (
                        new_price_status,
                        order_id,
                        offered_by_telegram_id,
                        expected_current_price_status,
                    ),
                )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return None

            await cursor.close()

            # -------------------------------------------------
            # 3. Старое pending-предложение
            #    больше не является актуальным
            # -------------------------------------------------

            await db.execute(
                """
                UPDATE order_price_offers
                SET status = ?
                WHERE
                    order_id = ?
                    AND status = ?
                """,
                (
                    OFFER_STATUS_SUPERSEDED,
                    order_id,
                    OFFER_STATUS_PENDING,
                ),
            )

            # -------------------------------------------------
            # 4. Создаём новое предложение
            # -------------------------------------------------

            cursor = await db.execute(
                """
                INSERT INTO order_price_offers (
                    order_id,
                    offered_by,
                    offered_by_telegram_id,
                    amount,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, datetime('now', '+3 hours'))
                """,
                (
                    order_id,
                    offered_by,
                    offered_by_telegram_id,
                    amount,
                    OFFER_STATUS_PENDING,
                ),
            )

            offer_id = cursor.lastrowid
            await cursor.close()

            if offer_id is None:
                raise RuntimeError(
                    "Не удалось получить ID "
                    "ценового предложения."
                )

            await db.commit()

            return int(
                offer_id
            )

        except Exception:
            await db.rollback()
            raise


async def get_latest_price_offer(
    order_id: int,
) -> aiosqlite.Row | None:
    """
    Возвращает последнее ценовое предложение по заказу.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                id,
                order_id,
                offered_by,
                offered_by_telegram_id,
                amount,
                status,
                created_at
            FROM order_price_offers
            WHERE order_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (order_id,),
        )

        offer = await cursor.fetchone()
        await cursor.close()

        return offer


async def get_pending_price_offer(
    order_id: int,
) -> aiosqlite.Row | None:
    """
    Возвращает актуальное предложение,
    ожидающее ответа второй стороны.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                id,
                order_id,
                offered_by,
                offered_by_telegram_id,
                amount,
                status,
                created_at
            FROM order_price_offers
            WHERE
                order_id = ?
                AND status = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                order_id,
                OFFER_STATUS_PENDING,
            ),
        )

        offer = await cursor.fetchone()
        await cursor.close()

        return offer


async def accept_price_offer(
    order_id: int,
    offer_id: int,
    accepted_by: str,
    accepted_by_telegram_id: int,
    expected_price_status: str,
) -> tuple[bool, int | None]:
    """
    Атомарно принимает актуальное ценовое предложение.

    Проверяет:
    - заказ существует;
    - предложение относится к этому заказу;
    - предложение всё ещё pending;
    - заказ находится в статусе new;
    - состояние переговоров не изменилось;
    - итоговая цена ещё не согласована;
    - предложение создано противоположной стороной;
    - принимающий пользователь действительно имеет
      право принимать цену по этому заказу.

    Клиент может принять только предложение мастера
    по своему заказу.

    Мастер может принять только встречное предложение
    клиента по закреплённому за ним заказу.

    При успешном принятии:
    - сохраняет сумму в orders.agreed_price;
    - переводит price_status в agreed;
    - помечает предложение как accepted.

    Возвращает:
    (
        True,
        agreed_price,
    )

    либо:
    (
        False,
        None,
    )
    """

    if accepted_by not in {
        OFFERED_BY_CLIENT,
        OFFERED_BY_MASTER,
    }:
        raise ValueError(
            "Некорректная сторона, "
            "принимающая предложение."
        )

    # -------------------------------------------------
    # Проверяем допустимый сценарий переговоров.
    # -------------------------------------------------

    if accepted_by == OFFERED_BY_CLIENT:
        expected_offer_side = (
            OFFERED_BY_MASTER
        )

        required_price_status = (
            PRICE_STATUS_WAITING_CLIENT
        )

    else:
        expected_offer_side = (
            OFFERED_BY_CLIENT
        )

        required_price_status = (
            PRICE_STATUS_WAITING_MASTER_RESPONSE
        )

    if (
        expected_price_status
        != required_price_status
    ):
        raise ValueError(
            "Некорректное состояние переговоров "
            "для принимающей стороны."
        )

    async with aiosqlite.connect(
        DB_PATH
    ) as db:
        db.row_factory = aiosqlite.Row

        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        try:
            await db.execute(
                "BEGIN IMMEDIATE"
            )

            # -------------------------------------------------
            # 1. Проверяем предложение, заказ
            #    и права принимающей стороны.
            # -------------------------------------------------

            if accepted_by == OFFERED_BY_CLIENT:
                cursor = await db.execute(
                    """
                    SELECT
                        order_price_offers.amount
                    FROM order_price_offers

                    JOIN orders
                        ON orders.id =
                           order_price_offers.order_id

                    JOIN users
                        ON users.id =
                           orders.user_id

                    WHERE
                        order_price_offers.id = ?
                        AND order_price_offers.order_id = ?
                        AND order_price_offers.status = ?
                        AND order_price_offers.offered_by = ?

                        AND users.telegram_id = ?

                        AND orders.status = 'new'
                        AND orders.price_status = ?
                        AND orders.agreed_price IS NULL

                    LIMIT 1
                    """,
                    (
                        offer_id,
                        order_id,
                        OFFER_STATUS_PENDING,
                        expected_offer_side,
                        accepted_by_telegram_id,
                        expected_price_status,
                    ),
                )

            else:
                cursor = await db.execute(
                    """
                    SELECT
                        order_price_offers.amount
                    FROM order_price_offers

                    JOIN orders
                        ON orders.id =
                           order_price_offers.order_id

                    WHERE
                        order_price_offers.id = ?
                        AND order_price_offers.order_id = ?
                        AND order_price_offers.status = ?
                        AND order_price_offers.offered_by = ?

                        AND orders.assigned_master_telegram_id = ?

                        AND orders.status = 'new'
                        AND orders.price_status = ?
                        AND orders.agreed_price IS NULL

                    LIMIT 1
                    """,
                    (
                        offer_id,
                        order_id,
                        OFFER_STATUS_PENDING,
                        expected_offer_side,
                        accepted_by_telegram_id,
                        expected_price_status,
                    ),
                )

            offer = await cursor.fetchone()
            await cursor.close()

            if offer is None:
                await db.rollback()

                return (
                    False,
                    None,
                )

            amount = int(
                offer["amount"]
            )

            # -------------------------------------------------
            # 2. Фиксируем согласованную стоимость.
            #
            # Повторяем ownership-проверку в самом UPDATE,
            # чтобы операция оставалась защищённой даже
            # при конкурентном изменении данных.
            # -------------------------------------------------

            if accepted_by == OFFERED_BY_CLIENT:
                cursor = await db.execute(
                    """
                    UPDATE orders
                    SET
                        agreed_price = ?,
                        price_status = ?,
                        updated_at = datetime('now', '+3 hours')
                    WHERE
                        id = ?

                        AND user_id = (
                            SELECT id
                            FROM users
                            WHERE telegram_id = ?
                        )

                        AND status = 'new'
                        AND price_status = ?
                        AND agreed_price IS NULL
                    """,
                    (
                        amount,
                        PRICE_STATUS_AGREED,
                        order_id,
                        accepted_by_telegram_id,
                        expected_price_status,
                    ),
                )

            else:
                cursor = await db.execute(
                    """
                    UPDATE orders
                    SET
                        agreed_price = ?,
                        price_status = ?,
                        updated_at = datetime('now', '+3 hours')
                    WHERE
                        id = ?

                        AND assigned_master_telegram_id = ?

                        AND status = 'new'
                        AND price_status = ?
                        AND agreed_price IS NULL
                    """,
                    (
                        amount,
                        PRICE_STATUS_AGREED,
                        order_id,
                        accepted_by_telegram_id,
                        expected_price_status,
                    ),
                )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()

                return (
                    False,
                    None,
                )

            await cursor.close()

            # -------------------------------------------------
            # 3. Помечаем именно это предложение
            #    как принятое.
            # -------------------------------------------------

            cursor = await db.execute(
                """
                UPDATE order_price_offers
                SET status = ?
                WHERE
                    id = ?
                    AND order_id = ?
                    AND offered_by = ?
                    AND status = ?
                """,
                (
                    OFFER_STATUS_ACCEPTED,
                    offer_id,
                    order_id,
                    expected_offer_side,
                    OFFER_STATUS_PENDING,
                ),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()

                return (
                    False,
                    None,
                )

            await cursor.close()

            await db.commit()

            return (
                True,
                amount,
            )

        except Exception:
            await db.rollback()
            raise


async def reject_price_negotiation(
    order_id: int,
    offer_id: int,
    rejected_by: str,
    rejected_by_telegram_id: int,
    expected_price_status: str,
) -> bool:
    """
    Завершает текущие переговоры по цене, но НЕ отменяет заказ.

    После отказа заказ остаётся new, текущий мастер освобождается,
    а клиенту можно предложить другого или режим «Свободный мастер».
    """

    if rejected_by not in {OFFERED_BY_CLIENT, OFFERED_BY_MASTER}:
        raise ValueError("Некорректная сторона, отклоняющая предложение.")

    if rejected_by == OFFERED_BY_CLIENT:
        expected_offer_side = OFFERED_BY_MASTER
        required_price_status = PRICE_STATUS_WAITING_CLIENT
    else:
        expected_offer_side = OFFERED_BY_CLIENT
        required_price_status = PRICE_STATUS_WAITING_MASTER_RESPONSE

    if expected_price_status != required_price_status:
        raise ValueError(
            "Некорректное состояние переговоров для отклоняющей стороны."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            if rejected_by == OFFERED_BY_CLIENT:
                cursor = await db.execute(
                    """
                    SELECT orders.assigned_master_telegram_id AS master_id
                    FROM orders
                    JOIN users ON users.id = orders.user_id
                    JOIN order_price_offers
                        ON order_price_offers.order_id = orders.id
                    WHERE
                        orders.id = ?
                        AND users.telegram_id = ?
                        AND orders.status = 'new'
                        AND orders.assignment_status = 'assigned'
                        AND orders.price_status = ?
                        AND orders.agreed_price IS NULL
                        AND order_price_offers.id = ?
                        AND order_price_offers.status = ?
                        AND order_price_offers.offered_by = ?
                    LIMIT 1
                    """,
                    (
                        order_id,
                        rejected_by_telegram_id,
                        expected_price_status,
                        offer_id,
                        OFFER_STATUS_PENDING,
                        expected_offer_side,
                    ),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT orders.assigned_master_telegram_id AS master_id
                    FROM orders
                    JOIN order_price_offers
                        ON order_price_offers.order_id = orders.id
                    WHERE
                        orders.id = ?
                        AND orders.assigned_master_telegram_id = ?
                        AND orders.status = 'new'
                        AND orders.assignment_status = 'assigned'
                        AND orders.price_status = ?
                        AND orders.agreed_price IS NULL
                        AND order_price_offers.id = ?
                        AND order_price_offers.status = ?
                        AND order_price_offers.offered_by = ?
                    LIMIT 1
                    """,
                    (
                        order_id,
                        rejected_by_telegram_id,
                        expected_price_status,
                        offer_id,
                        OFFER_STATUS_PENDING,
                        expected_offer_side,
                    ),
                )

            row = await cursor.fetchone()
            await cursor.close()

            if row is None or row["master_id"] is None:
                await db.rollback()
                return False

            master_id = int(row["master_id"])

            cursor = await db.execute(
                """
                UPDATE order_price_offers
                SET status = ?
                WHERE
                    id = ?
                    AND order_id = ?
                    AND offered_by = ?
                    AND status = ?
                """,
                (
                    OFFER_STATUS_REJECTED,
                    offer_id,
                    order_id,
                    expected_offer_side,
                    OFFER_STATUS_PENDING,
                ),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return False
            await cursor.close()

            # Любые остальные старые pending-предложения тоже закрываем.
            await db.execute(
                """
                UPDATE order_price_offers
                SET status = ?
                WHERE order_id = ? AND status = ?
                """,
                (OFFER_STATUS_REJECTED, order_id, OFFER_STATUS_PENDING),
            )

            cursor = await db.execute(
                """
                UPDATE orders
                SET
                    assigned_master_telegram_id = NULL,
                    assignment_generation = assignment_generation + 1,
                    assignment_status = 'waiting_selection',
                    price_status = 'waiting_master',
                    agreed_price = NULL,
                    group_chat_id = NULL,
                    topic_id = NULL,
                    order_card_message_id = NULL,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    id = ?
                    AND status = 'new'
                    AND assignment_status = 'assigned'
                    AND assigned_master_telegram_id = ?
                    AND price_status = ?
                    AND agreed_price IS NULL
                """,
                (order_id, master_id, expected_price_status),
            )

            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return False
            await cursor.close()

            await db.execute(
                """
                INSERT INTO order_master_attempts (
                    order_id, master_telegram_id, outcome, reason,
                    created_at, updated_at
                )
                VALUES (
                    ?, ?, 'negotiation_failed', ?,
                    datetime('now', '+3 hours'),
                    datetime('now', '+3 hours')
                )
                ON CONFLICT(order_id, master_telegram_id)
                DO UPDATE SET
                    outcome = 'negotiation_failed',
                    reason = excluded.reason,
                    updated_at = datetime('now', '+3 hours')
                """,
                (
                    order_id,
                    master_id,
                    (
                        'client_rejected_master_price'
                        if rejected_by == OFFERED_BY_CLIENT
                        else 'master_rejected_client_price'
                    ),
                ),
            )

            await db.commit()
            return True

        except Exception:
            await db.rollback()
            raise


async def get_order_price_offers(
    order_id: int,
) -> list[aiosqlite.Row]:
    """
    Возвращает историю предложений по заказу.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                id,
                order_id,
                offered_by,
                offered_by_telegram_id,
                amount,
                status,
                created_at
            FROM order_price_offers
            WHERE order_id = ?
            ORDER BY id ASC
            """,
            (order_id,),
        )

        offers = await cursor.fetchall()
        await cursor.close()

        return list(offers)


async def reject_client_price_by_master(
    order_id: int,
    offer_id: int,
    master_telegram_id: int,
    expected_price_status: str,
) -> bool:
    """
    Мастер отклоняет встречную цену клиента.

    Это завершает только текущую попытку с данным мастером:
    сам заказ остаётся new и возвращается клиенту на перевыбор.
    """

    return await reject_price_negotiation(
        order_id=order_id,
        offer_id=offer_id,
        rejected_by=OFFERED_BY_MASTER,
        rejected_by_telegram_id=master_telegram_id,
        expected_price_status=expected_price_status,
    )
