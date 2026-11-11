"""Малый воспроизводимый workload; не capacity/stress benchmark."""
import asyncio
import hashlib
import importlib.util
import json
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.types import Chat, Document, Message, User

from database.chat_archive import set_attachment_result
from services import archive_export, chat_capture
from test_dispatcher_phase2 import LocalSession
import test_export_phase2 as samples

export_sample = samples.export_sample


async def test_bounded_synthetic_measurements(db, tmp_path, export_sample):
    aid, source, _ = export_sample
    # 1 MiB с неповторяющимся шаблоном; нагрузка намеренно ограничена.
    content = bytes(range(256)) * 4096
    source.write_bytes(content)
    await set_attachment_result(aid, path='1/attachments/example.bin', sha256=hashlib.sha256(content).hexdigest())
    await db.execute('UPDATE chat_archive_attachments SET file_size=? WHERE id=?', (len(content),aid))
    await db.commit()
    gaps, stop = [], asyncio.Event()

    async def heartbeat():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            gaps.append(now-last)
            last = now

    task = asyncio.create_task(heartbeat())
    tracemalloc.start()
    started = time.perf_counter()
    async with archive_export.export_order_archive(1) as (output, _):
        size = output.stat().st_size
    elapsed = time.perf_counter()-started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    stop.set()
    await task
    assert gaps and source.exists() and size < archive_export.MAX_ZIP_BYTES

    # Базовый capture загружается только из source-only checkpoint; storage
    # явно направлен в synthetic root до первого вызова. Реальных данных нет.
    checkpoint = Path('/home/igor/coliseum_build_checkpoints/phase2_prebuild/services/chat_capture.py')
    spec = importlib.util.spec_from_file_location('phase1_capture_measurement', checkpoint)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    baseline.STORAGE_ROOT = tmp_path/'baseline_synthetic_storage'
    session = LocalSession()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(url, **kwargs):
        entered.set()
        await release.wait()
        yield b'test'

    session.stream_content = delayed
    bot = Bot('123456:synthetic-test-token', session=session)
    message = Message(message_id=88, date=datetime.now(timezone.utc), chat=Chat(id=88101,type='private'),
        from_user=User(id=88101,is_bot=False,first_name='Synthetic'), document=Document(file_id='synthetic',file_unique_id='u',file_size=4))
    started = time.perf_counter()
    old_task = asyncio.create_task(baseline.capture_client_message(message, bot, order_id=1,group_chat_id=-10088001,topic_id=1))
    await asyncio.wait_for(entered.wait(), 2)
    assert not old_task.done()
    await asyncio.sleep(0.05)  # известная задержка, не механизм синхронизации гонки
    release.set()
    await old_task
    baseline_seconds = time.perf_counter()-started
    started = time.perf_counter()
    await chat_capture.capture_client_message(message.model_copy(update={'message_id':89}), bot,
        order_id=1,group_chat_id=-10088001,topic_id=1)
    capture_seconds = time.perf_counter()-started
    result = dict(workload_bytes=len(content), zip_bytes=size, export_seconds=elapsed,
        traced_peak_bytes=peak, heartbeat_max_gap_seconds=max(gaps),
        phase1_capture_with_50ms_delay_seconds=baseline_seconds, phase2_capture_seconds=capture_seconds)
    Path('/tmp/opencode/study_bot_audit_tests/phase2_measurements.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    await session.close()
