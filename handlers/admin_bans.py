"""Administrator-only /ban and /unban commands."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_IDS
from database.access_control import ban_user, resolve_telegram_id, unban_user
from database.activity_logs import log_user_activity

router = Router()
logger = logging.getLogger(__name__)


async def _change_ban(message: Message, *, blocking: bool) -> None:
    if message.from_user is None or message.from_user.id not in ADMIN_IDS:
        return
    if message.chat.type != 'private':
        await message.answer('Эта команда доступна только в личном чате с ботом.')
        return

    parts = (message.text or '').split()
    command = '/ban' if blocking else '/unban'
    if len(parts) != 2:
        await message.answer(f'Использование: {command} @username\nИли: {command} 123456789')
        return
    committed = False
    try:
        target = await resolve_telegram_id(parts[1])
        if target is None:
            await message.answer(
                'Пользователь не найден. Бот может искать только тех, кто уже '
                'есть в его базе. Можно указать числовой Telegram ID.'
            )
            return
        target_id, stored_username = target
        if target_id in ADMIN_IDS:
            await message.answer('Нельзя заблокировать администратора.')
            return
        changed = (
            await ban_user(target_id, stored_username, message.from_user.id)
            if blocking else await unban_user(target_id, message.from_user.id)
        )
        committed = True
        if changed:
            try:
                await log_user_activity(
                    telegram_id=message.from_user.id,
                    action='admin_banned_user' if blocking else 'admin_unbanned_user',
                    entity_type='user',
                    details={'target_telegram_id': target_id},
                )
            except Exception:
                logger.exception('Блокировка сохранена; не удалось обновить журнал активности')
        if blocking:
            result = 'Пользователь заблокирован.' if changed else 'Пользователь уже заблокирован.'
        else:
            result = 'Пользователь разблокирован.' if changed else 'Пользователь не был заблокирован.'
        await message.answer(f'{result}\nTelegram ID: {target_id}')
    except Exception:
        logger.exception('Ошибка команды %s', command)
        if committed:
            return  # Ошибка ответа не превращает зафиксированную операцию в неудачу.
        await message.answer('Не удалось изменить блокировку. Проверьте базу данных.')


@router.message(Command('ban'))
async def ban_command(message: Message) -> None:
    await _change_ban(message, blocking=True)


@router.message(Command('unban'))
async def unban_command(message: Message) -> None:
    await _change_ban(message, blocking=False)
