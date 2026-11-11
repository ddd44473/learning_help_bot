import aiosqlite

from database.db import DB_PATH
from database.operations import assignment_actor_allowed_in_tx
from database.referrals import (
    BONUS_MAX_ORDER_PERCENT,
    REFERRAL_DISCOUNT_AMOUNT,
    consume_bonus_grants_in_tx,
    expire_bonus_grants_in_tx,
    percent_amount,
    restore_bonus_usage_for_order_in_tx,
)
from utils.order_statuses import (
    ORDER_STATUS_IN_PROGRESS,
    ORDER_STATUS_NEW,
)


PAYMENT_RESULT_SUCCESS = "success"
PAYMENT_RESULT_ORDER_NOT_FOUND = "order_not_found"
PAYMENT_RESULT_NOT_AVAILABLE = "not_available"
PAYMENT_RESULT_INSUFFICIENT_FUNDS = "insufficient_funds"

REFUND_RESULT_SUCCESS = "success"
REFUND_RESULT_ORDER_NOT_FOUND = "order_not_found"
REFUND_RESULT_NOT_AVAILABLE = "not_available"
REFUND_RESULT_ALREADY_REFUNDED = "already_refunded"


def _payment_amounts(
    *,
    price: int,
    balance: int,
    bonus_balance: int,
    referral_eligible: bool,
) -> dict[str, int | bool]:
    referral_discount = (
        min(REFERRAL_DISCOUNT_AMOUNT, price)
        if referral_eligible
        else 0
    )

    cash_without_bonus = max(price - referral_discount, 0)

    max_bonus_available = min(
        bonus_balance,
        percent_amount(price, BONUS_MAX_ORDER_PERCENT),
        cash_without_bonus,
    )

    cash_with_bonus = max(
        price - referral_discount - max_bonus_available,
        0,
    )

    return {
        "price": price,
        "balance": balance,
        "bonus_balance": bonus_balance,
        "referral_discount": referral_discount,
        "max_bonus_available": max_bonus_available,
        "cash_without_bonus": cash_without_bonus,
        "cash_with_bonus": cash_with_bonus,
        "can_pay_without_bonus": balance >= cash_without_bonus,
        "can_pay_with_bonus": (
            max_bonus_available > 0
            and balance >= cash_with_bonus
        ),
    }


