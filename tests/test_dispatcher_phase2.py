"""Реальный Dispatcher.feed_update: те же callbacks/filters, fake BaseSession."""
import asyncio
import importlib
import io
import zipfile
from pathlib import Path
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.types import FSInputFile, InaccessibleMessage
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import CallbackQuery, Chat, Document, File, Message, MessageEntity, MessageId, Update, User
import test_financial_concurrency as financial

shop_db = financial.shop_db

from database.access_control import ban_user
from database.users import create_empty_user, get_user_by_telegram_id
from middlewares.activity_audit import ActivityAuditMiddleware
from middlewares.chat_archive import ChatArchiveMiddleware
from middlewares.security import SecurityMiddleware
from states.order_chat import OrderChatState
from states.registration import RegistrationState


class LocalSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.counter = 100
        self.sent_files = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        self.counter += 1
        name = method.__api_method__
        if name == 'sendDocument' and isinstance(method.document, FSInputFile):
            path = Path(method.document.path).resolve()
            assert path.is_relative_to('/tmp/opencode/study_bot_audit_tests')
            self.sent_files.append((path, path.read_bytes()))
        if name == 'getMe':
            return User(id=bot.id, is_bot=True, first_name='Synthetic bot', username='synthetic_bot')
        if name == 'copyMessage':
            return MessageId(message_id=self.counter)
        if name == 'getFile':
            return File(file_id='synthetic', file_unique_id='unique', file_size=4, file_path='synthetic')
        if name in {'sendMessage', 'sendPhoto', 'sendDocument', 'editMessageText', 'editMessageCaption'}:
            cid = int(method.chat_id)
            return Message(message_id=self.counter, date=datetime.now(timezone.utc),
                chat=Chat(id=cid, type='private' if cid > 0 else 'supergroup'), text=getattr(method, 'text', None))
        return True

    async def stream_content(self, url, **kwargs):
        yield b'test'


def cloned_router(module):
    # Не переиспользуем parent_router singleton между отдельными event loops.
    router = Router(name=module.__name__ + '_isolated')
    for name in ('message', 'callback_query'):
        for handler in module.router.observers[name].handlers:
            router.observers[name].register(handler.callback, *(item.callback for item in handler.filters), flags=handler.flags)
    return router


@pytest.fixture
async def routing():
    session = LocalSession()
    bot = Bot('123456:synthetic-test-token', session=session)
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    dp.update.outer_middleware(SecurityMiddleware())
    dp.update.outer_middleware(ActivityAuditMiddleware())
    dp.message.outer_middleware(ChatArchiveMiddleware())
    # main.py инжектирует изоляцию FSM в handlers workflow-data;
    # клонированный production dispatcher обязан воспроизводить это.
    dp["fsm_events_isolation"] = dp.fsm.events_isolation
    # Порядок совпадает с соответствующей частью main.py; все настоящие filters.
    for name in ('admin_bans', 'admin_archive', 'start', 'menu', 'profile', 'my_orders', 'help',
                 'client_order_chat', 'order_form', 'master_order', 'master_chat',
                 'admin_balance', 'client_price', 'admin_logs', 'transactions'):
        dp.include_router(cloned_router(importlib.import_module('handlers.' + name)))
    yield dp, bot, session
    await dp.fsm.close()
    await session.close()


def incoming(uid=88101, text='synthetic', *, chat_type='private', mid=1, callback=None, document=None):
    user = User(id=uid, is_bot=False, first_name='Synthetic', username=None)
    message = Message(message_id=mid, date=datetime.now(timezone.utc), from_user=user,
        chat=Chat(id=uid if chat_type == 'private' else -10088001, type=chat_type),
        text=text if document is None else None, document=document,
        message_thread_id=1 if chat_type == 'supergroup' else None,
        entities=[MessageEntity(type='bot_command', offset=0, length=len(text.split()[0]))] if text.startswith('/') else None)
    if callback is not None:
        return Update(update_id=mid, callback_query=CallbackQuery(id=f'synthetic-{uid}-{mid}', from_user=user,
            chat_instance='synthetic', message=message, data=callback))
    return Update(update_id=mid, message=message)


def texts(session):
    """Все исходящие текстовые подписи/тела сообщений (text или caption)."""
    out = []
    for call in session.calls:
        value = getattr(call, 'text', None) or getattr(call, 'caption', None) or ''
        out.append(value)
    return out


def new_texts_after(session, before_count):
    """Тексты/подписи, появившиеся после указанного количества вызовов."""
    return texts(session)[before_count:]


