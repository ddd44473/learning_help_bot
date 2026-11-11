import aiosqlite

from database.db import DB_PATH
from utils.export_limits import bounded_rows


VALID_EXPORT_PERIODS = {
    "day",
    "week",
    "all",
}


def _period_where_sql(period: str) -> str:
    if period == "day":
        return "AND bt.created_at >= datetime('now', '+3 hours', '-1 day')"

    if period == "week":
        return "AND bt.created_at >= datetime('now', '+3 hours', '-7 days')"

    if period == "all":
        return ""

    raise ValueError(
        f"Неизвестный период выгрузки транзакций: {period}"
    )


async def get_user_transactions(
    user_id: int,
    limit: int = 5,
    offset: int = 0,
) -> list[aiosqlite.Row]:
    safe_limit = max(1, min(limit, 50))
    safe_offset = max(0, offset)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                bt.id,
                bt.user_id,
                bt.amount,
                bt.balance_before,
                bt.balance_after,
                bt.transaction_type,
                bt.admin_telegram_id,
                bt.order_id,
                bt.comment,
                bt.created_at
            FROM balance_transactions AS bt
            WHERE bt.user_id = ?
            ORDER BY bt.created_at DESC, bt.id DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, safe_limit, safe_offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


async def get_user_transactions_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM balance_transactions WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return 0 if row is None else int(row[0])


async def get_user_by_internal_id(
    user_id: int,
) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE id = ? LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def get_all_transactions(
    limit: int = 5,
    offset: int = 0,
) -> list[aiosqlite.Row]:
    safe_limit = max(1, min(limit, 50))
    safe_offset = max(0, offset)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                bt.id,
                bt.user_id,
                bt.amount,
                bt.balance_before,
                bt.balance_after,
                bt.transaction_type,
                bt.admin_telegram_id,
                bt.order_id,
                bt.comment,
                bt.created_at,
                users.telegram_id,
                users.tg_username,
                users.tg_full_name,
                users.bot_nickname,
                users.balance AS current_balance
            FROM balance_transactions AS bt
            JOIN users ON users.id = bt.user_id
            ORDER BY bt.created_at DESC, bt.id DESC
            LIMIT ? OFFSET ?
            """,
            (safe_limit, safe_offset),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)


async def get_all_transactions_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM balance_transactions"
        )
        row = await cursor.fetchone()
        await cursor.close()
        return 0 if row is None else int(row[0])


async def get_transaction_by_id(
    transaction_id: int,
) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cursor = await db.execute(
            """
            SELECT
                bt.id,
                bt.user_id,
                bt.amount,
                bt.balance_before,
                bt.balance_after,
                bt.transaction_type,
                bt.admin_telegram_id,
                bt.order_id,
                bt.comment,
                bt.created_at,
                users.telegram_id,
                users.tg_username,
                users.tg_full_name,
                users.bot_nickname,
                users.balance AS current_balance
            FROM balance_transactions AS bt
            JOIN users ON users.id = bt.user_id
            WHERE bt.id = ?
            LIMIT 1
            """,
            (transaction_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def get_transactions_for_export(
    *,
    period: str,
    user_id: int | None = None,
) -> list[aiosqlite.Row]:
    if period not in VALID_EXPORT_PERIODS:
        raise ValueError(
            f"Неизвестный период выгрузки транзакций: {period}"
        )

    period_sql = _period_where_sql(period)
    where_user_sql = ""
    params: list[object] = []

    if user_id is not None:
        where_user_sql = "AND bt.user_id = ?"
        params.append(user_id)

    query = f"""
        SELECT
            bt.id,
            bt.user_id,
            bt.amount,
            bt.balance_before,
            bt.balance_after,
            bt.transaction_type,
            bt.admin_telegram_id,
            bt.order_id,
            bt.comment,
            bt.created_at,
            users.telegram_id,
            users.tg_username,
            users.tg_full_name,
            users.bot_nickname
        FROM balance_transactions AS bt
        JOIN users ON users.id = bt.user_id
        WHERE 1 = 1
            {where_user_sql}
            {period_sql}
        ORDER BY bt.created_at DESC, bt.id DESC
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        cursor = await db.execute(query, tuple(params))
        return list(await bounded_rows(cursor))