async def get_order_payment_preview(
    order_id: int,
    user_id: int,
) -> dict[str, int | bool] | None:
    """Возвращает актуальный расчёт оплаты и действующих бонусов."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")

        await expire_bonus_grants_in_tx(db, user_id)

        cursor = await db.execute(
            """
            SELECT
                orders.id,
                orders.status,
                orders.price_status,
                orders.agreed_price,
                users.balance,
                users.bonus_balance,
                referrals.id AS referral_id,
                referrals.first_discount_order_id,
                CASE
                    WHEN referrals.id IS NOT NULL
                         AND referrals.first_discount_order_id IS NULL
                         AND referrals.created_at > datetime(
                             'now', '+3 hours', '-60 days'
                         )
                    THEN 1
                    ELSE 0
                END AS referral_discount_active
            FROM orders
            JOIN users ON users.id = orders.user_id
            LEFT JOIN referrals
                ON referrals.referred_user_id = users.id
            WHERE orders.id = ? AND orders.user_id = ?
            LIMIT 1
            """,
            (order_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if (
            row is None
            or str(row["status"]) != ORDER_STATUS_NEW
            or str(row["price_status"]) != "agreed"
            or row["agreed_price"] is None
        ):
            await db.rollback()
            return None

        amounts = _payment_amounts(
            price=int(row["agreed_price"]),
            balance=int(row["balance"]),
            bonus_balance=int(row["bonus_balance"]),
            referral_eligible=bool(row["referral_discount_active"]),
        )
        await db.commit()
        return amounts


async def pay_order_from_balance(
    order_id: int,
    user_id: int,
    *,
    use_bonus: bool = False,
) -> tuple[str, int | None, int | None]:
    """
    Атомарно оплачивает заказ.

    Приглашённому автоматически применяется фиксированная скидка 250 ₽
    на первый оплаченный заказ, если 60-дневный срок ещё не истёк.
    Действующими бонусами по выбору клиента можно оплатить максимум 15%
    полной согласованной стоимости заказа. Скидки сервиса не уменьшают
    согласованную стоимость для исполнителя.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")
            await expire_bonus_grants_in_tx(db, user_id)

            cursor = await db.execute(
                """
                SELECT
                    orders.id,
                    orders.user_id,
                    orders.status,
                    orders.price_status,
                    orders.agreed_price,
                    users.balance,
                    users.bonus_balance,
                    referrals.id AS referral_id,
                    referrals.first_discount_order_id,
                    CASE
                        WHEN referrals.id IS NOT NULL
                             AND referrals.first_discount_order_id IS NULL
                             AND referrals.created_at > datetime(
                                 'now', '+3 hours', '-60 days'
                             )
                        THEN 1
                        ELSE 0
                    END AS referral_discount_active
                FROM orders
                JOIN users ON users.id = orders.user_id
                LEFT JOIN referrals
                    ON referrals.referred_user_id = users.id
                WHERE orders.id = ? AND orders.user_id = ?
                LIMIT 1
                """,
                (order_id, user_id),
            )
            order = await cursor.fetchone()
            await cursor.close()

            if order is None:
                await db.rollback()
                return PAYMENT_RESULT_ORDER_NOT_FOUND, None, None

            agreed_price = order["agreed_price"]
            if (
                str(order["status"]) != ORDER_STATUS_NEW
                or str(order["price_status"]) != "agreed"
                or agreed_price is None
            ):
                await db.rollback()
                return (
                    PAYMENT_RESULT_NOT_AVAILABLE,
                    int(order["balance"]),
                    int(agreed_price) if agreed_price is not None else None,
                )

            price = int(agreed_price)
            balance_before = int(order["balance"])
            bonus_before = int(order["bonus_balance"])

            amounts = _payment_amounts(
                price=price,
                balance=balance_before,
                bonus_balance=bonus_before,
                referral_eligible=bool(order["referral_discount_active"]),
            )

            referral_discount = int(amounts["referral_discount"])
            bonus_used = (
                int(amounts["max_bonus_available"])
                if use_bonus
                else 0
            )
            cash_due = max(
                price - referral_discount - bonus_used,
                0,
            )

            if balance_before < cash_due:
                await db.rollback()
                return (
                    PAYMENT_RESULT_INSUFFICIENT_FUNDS,
                    balance_before,
                    cash_due,
                )

            balance_after = balance_before - cash_due
            cursor = await db.execute(
                """
                UPDATE users
                SET balance = ?, updated_at = datetime('now', '+3 hours')
                WHERE id = ? AND balance = ?
                """,
                (balance_after, user_id, balance_before),
            )
            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return PAYMENT_RESULT_NOT_AVAILABLE, None, cash_due
            await cursor.close()

            bonus_after = bonus_before
            if bonus_used > 0:
                consumed, actual_bonus_before, bonus_after = (
                    await consume_bonus_grants_in_tx(
                        db,
                        user_id=user_id,
                        order_id=order_id,
                        amount=bonus_used,
                    )
                )
                if consumed != bonus_used:
                    raise RuntimeError("Списана некорректная сумма бонусов")

                await db.execute(
                    """
                    INSERT INTO bonus_transactions (
                        user_id, amount, bonus_before, bonus_after,
                        transaction_type, order_id, comment, created_at
                    )
                    VALUES (
                        ?, ?, ?, ?, 'payment', ?, ?,
                        datetime('now', '+3 hours')
                    )
                    """,
                    (
                        user_id,
                        -bonus_used,
                        actual_bonus_before,
                        bonus_after,
                        order_id,
                        f"Оплата бонусами заказа №{order_id}",
                    ),
                )

            if cash_due > 0:
                await db.execute(
                    """
                    INSERT INTO balance_transactions (
                        user_id, amount, balance_before, balance_after,
                        transaction_type, admin_telegram_id, order_id, comment,
                        created_at
                    ) VALUES (
                        ?, ?, ?, ?, 'payment', NULL, ?, ?,
                        datetime('now', '+3 hours')
                    )
                    """,
                    (
                        user_id,
                        -cash_due,
                        balance_before,
                        balance_after,
                        order_id,
                        f"Оплата заказа №{order_id}. Средства зарезервированы сервисом",
                    ),
                )

            if bool(order["referral_discount_active"]):
                cursor = await db.execute(
                    """
                    UPDATE referrals
                    SET
                        first_discount_order_id = ?,
                        first_order_discount_amount = ?
                    WHERE id = ? AND first_discount_order_id IS NULL
                    """,
                    (
                        order_id,
                        referral_discount,
                        int(order["referral_id"]),
                    ),
                )
                if cursor.rowcount <= 0:
                    await cursor.close()
                    await db.rollback()
                    return PAYMENT_RESULT_NOT_AVAILABLE, balance_before, cash_due
                await cursor.close()

            cursor = await db.execute(
                """
                UPDATE orders
                SET
                    status = ?,
                    cash_paid_amount = ?,
                    bonus_used = ?,
                    referral_discount = ?,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    id = ?
                    AND user_id = ?
                    AND status = ?
                    AND price_status = 'agreed'
                    AND agreed_price = ?
                """,
                (
                    ORDER_STATUS_IN_PROGRESS,
                    cash_due,
                    bonus_used,
                    referral_discount,
                    order_id,
                    user_id,
                    ORDER_STATUS_NEW,
                    price,
                ),
            )
            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return PAYMENT_RESULT_NOT_AVAILABLE, balance_before, cash_due
            await cursor.close()

            await db.commit()
            return PAYMENT_RESULT_SUCCESS, balance_after, cash_due

        except Exception:
            await db.rollback()
            raise


