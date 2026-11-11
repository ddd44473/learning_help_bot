"""ARC-01: реальные file-backed FK/claims; fake streaming Telegram без сети."""
import asyncio
import errno
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import aiosqlite
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetFile
from aiogram.types import Chat, Document, Message, User

from database.chat_archive import init_chat_archive, insert_message, upsert_attachment
from services import archive_worker as module
from services.archive_worker import ArchiveDownloadWorker, Claim, WorkerConfig


class FakeBot:
    token = '123456:synthetic'

    def __init__(self, content=b'synthetic file content'):
        self.content = content
        self.get_file_calls = []
        self.download_calls = []
        self.error = None
        self.no_file_path = False
        self.entered = asyncio.Event()
        self.two_entered = asyncio.Event()
        self.release = None
        self.active = self.max_active = 0
        self.session = SimpleNamespace(api=SimpleNamespace(file_url=lambda *_: 'https://invalid.local/synthetic'), stream_content=self.stream)

    async def get_file(self, file_id):
        self.get_file_calls.append(file_id)
        if isinstance(self.error, TelegramBadRequest):
            raise self.error
        return SimpleNamespace(file_path=None if self.no_file_path else 'synthetic/file', file_size=len(self.content))

    async def stream(self, url, **kwargs):
        self.download_calls.append(url)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        if self.active == 2:
            self.two_entered.set()
        try:
            yield self.content[:4]
            if self.release:
                await self.release.wait()
            if self.error:
                raise self.error
            yield self.content[4:]
        finally:
            self.active -= 1


@pytest.fixture
async def any_worker(tmp_path, use_db_path):
    from database.db import init_db
    from database.migrations import run_migrations
    path = tmp_path / 'test.db'
    use_db_path(path)
    await init_db()
    await run_migrations()
    await init_chat_archive()
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO users(id,telegram_id) VALUES (1,88101)")
        await db.execute("INSERT INTO orders(id,user_id,subject,work_type,description) VALUES (1,1,'synthetic','solution','synthetic')")
        await db.execute("INSERT INTO chat_archive_topics(order_id,group_chat_id,topic_id) VALUES (1,-10088001,1)")
        await db.commit()
    now = [1000.0]
    bot = FakeBot()
    worker = ArchiveDownloadWorker(db_path=path, storage_root=tmp_path/'arc_storage', bot=bot,
        config=WorkerConfig(max_file_bytes=100, max_pending_jobs=10, min_free_bytes=0, poll_interval_seconds=0.01), clock=lambda: now[0])
    worker.test_clock = now
    yield worker, bot, worker.storage_root, path
    await worker.stop()
    async with aiosqlite.connect(path) as db:
        assert await (await db.execute('PRAGMA foreign_key_check')).fetchall() == []
        assert (await (await db.execute('PRAGMA integrity_check')).fetchone())[0] == 'ok'


async def _insert_attachment(path, message_id=1, file_id='file-1', size=22):
    # Telegram source ID не является внутренним PK chat_archive_messages.
    archive_id = await insert_message(order_id=1, archive_topic_id=1, source_chat_id=-10088001,
        source_message_id=message_id, sender_telegram_id=88101, sender_name='Synthetic', sender_role='client',
        content_type='document', body='synthetic body', sent_at='2026-10-27 12:00:00')
    return await upsert_attachment(archive_id, 'document', file_id, 'task.txt', 'text/plain', size)


async def _row(path, table, key):
    assert table in {'chat_archive_download_jobs', 'chat_archive_attachments'}
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(f'SELECT * FROM {table} WHERE id=?', (key,))).fetchone()
    return dict(row) if row else None


async def _attempt(worker):
    claim = await worker._claim_job()
    assert isinstance(claim, Claim)
    await worker._process_job(claim)
    return await _row(worker.db_path, 'chat_archive_download_jobs', claim.id)


async def test_document_and_media_group_enqueued_without_download(any_worker):
    worker, bot, root, path = any_worker
    for source_id in (10, 11, 12):
        attachment = await _insert_attachment(path, source_id, f'file-{source_id}')
        assert await worker.enqueue_download(attachment)
    assert (await _row(path, 'chat_archive_download_jobs', 1))['attempts'] == 0
    assert not bot.get_file_calls and not root.exists()