async def test_registration_new_returning_no_username(routing):
    dp, bot, session = routing
    await dp.feed_update(bot, incoming(text='/start'))
    user = await get_user_by_telegram_id(88101)
    assert user is not None and user['tg_username'] is None
    await dp.feed_update(bot, incoming(callback='accept_rules', mid=2))
    await dp.feed_update(bot, incoming(callback='profile', mid=3))
    await dp.feed_update(bot, incoming(text='Ёж_Test', mid=4))
    user = await get_user_by_telegram_id(88101)
    assert user['is_registered'] == 1 and user['bot_nickname_normalized'] == 'ёж_test'

    # Запоминаем количество исходящих вызовов до повторного /start.
    calls_before = len(session.calls)
    await dp.feed_update(bot, incoming(text='/start', mid=5))
    # Повторный /start должен породить новые исходящие вызовы.
    assert len(session.calls) > calls_before
    # Приветствие после повторного /start должно быть именно в новых вызовах,
    # а не взято из первого приветствия.
    assert any(
        'Добро пожаловать' in t for t in new_texts_after(session, calls_before)
    )


async def test_concurrent_normalized_nickname_conflict_is_handled(routing, monkeypatch):
    from handlers import profile
    dp, bot, session = routing
    for uid in (88101, 88102):
        await create_empty_user(uid, None, 'Synthetic')
        state = dp.fsm.get_context(bot=bot, chat_id=uid, user_id=uid)
        await state.set_state(RegistrationState.waiting_for_nickname)
    barrier = asyncio.Barrier(2)

    async def same_precheck(_):
        await barrier.wait()
        return None

    monkeypatch.setattr(profile, 'get_user_by_bot_nickname', same_precheck)
    result = await asyncio.gather(dp.feed_update(bot, incoming(text='SameName')),
        dp.feed_update(bot, incoming(uid=88102, text='SAMENAME')), return_exceptions=True)
    assert not [item for item in result if isinstance(item, BaseException)]
    users = [await get_user_by_telegram_id(uid) for uid in (88101, 88102)]
    assert sum(user['is_registered'] for user in users) == 1
    loser = next(user for user in users if not user['is_registered'])
    assert await dp.fsm.get_context(bot=bot, chat_id=loser['telegram_id'], user_id=loser['telegram_id']).get_state() == RegistrationState.waiting_for_nickname.state
    assert any('занят' in text for text in texts(session))


@pytest.mark.parametrize('kind', ['command', 'message', 'callback', 'group', 'fsm'])
async def test_ban_runs_before_audit_and_handlers(routing, db, kind):
    dp, bot, session = routing
    await create_empty_user(88101, None, 'Synthetic')
    await ban_user(88101, None, 88001)
    if kind == 'fsm':
        await dp.fsm.get_context(bot=bot, chat_id=88101, user_id=88101).set_state(RegistrationState.waiting_for_nickname)
    event = incoming(text='/start' if kind == 'command' else 'synthetic',
        callback='profile' if kind == 'callback' else None, chat_type='supergroup' if kind == 'group' else 'private')
    await dp.feed_update(bot, event)
    assert all(call.__api_method__ == 'answerCallbackQuery' for call in session.calls)
    assert (await (await db.execute('SELECT COUNT(*) FROM user_activity_logs')).fetchone())[0] == 0
    assert (await get_user_by_telegram_id(88101))['is_registered'] == 0


@pytest.mark.parametrize('admin,chat_type', [(True,'group'), (True,'supergroup'), (False,'private'), (False,'group')])
async def test_admin_private_guard_in_dispatcher(routing, admin, chat_type):
    dp, bot, session = routing
    uid = 88001 if admin else 88101
    await dp.feed_update(bot, incoming(uid=uid, chat_type=chat_type, callback='user_logs:all:csv:88102'))
    assert not [call for call in session.calls if call.__api_method__ in {'sendDocument', 'sendMessage', 'copyMessage'}]


async def test_slash_command_not_relayed_from_active_chat(routing, db):
    from test_archive_exports import prepare
    await prepare(db)
    await db.execute("UPDATE users SET is_registered=1 WHERE id=1")
    await db.execute("UPDATE orders SET status='in_progress', group_chat_id=-10088001, topic_id=1 WHERE id=1")
    await db.commit()
    dp, bot, session = routing
    state = dp.fsm.get_context(bot=bot, chat_id=88101, user_id=88101)
    await state.set_state(OrderChatState.active)
    await state.set_data(dict(order_id=1, group_chat_id=-10088001, topic_id=1))
    await dp.feed_update(bot, incoming(text='/unknown'))
    # Даже неполный заказ не должен приводить к выполнению relay handler.
    assert not any('Диалог' in text or 'заказ' in text.lower() for text in texts(session))
    assert not [call for call in session.calls if call.__api_method__ == 'copyMessage']


