"""Persistent, Telegram-ID based bans (independent from user registration)."""
from __future__ import annotations

import aiosqlite

from database.db import DB_PATH


async def init_access_control() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS banned_telegram_users (
                telegram_id INTEGER PRIMARY KEY,
                username_at_ban TEXT,
                banned_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours'))
            )
        ''')
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_banned_telegram_users_username
            ON banned_telegram_users(username_at_ban COLLATE NOCASE)
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ban_business_audit (
                id INTEGER PRIMARY KEY,
                actor_telegram_id INTEGER,
                target_telegram_id INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('ban', 'unban')),
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours'))
            )
        ''')
        await db.commit()


async def is_banned(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            'SELECT 1 FROM banned_telegram_users WHERE telegram_id = ? LIMIT 1',
            (telegram_id,),
        ) as cursor:
            return await cursor.fetchone() is not None


async def resolve_telegram_id(identifier: str) -> tuple[int, str | None] | None:
    """Resolve a known user's @username, or their immutable Telegram ID.

    Telegram Bot API cannot lookup arbitrary strangers by @username.
    """
    raw = identifier.strip()
    if not raw:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if raw.isdecimal():
            tid = int(raw)
            if tid <= 0 or tid > 9223372036854775807:
                return None
            for table, column in (('users', 'tg_username'), ('masters', 'username')):
                async with db.execute(
                    f'SELECT {column} FROM {table} WHERE telegram_id = ? LIMIT 1', (tid,)
                ) as cursor:
                    found = await cursor.fetchone()
                if found:
                    return tid, found[column]
            async with db.execute(
                'SELECT username_at_ban FROM banned_telegram_users WHERE telegram_id = ?', (tid,)
            ) as cursor:
                found = await cursor.fetchone()
            return tid, found['username_at_ban'] if found else None
        username = raw.removeprefix('@').strip()
        if not (1 <= len(username) <= 32 and username.replace('_', '').isalnum()
                and username.isascii()):
            return None
        # A username might be recycled after somebody was banned. If it now
        # resolves to two different Telegram IDs, refuse to guess: admin must
        # supply the immutable numeric ID instead.
        matches: dict[int, str] = {}
        for table, column in (
            ('users', 'tg_username'),
            ('masters', 'username'),
            ('banned_telegram_users', 'username_at_ban'),
        ):
            async with db.execute(
                f'SELECT telegram_id, {column} FROM {table} '
                f'WHERE {column} = ? COLLATE NOCASE', (username,)
            ) as cursor:
                rows = await cursor.fetchall()
            for found in rows:
                matches[int(found['telegram_id'])] = str(found[column] or '')
        if len(matches) == 1:
            tid, current_name = next(iter(matches.items()))
            return tid, current_name
    return None


async def ban_user(telegram_id: int, username: str | None, admin_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('BEGIN IMMEDIATE')
        cursor = await db.execute('''
            INSERT OR IGNORE INTO banned_telegram_users
                (telegram_id, username_at_ban, banned_by)
            VALUES (?, ?, ?)
        ''', (telegram_id, username, admin_id))
        inserted = cursor.rowcount > 0
        await cursor.close()
        if inserted:
            await db.execute(
                "INSERT INTO ban_business_audit (actor_telegram_id, target_telegram_id, action) VALUES (?, ?, 'ban')",
                (admin_id, telegram_id),
            )
        await db.commit()
        return inserted


async def unban_user(telegram_id: int, admin_id: int | None = None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('BEGIN IMMEDIATE')
        cursor = await db.execute(
            'DELETE FROM banned_telegram_users WHERE telegram_id = ?', (telegram_id,)
        )
        removed = cursor.rowcount > 0
        await cursor.close()
        if removed:
            await db.execute(
                "INSERT INTO ban_business_audit (actor_telegram_id, target_telegram_id, action) VALUES (?, ?, 'unban')",
                (admin_id, telegram_id),
            )
        await db.commit()
        return removed