async def _restore_bonus_and_referral_claim(
    db: aiosqlite.Connection,
    *,
    order_id: int,
    user_id: int,
    bonus_used: int,
    bonus_before: int,
) -> int:
    del bonus_before

    restored = 0
    if bonus_used > 0:
        restored, actual_before, actual_after = (
            await restore_bonus_usage_for_order_in_tx(
                db,
                user_id=user_id,
                order_id=order_id,
                fallback_amount=bonus_used,
            )
        )

        if restored > 0:
            await db.execute(
                """
                INSERT INTO bonus_transactions (
                    user_id, amount, bonus_before, bonus_after,
                    transaction_type, order_id, comment, created_at
                )
                VALUES (
                    ?, ?, ?, ?, 'refund', ?, ?,
                    datetime('now', '+3 hours')
                )
                """,
                (
                    user_id,
                    restored,
                    actual_before,
                    actual_after,
                    order_id,
                    f"Возврат бонусов по заказу №{order_id}",
                ),
            )

    # Возврат заказа освобождает право на 250 ₽ скидки, если награда
    # пригласившему ещё не была зафиксирована. 60-дневный срок при этом
    # не продлевается: он по-прежнему считается от перехода по ссылке.
    await db.execute(
        """
        UPDATE referrals
        SET
            first_discount_order_id = NULL,
            first_order_discount_amount = 0
        WHERE
            referred_user_id = ?
            AND first_discount_order_id = ?
            AND reward_order_id IS NULL
        """,
        (user_id, order_id),
    )

    cursor = await db.execute(
        "SELECT bonus_balance FROM users WHERE id = ?",
        (user_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return int(row["bonus_balance"] if row is not None else 0)


async def _get_held_cash_amount(
    db: aiosqlite.Connection,
    *,
    order_id: int,
    user_id: int,
) -> int:
    cursor = await db.execute(
        """
        SELECT COALESCE(SUM(
            CASE
                WHEN transaction_type = 'payment' THEN -amount
                WHEN transaction_type = 'refund' THEN -amount
                ELSE 0
            END
        ), 0) AS held_amount
        FROM balance_transactions
        WHERE
            order_id = ?
            AND user_id = ?
            AND transaction_type IN ('payment', 'refund')
        """,
        (order_id, user_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return int(row["held_amount"] if row is not None else 0)


async def cancel_paid_order_with_refund(
    order_id: int,
    *,
    actor_telegram_id: int | None = None,
    expected_assignment_generation: int | None = None,
) -> tuple[str, int | None, int | None]:
    """Отменяет оплаченный заказ и возвращает деньги и использованные бонусы."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            if actor_telegram_id is not None and not await assignment_actor_allowed_in_tx(
                db, order_id, actor_telegram_id, expected_assignment_generation
            ):
                await db.rollback()
                return REFUND_RESULT_NOT_AVAILABLE, None, None

            cursor = await db.execute(
                """
                SELECT
                    orders.id,
                    orders.user_id,
                    orders.status,
                    orders.agreed_price,
                    orders.cash_paid_amount,
                    orders.bonus_used,
                    users.balance,
                    users.bonus_balance
                FROM orders
                JOIN users ON users.id = orders.user_id
                WHERE orders.id = ?
                LIMIT 1
                """,
                (order_id,),
            )
            order = await cursor.fetchone()
            await cursor.close()

            if order is None:
                await db.rollback()
                return REFUND_RESULT_ORDER_NOT_FOUND, None, None

            current_status = str(order["status"])
            agreed_price = order["agreed_price"]

            if (
                current_status not in {ORDER_STATUS_IN_PROGRESS, "waiting_client"}
                or agreed_price is None
            ):
                await db.rollback()
                return (
                    REFUND_RESULT_NOT_AVAILABLE,
                    int(order["balance"]),
                    int(agreed_price) if agreed_price is not None else None,
                )

            user_id = int(order["user_id"])
            cash_refund = (
                int(order["cash_paid_amount"])
                if order["cash_paid_amount"] is not None
                else int(agreed_price)
            )
            bonus_used = int(order["bonus_used"] or 0)

            held_amount = await _get_held_cash_amount(
                db,
                order_id=order_id,
                user_id=user_id,
            )
            if held_amount < cash_refund:
                await db.rollback()
                return REFUND_RESULT_ALREADY_REFUNDED, int(order["balance"]), cash_refund

            balance_before = int(order["balance"])
            balance_after = balance_before + cash_refund

            cursor = await db.execute(
                """
                UPDATE users
                SET balance = ?, updated_at = datetime('now', '+3 hours')
                WHERE id = ? AND balance = ?
                """,
                (balance_after, user_id, balance_before),
            )
            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return REFUND_RESULT_NOT_AVAILABLE, None, cash_refund
            await cursor.close()

            if cash_refund > 0:
                await db.execute(
                    """
                    INSERT INTO balance_transactions (
                        user_id, amount, balance_before, balance_after,
                        transaction_type, admin_telegram_id, order_id, comment,
                        created_at
                    ) VALUES (
                        ?, ?, ?, ?, 'refund', NULL, ?, ?,
                        datetime('now', '+3 hours')
                    )
                    """,
                    (
                        user_id,
                        cash_refund,
                        balance_before,
                        balance_after,
                        order_id,
                        f"Возврат средств по заказу №{order_id}",
                    ),
                )

            await _restore_bonus_and_referral_claim(
                db,
                order_id=order_id,
                user_id=user_id,
                bonus_used=bonus_used,
                bonus_before=int(order["bonus_balance"]),
            )

            cursor = await db.execute(
                """
                UPDATE orders
                SET status = 'cancelled', updated_at = datetime('now', '+3 hours')
                WHERE id = ? AND status = ? AND agreed_price = ?
                """,
                (order_id, current_status, int(agreed_price)),
            )
            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return REFUND_RESULT_NOT_AVAILABLE, balance_before, cash_refund
            await cursor.close()

            await db.commit()
            return REFUND_RESULT_SUCCESS, balance_after, cash_refund

        except Exception:
            await db.rollback()
            raise


async def refund_paid_order_for_master_reassignment(
    order_id: int,
    master_telegram_id: int,
    decline_reason: str | None = None,
    *,
    expected_assignment_generation: int | None = None,
) -> tuple[str, int | None, int | None]:
    """Возвращает оплату и бонусы и атомарно возвращает заказ к перевыбору мастера."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            if expected_assignment_generation is not None and not await assignment_actor_allowed_in_tx(
                db, order_id, master_telegram_id, expected_assignment_generation
            ):
                await db.rollback()
                return REFUND_RESULT_NOT_AVAILABLE, None, None

            cursor = await db.execute(
                """
                SELECT
                    orders.id,
                    orders.user_id,
                    orders.status,
                    orders.assignment_status,
                    orders.assigned_master_telegram_id,
                    orders.agreed_price,
                    orders.cash_paid_amount,
                    orders.bonus_used,
                    users.balance,
                    users.bonus_balance
                FROM orders
                JOIN users ON users.id = orders.user_id
                WHERE orders.id = ?
                LIMIT 1
                """,
                (order_id,),
            )
            order = await cursor.fetchone()
            await cursor.close()

            if order is None:
                await db.rollback()
                return REFUND_RESULT_ORDER_NOT_FOUND, None, None

            agreed_price = order["agreed_price"]
            if (
                str(order["status"]) not in {ORDER_STATUS_IN_PROGRESS, "waiting_client"}
                or str(order["assignment_status"]) != "assigned"
                or order["assigned_master_telegram_id"] is None
                or int(order["assigned_master_telegram_id"]) != master_telegram_id
                or agreed_price is None
            ):
                await db.rollback()
                return (
                    REFUND_RESULT_NOT_AVAILABLE,
                    int(order["balance"]),
                    int(agreed_price) if agreed_price is not None else None,
                )

            user_id = int(order["user_id"])
            cash_refund = (
                int(order["cash_paid_amount"])
                if order["cash_paid_amount"] is not None
                else int(agreed_price)
            )
            bonus_used = int(order["bonus_used"] or 0)

            held_amount = await _get_held_cash_amount(
                db,
                order_id=order_id,
                user_id=user_id,
            )
            if held_amount < cash_refund:
                await db.rollback()
                return REFUND_RESULT_ALREADY_REFUNDED, int(order["balance"]), cash_refund

            balance_before = int(order["balance"])
            balance_after = balance_before + cash_refund

            cursor = await db.execute(
                """
                UPDATE users
                SET balance = ?, updated_at = datetime('now', '+3 hours')
                WHERE id = ? AND balance = ?
                """,
                (balance_after, user_id, balance_before),
            )
            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return REFUND_RESULT_NOT_AVAILABLE, None, cash_refund
            await cursor.close()

            if cash_refund > 0:
                await db.execute(
                    """
                    INSERT INTO balance_transactions (
                        user_id, amount, balance_before, balance_after,
                        transaction_type, admin_telegram_id, order_id, comment,
                        created_at
                    ) VALUES (
                        ?, ?, ?, ?, 'refund', NULL, ?, ?,
                        datetime('now', '+3 hours')
                    )
                    """,
                    (
                        user_id,
                        cash_refund,
                        balance_before,
                        balance_after,
                        order_id,
                        f"Возврат средств по заказу №{order_id}: исполнитель отказался после оплаты",
                    ),
                )

            await _restore_bonus_and_referral_claim(
                db,
                order_id=order_id,
                user_id=user_id,
                bonus_used=bonus_used,
                bonus_before=int(order["bonus_balance"]),
            )

            cursor = await db.execute(
                """
                UPDATE orders
                SET
                    status = ?,
                    assigned_master_telegram_id = NULL,
                    assignment_status = 'waiting_selection',
                    price_status = 'waiting_master',
                    agreed_price = NULL,
                    cash_paid_amount = NULL,
                    bonus_used = 0,
                    referral_discount = 0,
                    group_chat_id = NULL,
                    topic_id = NULL,
                    order_card_message_id = NULL,
                    assignment_generation = assignment_generation + 1,
                    updated_at = datetime('now', '+3 hours')
                WHERE
                    id = ?
                    AND status = ?
                    AND assigned_master_telegram_id = ?
                    AND assignment_status = 'assigned'
                    AND agreed_price = ?
                """,
                (
                    ORDER_STATUS_NEW,
                    order_id,
                    str(order["status"]),
                    master_telegram_id,
                    int(agreed_price),
                ),
            )
            if cursor.rowcount <= 0:
                await cursor.close()
                await db.rollback()
                return REFUND_RESULT_NOT_AVAILABLE, balance_before, cash_refund
            await cursor.close()

            await db.execute(
                "UPDATE order_price_offers SET status = 'rejected' WHERE order_id = ? AND status = 'pending'",
                (order_id,),
            )
            await db.execute(
                """
                INSERT INTO order_master_attempts (
                    order_id, master_telegram_id, outcome, reason,
                    created_at, updated_at
                )
                VALUES (
                    ?, ?, 'declined', ?,
                    datetime('now', '+3 hours'),
                    datetime('now', '+3 hours')
                )
                ON CONFLICT(order_id, master_telegram_id) DO UPDATE SET
                    outcome = 'declined',
                    reason = excluded.reason,
                    updated_at = datetime('now', '+3 hours')
                """,
                (
                    order_id,
                    master_telegram_id,
                    (
                        decline_reason.strip()
                        if decline_reason and decline_reason.strip()
                        else "master_declined_after_payment"
                    ),
                ),
            )

            await db.commit()
            return REFUND_RESULT_SUCCESS, balance_after, cash_refund

        except Exception:
            await db.rollback()
            raise
