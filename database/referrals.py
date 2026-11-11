import secrets
import sqlite3

import aiosqlite

from database.db import DB_PATH


REFERRAL_DISCOUNT_AMOUNT = 250
REFERRAL_REWARD_AMOUNT = 250
BONUS_MAX_ORDER_PERCENT = 15
REFERRAL_DISCOUNT_VALID_DAYS = 60
BONUS_VALID_DAYS = 60
BONUS_REMINDER_DAYS = 5


def percent_amount(amount: int, percent: int) -> int:
    """Возвращает целую сумму процента без округления вверх."""
    if amount <= 0 or percent <= 0:
        return 0
    return (amount * percent) // 100


async def ensure_referral_code(user_id: int) -> str | None:
    """Возвращает стабильный публичный referral-код пользователя."""
    for _ in range(10):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")

            cursor = await db.execute(
                "SELECT referral_code FROM users WHERE id = ? LIMIT 1",
                (user_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()

            if row is None:
                return None

            existing_code = row["referral_code"]
            if existing_code:
                return str(existing_code)

            # Короткий публичный код не раскрывает внутренний ID пользователя.
            new_code = secrets.token_hex(5)

            try:
                cursor = await db.execute(
                    """
                    UPDATE users
                    SET referral_code = ?, updated_at = datetime('now', '+3 hours')
                    WHERE id = ? AND referral_code IS NULL
                    """,
                    (new_code, user_id),
                )
                await db.commit()

                if cursor.rowcount > 0:
                    await cursor.close()
                    return new_code

                await cursor.close()
            except sqlite3.IntegrityError:
                await db.rollback()
                continue

    return None


async def attach_referral_by_code(
    referred_telegram_id: int,
    referral_code: str,
) -> bool:
    """
    Фиксирует реферальную связь при первом запуске по персональной ссылке.

    Сам переход увеличивает статистику приглашений, но никаких бонусов
    пригласившему не начисляет. Повторная привязка, самореферал, уже
    зарегистрированный пользователь и пользователь с существующими заказами
    не допускаются.
    """
    normalized_code = referral_code.strip().lower()
    if not normalized_code:
        return False

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            cursor = await db.execute(
                """
                SELECT id, is_registered
                FROM users
                WHERE telegram_id = ?
                LIMIT 1
                """,
                (referred_telegram_id,),
            )
            referred = await cursor.fetchone()
            await cursor.close()

            if referred is None or bool(referred["is_registered"]):
                await db.rollback()
                return False

            referred_user_id = int(referred["id"])

            cursor = await db.execute(
                """
                SELECT id
                FROM users
                WHERE LOWER(referral_code) = ? AND is_registered = 1
                LIMIT 1
                """,
                (normalized_code,),
            )
            referrer = await cursor.fetchone()
            await cursor.close()

            if referrer is None:
                await db.rollback()
                return False

            referrer_user_id = int(referrer["id"])
            if referrer_user_id == referred_user_id:
                await db.rollback()
                return False

            cursor = await db.execute(
                "SELECT 1 FROM referrals WHERE referred_user_id = ? LIMIT 1",
                (referred_user_id,),
            )
            already_attached = await cursor.fetchone()
            await cursor.close()

            if already_attached is not None:
                await db.rollback()
                return False

            cursor = await db.execute(
                "SELECT 1 FROM orders WHERE user_id = ? LIMIT 1",
                (referred_user_id,),
            )
            has_orders = await cursor.fetchone()
            await cursor.close()

            if has_orders is not None:
                await db.rollback()
                return False

            await db.execute(
                """
                INSERT INTO referrals (
                    referrer_user_id,
                    referred_user_id,
                    created_at
                )
                VALUES (?, ?, datetime('now', '+3 hours'))
                """,
                (referrer_user_id, referred_user_id),
            )

            await db.commit()
            return True

        except sqlite3.IntegrityError:
            await db.rollback()
            return False
        except Exception:
            await db.rollback()
            raise


async def _sync_bonus_balance_in_tx(
    db: aiosqlite.Connection,
    user_id: int,
) -> int:
    cursor = await db.execute(
        """
        SELECT COALESCE(SUM(remaining_amount), 0) AS balance
        FROM bonus_grants
        WHERE
            user_id = ?
            AND remaining_amount > 0
            AND expires_at > datetime('now', '+3 hours')
        """,
        (user_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()

    balance = int(row["balance"] if row is not None else 0)
    await db.execute(
        """
        UPDATE users
        SET bonus_balance = ?, updated_at = datetime('now', '+3 hours')
        WHERE id = ?
        """,
        (balance, user_id),
    )
    return balance


async def expire_bonus_grants_in_tx(
    db: aiosqlite.Connection,
    user_id: int | None = None,
) -> set[int]:
    """Сжигает просроченные остатки и синхронизирует кеш-баланс users."""
    params: tuple[int, ...] = ()
    user_filter = ""
    if user_id is not None:
        user_filter = " AND user_id = ?"
        params = (user_id,)

    cursor = await db.execute(
        f"""
        SELECT DISTINCT user_id
        FROM bonus_grants
        WHERE
            remaining_amount > 0
            AND expires_at <= datetime('now', '+3 hours')
            {user_filter}
        """,
        params,
    )
    rows = await cursor.fetchall()
    await cursor.close()
    affected = {int(row["user_id"]) for row in rows}

    await db.execute(
        f"""
        UPDATE bonus_grants
        SET
            remaining_amount = 0,
            expired_at = COALESCE(expired_at, datetime('now', '+3 hours'))
        WHERE
            remaining_amount > 0
            AND expires_at <= datetime('now', '+3 hours')
            {user_filter}
        """,
        params,
    )

    if user_id is not None:
        affected.add(user_id)

    for affected_user_id in affected:
        await _sync_bonus_balance_in_tx(db, affected_user_id)

    return affected


async def expire_all_bonus_grants() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")
        await expire_bonus_grants_in_tx(db)
        await db.commit()


async def get_bonus_balance(user_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")

        cursor = await db.execute(
            "SELECT id FROM users WHERE id = ? LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            await db.rollback()
            return None

        await expire_bonus_grants_in_tx(db, user_id)
        balance = await _sync_bonus_balance_in_tx(db, user_id)
        await db.commit()
        return balance


async def get_active_referral_discount(
    user_id: int,
) -> dict[str, int | str] | None:
    """Возвращает действующую скидку приглашённого на первый заказ."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            f"""
            SELECT
                {REFERRAL_DISCOUNT_AMOUNT} AS amount,
                created_at,
                datetime(created_at, '+{REFERRAL_DISCOUNT_VALID_DAYS} days')
                    AS expires_at
            FROM referrals
            WHERE
                referred_user_id = ?
                AND first_discount_order_id IS NULL
                AND created_at > datetime(
                    'now', '+3 hours', '-{REFERRAL_DISCOUNT_VALID_DAYS} days'
                )
            LIMIT 1
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return None

        return {
            "amount": int(row["amount"]),
            "created_at": str(row["created_at"]),
            "expires_at": str(row["expires_at"]),
        }


async def get_active_bonus_grants(user_id: int) -> list[dict[str, int | str]]:
    """Возвращает активные начисления с независимыми сроками сгорания."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")
        await expire_bonus_grants_in_tx(db, user_id)

        cursor = await db.execute(
            """
            SELECT id, amount, remaining_amount, granted_at, expires_at
            FROM bonus_grants
            WHERE
                user_id = ?
                AND remaining_amount > 0
                AND expires_at > datetime('now', '+3 hours')
            ORDER BY expires_at ASC, id ASC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        await db.commit()

        return [
            {
                "id": int(row["id"]),
                "amount": int(row["amount"]),
                "remaining_amount": int(row["remaining_amount"]),
                "granted_at": str(row["granted_at"]),
                "expires_at": str(row["expires_at"]),
            }
            for row in rows
        ]


async def get_referral_stats(user_id: int) -> dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")
        await expire_bonus_grants_in_tx(db, user_id)

        cursor = await db.execute(
            """
            SELECT
                COUNT(*) AS invited_count,
                COALESCE(SUM(
                    CASE WHEN reward_order_id IS NOT NULL THEN 1 ELSE 0 END
                ), 0) AS completed_count
            FROM referrals
            WHERE referrer_user_id = ?
            """,
            (user_id,),
        )
        stats = await cursor.fetchone()
        await cursor.close()

        bonus_balance = await _sync_bonus_balance_in_tx(db, user_id)
        await db.commit()

        invited_count = int(stats["invited_count"] if stats else 0)
        completed_count = int(stats["completed_count"] if stats else 0)
        return {
            "invited_count": invited_count,
            "completed_count": completed_count,
            "waiting_count": max(invited_count - completed_count, 0),
            "bonus_balance": bonus_balance,
        }


async def consume_bonus_grants_in_tx(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    order_id: int,
    amount: int,
) -> tuple[int, int, int]:
    """
    Списывает указанную сумму из ближайших к сгоранию начислений.

    Возвращает (реально списано, баланс до, баланс после).
    """
    if amount <= 0:
        balance = await _sync_bonus_balance_in_tx(db, user_id)
        return 0, balance, balance

    await expire_bonus_grants_in_tx(db, user_id)
    balance_before = await _sync_bonus_balance_in_tx(db, user_id)
    if balance_before < amount:
        raise RuntimeError("Недостаточно действующих бонусов")

    cursor = await db.execute(
        """
        SELECT id, remaining_amount
        FROM bonus_grants
        WHERE
            user_id = ?
            AND remaining_amount > 0
            AND expires_at > datetime('now', '+3 hours')
        ORDER BY expires_at ASC, id ASC
        """,
        (user_id,),
    )
    grants = await cursor.fetchall()
    await cursor.close()

    remaining_to_use = amount
    for grant in grants:
        if remaining_to_use <= 0:
            break

        grant_remaining = int(grant["remaining_amount"])
        take = min(grant_remaining, remaining_to_use)
        if take <= 0:
            continue

        cursor = await db.execute(
            """
            UPDATE bonus_grants
            SET remaining_amount = remaining_amount - ?
            WHERE id = ? AND remaining_amount >= ?
            """,
            (take, int(grant["id"]), take),
        )
        if cursor.rowcount <= 0:
            await cursor.close()
            raise RuntimeError("Не удалось списать бонусное начисление")
        await cursor.close()

        await db.execute(
            """
            INSERT INTO bonus_usage_allocations (
                order_id, grant_id, amount, created_at
            )
            VALUES (?, ?, ?, datetime('now', '+3 hours'))
            """,
            (order_id, int(grant["id"]), take),
        )
        remaining_to_use -= take

    if remaining_to_use != 0:
        raise RuntimeError("Не удалось распределить списание бонусов")

    balance_after = await _sync_bonus_balance_in_tx(db, user_id)
    return amount, balance_before, balance_after


async def restore_bonus_usage_for_order_in_tx(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    order_id: int,
    fallback_amount: int = 0,
) -> tuple[int, int, int]:
    """
    Возвращает не восстановленные бонусные списания текущего заказа.

    Срок конкретного начисления не продлевается: восстановленные бонусы
    сохраняют исходную дату сгорания.
    """
    await expire_bonus_grants_in_tx(db, user_id)
    balance_before = await _sync_bonus_balance_in_tx(db, user_id)

    cursor = await db.execute(
        """
        SELECT
            bua.id AS allocation_id,
            bua.grant_id,
            bua.amount,
            bg.expires_at
        FROM bonus_usage_allocations bua
        JOIN bonus_grants bg ON bg.id = bua.grant_id
        WHERE
            bua.order_id = ?
            AND bg.user_id = ?
            AND bua.restored_at IS NULL
        ORDER BY bua.id ASC
        """,
        (order_id, user_id),
    )
    allocations = await cursor.fetchall()
    await cursor.close()

    restored = 0
    for allocation in allocations:
        amount = int(allocation["amount"])
        await db.execute(
            """
            UPDATE bonus_grants
            SET
                remaining_amount = remaining_amount + ?,
                expired_at = CASE
                    WHEN expires_at > datetime('now', '+3 hours') THEN NULL
                    ELSE expired_at
                END
            WHERE id = ?
            """,
            (amount, int(allocation["grant_id"])),
        )
        await db.execute(
            """
            UPDATE bonus_usage_allocations
            SET restored_at = datetime('now', '+3 hours')
            WHERE id = ? AND restored_at IS NULL
            """,
            (int(allocation["allocation_id"]),),
        )
        restored += amount

    # Для заказов, оплаченных старой версией бота, детализации по
    # начислениям ещё не было. При возврате не теряем такие бонусы:
    # создаём отдельное 60-дневное восстановительное начисление.
    if restored == 0 and fallback_amount > 0:
        await db.execute(
            f"""
            INSERT INTO bonus_grants (
                user_id, amount, remaining_amount, source_type,
                source_order_id, granted_at, expires_at
            )
            VALUES (
                ?, ?, ?, 'legacy_balance', ?,
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours', '+{BONUS_VALID_DAYS} days')
            )
            """,
            (user_id, fallback_amount, fallback_amount, order_id),
        )
        restored = fallback_amount

    # Сразу снова сжигаем то, чей исходный срок уже истёк.
    await expire_bonus_grants_in_tx(db, user_id)
    balance_after = await _sync_bonus_balance_in_tx(db, user_id)
    return restored, balance_before, balance_after


async def grant_referral_reward_for_completed_order_in_tx(
    db: aiosqlite.Connection,
    *,
    order_id: int,
    referred_user_id: int,
    agreed_price: int,
) -> dict[str, int | str] | None:
    """
    Начисляет пригласившему фиксированные 250 бонусов после первого
    успешно завершённого заказа приглашённого.

    agreed_price оставлен в сигнатуре для совместимости с вызывающим кодом;
    размер награды от стоимости заказа больше не зависит.
    """
    del agreed_price

    cursor = await db.execute(
        """
        SELECT
            referrals.id AS referral_id,
            referrals.referrer_user_id,
            users.telegram_id AS referrer_telegram_id
        FROM referrals
        JOIN users ON users.id = referrals.referrer_user_id
        WHERE
            referrals.referred_user_id = ?
            AND referrals.reward_order_id IS NULL
        LIMIT 1
        """,
        (referred_user_id,),
    )
    referral = await cursor.fetchone()
    await cursor.close()

    if referral is None:
        return None

    referral_id = int(referral["referral_id"])
    referrer_user_id = int(referral["referrer_user_id"])
    await expire_bonus_grants_in_tx(db, referrer_user_id)
    bonus_before = await _sync_bonus_balance_in_tx(db, referrer_user_id)

    reward_amount = REFERRAL_REWARD_AMOUNT
    cursor = await db.execute(
        f"""
        INSERT INTO bonus_grants (
            user_id,
            amount,
            remaining_amount,
            source_type,
            source_order_id,
            related_user_id,
            granted_at,
            expires_at
        )
        VALUES (
            ?, ?, ?, 'referral_reward', ?, ?,
            datetime('now', '+3 hours'),
            datetime('now', '+3 hours', '+{BONUS_VALID_DAYS} days')
        )
        """,
        (
            referrer_user_id,
            reward_amount,
            reward_amount,
            order_id,
            referred_user_id,
        ),
    )
    grant_id = int(cursor.lastrowid)
    await cursor.close()

    bonus_after = await _sync_bonus_balance_in_tx(db, referrer_user_id)

    await db.execute(
        """
        INSERT INTO bonus_transactions (
            user_id,
            amount,
            bonus_before,
            bonus_after,
            transaction_type,
            order_id,
            related_user_id,
            comment,
            created_at
        )
        VALUES (
            ?, ?, ?, ?, 'referral_reward', ?, ?, ?,
            datetime('now', '+3 hours')
        )
        """,
        (
            referrer_user_id,
            reward_amount,
            bonus_before,
            bonus_after,
            order_id,
            referred_user_id,
            f"250 бонусов за первый завершённый заказ приглашённого пользователя №{order_id}",
        ),
    )

    cursor = await db.execute(
        """
        UPDATE referrals
        SET
            reward_order_id = ?,
            reward_amount = ?,
            reward_granted_at = datetime('now', '+3 hours')
        WHERE id = ? AND reward_order_id IS NULL
        """,
        (order_id, reward_amount, referral_id),
    )
    if cursor.rowcount <= 0:
        await cursor.close()
        raise RuntimeError("Реферальная награда уже была обработана")
    await cursor.close()

    cursor = await db.execute(
        "SELECT expires_at FROM bonus_grants WHERE id = ?",
        (grant_id,),
    )
    grant = await cursor.fetchone()
    await cursor.close()

    return {
        "referrer_user_id": referrer_user_id,
        "referrer_telegram_id": int(referral["referrer_telegram_id"]),
        "reward_amount": reward_amount,
        "bonus_balance": bonus_after,
        "expires_at": str(grant["expires_at"]),
    }


async def get_referral_reward_for_order(
    order_id: int,
) -> dict[str, int | str] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                referrals.reward_amount,
                users.id AS referrer_user_id,
                users.telegram_id AS referrer_telegram_id,
                users.bonus_balance,
                bonus_grants.expires_at
            FROM referrals
            JOIN users ON users.id = referrals.referrer_user_id
            LEFT JOIN bonus_grants
                ON bonus_grants.user_id = referrals.referrer_user_id
                AND bonus_grants.source_type = 'referral_reward'
                AND bonus_grants.source_order_id = referrals.reward_order_id
            WHERE referrals.reward_order_id = ?
            ORDER BY bonus_grants.id DESC
            LIMIT 1
            """,
            (order_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return None

        return {
            "reward_amount": int(row["reward_amount"]),
            "referrer_user_id": int(row["referrer_user_id"]),
            "referrer_telegram_id": int(row["referrer_telegram_id"]),
            "bonus_balance": int(row["bonus_balance"]),
            "expires_at": str(row["expires_at"] or ""),
        }


async def get_due_bonus_reminders(
    limit: int = 100,
) -> list[dict[str, int | str]]:
    """Возвращает ещё не отправленные напоминания за 5 дней до сгорания."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")
        await expire_bonus_grants_in_tx(db)

        cursor = await db.execute(
            f"""
            SELECT
                bg.id AS grant_id,
                bg.remaining_amount,
                bg.expires_at,
                u.telegram_id
            FROM bonus_grants bg
            JOIN users u ON u.id = bg.user_id
            WHERE
                bg.remaining_amount > 0
                AND bg.reminder_sent_at IS NULL
                AND bg.expires_at > datetime('now', '+3 hours')
                AND bg.expires_at <= datetime(
                    'now', '+3 hours', '+{BONUS_REMINDER_DAYS} days'
                )
            ORDER BY bg.expires_at ASC, bg.id ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        await db.commit()

        return [
            {
                "grant_id": int(row["grant_id"]),
                "remaining_amount": int(row["remaining_amount"]),
                "expires_at": str(row["expires_at"]),
                "telegram_id": int(row["telegram_id"]),
            }
            for row in rows
        ]


async def mark_bonus_reminder_sent(grant_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE bonus_grants
            SET reminder_sent_at = datetime('now', '+3 hours')
            WHERE id = ? AND reminder_sent_at IS NULL
            """,
            (grant_id,),
        )
        await db.commit()