async def test_worker_downloads_and_hashes(any_worker):
    worker, bot, root, path = any_worker
    aid = await _insert_attachment(path, message_id=50)
    await worker.enqueue_download(aid)
    result = await _attempt(worker)
    assert result['status'] == 'completed' and result['attempts'] == 1
    attachment = await _row(path, 'chat_archive_attachments', aid)
    assert attachment['save_status'] == 'saved'
    assert attachment['sha256'] == hashlib.sha256(bot.content).hexdigest()
    assert attachment['file_size'] == len(bot.content)
    assert (root/attachment['relative_path']).read_bytes() == bot.content
    assert bot.get_file_calls == ['file-1']
    assert not list(root.rglob('*.part'))


async def test_slow_download_does_not_block_relay(any_worker):
    # В реальном capture API metadata+job завершаются, пока download закрыт Event.
    from services.chat_capture import capture_client_message
    worker, bot, _, path = any_worker
    bot.release = asyncio.Event()
    original = module.archive_worker
    module.archive_worker = worker
    try:
        message = Message(message_id=40, date=datetime.now(timezone.utc), chat=Chat(id=88101, type='private'),
            from_user=User(id=88101, is_bot=False, first_name='Synthetic'), media_group_id='album',
            document=Document(file_id='file-40', file_unique_id='unique-40', file_size=22))
        entry = await capture_client_message(message, bot, order_id=1, group_chat_id=-10088001, topic_id=1)
        claim = await worker._claim_job()
        task = asyncio.create_task(worker._process_job(claim))
        await asyncio.wait_for(bot.entered.wait(), 2)
        # Эквивалент lightweight capture следующего элемента перед relay не ждёт binary IO.
        second = message.model_copy(update={'message_id': 41})
        assert await asyncio.wait_for(capture_client_message(second, bot, order_id=1, group_chat_id=-10088001, topic_id=1), 2) != entry
        assert not task.done()
        bot.release.set()
        await task
    finally:
        module.archive_worker = original
        bot.release.set()


async def test_download_failure_becomes_permanent(any_worker):
    worker, bot, _, path = any_worker
    bot.error = TelegramBadRequest(method=GetFile(file_id='synthetic'), message='wrong file id')
    aid = await _insert_attachment(path)
    await worker.enqueue_download(aid)
    row = await _attempt(worker)
    assert row['status'] == 'permanent_failure' and row['error'] == 'TelegramBadRequest'
    assert (await _row(path, 'chat_archive_attachments', aid))['save_status'] == 'failed'


async def test_invalid_file_path_and_oversized_file(any_worker):
    worker, bot, _, path = any_worker
    bot.no_file_path = True
    aid = await _insert_attachment(path)
    await worker.enqueue_download(aid)
    assert (await _attempt(worker))['status'] == 'permanent_failure'
    bot.no_file_path = False
    other = await _insert_attachment(path, 2, 'file-2', size=9999)
    await worker.enqueue_download(other)
    assert await worker._claim_job() is None
    assert (await _row(path, 'chat_archive_download_jobs', 2))['error'] == 'file_size_limit'


async def test_transient_failure_retries_then_permanent(any_worker):
    worker, bot, _, path = any_worker
    worker.config = replace(worker.config, max_attempts=2)
    bot.error = OSError(errno.ENOSPC, 'synthetic disk full')
    aid = await _insert_attachment(path)
    await worker.enqueue_download(aid)
    first = await _attempt(worker)
    assert first['status'] == 'retryable_failure'
    assert first['next_attempt_at'] == 1030
    assert await worker._claim_job() is None
    worker.test_clock[0] = 1030
    assert (await _attempt(worker))['status'] == 'permanent_failure'


