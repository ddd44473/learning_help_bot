"""Проверка и экспорт сохранённых переписок администраторам."""
from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from database.chat_archive import get_archive_contents, get_archive_order
import config
from services.archive_worker import disk_call
from utils.export_limits import ExportLimitError, LimitedParts, checked_output
from utils.private_files import contained_path, file_digest

MAX_EXPORT_FILES = 40 * 1024 * 1024  # запас до типичного лимита отправки документов Telegram
MAX_ZIP_BYTES = 40 * 1024**2  # local budget, не обещание provider upload capacity
MIN_EXPORT_FREE_BYTES = 64 * 1024**2
EXPORT_TMP_ROOT = Path(__file__).resolve().parent.parent / 'storage' / 'export_tmp'


def archive_problems(topics, messages, attachments) -> list[str]:
    problems: list[str] = []
    if not topics:
        problems.append('Не зарегистрирована ни одна Telegram-тема.')
    if any(topic['is_legacy'] for topic in topics):
        problems.append('Есть старые темы: сообщения до установки архива недоступны боту.')
    if any(topic['issue_count'] for topic in topics):
        problems.append('При сохранении были ошибки: проверьте вложения и журнал.')
    if any(topic['hold'] for topic in topics):
        problems.append('Администратор запретил очистку.')
    if any(a['save_status'] != 'saved' for a in attachments):
        problems.append('Не все вложения скачаны и сохранены.')
    if not messages:
        problems.append('В архиве нет сохранённых сообщений.')
    for a in attachments:
        if a['save_status'] == 'saved':
            relpath = a['relative_path']
            if not relpath or not contained_path(config.ARCHIVE_STORAGE_ROOT, relpath).is_file():
                problems.append(f"Отсутствует файл вложения #{a['id']}.")
    # В первой фазе мы не захватываем все исходящие ботом сообщения.
    # Поэтому состояние NEVER считается основанием для автоматического удаления.
    problems.append('Пилотная версия: исходящие служебные сообщения бота ещё не охвачены полностью.')
    return problems


def _pretty(value) -> str:
    return html.escape(str(value if value is not None else ''))


def _html_transcript(order, topics, messages, attachments, events, offers, warnings):
    files_by_message: dict[int, list] = {}
    for attachment in attachments:
        files_by_message.setdefault(int(attachment['message_id']), []).append(attachment)
    parts = LimitedParts(["""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Архив заказа</title><style>
body{max-width:850px;margin:36px auto;padding:0 18px;background:#f7f8fa;color:#18202a;font:15px/1.55 system-ui, sans-serif}
article,.notice{background:white;border:1px solid #e0e4ea;border-radius:12px;padding:13px 17px;margin:10px 0}
small{color:#67758a}.client{border-left:4px solid #3498db}.master{border-left:4px solid #29a36a}
.admin{border-left:4px solid #d29b2f}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}
a{color:#165ec3}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}
</style></head><body>"""])
    parts += [f'<h1>Заказ №{order["id"]}</h1>',
              f'<p>Статус: {_pretty(order["status"])} · Клиент: {_pretty(order["client_name"])} · Дата: {_pretty(order["created_at"])} МСК</p>']
    if warnings:
        parts.append('<div class="notice"><strong>⚠ Неполный архив</strong><ul>')
        parts.extend(f'<li>{_pretty(w)}</li>' for w in warnings)
        parts.append('</ul></div>')
    parts.append('<h2>История сообщений</h2>')
    for msg in messages:
        role = str(msg['sender_role'])
        parts.append(f'<article class="{_pretty(role)}"><small>{_pretty(msg["sent_at"])} МСК · {_pretty(role)} · {_pretty(msg["sender_name"])} · {_pretty(msg["content_type"])}</small>')
        if msg['body']:
            parts.append('<pre>' + _pretty(msg['body']) + '</pre>')
        if msg['id'] in files_by_message:
            parts.append('<ul>')
            for item in files_by_message[msg['id']]:
                label = _pretty(item['file_name'] or (item['file_kind'] + ' #' + str(item['id'])))
                if item['save_status'] == 'saved' and item['relative_path']:
                    fn = Path(item['relative_path']).name
                    parts.append(f'<li><a href="attachments/{_pretty(fn)}">{label}</a></li>')
                else:
                    parts.append(f'<li>{label} (файл не сохранён)</li>')
            parts.append('</ul>')
        parts.append('</article>')
    parts.append('<h2>Предложения цены</h2><table><tr><th>Время</th><th>Автор</th><th>Сумма</th><th>Состояние</th></tr>')
    for o in offers:
        parts.append(f'<tr><td>{_pretty(o["created_at"])}</td><td>{_pretty(o["offered_by"])}</td><td>{_pretty(o["amount"])}</td><td>{_pretty(o["status"])}</td></tr>')
    parts.append('</table><h2>Технические события</h2><ul>')
    for e in events:
        parts.append(f'<li>{_pretty(e["created_at"])} — {_pretty(e["event_type"])} {_pretty(e["details"])}</li>')
    parts.append('</ul><p><small>История сформирована из записей, полученных ботом. Служебные сообщения Telegram и история до установки архива могут отсутствовать.</small></p></body></html>')
    return checked_output('\n'.join(parts).encode('utf-8')).decode('utf-8')


