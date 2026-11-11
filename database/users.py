import aiosqlite

from database.db import DB_PATH


async def get_user_by_telegram_id(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return await cursor.fetchone()


async def get_user_by_bot_nickname(bot_nickname: str):
    normalized_nickname = (
        bot_nickname
        .strip()
        .lstrip("@")
        .casefold()
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT *
            FROM users
            WHERE bot_nickname_normalized = ?
            """,
            (normalized_nickname,),
        )

        return await cursor.fetchone()


async def create_empty_user(
    telegram_id: int,
    tg_username: str | None,
    tg_full_name: str | None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO users (
                telegram_id,
                tg_username,
                tg_full_name,
                created_at,
                updated_at,
                last_active_at
            )
            VALUES (
                ?, ?, ?,
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours'),
                datetime('now', '+3 hours')
            )
            """,
            (telegram_id, tg_username, tg_full_name),
        )
        await db.commit()


async def finish_user_registration(
    telegram_id: int,
    bot_nickname: str,
):
    normalized_nickname = bot_nickname.casefold()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE users
            SET
                bot_nickname = ?,
                bot_nickname_normalized = ?,
                is_registered = 1,
                updated_at = datetime('now', '+3 hours'),
                last_active_at = datetime('now', '+3 hours')
            WHERE telegram_id = ?
            """,
            (
                bot_nickname,
                normalized_nickname,
                telegram_id,
            ),
        )

        await db.commit()


async def get_internal_user_id(
    telegram_id: int,
) -> int | None:
    user = await get_user_by_telegram_id(telegram_id)

    if user is None or not user["is_registered"]:
        return None

    return user["id"]


async def get_user_by_tg_username(
    tg_username: str,
) -> aiosqlite.Row | None:
    normalized_username = (
        tg_username
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
            SELECT *
            FROM users
            WHERE LOWER(tg_username) = ?
            LIMIT 1
            """,
            (normalized_username,),
        )

        user = await cursor.fetchone()
        await cursor.close()

        return user


async def accept_user_rules(
    telegram_id: int,
) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE users
            SET
                rules_accepted = 1,
                updated_at = datetime('now', '+3 hours')
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )

        await db.commit()

        return cursor.rowcount > 0

async def has_accepted_rules(
    telegram_id: int,
) -> bool:
    user = await get_user_by_telegram_id(
        telegram_id
    )

    if user is None:
        return False

    return bool(
        user["rules_accepted"]
    )