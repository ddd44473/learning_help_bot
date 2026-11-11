"""Схема и операции архива диалогов заказов.

Новая схема не меняет существующие таблицы и не помечает старые темы
полными: Telegram Bot API не позволяет выгрузить их прошлую переписку.
"""
from __future__ import annotations

import aiosqlite

from database.db import DB_PATH
from utils.export_limits import bounded_rows


async def init_chat_archive() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS chat_archive_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            group_chat_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            is_legacy INTEGER NOT NULL DEFAULT 0 CHECK(is_legacy IN (0,1)),
            capture_started_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
            closed_at TEXT,
            hold INTEGER NOT NULL DEFAULT 0 CHECK(hold IN (0,1)),
            issue_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            UNIQUE(group_chat_id, topic_id)
        );
        CREATE INDEX IF NOT EXISTS idx_chat_archive_topics_order
        ON chat_archive_topics(order_id);

        CREATE TABLE IF NOT EXISTS chat_archive_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            archive_topic_id INTEGER REFERENCES chat_archive_topics(id) ON DELETE SET NULL,
            source_chat_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            sender_telegram_id INTEGER,
            sender_name TEXT NOT NULL,
            sender_role TEXT NOT NULL,
            content_type TEXT NOT NULL,
            body TEXT,
            sent_at TEXT NOT NULL,
            saved_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
            copied_chat_id INTEGER,
            copied_message_id INTEGER,
            UNIQUE(source_chat_id, source_message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_chat_archive_messages_order
        ON chat_archive_messages(order_id, sent_at, id);

        CREATE TABLE IF NOT EXISTS chat_archive_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES chat_archive_messages(id) ON DELETE CASCADE,
            file_kind TEXT NOT NULL,
            telegram_file_id TEXT NOT NULL,
            file_name TEXT,
            mime_type TEXT,
            file_size INTEGER,
            relative_path TEXT,
            sha256 TEXT,
            save_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(save_status IN ('pending','saved','failed')),
            save_error TEXT,
            UNIQUE(message_id, file_kind, telegram_file_id)
        );
        CREATE INDEX IF NOT EXISTS idx_chat_archive_attachments_message
        ON chat_archive_attachments(message_id);

        CREATE TABLE IF NOT EXISTS chat_archive_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            archive_topic_id INTEGER REFERENCES chat_archive_topics(id) ON DELETE SET NULL,
            event_type TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours'))
        );
        CREATE INDEX IF NOT EXISTS idx_chat_archive_events_order
        ON chat_archive_events(order_id, created_at);

        CREATE TABLE IF NOT EXISTS chat_archive_admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_telegram_id INTEGER NOT NULL,
            order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours'))
        );
        """)
        # Темы, созданные до установки архивирования, помечаем как legacy.
        # Для них нельзя утверждать, что весь диалог сохранён.
        await db.execute("""
            INSERT OR IGNORE INTO chat_archive_topics
                (order_id, group_chat_id, topic_id, is_legacy)
            SELECT id, group_chat_id, topic_id, 1
            FROM orders
            WHERE group_chat_id IS NOT NULL AND topic_id IS NOT NULL
        """)
        await db.commit()


async def register_order_topic_in_tx(
    db: aiosqlite.Connection, order_id: int, group_chat_id: int, topic_id: int
) -> None:
    """Вызывать вместе с UPDATE orders.topic_id в одной транзакции."""
    # Предыдущая тема не считается закрытой только из-за смены ссылки.
    # Реальное закрытие фиксирует archive_order_topic() после Telegram API.
    await db.execute("""
        INSERT OR IGNORE INTO chat_archive_topics
            (order_id, group_chat_id, topic_id, is_legacy)
        VALUES (?, ?, ?, 0)
    """, (order_id, group_chat_id, topic_id))
    await db.execute("""
        INSERT INTO chat_archive_events(order_id, archive_topic_id, event_type)
        SELECT ?, id, 'topic_linked' FROM chat_archive_topics
        WHERE group_chat_id = ? AND topic_id = ?
    """, (order_id, group_chat_id, topic_id))


async def close_topic(group_chat_id: int, topic_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE chat_archive_topics
            SET closed_at = COALESCE(closed_at, datetime('now', '+3 hours'))
            WHERE group_chat_id = ? AND topic_id = ?
        """, (group_chat_id, topic_id))
        await db.commit()