class _BoundedFile:
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        if self.stream.tell() + len(data) > MAX_ZIP_BYTES:
            raise ExportLimitError('ZIP превышает локальный лимит 40 МБ.')
        return self.stream.write(data)

    def __getattr__(self, key):
        return getattr(self.stream, key)


def _build_zip(output, order, topics, messages, attachments, events, offers, warnings):
    total = 0
    names = set()
    verified = []
    with output.open('w+b') as file_output, zipfile.ZipFile(_BoundedFile(file_output), 'w', zipfile.ZIP_DEFLATED) as archive:
        for attachment in attachments:
            if attachment['save_status'] != 'saved' or not attachment['relative_path']:
                continue
            stored = contained_path(config.ARCHIVE_STORAGE_ROOT, attachment['relative_path'])
            if not stored.is_file():
                continue
            size = stored.stat().st_size
            total += size
            if total > MAX_EXPORT_FILES:
                raise ExportLimitError('Вложения превышают локальный лимит 40 МБ.')
            digest, measured = file_digest(stored, MAX_EXPORT_FILES)
            if not attachment['sha256'] or digest != attachment['sha256']:
                warnings.append(f"Вложение #{attachment['id']} не прошло SHA-256; bytes исключены.")
                continue
            name = 'attachments/' + stored.name
            if name in names:
                raise ExportLimitError('Конфликт имён вложений: требуется проверка архива.')
            names.add(name)
            check, copied = hashlib.sha256(), 0
            fd = os.open(stored, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source, archive.open(name, 'w') as destination:
                for chunk in iter(lambda: source.read(65536), b''):
                    copied += len(chunk)
                    if copied > measured:
                        raise ExportLimitError('Вложение изменилось во время выгрузки.')
                    check.update(chunk)
                    destination.write(chunk)
            if copied != measured or check.hexdigest() != digest:
                raise ExportLimitError('Вложение изменилось во время выгрузки.')
            verified.append({'id': attachment['id'], 'size': measured, 'sha256': digest})
        archive.writestr('transcript.html', _html_transcript(order, topics, messages, attachments, events, offers, warnings))
        manifest = dict(order_id=int(order['id']), status=str(order['status']), topics=[dict(row) for row in topics],
                        message_count=len(messages), attachment_count=len(attachments), warnings=warnings, verified_files=verified)
        archive.writestr('manifest.json', checked_output(json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')))


@asynccontextmanager
async def export_order_archive(order_id: int):
    """Bounded on-disk ZIP; держать context открытым до окончания upload."""
    order = await get_archive_order(order_id)
    if order is None:
        raise ValueError('Заказ не найден')
    topics, messages, attachments, events, offers = await get_archive_contents(order_id)
    warnings = await disk_call(archive_problems, topics, messages, attachments)
    contained_path(EXPORT_TMP_ROOT, 'boundary-check', mkdir=True)
    if shutil.disk_usage(EXPORT_TMP_ROOT).free < 2 * MAX_ZIP_BYTES + MIN_EXPORT_FREE_BYTES:
        raise ExportLimitError('Недостаточно свободного места для безопасной выгрузки.')
    with tempfile.TemporaryDirectory(prefix='export_', dir=EXPORT_TMP_ROOT) as owned:
        output = Path(owned)/f'order_{order_id}.zip'
        await disk_call(_build_zip, output, order, topics, messages, attachments, events, offers, warnings)
        yield output, warnings
