import json
from typing import Any

import aiosqlite

from database.db import DB_PATH

# Отдельные типы операций, для которых используется durable-журнал.
OP_KIND_ADMIN_BALANCE = "admin_balance"
OP_KIND_ORDER_CONFIRM = "order_confirm"


async def reserve_operation_in_tx(
    db: aiosqlite.Connection,
    *,
    op_key: str,
    op_kind: str,
    content_hash: str | None = None,
) -> tuple[bool, str | None]:
    """
    Надёжно резервирует однократную операцию внутри открытой транзакции.

    Возвращает (fresh, result_ref):
    - fresh=True — операция зарезервирована впервые;
    - fresh=False — операция уже выполнялась ранее; result_ref содержит
      сохранённый дескриптор результата (или None, если он не сохранён).

    Резервирование выполняется в той же транзакции, что и основная
    мутация: при откате резерв откатывается вместе с ней, поэтому
    неудачная попытка не расходует идемпотентность повтора.

    Защита от злоупотребления: если тот же ключ применяется с другим
    content_hash (например, отредактированное сообщение команды),
    операция отклоняется — это не считается повтором того же события.
    """
    cursor = await db.execute(
        """
        INSERT OR IGNORE INTO operation_log (
            op_key, op_kind, content_hash
        )
        VALUES (?, ?, ?)
        """,
        (op_key, op_kind, content_hash),
    )
    inserted = cursor.rowcount > 0
    await cursor.close()

    if content_hash is not None:
        cursor = await db.execute(
            "SELECT content_hash FROM operation_log WHERE op_key = ?",
            (op_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None and row[0] != content_hash:
            raise RuntimeError(
                "Идемпотентный ключ уже использован с другим содержимым."
            )

        if inserted:
            return True, None

    if inserted:
        return True, None

    cursor = await db.execute(
        "SELECT result_ref FROM operation_log WHERE op_key = ?",
        (op_key,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return False, str(row[0]) if row is not None and row[0] is not None else None


async def attach_operation_result_in_tx(
    db: aiosqlite.Connection,
    *,
    op_key: str,
    result: dict[str, Any],
) -> None:
    """Сохраняет результат однократной операции в той же транзакции."""
    cursor = await db.execute(
        """
        UPDATE operation_log
        SET result_ref = ?
        WHERE op_key = ?
        """,
        (json.dumps(result, ensure_ascii=False), op_key),
    )
    if cursor.rowcount <= 0:
        await cursor.close()
        raise RuntimeError("Резерв идемпотентной операции не найден.")
    await cursor.close()


async def get_operation_result(
    op_key: str,
) -> dict[str, Any] | None:
    """Возвращает сохранённый результат ранее применённой операции."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            "SELECT result_ref FROM operation_log WHERE op_key = ?",
            (op_key,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        if row is None or row["result_ref"] is None:
            return None

        try:
            return json.loads(row["result_ref"])
        except (TypeError, ValueError):
            return None


async def assignment_actor_allowed_in_tx(
    db: aiosqlite.Connection,
    order_id: int,
    actor_telegram_id: int,
    expected_assignment_generation: int | None,
) -> bool:
    """Авторизация и ABA-защита внутри транзакции мутации."""
    from config import ADMIN_IDS

    if expected_assignment_generation is None:
        return False
    cursor = await db.execute(
        """SELECT 1 FROM orders
        WHERE id = ? AND assignment_generation = ?
          AND (assigned_master_telegram_id = ? OR ?)
          AND NOT EXISTS (SELECT 1 FROM banned_telegram_users WHERE telegram_id = ?)
        """,
        (order_id, expected_assignment_generation, actor_telegram_id,
         actor_telegram_id in ADMIN_IDS, actor_telegram_id),
    )
    allowed = await cursor.fetchone() is not None
    await cursor.close()
    return allowed
