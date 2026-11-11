"""ARC-01: SQLite intent → leased bounded consumers → verified private blob.

Нет download-задач на каждый update. Один consumer выполняет не более одной
задачи; DB ограничивает также суммарные живые claims разных процессов.
Неизвестный результат rename восстанавливается по заранее сохранённым hash/size.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import aiosqlite
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from database import db as database
import config
from utils.private_files import StorageBoundaryError, contained_path, file_digest, storage_bytes

logger = logging.getLogger(__name__)
JOB_PENDING = "pending"
JOB_IN_PROGRESS = "in_progress"
JOB_COMPLETED = "completed"
JOB_RETRYABLE = "retryable_failure"
JOB_PERMANENT = "permanent_failure"


@dataclass(frozen=True)
class WorkerConfig:
    max_concurrent: int = 2
    max_pending_jobs: int = 2000
    max_disk_bytes: int = 2 * 1024**3
    max_file_bytes: int = 20 * 1024**2  # hosted Telegram getFile boundary
    max_order_bytes: int = 200 * 1024**2
    max_order_files: int = 100
    min_free_bytes: int = 64 * 1024**2
    max_attempts: int = 5
    retry_base_seconds: float = 30
    retry_max_seconds: float = 1800
    poll_interval_seconds: float = 2
    download_timeout: float = 60
    stale_claim_seconds: float = 180

    def __post_init__(self):
        if min(self.max_concurrent, self.max_file_bytes, self.max_attempts, self.max_order_files) < 1:
            raise ValueError("Лимиты архивного воркера должны быть положительными")
        if self.max_file_bytes > 20 * 1024**2 or self.stale_claim_seconds <= self.download_timeout + 5:
            raise ValueError("Некорректные download/lease лимиты")
        if min(self.max_order_bytes, self.max_disk_bytes, self.retry_base_seconds, self.retry_max_seconds, self.poll_interval_seconds, self.download_timeout) <= 0 or min(self.max_pending_jobs, self.min_free_bytes) < 0:
            raise ValueError("Некорректные квоты или интервалы воркера")


@dataclass(frozen=True)
class Claim:
    id: int
    attachment_id: int
    token: str


class LostClaim(RuntimeError):
    pass


class PermanentDownloadError(ValueError):
    pass


async def disk_call(function, *args, **kwargs):
    """При отмене дожидаемся своего thread IO ДО закрытия fd/удаления temp."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class ArchiveDownloadWorker:
    def __init__(self, *, db_path=None, storage_root=None, config=None, bot=None, clock=time.time):
        self._db_path = db_path
        self._storage_root = storage_root
        self.config = config or WorkerConfig()
        self.bot = bot
        self.clock = clock
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def db_path(self):
        return self._db_path if self._db_path is not None else database.DB_PATH

    @property
    def storage_root(self):
        if self._storage_root is not None:
            return Path(self._storage_root)
        # Циклический lazy-импорт убран (Phase 3); единая точка правды --
        # config.ARCHIVE_STORAGE_ROOT.
        return config.ARCHIVE_STORAGE_ROOT

    @property
    def is_running(self):
        return any(not task.done() for task in self._tasks)

    async def start(self, bot=None):
        if self.is_running:
            return
        self.bot = bot if bot is not None else self.bot
        if self.bot is None:
            raise ValueError("Воркеру необходим явный Bot")
        self._stop.clear()
        # Никакого reset чужих активных claims и wildcard cleanup.
        self._tasks = [asyncio.create_task(self._run()) for _ in range(self.config.max_concurrent)]

    async def stop(self):
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def enqueue_download(self, attachment_id: int, *, connection=None) -> bool:
        if connection is None:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("BEGIN IMMEDIATE")
                result = await self.enqueue_download(attachment_id, connection=db)
                await db.commit()
                return result
        db = connection
        existing = await (await db.execute("SELECT 1 FROM chat_archive_download_jobs WHERE attachment_id=?", (attachment_id,))).fetchone()
        if existing:
            return True  # дубль не зависит от текущей ёмкости очереди
        attachment = await (await db.execute("SELECT save_status FROM chat_archive_attachments WHERE id=?", (attachment_id,))).fetchone()
        if attachment is None:
            raise ValueError("Неизвестное вложение")
        if attachment[0] == 'saved':
            return True  # существующие успешные bytes не трогаем
        active = (await (await db.execute("SELECT COUNT(*) FROM chat_archive_download_jobs WHERE status IN ('pending','in_progress','retryable_failure')")).fetchone())[0]
        if active >= self.config.max_pending_jobs:
            await db.execute("UPDATE chat_archive_attachments SET save_status='failed', save_error='queue_capacity' WHERE id=?", (attachment_id,))
            return False
        await db.execute("INSERT INTO chat_archive_download_jobs(attachment_id,max_attempts) VALUES (?,?)", (attachment_id, self.config.max_attempts))
        return True

    async def _run(self):
        while not self._stop.is_set():
            try:
                claim = await self._claim_job()
                if claim is not None:
                    await self._process_job(claim)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 -- consumer boundary, redacted error type only
                logger.error("Archive consumer error: %s", type(error).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), self.config.poll_interval_seconds)
            except TimeoutError:
                pass

    async def _claim_job(self) -> Claim | None:
        now, cfg = self.clock(), self.config
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            active = (await (await db.execute("SELECT COUNT(*) FROM chat_archive_download_jobs WHERE status='in_progress' AND lease_until>?", (now,))).fetchone())[0]
            if active >= cfg.max_concurrent:
                return None
            row = await (await db.execute(
                """SELECT j.*, a.file_size, m.order_id FROM chat_archive_download_jobs j
                JOIN chat_archive_attachments a ON a.id=j.attachment_id
                JOIN chat_archive_messages m ON m.id=a.message_id
                WHERE (j.status='pending' OR (j.status='retryable_failure' AND j.next_attempt_at<=?)
                    OR (j.status='in_progress' AND j.lease_until<=?))
                ORDER BY j.id LIMIT 1""", (now, now),
            )).fetchone()
            if row is None:
                return None
            if row['attempts'] >= row['max_attempts']:
                await self._terminal(db, row['id'], row['attachment_id'], 'attempts_exhausted')
                await db.commit()
                return None
            # Полная резервация даже при неизвестном размере; scan+reservations
            # консервативно могут считать live temp дважды, но не недосчитывают.
            need = cfg.max_file_bytes
            reason = None
            if row['file_size'] is not None and int(row['file_size']) > cfg.max_file_bytes:
                reason = 'file_size_limit'
            order_count = (await (await db.execute("SELECT COUNT(*) FROM chat_archive_attachments a JOIN chat_archive_messages m ON m.id=a.message_id WHERE m.order_id=? AND a.id<=?", (row['order_id'], row['attachment_id']))).fetchone())[0]
            if order_count > cfg.max_order_files:
                reason = 'order_file_limit'
            used = await disk_call(storage_bytes, self.storage_root)
            order_root = contained_path(self.storage_root, str(row['order_id']), mkdir=True)
            order_used = await disk_call(storage_bytes, order_root)
            reserved = (await (await db.execute("SELECT COALESCE(SUM(reserved_bytes),0) FROM chat_archive_download_jobs WHERE status='in_progress' AND lease_until>?", (now,))).fetchone())[0]
            order_reserved = (await (await db.execute("SELECT COALESCE(SUM(j.reserved_bytes),0) FROM chat_archive_download_jobs j JOIN chat_archive_attachments a ON a.id=j.attachment_id JOIN chat_archive_messages m ON m.id=a.message_id WHERE j.status='in_progress' AND j.lease_until>? AND m.order_id=?", (now, row['order_id']))).fetchone())[0]
            if used + reserved + need > cfg.max_disk_bytes or order_used + order_reserved + need > cfg.max_order_bytes:
                reason = 'storage_quota'
            if shutil.disk_usage(self.storage_root).free < cfg.min_free_bytes + reserved + need:
                reason = 'disk_space'
            if reason:
                await self._terminal(db, row['id'], row['attachment_id'], reason)
                await db.commit()
                return None
            token = uuid4().hex
            if row['status'] == JOB_IN_PROGRESS and row['lease_token'] and re.fullmatch(r'[0-9a-f]{32}', row['lease_token']):
                old_relative = f"{int(row['order_id'])}/attachments/arc_{row['attachment_id']}.bin.{row['lease_token']}.part"
                contained_path(self.storage_root, old_relative).unlink(missing_ok=True)
            await db.execute("UPDATE chat_archive_download_jobs SET status='in_progress', lease_token=?, lease_until=?, reserved_bytes=?, attempts=attempts+1, claimed_at=datetime('now','+3 hours') WHERE id=?", (token, now+cfg.stale_claim_seconds, need, row['id']))
            await db.commit()
            return Claim(int(row['id']), int(row['attachment_id']), token)

    @staticmethod
    async def _terminal(db, job_id, attachment_id, reason):
        await db.execute("UPDATE chat_archive_download_jobs SET status='permanent_failure',error=?,reserved_bytes=0,lease_until=0 WHERE id=?", (reason, job_id))
        await db.execute("UPDATE chat_archive_attachments SET save_status='failed',save_error=? WHERE id=? AND save_status!='saved'", (reason, attachment_id))

    async def _owned(self, db, claim):
        row = await (await db.execute("SELECT j.*, a.telegram_file_id, a.file_size, m.order_id FROM chat_archive_download_jobs j JOIN chat_archive_attachments a ON a.id=j.attachment_id JOIN chat_archive_messages m ON m.id=a.message_id WHERE j.id=? AND j.attachment_id=? AND j.lease_token=? AND j.status='in_progress' AND j.lease_until>?", (claim.id, claim.attachment_id, claim.token, self.clock()))).fetchone()
        if row is None:
            raise LostClaim("Claim уже недействителен")
        return row

    async def _process_job(self, claim: Claim):
        if not isinstance(claim, Claim):
            raise TypeError("Обработка требует атомарно полученный Claim")
        try:
            await self._download_and_finalize(claim)
        except asyncio.CancelledError:
            await self._fail_job(claim, 'cancelled', release=True)
            raise
        except LostClaim:
            return
        except Exception as error:  # noqa: BLE001 -- bounded retry protocol for all download failures
            permanent = isinstance(error, (PermanentDownloadError, StorageBoundaryError, TelegramBadRequest, TelegramForbiddenError, PermissionError))
            # Только коды, не str(exc): Telegram URL может содержать token.
            await self._fail_job(claim, type(error).__name__, permanent=permanent,
                retry_after=error.retry_after if isinstance(error, TelegramRetryAfter) else 0)

    async def _download_and_finalize(self, claim):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await self._owned(db, claim)
        relative = f"{int(row['order_id'])}/attachments/arc_{claim.attachment_id}.bin"
        output = contained_path(self.storage_root, relative, mkdir=True)
        temp = contained_path(self.storage_root, relative + f'.{claim.token}.part')
        # Только явно сохранённые hash/size разрешают adopt после rename crash.
        if output.exists():
            if not row['content_sha256'] or row['content_size'] is None:
                raise PermanentDownloadError('unexpected_existing_file')
            digest, size = await disk_call(file_digest, output, self.config.max_file_bytes)
            if (digest, size) != (row['content_sha256'], row['content_size']):
                raise PermanentDownloadError('integrity_mismatch')
            await self._complete(claim, relative, digest, size, None)
            return
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                digest, size = hashlib.sha256(), 0
                async with asyncio.timeout(self.config.download_timeout):
                    info = await self.bot.get_file(row['telegram_file_id'])
                    if not info.file_path:
                        raise PermanentDownloadError('file_path_missing')
                    if info.file_size is not None and info.file_size > self.config.max_file_bytes:
                        raise PermanentDownloadError('file_size_limit')
                    url = self.bot.session.api.file_url(self.bot.token, info.file_path)
                    async for chunk in self.bot.session.stream_content(url, timeout=self.config.download_timeout, chunk_size=65536):
                        size += len(chunk)
                        if size > self.config.max_file_bytes:
                            raise PermanentDownloadError('file_size_limit')
                        if shutil.disk_usage(self.storage_root).free < self.config.min_free_bytes:
                            raise OSError('disk_space')
                        await disk_call(stream.write, chunk)
                        digest.update(chunk)
                if info.file_size is not None and size != info.file_size:
                    raise PermanentDownloadError('size_mismatch')
                await disk_call(stream.flush)
                await disk_call(os.fsync, stream.fileno())
            hexdigest = digest.hexdigest()
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute('BEGIN IMMEDIATE')
                await self._owned(db, claim)
                await db.execute("UPDATE chat_archive_download_jobs SET content_sha256=?,content_size=?,relative_path=? WHERE id=?", (hexdigest, size, relative, claim.id))
                await db.commit()
            await self._complete(claim, relative, hexdigest, size, temp)
        finally:
            # Только temp нашего точного lease. Архивные успешные blobs не удаляем.
            temp.unlink(missing_ok=True)

    async def _complete(self, claim, relative, digest, size, temp):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            await self._owned(db, claim)
            output = contained_path(self.storage_root, relative)
            if temp is not None:
                # hard-link является atomic no-clobber publish на том же filesystem.
                await disk_call(os.link, temp, output)
            await db.execute("UPDATE chat_archive_attachments SET save_status='saved',relative_path=?,sha256=?,file_size=?,save_error=NULL WHERE id=?", (relative, digest, size, claim.attachment_id))
            await db.execute("UPDATE chat_archive_download_jobs SET status='completed',error=NULL,reserved_bytes=0,lease_until=0 WHERE id=? AND lease_token=?", (claim.id, claim.token))
            await db.commit()

    async def _fail_job(self, claim, reason, *, permanent=False, release=False, retry_after=0):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            try:
                row = await self._owned(db, claim)
            except LostClaim:
                return
            if permanent or row['attempts'] >= row['max_attempts']:
                await self._terminal(db, claim.id, claim.attachment_id, reason)
            else:
                backoff = 0 if release else min(self.config.retry_base_seconds * 2**min(row['attempts']-1, 20), self.config.retry_max_seconds)
                backoff = max(backoff, retry_after)
                await db.execute("UPDATE chat_archive_download_jobs SET status='retryable_failure',error=?,next_attempt_at=?,lease_until=0,reserved_bytes=0 WHERE id=? AND lease_token=?", (reason, self.clock()+backoff, claim.id, claim.token))
            await db.commit()


archive_worker = ArchiveDownloadWorker()


async def enqueue_attachment_download(attachment_id: int) -> bool:
    return await archive_worker.enqueue_download(attachment_id)