@pytest.mark.parametrize('direction', ['client', 'master'])
async def test_document_relay_persists_job_without_downloading(routing, db, direction):
    from test_archive_exports import prepare
    await prepare(db)
    await db.execute("UPDATE users SET is_registered=1 WHERE id=1")
    await db.execute("INSERT INTO masters(telegram_id,full_name,is_active) VALUES (88901,'Synthetic',1)")
    await db.execute("UPDATE orders SET status='in_progress', group_chat_id=-10088001, topic_id=1, assigned_master_telegram_id=88901 WHERE id=1")
    await db.commit()
    dp, bot, session = routing
    if direction == 'client':
        state = dp.fsm.get_context(bot=bot, chat_id=88101, user_id=88101)
        await state.set_state(OrderChatState.active)
        await state.set_data({'order_id': 1, 'group_chat_id': -10088001, 'topic_id': 1})
    event = incoming(uid=88101 if direction == 'client' else 88901, mid=50,
        chat_type='private' if direction == 'client' else 'supergroup',
        document=Document(file_id='synthetic-file', file_unique_id='unique', file_size=4))
    await dp.feed_update(bot, event)
    copies = [call for call in session.calls if call.__api_method__ == 'copyMessage']
    assert len(copies) == 1
    assert int(copies[0].chat_id) == (-10088001 if direction == 'client' else 88101)
    assert not [call for call in session.calls if call.__api_method__ == 'getFile']
    row = await (await db.execute('SELECT j.status, m.order_id, m.source_message_id FROM chat_archive_download_jobs j JOIN chat_archive_attachments a ON a.id=j.attachment_id JOIN chat_archive_messages m ON m.id=a.message_id')).fetchone()
    assert tuple(row) == ('pending', 1, 50)


async def test_accept_pay_complete_workflow_and_wrong_owner(routing, shop_db):
    import aiosqlite
    from database.chat_archive import init_chat_archive
    from database.orders import get_order_by_id
    path, order_id = shop_db
    await init_chat_archive()
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE orders SET group_chat_id=-10088001,topic_id=1,order_card_message_id=500 WHERE id=?", (order_id,))
        await db.commit()
    offer = await financial.insert_offer(path, order_id, financial.MASTER_A_TG, 1000, 'waiting_client')
    dp, bot, _ = routing
    await dp.feed_update(bot, incoming(uid=financial.REFERRER_TG, callback=f'accept_price:{order_id}:{offer}'))
    assert (await get_order_by_id(order_id))['agreed_price'] is None
    await dp.feed_update(bot, incoming(uid=financial.CLIENT_TG, callback=f'accept_price:{order_id}:{offer}', mid=2))
    assert (await get_order_by_id(order_id))['agreed_price'] == 1000
    await dp.feed_update(bot, incoming(uid=financial.CLIENT_TG, callback=f'pay_order:{order_id}', mid=3))
    assert (await get_order_by_id(order_id))['status'] == 'in_progress'
    await dp.feed_update(bot, incoming(uid=financial.MASTER_A_TG, chat_type='supergroup',
        callback=f'master_status:{order_id}:waiting_client', mid=500))
    assert (await get_order_by_id(order_id))['status'] == 'waiting_client'
    await dp.feed_update(bot, incoming(uid=financial.CLIENT_TG, callback=f'confirm_completion:{order_id}', mid=5))
    assert (await get_order_by_id(order_id))['status'] == 'completed'
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute('SELECT balance FROM users WHERE id=1')).fetchone())[0] == 4250
        assert (await (await db.execute("SELECT COUNT(*) FROM balance_transactions WHERE transaction_type='payment'")).fetchone())[0] == 1
        assert (await (await db.execute('SELECT bonus_balance FROM users WHERE id=2')).fetchone())[0] == 250


async def test_private_admin_archive_upload_owns_temp_until_sent(routing, db):
    from test_archive_exports import prepare
    await prepare(db)
    dp, bot, session = routing
    await dp.feed_update(bot, incoming(uid=88001, callback='archive:export:1'))
    assert len(session.sent_files) == 1
    path, data = session.sent_files[0]
    assert not path.exists()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None and 'manifest.json' in archive.namelist()


@pytest.mark.parametrize('missing', [True, False])
async def test_inaccessible_admin_callback_in_dispatcher(routing, missing):
    dp, bot, session = routing
    event = incoming(uid=88001, callback='archive:export:1')
    callback = event.callback_query.model_copy(update={'message': None if missing else InaccessibleMessage(
        chat=Chat(id=88001, type='private'), message_id=1, date=0)})
    await dp.feed_update(bot, event.model_copy(update={'callback_query': callback}))
    assert not [call for call in session.calls if call.__api_method__ in {'sendDocument','sendMessage'}]


async def test_legacy_csv_keyboard_works_in_private_admin_chat(routing, db):
    await db.execute("INSERT INTO user_activity_logs(telegram_id,action) VALUES (88101,'synthetic')")
    await db.commit()
    dp, bot, session = routing
    await dp.feed_update(bot, incoming(uid=88001, callback='user_logs:all:88101'))
    documents = [call for call in session.calls if call.__api_method__ == 'sendDocument']
    assert len(documents) == 1 and int(documents[0].chat_id) == 88001
    assert documents[0].document.filename.endswith('.csv')
    assert documents[0].document.data.startswith(b'\xef\xbb\xbf')
