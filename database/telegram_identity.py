import aiosqlite

from database.db import DB_PATH


async def sync_telegram_username(
    telegram_id: int,
    username: str | None,
) -> None:
    """
    Синхронизирует изменяемый Telegram username по стабильному Telegram ID.

    Функция обновляет только уже существующие записи:
    - users.tg_username;
    - masters.username.

    Новые пользователи/исполнители здесь не создаются.

    Если username отсутствует, в БД сохраняется NULL. Если текущий username
    раньше оставался у другой записи (например, после смены/освобождения
    username в Telegram), старое значение очищается, чтобы поиск по username
    не мог привести к прежнему владельцу.
    """

    normalized_username = None
    if username is not None:
        candidate = username.strip().lstrip("@")
        if candidate:
            normalized_username = candidate

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")

        try:
            if normalized_username is not None:
                # Telegram username в конкретный момент принадлежит одному
                # Telegram ID. Очищаем возможные устаревшие привязки у других
                # записей, не затрагивая текущего владельца.
                await db.execute(
                    """
                    UPDATE users
                    SET
                        tg_username = NULL,
                        updated_at = datetime('now', '+3 hours')
                    WHERE
                        telegram_id <> ?
                        AND tg_username IS NOT NULL
                        AND LOWER(tg_username) = LOWER(?)
                    """,
                    (telegram_id, normalized_username),
                )

                await db.execute(
                    """
                    UPDATE masters
                    SET
                        username = NULL,
                        updated_at = datetime('now', '+3 hours')
                    WHERE
                        telegram_id <> ?
                        AND username IS NOT NULL
                        AND LOWER(username) = LOWER(?)
                    """,
                    (telegram_id, normalized_username),
                )

            # У пользователя дополнительно обновляем last_active_at: этот код
            # запускается только при реальной активности в Telegram.
            await db.execute(
                """
                UPDATE users
                SET
                    tg_username = ?,
                    updated_at = CASE
                        WHEN tg_username IS NOT ?
                            THEN datetime('now', '+3 hours')
                        ELSE updated_at
                    END,
                    last_active_at = datetime('now', '+3 hours')
                WHERE telegram_id = ?
                """,
                (
                    normalized_username,
                    normalized_username,
                    telegram_id,
                ),
            )

            # У мастера updated_at меняется только при фактической смене
            # username, чтобы обычные сообщения в группе не выглядели как
            # редактирование его карточки.
            await db.execute(
                """
                UPDATE masters
                SET
                    username = ?,
                    updated_at = CASE
                        WHEN username IS NOT ?
                            THEN datetime('now', '+3 hours')
                        ELSE updated_at
                    END
                WHERE telegram_id = ?
                """,
                (
                    normalized_username,
                    normalized_username,
                    telegram_id,
                ),
            )

            await db.commit()

        except Exception:
            await db.rollback()
            raise