async def test_partial_temp_file_cleaned_on_failure(any_worker):
    worker, bot, root, path = any_worker
    bot.error = PermissionError('synthetic')
    aid = await _insert_attachment(path)
    await worker.enqueue_download(aid)
    assert (await _attempt(worker))['status'] == 'permanent_failure'
    assert list(root.rglob('*.part')) == []


async def test_restart_resumes_pending_without_duplicates(any_worker):
    worker, bot, _, path = any_worker
    aid = await _insert_attachment(path)
    await worker.enqueue_download(aid)
    old = await worker._claim_job()
    other = ArchiveDownloadWorker(db_path=path, storage_root=worker.storage_root, bot=bot, config=worker.config, clock=worker.clock)
    assert await other._claim_job() is None  # живой lease не сбрасывается restart'ом
    worker.test_clock[0] += 181
    new = await other._claim_job()
    assert new.token != old.token
    await worker._process_job(old)
    assert not bot.get_file_calls
    await other._process_job(new)
    assert (await _row(path, 'chat_archive_download_jobs', 1))['status'] == 'completed'


async def test_two_workers_do_not_double_download(any_worker):
    worker, bot, root, path = any_worker
    aid = await _insert_attachment(path)
    await worker.enqueue_download(aid)
    other = ArchiveDownloadWorker(db_path=path, storage_root=root, bot=bot, config=worker.config, clock=worker.clock)
    claims = await asyncio.gather(worker._claim_job(), other._claim_job())
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim is not None)
    await worker._process_job(claim)
    assert bot.get_file_calls == ['file-1']


async def test_backlog_capacity_reached(any_worker):
    worker, _, _, path = any_worker
    worker.config = replace(worker.config, max_pending_jobs=1)
    first = await _insert_attachment(path)
    second = await _insert_attachment(path, 2, 'file-2')
    assert await worker.enqueue_download(first)
    assert await worker.enqueue_download(first)  # duplicate при полной очереди
    assert not await worker.enqueue_download(second)
    assert (await _row(path, 'chat_archive_attachments', second))['save_error'] == 'queue_capacity'


async def test_shutdown_keeps_jobs(any_worker):
    worker, bot, root, path = any_worker
    bot.release = asyncio.Event()
    for i in range(3):
        await worker.enqueue_download(await _insert_attachment(path, 10+i, f'file-{i}'))
    await worker.start()
    await asyncio.wait_for(bot.two_entered.wait(), 3)
    assert bot.max_active == 2 and len(worker._tasks) == 2
    await worker.stop()
    assert not worker.is_running and bot.active == 0
    assert list(root.rglob('*.part')) == []
    async with aiosqlite.connect(path) as db:
        states = await (await db.execute('SELECT status FROM chat_archive_download_jobs ORDER BY id')).fetchall()
    assert states == [('retryable_failure',), ('retryable_failure',), ('pending',)]


async def test_idempotent_duplicate_enqueue(any_worker):
    worker, bot, _, path = any_worker
    aid = await _insert_attachment(path)
    await worker.enqueue_download(aid)
    await _attempt(worker)
    assert await worker.enqueue_download(aid)
    assert await worker._claim_job() is None
    assert len(bot.get_file_calls) == 1


async def test_crash_after_publish_adopts_verified_file(any_worker, monkeypatch):
    worker, bot, root, path = any_worker
    aid = await _insert_attachment(path)
    await worker.enqueue_download(aid)
    original = module.disk_call

    class Crash(BaseException):
        pass

    async def injected(function, *args):
        result = await original(function, *args)
        if function is module.os.link:
            raise Crash()
        return result

    monkeypatch.setattr(module, 'disk_call', injected)
    with pytest.raises(Crash):
        await _attempt(worker)
    assert len(list(root.rglob('*.bin'))) == 1
    assert (await _row(path, 'chat_archive_attachments', aid))['save_status'] == 'pending'
    monkeypatch.setattr(module, 'disk_call', original)
    worker.test_clock[0] += 181
    assert (await _attempt(worker))['status'] == 'completed'
    assert len(bot.get_file_calls) == 1