async def get_topic(group_chat_id: int, topic_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM chat_archive_topics
            WHERE group_chat_id = ? AND topic_id = ? LIMIT 1
        """, (group_chat_id, topic_id))
        return await cursor.fetchone()


async def note_issue(group_chat_id: int, topic_id: int, error: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE chat_archive_topics
            SET issue_count = issue_count + 1, last_error = ?
            WHERE group_chat_id = ? AND topic_id = ?
        """, (error[:700], group_chat_id, topic_id))
        await db.commit()


async def insert_message(
    *, order_id: int, archive_topic_id: int | None,
    source_chat_id: int, source_message_id: int,
    sender_telegram_id: int | None, sender_name: str, sender_role: str,
    content_type: str, body: str | None, sent_at: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("""
            INSERT OR IGNORE INTO chat_archive_messages (
                order_id, archive_topic_id, source_chat_id, source_message_id,
                sender_telegram_id, sender_name, sender_role, content_type, body, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order_id, archive_topic_id, source_chat_id, source_message_id,
              sender_telegram_id, sender_name[:200], sender_role,
              content_type, body, sent_at))
        cursor = await db.execute("""
            SELECT id FROM chat_archive_messages
            WHERE source_chat_id = ? AND source_message_id = ?
        """, (source_chat_id, source_message_id))
        row = await cursor.fetchone()
        await db.commit()
        if row is None:
            raise RuntimeError('Сообщение архива не сохранено')
        return int(row[0])


async def update_delivery(message_id: int, chat_id: int, copied_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE chat_archive_messages
            SET copied_chat_id = ?, copied_message_id = ?
            WHERE id = ?
        """, (chat_id, copied_id, message_id))
        await db.commit()


async def upsert_attachment(
    message_id: int, kind: str, file_id: str, filename: str | None,
    mime_type: str | None, size: int | None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("""
            INSERT OR IGNORE INTO chat_archive_attachments
                (message_id, file_kind, telegram_file_id, file_name, mime_type, file_size)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (message_id, kind, file_id, filename, mime_type, size))
        cursor = await db.execute("""
            SELECT id FROM chat_archive_attachments
            WHERE message_id = ? AND file_kind = ? AND telegram_file_id = ?
        """, (message_id, kind, file_id))
        row = await cursor.fetchone()
        await db.commit()
        return int(row[0])


async def persist_attachment_intent(db, message_id, kind, file_id, filename, mime_type, size):
    """Уже открытая транзакция: metadata и job должны фиксироваться вместе."""
    await db.execute(
        "INSERT OR IGNORE INTO chat_archive_attachments(message_id,file_kind,telegram_file_id,file_name,mime_type,file_size) VALUES (?,?,?,?,?,?)",
        (message_id, kind, file_id, filename, mime_type, size),
    )
    cursor = await db.execute(
        "SELECT id FROM chat_archive_attachments WHERE message_id=? AND file_kind=? AND telegram_file_id=?",
        (message_id, kind, file_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return int(row[0])


async def set_attachment_result(
    attachment_id: int, *, path: str | None = None,
    sha256: str | None = None, error: str | None = None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE chat_archive_attachments
            SET save_status = ?, relative_path = ?, sha256 = ?, save_error = ?
            WHERE id = ?
        """, ('failed' if error is not None else 'saved', path, sha256,
              error[:700] if error else None, attachment_id))
        await db.commit()


async def get_archive_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT o.id, o.status, o.subject, o.created_at,
                   o.agreed_price, u.bot_nickname AS client_name,
                   o.assigned_master_telegram_id
            FROM orders o JOIN users u ON u.id = o.user_id
            WHERE o.id = ?
        """, (order_id,))
        return await cursor.fetchone()


async def list_archive_orders(limit: int = 10, offset: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT o.id, o.subject, o.status, COUNT(DISTINCT m.id) AS message_count
            FROM orders o
            LEFT JOIN chat_archive_messages m ON m.order_id = o.id
            GROUP BY o.id ORDER BY o.id DESC LIMIT ? OFFSET ?
        """, (limit, offset))
        return await cursor.fetchall()


async def get_archive_contents(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        topics = await bounded_rows(await db.execute("""
            SELECT * FROM chat_archive_topics WHERE order_id = ? ORDER BY id
        """, (order_id,)))
        messages = await bounded_rows(await db.execute("""
            SELECT * FROM chat_archive_messages WHERE order_id = ?
            ORDER BY sent_at, id
        """, (order_id,)))
        attachments = await bounded_rows(await db.execute("""
            SELECT a.* FROM chat_archive_attachments a
            JOIN chat_archive_messages m ON m.id = a.message_id
            WHERE m.order_id = ? ORDER BY a.id
        """, (order_id,)))
        events = await bounded_rows(await db.execute("""
            SELECT * FROM chat_archive_events WHERE order_id = ? ORDER BY created_at, id
        """, (order_id,)))
        offers = await bounded_rows(await db.execute("""
            SELECT offered_by, amount, status, created_at
            FROM order_price_offers WHERE order_id = ? ORDER BY created_at, id
        """, (order_id,)))
        return topics, messages, attachments, events, offers


async def toggle_hold(order_id: int, hold: bool) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE chat_archive_topics SET hold = ? WHERE order_id = ?
        """, (int(hold), order_id))
        await db.commit()
        return cursor.rowcount


async def audit_admin_action(admin_id: int, order_id: int | None, action: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO chat_archive_admin_audit(admin_telegram_id, order_id, action)
            VALUES (?, ?, ?)
        """, (admin_id, order_id, action))
        await db.commit()


# ----------------------------------------------------------------------
# REL-01: durable-контрольные точки публикации темы заказа.
# Точка создаётся сразу после создания forum topic и ДО отправки
# карточки/вложений/уведомлений; незавершённые точки возобновляются
# при следующем запуске бота.
# ----------------------------------------------------------------------

TOPIC_CHECKPOINT_STATES = ("created", "publishing", "published", "failed")


async def upsert_topic_checkpoint(
    order_id: int,
    group_chat_id: int,
    topic_id: int,
    state: str,
    detail: str | None = None,
    assignment_generation: int = -1,
) -> None:
    if state not in TOPIC_CHECKPOINT_STATES:
        raise ValueError(f"Неизвестное состояние контрольной точки: {state}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO order_topic_checkpoints
                (order_id, group_chat_id, topic_id, state, detail, assignment_generation, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            ON CONFLICT(order_id) DO UPDATE SET
                group_chat_id = excluded.group_chat_id,
                topic_id = excluded.topic_id,
                state = excluded.state,
                detail = excluded.detail,
                assignment_generation = excluded.assignment_generation,
                updated_at = datetime('now', '+3 hours')
            """,
            (order_id, group_chat_id, topic_id, state, detail, assignment_generation),
        )
        await db.commit()


async def mark_topic_checkpoint_state(
    order_id: int,
    state: str,
    detail: str | None = None,
) -> bool:
    if state not in TOPIC_CHECKPOINT_STATES:
        raise ValueError(f"Неизвестное состояние контрольной точки: {state}")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE order_topic_checkpoints
            SET state = ?,
                detail = ?,
                updated_at = datetime('now', '+3 hours')
            WHERE order_id = ?
            """,
            (state, detail, order_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_topic_checkpoint(order_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM order_topic_checkpoints WHERE order_id = ?",
            (order_id,),
        )
        await db.commit()


async def get_topic_checkpoint(order_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM order_topic_checkpoints WHERE order_id = ?",
            (order_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
    return None if row is None else dict(row)


async def list_unfinished_topic_checkpoints() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM order_topic_checkpoints
            WHERE state != 'published'
            ORDER BY order_id
            """,
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [dict(row) for row in rows]


async def reserve_topic_creation(order_id: int, group_chat_id: int, generation: int) -> bool:
    """Записывает неизвестный remote outcome ДО create; один создатель."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        # История опубликованной темы уже есть в chat_archive_topics.
        await db.execute(
            "DELETE FROM order_topic_checkpoints WHERE order_id=? AND state='published' AND assignment_generation != ?",
            (order_id, generation),
        )
        # Only this explicit, atomically created marker proves no remote call
        # was attempted. Consume it with CAS BEFORE calling Telegram; even a
        # crash before that call is thereafter treated as an unknown outcome.
        cursor = await db.execute(
            """UPDATE order_topic_checkpoints
            SET group_chat_id=?, detail='create_outcome_unknown_manual_review',
                updated_at=datetime('now', '+3 hours')
            WHERE order_id=? AND assignment_generation=? AND topic_id=0
                AND state='failed' AND detail='local_creation_pending'
                AND group_chat_id IN (0, ?)
                AND EXISTS (SELECT 1 FROM orders WHERE id=? AND assignment_generation=?
                    AND status='new' AND topic_id IS NULL AND group_chat_id IS NULL
                    AND assignment_status IN ('open', 'assigned'))""",
            (group_chat_id, order_id, generation, group_chat_id, order_id, generation),
        )
        reserved = cursor.rowcount > 0
        await cursor.close()
        if reserved:
            await db.commit()
            return True
        cursor = await db.execute(
            """INSERT OR IGNORE INTO order_topic_checkpoints
               (order_id, group_chat_id, topic_id, state, detail, assignment_generation)
               SELECT id, ?, 0, 'failed', 'create_outcome_unknown_manual_review', ?
               FROM orders WHERE id=? AND assignment_generation=? AND status='new'
               AND topic_id IS NULL AND group_chat_id IS NULL
               AND assignment_status IN ('open', 'assigned')""",
            (group_chat_id, generation, order_id, generation),
        )
        reserved = cursor.rowcount > 0
        await cursor.close()
        await db.commit()
        return reserved


async def link_checkpoint_topic(order_id: int, group_chat_id: int, topic_id: int, generation: int, card_message_id: int | None = None) -> bool:
    """История известной темы и ссылка на точную генерацию — атомарно."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("BEGIN IMMEDIATE")
        await register_order_topic_in_tx(db, order_id, group_chat_id, topic_id)
        cursor = await db.execute(
            """UPDATE orders SET group_chat_id=?, topic_id=?, order_card_message_id=COALESCE(?, order_card_message_id)
            WHERE id=? AND assignment_generation=? AND status='new'
            AND assignment_status IN ('assigned', 'open')
            AND (topic_id IS NULL OR (topic_id=? AND group_chat_id=?))""",
            (group_chat_id, topic_id, card_message_id, order_id, generation, topic_id, group_chat_id),
        )
        linked = cursor.rowcount > 0
        await cursor.close()
        await db.commit()
        return linked


async def claim_topic_publication(order_id: int, generation: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """UPDATE order_topic_checkpoints SET state='publishing', detail='claimed'
            WHERE order_id=? AND assignment_generation=? AND topic_id > 0
            AND (state='created' OR (state='failed' AND detail='card_rejected_safe_retry'))""",
            (order_id, generation),
        )
        claimed = cursor.rowcount > 0
        await cursor.close()
        await db.commit()
        return claimed
