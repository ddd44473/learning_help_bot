import json

import aiosqlite

from database.db import DB_PATH
from utils.export_limits import bounded_rows


async def log_user_activity(
    telegram_id: int,
    action: str,
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: dict | None = None,
) -> int:
    """
    Записывает осмысленное действие пользователя
    в журнал активности.

    telegram_id:
        Telegram ID пользователя.

    action:
        Машинное название действия.
        Например:
        - order_created
        - master_selected
        - price_offered
        - price_accepted
        - payment_succeeded

    user_id:
        Внутренний ID пользователя в таблице users.
        Может быть None, например до регистрации.

    entity_type:
        Тип сущности, с которой связано действие.
        Например:
        - order
        - master
        - review

    entity_id:
        ID конкретной сущности.

    details:
        Дополнительные структурированные данные.
        Сохраняются в JSON.

    Возвращает ID созданной записи.
    """

    normalized_action = action.strip()

    if not normalized_action:
        raise ValueError(
            "Название действия не может быть пустым."
        )

    normalized_entity_type = (
        entity_type.strip()
        if entity_type is not None
        and entity_type.strip()
        else None
    )

    details_json = None

    if details is not None:
        details_json = json.dumps(
            details,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            INSERT INTO user_activity_logs (
                user_id,
                telegram_id,
                action,
                entity_type,
                entity_id,
                details,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                datetime('now', '+3 hours')
            )
            """,
            (
                user_id,
                telegram_id,
                normalized_action,
                normalized_entity_type,
                entity_id,
                details_json,
            ),
        )

        log_id = cursor.lastrowid
        await cursor.close()

        await db.commit()

        if log_id is None:
            raise RuntimeError(
                "Не удалось получить ID записи журнала."
            )

        return int(log_id)


async def get_user_activity_logs(
    telegram_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list[aiosqlite.Row]:
    """
    Возвращает журнал действий пользователя
    от новых записей к старым.
    """

    if limit <= 0:
        raise ValueError(
            "limit должен быть больше нуля."
        )

    if limit > 100:
        limit = 100

    if offset < 0:
        raise ValueError(
            "offset не может быть отрицательным."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            SELECT
                id,
                user_id,
                telegram_id,
                action,
                entity_type,
                entity_id,
                details,
                created_at
            FROM user_activity_logs
            WHERE telegram_id = ?
            ORDER BY
                created_at DESC,
                id DESC
            LIMIT ?
            OFFSET ?
            """,
            (
                telegram_id,
                limit,
                offset,
            ),
        )

        rows = await cursor.fetchall()
        await cursor.close()

        return rows


async def get_order_activity_logs(
    order_id: int,
    limit: int = 100,
) -> list[aiosqlite.Row]:
    """
    Возвращает журнал действий,
    связанных с конкретным заказом.
    """

    if limit <= 0:
        raise ValueError(
            "limit должен быть больше нуля."
        )

    if limit > 200:
        limit = 200

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        cursor = await db.execute(
            """
            SELECT
                id,
                user_id,
                telegram_id,
                action,
                entity_type,
                entity_id,
                details,
                created_at
            FROM user_activity_logs
            WHERE
                entity_type = 'order'
                AND entity_id = ?
            ORDER BY
                created_at DESC,
                id DESC
            LIMIT ?
            """,
            (
                order_id,
                limit,
            ),
        )

        rows = await cursor.fetchall()
        await cursor.close()

        return rows


def parse_activity_details(
    details: str | None,
) -> dict:
    """
    Безопасно преобразует JSON из поля details
    обратно в словарь.
    """

    if not details:
        return {}

    try:
        parsed = json.loads(
            details
        )

    except (
        json.JSONDecodeError,
        TypeError,
    ):
        return {}

    if not isinstance(
        parsed,
        dict,
    ):
        return {}

    return parsed


async def get_user_activity_logs_for_export(
    telegram_id: int,
    period: str,
) -> list[aiosqlite.Row]:
    """
    Возвращает журнал активности пользователя
    для экспорта в CSV.

    period:
        - day  — последние 24 часа;
        - week — последние 7 дней;
        - all  — всё время.

    Слишком большая выгрузка отклоняется до полной материализации.
    """

    allowed_periods = {
        "day",
        "week",
        "all",
    }

    if period not in allowed_periods:
        raise ValueError(
            "Неизвестный период выгрузки логов."
        )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        await db.execute(
            "PRAGMA foreign_keys = ON"
        )

        if period == "day":
            date_condition = (
                "AND created_at >= "
                "datetime('now', '+3 hours', '-1 day')"
            )

        elif period == "week":
            date_condition = (
                "AND created_at >= "
                "datetime('now', '+3 hours', '-7 days')"
            )

        else:
            date_condition = ""

        cursor = await db.execute(
            f"""
            SELECT
                id,
                user_id,
                telegram_id,
                action,
                entity_type,
                entity_id,
                details,
                created_at
            FROM user_activity_logs
            WHERE
                (
                    telegram_id = ?
                    OR (
                        entity_type = 'order'
                        AND entity_id IN (
                            SELECT orders.id
                            FROM orders
                            JOIN users
                                ON users.id = orders.user_id
                            WHERE users.telegram_id = ?
                        )
                    )
                )
                {date_condition}
            ORDER BY
                created_at ASC,
                id ASC
            """,
            (
                telegram_id,
                telegram_id,
            ),
        )

        return await bounded_rows(cursor)

async def get_log_actor_names(telegram_ids: set[int]) -> dict[int, str]:
    """Возвращает *актуальные* имена пользователей и мастеров одним пакетом.

    Telegram ID используется только как неизменный ключ связи.
    Если @username отсутствует, используем внутренний ник или имя.
    Удалённые аккаунты, которых нет в базе, остаются без имени.
    """
    if not telegram_ids:
        return {}

    names: dict[int, str] = {}
    ids = sorted(telegram_ids)
    # Не приближаемся к ограничению параметров SQLite (старые версии: 999).
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for offset in range(0, len(ids), 400):
            chunk = ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            cursor = await db.execute(
                f"SELECT telegram_id, tg_username, bot_nickname, tg_full_name "
                f"FROM users WHERE telegram_id IN ({placeholders})",
                chunk,
            )
            users = await cursor.fetchall()
            await cursor.close()
            for user in users:
                tg_id = int(user["telegram_id"])
                username = str(user["tg_username"] or "").strip().lstrip("@")
                nickname = str(user["bot_nickname"] or "").strip()
                full_name = str(user["tg_full_name"] or "").strip()
                if username:
                    names[tg_id] = f"@{username}"
                elif nickname:
                    names[tg_id] = f"{nickname} (без @username)"
                elif full_name:
                    names[tg_id] = f"{full_name} (без @username)"

            cursor = await db.execute(
                f"SELECT telegram_id, username, full_name "
                f"FROM masters WHERE telegram_id IN ({placeholders})",
                chunk,
            )
            masters = await cursor.fetchall()
            await cursor.close()
            for master in masters:
                tg_id = int(master["telegram_id"])
                username = str(master["username"] or "").strip().lstrip("@")
                full_name = str(master["full_name"] or "").strip()
                # При совпадении ID предпочтение актуальному @username мастера
                # вместо запасного внутреннего ника клиента.
                if username and (tg_id not in names or "(без @username)" in names[tg_id]):
                    names[tg_id] = f"@{username}"
                elif tg_id not in names and full_name:
                    names[tg_id] = f"{full_name} (без @username)"
    return names