@pytest.mark.parametrize('reason', ['storage_quota', 'order_quota', 'stream_size', 'symlink'])
async def test_resource_and_path_boundaries(any_worker, reason, tmp_path):
    worker, bot, root, path = any_worker
    if reason == 'storage_quota':
        worker.config = replace(worker.config, max_disk_bytes=99)
    elif reason == 'order_quota':
        worker.config = replace(worker.config, max_order_bytes=99)
    elif reason == 'stream_size':
        bot.content = b'x'*101
    else:
        root.mkdir()
        (root/'1').symlink_to(tmp_path/'outside')
    aid = await _insert_attachment(path, size=None)
    await worker.enqueue_download(aid)
    if reason == 'symlink':
        from utils.private_files import StorageBoundaryError
        with pytest.raises(StorageBoundaryError):
            await worker._claim_job()
    elif reason in {'storage_quota', 'order_quota'}:
        assert await worker._claim_job() is None
        assert (await _row(path, 'chat_archive_attachments', aid))['save_status'] == 'failed'
    else:
        assert (await _attempt(worker))['status'] == 'permanent_failure'
    assert not list(root.rglob('*.bin'))


async def test_download_timeout_is_retryable_and_cleans_partial(any_worker):
    worker, bot, root, path = any_worker
    worker.config = replace(worker.config, download_timeout=0.05)
    bot.release = asyncio.Event()
    await worker.enqueue_download(await _insert_attachment(path))
    row = await _attempt(worker)
    assert row['status'] == 'retryable_failure' and row['error'] == 'TimeoutError'
    assert not list(root.rglob('*.part')) and bot.active == 0


async def test_enforced_size_during_stream_with_unknown_provider_size(any_worker, monkeypatch):
    worker, bot, root, path = any_worker
    bot.content = b'x'*101

    async def info(_):
        return SimpleNamespace(file_path='synthetic', file_size=None)

    monkeypatch.setattr(bot, 'get_file', info)
    await worker.enqueue_download(await _insert_attachment(path, size=None))
    assert (await _attempt(worker))['status'] == 'permanent_failure'
    assert bot.download_calls and not list(root.rglob('*.part'))


async def test_capture_attachment_and_job_commit_together(any_worker, monkeypatch):
    from services import chat_capture
    worker, _, _, path = any_worker
    entry = await insert_message(order_id=1, archive_topic_id=1, source_chat_id=88101, source_message_id=50,
        sender_telegram_id=88101, sender_name='Synthetic', sender_role='client', content_type='document',
        body=None, sent_at='2026-10-27 12:00:00')
    monkeypatch.setattr(chat_capture, 'archive_worker', worker)

    async def fail(*args, **kwargs):
        raise RuntimeError('after metadata insert before commit')

    monkeypatch.setattr(worker, 'enqueue_download', fail)
    with pytest.raises(RuntimeError):
        await chat_capture._enqueue_attachment(entry, 'document', 'file', 'task.txt', None, 22,
            group_chat_id=-10088001, topic_id=1, order_id=1)
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute('SELECT COUNT(*) FROM chat_archive_attachments')).fetchone())[0] == 0
        assert (await (await db.execute('SELECT COUNT(*) FROM chat_archive_download_jobs')).fetchone())[0] == 0


async def test_pending_job_retains_historical_actor_topic_after_ban_reassign(any_worker):
    from database.access_control import ban_user
    worker, bot, _, path = any_worker
    aid = await _insert_attachment(path)
    await worker.enqueue_download(aid)
    await ban_user(88101, None, 88001)
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO masters(telegram_id,full_name) VALUES (88902,'Synthetic')")
        await db.execute('UPDATE orders SET assigned_master_telegram_id=88902, assignment_generation=assignment_generation+1 WHERE id=1')
        await db.commit()
    assert (await _attempt(worker))['status'] == 'completed'
    async with aiosqlite.connect(path) as db:
        row = await (await db.execute('SELECT order_id,archive_topic_id,sender_telegram_id,sender_role FROM chat_archive_messages')).fetchone()
        assert tuple(row) == (1,1,88101,'client')
    assert len(bot.get_file_calls) == 1  # worker выполняет только download, не уведомляет новых участников
