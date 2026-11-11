"""Доступный только администраторам интерфейс просмотра архива заказов."""
from __future__ import annotations

import html
import logging
import aiosqlite
from database import db as database

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    FSInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)

from config import ADMIN_IDS
from utils.admin_access import require_private_admin
from database.chat_archive import (
    get_archive_contents, get_archive_order, list_archive_orders, toggle_hold,
    audit_admin_action,
)
from services.archive_export import archive_problems, export_order_archive
from services.export_jobs import bounded_export
from utils.export_limits import ExportLimitError

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command('archive_status'))
async def archive_status(message: Message):
    """Только metadata: нет переписки, filenames, file IDs, paths или tokens."""
    if not await require_private_admin(message):
        return
    async with aiosqlite.connect(database.DB_PATH) as db:
        states = await (await db.execute('SELECT status, COUNT(*) FROM chat_archive_download_jobs GROUP BY status')).fetchall()
        checkpoints = await (await db.execute("SELECT order_id,assignment_generation,topic_id,state,detail='local_creation_pending' FROM order_topic_checkpoints WHERE state!='published' ORDER BY order_id LIMIT 50")).fetchall()
    lines = ['📋 Состояние архива (metadata)', 'Задачи:']
    lines.extend(f'{state}: {count}' for state, count in states)
    lines.append('Незавершённые темы (не более 50):')
    lines.extend(f'Заказ {order}; generation {generation}; topic {topic}; state {state}; local_pending={bool(pending)}' for order, generation, topic, state, pending in checkpoints)
    lines.append('local_pending=True: ожидает первой публикации; остальные topic=0 требуют ручной сверки, повтор запрещён.')
    await message.answer('\n'.join(lines))


def _admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def _keyboard_order(order_id: int, held: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📦 Скачать архив (.zip)', callback_data=f'archive:export:{order_id}')],
        [InlineKeyboardButton(text='🔓 Снять защиту' if held else '🔒 Защитить от удаления',
                              callback_data=f'archive:hold:{order_id}')],
        [InlineKeyboardButton(text='↩️ Список заказов', callback_data='archive:list:0')],
    ])


async def _order_card(order_id: int):
    order = await get_archive_order(order_id)
    if order is None:
        return None, None
    topics, messages, attachments, events, offers = await get_archive_contents(order_id)
    problems = archive_problems(topics, messages, attachments)
    held = any(t['hold'] for t in topics)
    topic_lines = '\n'.join(
        f"  • {t['topic_id']} — " + ('старая тема' if t['is_legacy'] else 'новая тема')
        + (' · закрыта' if t['closed_at'] else ' · не закрыта')
        for t in topics
    ) or '  • Нет зарегистрированных тем'
    text = (
        f"📁 <b>Архив заказа №{order_id}</b>\n\n"
        f"Предмет: {html.escape(str(order['subject'] or '—'))}\n"
        f"Статус: {html.escape(str(order['status']))}\n"
        f"Клиент: {html.escape(str(order['client_name'] or '—'))}\n"
        f"Сообщений: {len(messages)} · Вложений: {len(attachments)}\n"
        f"Предложений цены: {len(offers)}\n\n"
        f"<b>Темы:</b>\n{topic_lines}\n\n"
        f"Защита: {'включена' if held else 'не установлена'}\n"
        "<b>Проверка архива:</b>\n"
        + ('\n'.join('⚠️ ' + html.escape(p) for p in problems[:6]) if problems else '✅ Замечаний нет')
        + '\n\nНа первом этапе удаление тем отключено: проверяем полноту сохранения.'
    )
    return text, _keyboard_order(order_id, held)


@router.message(Command('archive'))
async def archive_command(message: Message):
    if not await require_private_admin(message):
        return
    if not message.from_user or not _admin(message.from_user.id):
        return
    if message.chat.type != 'private':
        await message.reply('Откройте /archive в личном чате с ботом.')
        return
    words = (message.text or '').split()
    if len(words) > 1:
        if not words[1].isdigit():
            await message.answer('Введите номер заказа: /archive 154')
            return
        order_id = int(words[1])
        text, markup = await _order_card(order_id)
        if text is None:
            await message.answer('Заказ не найден.')
        else:
            await audit_admin_action(message.from_user.id, order_id, 'view')
            await message.answer(text, parse_mode='HTML', reply_markup=markup)
        return
    await audit_admin_action(message.from_user.id, None, 'list')
    await _archive_list(message, 0)


async def _archive_list(message: Message, offset: int):
    orders = await list_archive_orders(limit=10, offset=offset)
    rows = [
        [InlineKeyboardButton(text=f"№{o['id']} · {str(o['subject'])[:30]} · {o['message_count']} сообщ.",
                              callback_data=f"archive:order:{o['id']}")]
        for o in orders
    ]
    nav = []
    if offset:
        nav.append(InlineKeyboardButton(text='⬅️', callback_data=f'archive:list:{max(0, offset-10)}'))
    if len(orders) == 10:
        nav.append(InlineKeyboardButton(text='➡️', callback_data=f'archive:list:{offset+10}'))
    if nav:
        rows.append(nav)
    await message.answer(
        '📁 <b>Архив заказов</b>\n\n'
        'Пока сохраняются новые сообщения и вложения. '
        'У ранее созданных тем история может быть неполной.\n\n'
        'Нажмите на заказ или используйте /archive НОМЕР.',
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith('archive:'))
@bounded_export
async def archive_callbacks(callback: CallbackQuery):
    if not await require_private_admin(callback):
        return
    if not _admin(callback.from_user.id):
        await callback.answer('Нет доступа.', show_alert=True)
        return
    if callback.message is None or callback.message.chat.type != 'private':
        await callback.answer('Доступно только в личном чате.', show_alert=True)
        return
    try:
        _, action, argument = (callback.data or '').split(':', maxsplit=2)
        value = int(argument)
        if value < 0:
            raise ValueError
    except ValueError:
        await callback.answer('Некорректные данные.', show_alert=True)
        return
    if action == 'list':
        await audit_admin_action(callback.from_user.id, None, 'list')
        await _archive_list(callback.message, value)
        await callback.answer()
        return
    order = await get_archive_order(value)
    if order is None:
        await callback.answer('Заказ не найден.', show_alert=True)
        return
    if action == 'hold':
        topics, *_ = await get_archive_contents(value)
        await toggle_hold(value, not any(t['hold'] for t in topics))
        await audit_admin_action(callback.from_user.id, value, 'toggle_hold')
    elif action == 'export':
        await audit_admin_action(callback.from_user.id, value, 'export')
        await callback.answer('Формирую ZIP с перепиской и вложениями…')
        try:
            async with export_order_archive(value) as (path, warnings):
                await callback.message.answer_document(
                    FSInputFile(path, filename=f'order_{value}_archive.zip'),
                    caption=(f'📦 Заказ №{value}\n'
                             + ('⚠️ Архив неполный; см. manifest.json' if warnings else 'Архив собран')),
                )
        except ExportLimitError:
            raise
        except Exception:
            logger.exception('Ошибка экспорта заказа %s', value)
            await callback.message.answer('Не удалось экспортировать архив. Подробности доступны в журнале сервера.')
        return
    elif action != 'order':
        await callback.answer('Неизвестное действие.', show_alert=True)
        return
    await audit_admin_action(callback.from_user.id, value, 'view')
    text, markup = await _order_card(value)
    await callback.message.answer(text, parse_mode='HTML', reply_markup=markup)
    await callback.answer()
