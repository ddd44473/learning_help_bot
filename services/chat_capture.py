"""Сохранение метаданных сообщений и вложений архива без блокировки пересылки.

Вложение не скачивается здесь синхронно: создаётся durable-задача в
chat_archive_download_jobs, а фоновый воркер (services.archive_worker)
забирает её при старте приложения. Пересылка сообщения продолжается
независимо от завершения скачивания.
"""
from __future__ import annotations

import json
import logging
import aiosqlite

from aiogram import Bot
from aiogram.types import Message

from config import ADMIN_IDS
from database.chat_archive import (
    get_topic,
    insert_message,
    note_issue,
    set_attachment_result,
    persist_attachment_intent,
)
from services.archive_worker import archive_worker
from database import db as database
from utils.moscow_time import MOSCOW_TZ

logger = logging.getLogger(__name__)


def _files(message: Message):
    # Сохраняем максимальный вариант фотографии; каждое сообщение Telegram
    # представляет самостоятельное вложение, включая элементы медиагруппы.
    for kind in ('document', 'video', 'audio', 'voice', 'animation',
                 'video_note', 'sticker'):
        value = getattr(message, kind, None)
        if value is not None and getattr(value, 'file_id', None):
            yield (kind, value.file_id, getattr(value, 'file_name', None),
                   getattr(value, 'mime_type', None), getattr(value, 'file_size', None))
    if message.photo:
        p = message.photo[-1]
        yield ('photo', p.file_id, None, 'image/jpeg', p.file_size)


def _body(message: Message) -> str | None:
    text = message.text or message.caption
    if text:
        return text
    # Структурированные сообщения не должны теряться, даже если у них нет текста.
    details = {}
    for kind in ('contact', 'location', 'venue', 'poll', 'dice'):
        value = getattr(message, kind, None)
        if value is not None:
            details[kind] = value.model_dump(mode='json', exclude_none=True)
    return json.dumps(details, ensure_ascii=False) if details else None


async def capture_message(
    message: Message, bot: Bot, *, order_id: int, role: str,
    group_chat_id: int, topic_id: int,
) -> int:
    """Фиксирует lightweight metadata/intent без скачивания bytes."""
    topic = await get_topic(int(group_chat_id), int(topic_id))
    if topic is None:
        raise RuntimeError(f'Отсутствует запись темы {group_chat_id}/{topic_id}')
    if int(topic['order_id']) != order_id:
        raise ValueError('Тема принадлежит другому заказу')

    sender = message.from_user
    sender_id = int(sender.id) if sender else None
    name = sender.full_name if sender else (message.sender_chat.title if message.sender_chat else 'Неизвестный отправитель')
    sent_at = message.date.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S')
    entry_id = await insert_message(
        order_id=int(order_id), archive_topic_id=int(topic['id']),
        source_chat_id=int(message.chat.id), source_message_id=int(message.message_id),
        sender_telegram_id=sender_id, sender_name=name, sender_role=role,
        content_type=message.content_type, body=_body(message), sent_at=sent_at,
    )

    for kind, file_id, name, mime, size in _files(message):
        await _enqueue_attachment(
            entry_id, kind, file_id, name, mime, size,
            group_chat_id=group_chat_id, topic_id=topic_id, order_id=order_id,
        )
    return entry_id


async def _enqueue_attachment(
    entry_id: int, kind: str, file_id: str, name: str | None,
    mime: str | None, size: int | None,
    group_chat_id: int, topic_id: int, order_id: int,
) -> int:
    """Зарегистрировать вложение как задачу фонового скачивания.

    Файл не скачивается здесь: задача pending подхватывается воркером
    (services.archive_worker) независимо от пересылки сообщения.
    """
    async with aiosqlite.connect(database.DB_PATH) as db:
        await db.execute('PRAGMA foreign_keys=ON')
        await db.execute('BEGIN IMMEDIATE')
        attachment_id = await persist_attachment_intent(db, entry_id, kind, file_id, name, mime, size)
        enqueued = await archive_worker.enqueue_download(attachment_id, connection=db)
        await db.commit()
    if not enqueued:
        # Очередь переполнена: сообщение уже доставлено/записано, но
        # вложение не попадёт в архив без ручного вмешательства.
        await set_attachment_result(
            attachment_id,
            error="Очередь архивных задач переполнена",
        )
        await note_issue(
            group_chat_id,
            topic_id,
            f'Очередь скачивания вложения #{attachment_id} переполнена',
        )
        logger.warning(
            'Очередь скачивания вложений переполнена: вложение #%s заказа %s',
            attachment_id,
            order_id,
        )
    return attachment_id


async def capture_initial_order(bot: Bot, order_id: int) -> None:
    """Сохраняет исходное задание и файлы, полученные ещё до открытия диалога."""
    from database.orders import get_order_with_client, get_order_files
    order = await get_order_with_client(order_id)
    if order is None or order['group_chat_id'] is None or order['topic_id'] is None:
        return
    group_chat_id, topic_id = int(order['group_chat_id']), int(order['topic_id'])
    topic = await get_topic(group_chat_id, topic_id)
    if topic is None:
        raise RuntimeError('Невозможно сохранить исходное задание: тема не зарегистрирована')
    entry_id = await insert_message(
        order_id=int(order_id), archive_topic_id=int(topic['id']),
        source_chat_id=-int(order_id), source_message_id=-1,
        sender_telegram_id=order['client_telegram_id'],
        sender_name=str(order['client_telegram_id'] or 'Клиент'),
        sender_role='client', content_type='initial_order',
        body=str(order['description'] or ''), sent_at=str(order['created_at']),
    )
    files = await get_order_files(order_id)
    for file in files:
        await _enqueue_attachment(
            entry_id,
            str(file["file_type"]),
            str(file["telegram_file_id"]),
            file["file_name"],
            None,
            None,
            group_chat_id=group_chat_id,
            topic_id=topic_id,
            order_id=int(order_id),
        )


async def capture_group_message(message: Message, bot: Bot) -> None:
    if not message.message_thread_id:
        return
    topic = await get_topic(message.chat.id, message.message_thread_id)
    if topic is None:
        return
    sender = message.from_user
    if sender and sender.id in ADMIN_IDS:
        role = 'admin'
    else:
        from database.orders import get_order_with_client
        order = await get_order_with_client(int(topic['order_id']))
        assigned = order['assigned_master_telegram_id'] if order else None
        role = 'master' if sender and assigned is not None and sender.id == int(assigned) else 'participant'
    await capture_message(
        message, bot, order_id=int(topic['order_id']), role=role,
        group_chat_id=int(message.chat.id), topic_id=int(message.message_thread_id),
    )


async def capture_client_message(
    message: Message, bot: Bot, *, order_id: int,
    group_chat_id: int, topic_id: int,
) -> int:
    return await capture_message(
        message, bot, order_id=order_id, role='client',
        group_chat_id=group_chat_id, topic_id=topic_id,
    )


async def capture_error(group_chat_id: int, topic_id: int, detail: str) -> None:
    try:
        await note_issue(group_chat_id, topic_id, detail)
    except Exception:
        logger.exception('Не удалось зафиксировать ошибку архивирования в БД')
