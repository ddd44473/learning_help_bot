import asyncio
import hashlib
import json
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from database.chat_archive import insert_message, set_attachment_result, upsert_attachment
from database.activity_logs import get_user_activity_logs_for_export
import config
from handlers.admin_logs import build_activity_csv, build_activity_html
from services import archive_export, export_jobs
from utils import export_limits
from utils.export_limits import ExportLimitError
from utils.private_files import StorageBoundaryError
from test_archive_exports import prepare


@pytest.fixture
async def export_sample(db):
    await prepare(db)
    mid = await insert_message(order_id=1, archive_topic_id=1, source_chat_id=88101, source_message_id=10,
        sender_telegram_id=88101, sender_name='<b>name</b>', sender_role='client', content_type='document',
        body='<script>synthetic</script>', sent_at='2026-10-27 12:00:00')
    content = b'synthetic archive bytes'
    aid = await upsert_attachment(mid, 'document', 'file', '<task>.txt', 'text/plain', len(content))
    path = config.ARCHIVE_STORAGE_ROOT/'1/attachments/example.bin'
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    await set_attachment_result(aid, path='1/attachments/example.bin', sha256=hashlib.sha256(content).hexdigest())
    return aid, path, content


async def test_on_disk_zip_integrity_and_cleanup(export_sample):
    _, source, content = export_sample
    async with archive_export.export_order_archive(1) as (path, warnings):
        assert path.exists() and warnings
        with zipfile.ZipFile(path) as result:
            assert result.testzip() is None
            assert result.read('attachments/example.bin') == content
            html = result.read('transcript.html').decode()
            assert '&lt;script&gt;' in html and '<script>' not in html
            manifest = json.loads(result.read('manifest.json'))
            assert manifest['verified_files'][0]['sha256'] == hashlib.sha256(content).hexdigest()
    assert not path.exists() and source.read_bytes() == content


@pytest.mark.parametrize('damage', ['missing', 'hash', 'traversal', 'symlink'])
async def test_missing_corrupt_and_unsafe_paths(export_sample, db, damage, tmp_path):
    aid, source, content = export_sample
    if damage == 'missing':
        source.unlink()  # только synthetic fixture-owned file
    elif damage == 'hash':
        source.write_bytes(b'synthetic corruption')
    elif damage == 'traversal':
        await set_attachment_result(aid, path='../outside', sha256='0'*64)
    else:
        source.unlink()
        outside = tmp_path/'outside'
        outside.write_bytes(b'not archive bytes')
        source.symlink_to(outside)
    if damage in {'traversal', 'symlink'}:
        with pytest.raises(StorageBoundaryError):
            async with archive_export.export_order_archive(1):
                pytest.fail('unsafe archive must not be returned')
    else:
        async with archive_export.export_order_archive(1) as (path, warnings):
            with zipfile.ZipFile(path) as result:
                assert 'attachments/example.bin' not in result.namelist()
                assert warnings
    if damage == 'hash':
        assert source.read_bytes() != content  # exporter не переписывает и не удаляет source


async def test_zip_budget_rejects_and_cleans_owned_temp(export_sample, monkeypatch):
    _, source, _ = export_sample
    monkeypatch.setattr(archive_export, 'MAX_ZIP_BYTES', 100)
    with pytest.raises(ExportLimitError):
        async with archive_export.export_order_archive(1):
            pytest.fail('over-budget archive returned')
    assert source.exists()
    assert list(archive_export.EXPORT_TMP_ROOT.iterdir()) == []


async def test_database_byte_budget_precedes_serialization(db, monkeypatch):
    monkeypatch.setattr(export_limits, 'MAX_EXPORT_DATA_BYTES', 100)
    await db.execute("INSERT INTO user_activity_logs(telegram_id,action,details) VALUES (88101,'synthetic',?)", ('x'*200,))
    await db.commit()
    with pytest.raises(ExportLimitError):
        await get_user_activity_logs_for_export(88101, 'all')


async def test_unicode_html_csv_and_early_output_budget(monkeypatch):
    rows = [dict(telegram_id=88101, action='synthetic', entity_type=None, entity_id=None,
                 details=None, created_at='2026-10-27 12:00:00')]
    csv = build_activity_csv(rows, {88101: '\ufeff=FORMULA'})
    assert "'\ufeff=FORMULA" in csv.decode('utf-8-sig')
    html = build_activity_html(rows, {88101: '<Имя>'}, subject_name='<Предмет>', period_title='всё время').decode()
    assert '&lt;Имя&gt;' in html and '&lt;Предмет&gt;' in html
    monkeypatch.setattr(export_limits, 'MAX_EXPORT_OUTPUT_BYTES', 100)
    with pytest.raises(ExportLimitError):
        build_activity_csv(rows, {88101: 'Ё'*1000})
    with pytest.raises(ExportLimitError):
        build_activity_html(rows, subject_name='test', period_title='all')


async def test_concurrent_heavy_exports_have_admission_limit():
    entered = 0
    both = asyncio.Event()
    release = asyncio.Event()

    @export_jobs.bounded_export
    async def heavy(callback):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await release.wait()

    callback = SimpleNamespace(answer=AsyncMock())
    tasks = [asyncio.create_task(heavy(callback)) for _ in range(2)]
    await asyncio.wait_for(both.wait(), 2)
    try:
        await heavy(callback)
        assert entered == 2 and 'заняты' in callback.answer.call_args.args[0]
        # Обычная coroutine продолжает работать, пока export slots заняты.
        assert await asyncio.wait_for(asyncio.sleep(0, result='normal action'), 1) == 'normal action'
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_cancelled_thread_finishes_before_temp_cleanup(export_sample, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = archive_export._build_zip

    def paused(*args):
        entered.set()
        release.wait(5)
        return original(*args)

    monkeypatch.setattr(archive_export, '_build_zip', paused)

    async def run():
        async with archive_export.export_order_archive(1):
            pass

    task = asyncio.create_task(run())
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(archive_export.EXPORT_TMP_ROOT.iterdir()) == []
    assert export_sample[1].exists()
