import json

import aiosqlite

from database.db import DB_PATH
from database.operations import (
    OP_KIND_ADMIN_BALANCE,
    attach_operation_result_in_tx,
    reserve_operation_in_tx,
)


ALLOWED_TRANSACTION_TYPES = {
    "admin_deposit",
    "admin_withdrawal",
    "payment",
    "refund",
    "bonus",
}


async def _change_user_balance_in_tx(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    amount: int,
    transaction_type: str,
    admin_telegram_id: int | None = None,
    order_id: int | None = None,
    comment: str | None = None,
) -> tuple[bool, int | None]:
    """
    Ядро изменения баланса внутри уже открытой транзакции.

    Возвращает:
    - (True, новый баланс), если операция выполнена;
    - (False, None), если пользователь не найден,
      сумма равна нулю или средств недостаточно.
    Логика отказа откатывает транзакцию перед возвратом неудачи.
    """
    if amount == 0:
        await db.rollback()
        return False, None

    if transaction_type not in ALLOWED_TRANSACTION_TYPES:
        raise ValueError(
            f"Неизвестный тип транзакции: {transaction_type}"
        )

    normalized_comment = (
        comment.strip()
        if comment is not None and comment.strip()
        else None
    )

    # BEGIN IMMEDIATE уже открыт, поэтому чтение баланса и
    # запись операции атомарны относительно конкурирующих записей.
    cursor = await db.execute(
        """
        SELECT balance
        FROM users
        WHERE id = ?
        LIMIT 1
        """,
        (user_id,),
    )

    user = await cursor.fetchone()
    await cursor.close()

    if user is None:
        await db.rollback()
        return False, None

    balance_before = int(user["balance"])
    balance_after = balance_before + amount

    if balance_after < 0:
        await db.rollback()
        return False, None

    cursor = await db.execute(
        """
        UPDATE users
        SET
            balance = ?,
            updated_at = datetime('now', '+3 hours')
        WHERE
            id = ?
            AND balance = ?
        """,
        (
            balance_after,
            user_id,
            balance_before,
        ),
    )

    if cursor.rowcount <= 0:
        await cursor.close()
        await db.rollback()
        return False, None

    await cursor.close()

    await db.execute(
        """
        INSERT INTO balance_transactions (
            user_id,
            amount,
            balance_before,
            balance_after,
            transaction_type,
            admin_telegram_id,
            order_id,
            comment,
            created_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?,
            datetime('now', '+3 hours')
        )
        """,
        (
            user_id,
            amount,
            balance_before,
            balance_after,
            transaction_type,
            admin_telegram_id,
            order_id,
            normalized_comment,
        ),
    )

    return True, balance_after


async def change_user_balance(
    user_id: int,
    amount: int,
    transaction_type: str,
    *,
    admin_telegram_id: int | None = None,
    order_id: int | None = None,
    comment: str | None = None,
) -> tuple[bool, int | None]:
    """
    Атомарно изменяет баланс пользователя и сохраняет операцию.

    amount:
    - положительное значение — начисление;
    - отрицательное значение — списание.

    Возвращает:
    - (True, новый баланс), если операция выполнена;
    - (False, None), если пользователь не найден,
      сумма равна нулю или средств недостаточно.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            # BEGIN IMMEDIATE блокирует конкурирующие записи,
            # пока баланс и история операции не будут сохранены.
            await db.execute("BEGIN IMMEDIATE")

            ok, balance_after = await _change_user_balance_in_tx(
                db,
                user_id=user_id,
                amount=amount,
                transaction_type=transaction_type,
                admin_telegram_id=admin_telegram_id,
                order_id=order_id,
                comment=comment,
            )
            if not ok:
                return False, None

            await db.commit()
            return True, balance_after

        except Exception:
            await db.rollback()
            raise


async def change_user_balance_idempotent(
    op_key: str,
    content_hash: str | None,
    *,
    user_id: int,
    amount: int,
    transaction_type: str,
    admin_telegram_id: int | None = None,
    comment: str | None = None,
) -> tuple[bool, int | None, bool]:
    """
    Атомарно изменяет баланс с durable-идемпотентностью одной операции.

    Один op_key меняет баланс не более одного раза. Повтор обработки
    того же события (последовательный или конкурирующий) возвращает
    сохранённый результат, не двигая деньги второй раз. Резерв операции
    и мутация записываются в одной транзакции: откат неудачной попытки
    не оставляет ни резерва, ни списания.

    Возвращает (applied, новый баланс, это_повтор):
    - applied=True — операция целиком применена (деньги сдвинуты ровно
      один раз, даже если это возврат сохранённого повтором);
    - applied=False — операция не выполнена (не найден пользователь
      или недостаточно средств; для повтора сохранённой неудачи
      возвращается тот же результат).
    duplicate=True указывает обработчику, что событие уже приходило.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        try:
            await db.execute("BEGIN IMMEDIATE")

            fresh, result_ref = await reserve_operation_in_tx(
                db,
                op_key=op_key,
                op_kind=OP_KIND_ADMIN_BALANCE,
                content_hash=content_hash,
            )

            if not fresh:
                await db.rollback()
                stored = None
                if result_ref:
                    try:
                        stored = json.loads(result_ref)
                    except (TypeError, ValueError):
                        stored = None
                if stored and stored.get("applied"):
                    balance = stored.get("balance_after")
                    return (
                        True,
                        int(balance) if balance is not None else None,
                        True,
                    )
                return False, None, True

            ok, balance_after = await _change_user_balance_in_tx(
                db,
                user_id=user_id,
                amount=amount,
                transaction_type=transaction_type,
                admin_telegram_id=admin_telegram_id,
                comment=comment,
            )
            if not ok:
                # Откат транзакции снимает резерв: повтор события
                # сможет выполнить операцию заново, когда условие
                # изменится (например, пополнится баланс).
                return False, None, False

            cursor = await db.execute("SELECT last_insert_rowid()")
            transaction_id = int((await cursor.fetchone())[0])
            await cursor.close()
            await attach_operation_result_in_tx(
                db,
                op_key=op_key,
                result={
                    "applied": True,
                    "balance_after": balance_after,
                    "balance_before": balance_after - amount,
                    "user_id": user_id,
                    "amount": amount,
                    "transaction_id": transaction_id,
                    "admin_telegram_id": admin_telegram_id,
                },
            )

            await db.commit()
            return True, balance_after, False

        except Exception:
            await db.rollback()
            raise


async def get_user_balance(
    user_id: int,
) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT balance
            FROM users
            WHERE id = ?
            LIMIT 1
            """,
            (user_id,),
        )

        row = await cursor.fetchone()
        await cursor.close()

        if row is None:
            return None

        return int(row["balance"])


async def get_balance_transactions(
    user_id: int,
    limit: int = 10,
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
                id,
                amount,
                balance_before,
                balance_after,
                transaction_type,
                admin_telegram_id,
                order_id,
                comment,
                created_at
            FROM balance_transactions
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (
                user_id,
                safe_limit,
                safe_offset,
            ),
        )

        rows = await cursor.fetchall()
        await cursor.close()

        return list(rows)
