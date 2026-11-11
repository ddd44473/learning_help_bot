"""ORD-01: synthetic SQLite + fault injection, never live Telegram/data."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import test_ord01_create_order_dedup as dedup_tests
from test_ord01_create_order_dedup import ORDER_KWARGS, fetch_many
from test_rel01_topic_checkpoints import StubBot

from database import orders, chat_archive
from utils import order_card

prepared_db = dedup_tests.prepared_db

@pytest.fixture
async def atomic_shop(prepared_db):
    await chat_archive.init_chat_archive()
    return prepared_db


def refs(count=2):
    return [dict(telegram_file_id=f"synthetic-{i}", file_type="document",
                 file_name=f"synthetic-{i}.txt") for i in range(count)]


async def create(**kwargs):
    return await orders.create_order(
        **ORDER_KWARGS, direction="Математика", deadline="31-12-2030",
        dedup_key="order_confirm:123456:7770001:atomic-draft",
        dedup_content_hash="synthetic-draft-hash", **kwargs,
    )


async def test_isolation_before_fault_tests(fresh_database, tmp_path):
    import os
    import socket
    from pathlib import Path
    from database import db
    from utils import section_images

    root = Path('/tmp/opencode/study_bot_audit_tests').resolve()
    assert db.DB_PATH.resolve().is_relative_to(root)
    assert 'run_' in db.DB_PATH.parent.name
    assert str(db.DB_PATH) == os.environ['STUDY_BOT_DB_PATH']
    assert section_images.IMAGES_DIR.is_relative_to(tmp_path)
    # Deliberately invalid paths must be rejected BEFORE SQLite opens them.
    for connect in (aiosqlite.connect, __import__('sqlite3').connect):
        with pytest.raises(RuntimeError, match='ЗАЩИТА ТЕСТОВ'):
            connect('/outside-synthetic-root/forbidden.db')
    with pytest.raises(RuntimeError, match='сетевые соединения запрещены'):
        socket.create_connection(('synthetic.invalid', 443))


@pytest.mark.parametrize('count', [0, 1, 3])
@pytest.mark.parametrize('master', [None, 999001])
async def test_atomic_named_free_with_files(atomic_shop, count, master):
    order_id, fresh = await create(files=refs(count), assigned_master_telegram_id=master)
    assert fresh
    rows = await fetch_many(atomic_shop, 'SELECT * FROM order_files ORDER BY id')
    assert [(r['order_id'], r['telegram_file_id']) for r in rows] == [
        (order_id, ref['telegram_file_id']) for ref in refs(count)]
    cp = await chat_archive.get_topic_checkpoint(order_id)
    assert cp['detail'] == 'local_creation_pending' and cp['topic_id'] == 0
    assert cp['assignment_generation'] == 0
    row = (await fetch_many(atomic_shop, 'SELECT * FROM orders'))[0]
    assert row['deadline'] == '31-12-2030' and row['description'] == ORDER_KWARGS['description']
    assert row['assignment_status'] == ('open' if master is None else 'assigned')


@pytest.mark.parametrize('interrupt', [RuntimeError, asyncio.CancelledError])
async def test_mid_attachment_failure_rolls_back_and_retries(atomic_shop, monkeypatch, interrupt):
    original = aiosqlite.Connection.execute
    inserts = 0

    async def injected(self, sql, parameters=()):
        nonlocal inserts
        if 'INSERT INTO order_files' in sql:
            inserts += 1
            if inserts == 2:
                raise interrupt('synthetic interruption')
        return await original(self, sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, 'execute', injected)
        with pytest.raises(interrupt):
            await create(files=refs())
    for table in ('orders', 'order_files', 'operation_log', 'order_topic_checkpoints', 'order_master_attempts'):
        assert await fetch_many(atomic_shop, f'SELECT * FROM {table}') == []
    order_id, fresh = await create(files=refs())
    assert fresh and len(await orders.get_order_files(order_id)) == 2


async def test_legacy_creation_has_no_automatic_retry_marker(atomic_shop):
    # Historical callers have no evidence that attachment finalization finished.
    order_id, _ = await create()
    assert await chat_archive.get_topic_checkpoint(order_id) is None
    bot = StubBot()
    assert await order_card.recover_unfinished_topic_publications(bot) == []
    assert not bot.calls


async def test_handler_crash_during_second_ref_is_atomic(atomic_shop, monkeypatch):
    from handlers import order_form
    data = dict(draft_id='handler-draft', confirmation_message_id=50,
                direction='Математика', work_type='solution', description='synthetic',
                assignment_mode='specific', assigned_master_telegram_id=999001,
                files=refs())
    state = SimpleNamespace(get_data=AsyncMock(return_value=data),
                            set_state=AsyncMock(), clear=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=7770001), answer=AsyncMock(),
        message=SimpleNamespace(message_id=50, edit_reply_markup=AsyncMock(), answer=AsyncMock()))
    original = aiosqlite.Connection.execute
    count = 0

    async def crash(self, sql, parameters=()):
        nonlocal count
        if 'INSERT INTO order_files' in sql:
            count += 1
            if count == 2:
                raise asyncio.CancelledError('synthetic process interruption')
        return await original(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, 'execute', crash)
    with pytest.raises(asyncio.CancelledError):
        await order_form.confirm_order(callback, state, SimpleNamespace(id=123456))
    assert state.set_state.call_args.args[0] == order_form.OrderState.waiting_for_confirmation
    state.clear.assert_not_awaited()
    assert await fetch_many(atomic_shop, 'SELECT * FROM orders') == []
    assert await fetch_many(atomic_shop, 'SELECT * FROM order_files') == []
    assert await fetch_many(atomic_shop, 'SELECT * FROM operation_log') == []


@pytest.fixture
def publisher(monkeypatch):
    async def noop(*args, **kwargs):
        return None
    # Exercise the real publisher/DB; only notifications and archive download
    # capture are irrelevant to this fault protocol and are replaced here.
    import services.chat_capture
    monkeypatch.setattr(services.chat_capture, 'capture_initial_order', noop)
    monkeypatch.setattr(order_card, '_notify_assigned_master_about_order', noop)
    monkeypatch.setattr(order_card, '_notify_eligible_masters_about_open_order', noop)
    return StubBot()


@pytest.mark.parametrize('master', [None, 999001])
async def test_commit_then_crash_before_publisher_is_recoverable(atomic_shop, publisher, master):
    order_id, _ = await create(files=refs(), assigned_master_telegram_id=master)
    # This is the exact durable state a fresh process sees after local COMMIT:
    # no publisher function has been called and all file refs already exist.
    assert not publisher.calls
    assert (await chat_archive.get_topic_checkpoint(order_id))['detail'] == 'local_creation_pending'
    assert await order_card.recover_unfinished_topic_publications(publisher) == [order_id]
    assert (await chat_archive.get_topic_checkpoint(order_id))['state'] == 'published'
    assert len(await orders.get_order_files(order_id)) == 2
    assert sum(method == 'send_document' for method, _ in publisher.calls) == 2
    assert sum(method == 'create_forum_topic' for method, _ in publisher.calls) == 1
    assert await order_card.recover_unfinished_topic_publications(publisher) == []
    assert await create(files=refs(), assigned_master_telegram_id=master) == (order_id, False)
    assert len(await fetch_many(atomic_shop, 'SELECT * FROM orders')) == 1
    assert await fetch_many(atomic_shop, 'SELECT * FROM balance_transactions') == []
    assert (await fetch_many(atomic_shop, 'SELECT balance FROM users'))[0]['balance'] == 1000


async def test_remote_calls_never_hold_write_transaction(atomic_shop, publisher):
    order_id, _ = await create(files=refs(1))
    original = publisher.create_forum_topic

    async def remote(**kwargs):
        # A separate realistic writer must be able to acquire the lock during
        # external I/O. Fail quickly rather than hiding an open transaction.
        async with aiosqlite.connect(atomic_shop, timeout=0.1) as db:
            await db.execute('BEGIN IMMEDIATE')
            await db.rollback()
        return await original(**kwargs)

    publisher.create_forum_topic = remote
    assert await order_card.recover_unfinished_topic_publications(publisher) == [order_id]


@pytest.mark.parametrize('fault', ['reserved_before_call', 'timeout', 'cancel', 'checkpoint_write'])
async def test_ambiguous_remote_outcomes_are_never_recreated(atomic_shop, publisher, monkeypatch, fault):
    order_id, _ = await create(files=refs(1), assigned_master_telegram_id=999001)
    if fault == 'reserved_before_call':
        assert await chat_archive.reserve_topic_creation(order_id, order_card.MASTER_GROUP_ID, 0)
    else:
        with monkeypatch.context() as patch:
            if fault in ('timeout', 'cancel'):
                async def uncertain(**kwargs):
                    publisher.calls.append(('create_forum_topic', 1))
                    if fault == 'cancel':
                        raise asyncio.CancelledError()
                    raise TimeoutError('synthetic uncertain remote result')
                patch.setattr(publisher, 'create_forum_topic', uncertain)
            else:
                async def failed_write(**kwargs):
                    raise RuntimeError('synthetic checkpoint write failure')
                patch.setattr(order_card, 'upsert_topic_checkpoint', failed_write)
            with pytest.raises((RuntimeError, TimeoutError, asyncio.CancelledError)):
                await order_card.create_order_assignment_topic(publisher, order_id)
    count_before = len(publisher.calls)
    cp = await chat_archive.get_topic_checkpoint(order_id)
    assert cp['topic_id'] == 0 and cp['detail'] == 'create_outcome_unknown_manual_review'
    assert await order_card.recover_unfinished_topic_publications(publisher) == []
    assert len(publisher.calls) == count_before
    assert len(await orders.get_order_files(order_id)) == 1


@pytest.mark.parametrize('count', [2, 8])
async def test_duplicate_confirmations_have_one_order_and_file_set(atomic_shop, publisher, count):
    barrier = asyncio.Barrier(count)
    async def confirm():
        await barrier.wait()
        return await create(files=refs(), assigned_master_telegram_id=999001)
    results = await asyncio.gather(*(confirm() for _ in range(count)))
    assert sum(fresh for _, fresh in results) == 1
    assert len({order_id for order_id, _ in results}) == 1
    assert len(await fetch_many(atomic_shop, 'SELECT * FROM order_files')) == 2
    assert len(await fetch_many(atomic_shop, 'SELECT * FROM operation_log')) == 1
    recovered = await asyncio.gather(
        order_card.recover_unfinished_topic_publications(publisher),
        order_card.recover_unfinished_topic_publications(publisher),
    )
    assert any(recovered)
    assert sum(method == 'create_forum_topic' for method, _ in publisher.calls) == 1
    assert (await chat_archive.get_topic_checkpoint(results[0][0]))['state'] == 'published'


@pytest.mark.parametrize('payload', [
    [dict(file_type='document')],
    [dict(telegram_file_id='', file_type='photo')],
    [dict(telegram_file_id='synthetic', file_type='video')],
    [dict(telegram_file_id='synthetic', file_type='document', order_id=999)],
    [dict(telegram_file_id='synthetic', file_type='document', id=999)],
    [dict(telegram_file_id='synthetic', file_type='document', relative_path='../private')],
    [dict(telegram_file_id='with whitespace', file_type='photo')],
    [dict(telegram_file_id='synthetic', file_type='photo', file_name=12)],
    refs(11), ['not-a-ref'], 'not-a-list',
])
async def test_invalid_or_foreign_row_payload_cannot_partially_commit(atomic_shop, payload):
    with pytest.raises(ValueError):
        await create(files=payload)
    for table in ('orders', 'order_files', 'operation_log', 'order_topic_checkpoints'):
        assert await fetch_many(atomic_shop, f'SELECT * FROM {table}') == []


async def test_repeated_opaque_file_ids_retain_historical_upload_semantics(atomic_shop):
    payload = [refs(1)[0], refs(1)[0]]
    order_id, _ = await create(files=payload)
    rows = await orders.get_order_files(order_id)
    assert len(rows) == 2
    assert rows[0]['telegram_file_id'] == rows[1]['telegram_file_id']
    assert rows[0]['id'] < rows[1]['id']


@pytest.mark.parametrize('change', ['generation', 'cancelled', 'foreign_group'])
async def test_stale_pending_checkpoint_cannot_publish(atomic_shop, publisher, change):
    order_id, _ = await create(files=refs(1))
    sql = {
        'generation': 'UPDATE orders SET assignment_generation=1',
        'cancelled': "UPDATE orders SET status='cancelled'",
        'foreign_group': 'UPDATE order_topic_checkpoints SET group_chat_id=-10099999',
    }[change]
    async with aiosqlite.connect(atomic_shop) as db:
        await db.execute(sql)
        await db.commit()
    assert await order_card.recover_unfinished_topic_publications(publisher) == []
    assert publisher.calls == []
    assert (await chat_archive.get_topic_checkpoint(order_id))['detail'] == 'local_creation_pending'


async def test_local_failure_keeps_retryable_fsm_draft(atomic_shop, monkeypatch):
    from handlers import order_form
    draft = dict(draft_id='retryable-draft', confirmation_message_id=50,
                 direction='Математика', work_type='solution', description='synthetic',
                 assigned_master_telegram_id=999001, files=refs())
    state = SimpleNamespace(get_data=AsyncMock(return_value=draft),
                            set_state=AsyncMock(), clear=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=7770001), answer=AsyncMock(),
        message=SimpleNamespace(message_id=50, edit_reply_markup=AsyncMock(), answer=AsyncMock()))
    with monkeypatch.context() as patch:
        patch.setattr(order_form, 'create_order', AsyncMock(side_effect=RuntimeError('synthetic DB failure')))
        await order_form.confirm_order(callback, state, SimpleNamespace(id=123456))
    state.clear.assert_not_awaited()
    assert state.set_state.call_args.args[0] == order_form.OrderState.waiting_for_confirmation
    callback.message.edit_reply_markup.assert_not_awaited()
    assert draft['files'] == refs() and draft['draft_id'] == 'retryable-draft'
    assert await fetch_many(atomic_shop, 'SELECT * FROM orders') == []


@pytest.mark.parametrize('stage', ['checkpoint', 'dedup'])
async def test_failure_after_all_refs_still_rolls_back(atomic_shop, monkeypatch, stage):
    original = aiosqlite.Connection.execute
    async def failed_sql(self, sql, parameters=()):
        if 'INSERT INTO order_topic_checkpoints' in sql:
            raise RuntimeError('synthetic pending marker failure')
        return await original(self, sql, parameters)
    with monkeypatch.context() as patch:
        if stage == 'checkpoint':
            patch.setattr(aiosqlite.Connection, 'execute', failed_sql)
        else:
            patch.setattr(orders, 'attach_operation_result_in_tx', AsyncMock(side_effect=RuntimeError('synthetic dedup failure')))
        with pytest.raises(RuntimeError):
            await create(files=refs(), assigned_master_telegram_id=999001)
    for table in ('orders', 'order_files', 'order_master_attempts', 'operation_log', 'order_topic_checkpoints'):
        assert await fetch_many(atomic_shop, f'SELECT * FROM {table}') == []
    assert (await create(files=refs()))[1]


async def test_legacy_unknown_checkpoint_is_not_upgraded_to_pending(atomic_shop, publisher):
    from database.migrations import run_migrations
    order_id, _ = await create()
    await chat_archive.upsert_topic_checkpoint(order_id, order_card.MASTER_GROUP_ID, 0,
                                             'failed', assignment_generation=0)
    before = await chat_archive.get_topic_checkpoint(order_id)
    await run_migrations()
    assert await chat_archive.get_topic_checkpoint(order_id) == before
    assert await order_card.recover_unfinished_topic_publications(publisher) == []
    assert publisher.calls == []


def handler_input():
    draft = dict(draft_id='handler-replay', confirmation_message_id=50,
                 direction='Математика', work_type='solution', description='synthetic',
                 deadline='31-12-2030', assignment_mode='specific',
                 assigned_master_telegram_id=999001, files=refs())
    state = SimpleNamespace(get_data=AsyncMock(return_value=draft),
                            set_state=AsyncMock(), clear=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=7770001), answer=AsyncMock(),
        message=SimpleNamespace(message_id=50, edit_reply_markup=AsyncMock(), answer=AsyncMock()))
    return draft, state, callback


async def test_handler_happy_path_and_replayed_confirmation(atomic_shop, publisher, monkeypatch):
    from handlers import order_form
    _, state, callback = handler_input()
    # Use the real publisher/transactions, not a green call-count-only mock.
    publisher.id = 123456
    await order_form.confirm_order(callback, state, publisher)
    await order_form.confirm_order(callback, state, publisher)
    rows = await fetch_many(atomic_shop, 'SELECT * FROM orders')
    assert len(rows) == 1
    assert len(await orders.get_order_files(rows[0]['id'])) == 2
    assert (await chat_archive.get_topic_checkpoint(rows[0]['id']))['state'] == 'published'
    assert sum(method == 'create_forum_topic' for method, _ in publisher.calls) == 1


async def test_commit_ack_failure_keeps_draft_and_replay_is_deduplicated(atomic_shop, publisher, monkeypatch):
    from handlers import order_form
    _, state, callback = handler_input()
    publisher.id = 123456
    original = aiosqlite.Connection.commit
    failed = False
    async def ambiguous_commit(self):
        nonlocal failed
        await original(self)
        if not failed:
            failed = True
            raise RuntimeError('synthetic lost local COMMIT acknowledgement')
    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, 'commit', ambiguous_commit)
        await order_form.confirm_order(callback, state, publisher)
    state.clear.assert_not_awaited()
    rows = await fetch_many(atomic_shop, 'SELECT * FROM orders')
    assert len(rows) == 1 and len(await orders.get_order_files(rows[0]['id'])) == 2
    await order_form.confirm_order(callback, state, publisher)
    assert len(await fetch_many(atomic_shop, 'SELECT * FROM orders')) == 1
    assert await order_card.recover_unfinished_topic_publications(publisher) == [rows[0]['id']]
    assert sum(method == 'create_forum_topic' for method, _ in publisher.calls) == 1


async def test_attachment_replay_cannot_modify_saved_draft(atomic_shop):
    order_id, _ = await create(files=refs(1))
    with pytest.raises(RuntimeError, match='другим содержимым'):
        await orders.create_order(**ORDER_KWARGS, direction='Математика', files=refs(2),
            dedup_key='order_confirm:123456:7770001:atomic-draft', dedup_content_hash='changed-hash')
    assert len(await orders.get_order_files(order_id)) == 1


async def test_banned_named_executor_rolls_back_everything(atomic_shop):
    from database.access_control import ban_user
    await ban_user(999001, None, 88001)
    with pytest.raises(ValueError):
        await create(files=refs(), assigned_master_telegram_id=999001)
    assert await fetch_many(atomic_shop, 'SELECT * FROM orders') == []
    assert await fetch_many(atomic_shop, 'SELECT * FROM order_files') == []
    assert await fetch_many(atomic_shop, 'SELECT * FROM operation_log') == []
