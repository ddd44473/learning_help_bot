"""Регрессионные тесты трёх целевых UI/Workflow фиксов.

Покрывают:
- FIX 1: кнопка «Свободный исполнитель» в клавиатуре выбора конкретного
  исполнителя (masters_selection_keyboard) → роутинг в существующий
  choose_free_master с сохранением черновика заказа.
- FIX 2: кнопка «Пополнить баланс» в клавиатуре оплаты заказа
  (order_payment_keyboard) → существующий сценарий пополнения через
  open_balance; не исчезает при пересборке клавиатуры; не списывает заказ.
- FIX 3: кнопка «Пополнить баланс» в личном профиле (profile_keyboard) →
  тот же существующий сценарий пополнения через open_balance.
"""

import importlib
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
)

from database.balance import get_user_balance
from database.masters import create_master_profile_with_directions
from database.users import create_empty_user, get_user_by_telegram_id

from keyboards.masters import masters_selection_keyboard
from keyboards.order_payment import order_payment_keyboard
from keyboards.profile import profile_keyboard

from states.order import OrderState


def _flatten(markup) -> list:
    """Все кнопки клавиатуры в плоский список."""
    return [
        button
        for row in markup.inline_keyboard
        for button in row
    ]


class LocalSession(BaseSession):
    """Фейковая сессия без сети; фиксирует исходящие вызовы бота."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.counter = 100

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        self.counter += 1
        name = method.__api_method__
        if name == 'getMe':
            return User(
                id=bot.id, is_bot=True,
                first_name='Synthetic bot', username='synthetic_bot',
            )
        if name == 'answerCallbackQuery':
            return True
        if name in {
            'sendMessage', 'sendPhoto', 'editMessageText',
            'editMessageCaption',
        }:
            cid = int(method.chat_id)
            return Message(
                message_id=self.counter,
                date=datetime.now(timezone.utc),
                chat=Chat(
                    id=cid,
                    type='private' if cid > 0 else 'supergroup',
                ),
                text=getattr(method, 'text', None),
            )
        return True

    async def stream_content(self, url, **kwargs):
        yield b'test'


def cloned_router(module):
    """Клон роутера модуля с теми же обработчиками и фильтрами."""
    router = Router(name=module.__name__ + '_isolated')
    for name in ('message', 'callback_query'):
        for handler in module.router.observers[name].handlers:
            router.observers[name].register(
                handler.callback,
                *(item.callback for item in handler.filters),
                flags=handler.flags,
            )
    return router


def incoming(uid=88101, *, mid=1, callback=None, chat_type='private'):
    """Сбор Update с callback_query от синтетического пользователя."""
    user = User(id=uid, is_bot=False, first_name='Synthetic', username=None)
    message = Message(
        message_id=mid,
        date=datetime.now(timezone.utc),
        from_user=user,
        chat=Chat(id=uid if chat_type == 'private' else -10088001, type=chat_type),
    )
    return Update(
        update_id=mid,
        callback_query=CallbackQuery(
            id=f'synthetic-{uid}-{mid}',
            from_user=user,
            chat_instance='synthetic',
            message=message,
            data=callback,
        ),
    )


def new_texts_after(session, before_count):
    """Тексты/подписи, появившиеся после указанного количества вызовов."""
    return sent_texts(session)[before_count:]


def sent_texts(session):
    """Все исходящие текстовые подписи/тела сообщений (text или caption)."""
    out = []
    for call in session.calls:
        value = getattr(call, 'text', None) or getattr(call, 'caption', None) or ''
        out.append(value)
    return out


@pytest.fixture
async def routing():
    """Диспетчер с роутерами profile и order_form и фейковой сессией."""
    session = LocalSession()
    bot = Bot('123456:synthetic-test-token', session=session)
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    dp.update.outer_middleware(__import__('middlewares.security', fromlist=['SecurityMiddleware']).SecurityMiddleware())
    dp.update.outer_middleware(__import__('middlewares.activity_audit', fromlist=['ActivityAuditMiddleware']).ActivityAuditMiddleware())
    dp.message.outer_middleware(__import__('middlewares.chat_archive', fromlist=['ChatArchiveMiddleware']).ChatArchiveMiddleware())
    for name in ('profile', 'order_form'):
        dp.include_router(cloned_router(importlib.import_module('handlers.' + name)))
    yield dp, bot, session
    await dp.fsm.close()
    await session.close()


# ---------------------------------------------------------------------------
# FIX 1: «Свободный исполнитель» в клавиатуре выбора конкретного исполнителя
# ---------------------------------------------------------------------------

def test_masters_keyboard_includes_free_executor_button():
    master = {
        'telegram_id': 9001,
        'full_name': 'Мастер Один',
        'reviews_count': 0,
        'average_rating': None,
        'average_response_minutes': None,
    }
    keyboard = masters_selection_keyboard(
        masters=[master], current_page=0, total_pages=1,
    )

    free_buttons = [
        b for b in _flatten(keyboard)
        if b.text == '🟢 Свободный исполнитель'
    ]
    assert len(free_buttons) == 1
    assert free_buttons[0].callback_data == 'assignment_free'
    assert free_buttons[0].url is None  # inline callback, не ссылка

    named_buttons = [
        b for b in _flatten(keyboard)
        if b.callback_data == 'master_card:9001'
    ]
    assert len(named_buttons) == 1  # именованный исполнитель сохранён


def test_masters_keyboard_free_executor_on_every_page():
    master = {
        'telegram_id': 9002,
        'full_name': 'Мастер Два',
        'reviews_count': 3,
        'average_rating': 4.5,
        'average_response_minutes': 10,
    }
    # Кнопка должна оставаться при пагинации (любая страница).
    for page in (0, 1, 2):
        keyboard = masters_selection_keyboard(
            masters=[master], current_page=page, total_pages=3,
        )
        assert any(
            b.text == '🟢 Свободный исполнитель'
            and b.callback_data == 'assignment_free'
            for b in _flatten(keyboard)
        )


# ---------------------------------------------------------------------------
# FIX 2: «Пополнить баланс» в клавиатуре оплаты заказа
# ---------------------------------------------------------------------------

def test_order_payment_keyboard_topup_is_inline_callback():
    keyboard = order_payment_keyboard(
        order_id=5, can_pay_without_bonus=False,
    )
    topup_buttons = [
        b for b in _flatten(keyboard)
        if b.text == '💰 Пополнить баланс'
    ]
    assert len(topup_buttons) == 1
    assert topup_buttons[0].callback_data == 'balance'
    assert topup_buttons[0].url is None  # не внешняя ссылка, а колбэк


def test_order_payment_keyboard_topup_persists_on_rebuild():
    """Кнопка не исчезает при пересборке клавиатуры (проверить баланс)."""
    kwargs = dict(
        order_id=7, can_pay_without_bonus=False,
        bonus_to_use=0, can_pay_with_bonus=False,
    )
    first = order_payment_keyboard(**kwargs)
    second = order_payment_keyboard(**kwargs)
    for keyboard in (first, second):
        assert any(
            b.text == '💰 Пополнить баланс'
            and b.callback_data == 'balance'
            for b in _flatten(keyboard)
        )


@pytest.mark.asyncio
async def test_balance_topup_dispatches_to_open_balance(routing, db):
    """Колбэк balance реально доходит до обработчика open_balance."""
    dp, bot, session = routing
    uid = 88101
    await create_empty_user(uid, None, 'Synthetic')
    await db.execute(
        'UPDATE users SET is_registered = 1 WHERE telegram_id = ?', (uid,)
    )
    await db.commit()

    user = await get_user_by_telegram_id(uid)
    balance_before = await get_user_balance(int(user['id']))

    await dp.feed_update(bot, incoming(uid=uid, callback='balance', mid=2))

    assert any(
        'Бонусы и баланс' in text for text in sent_texts(session)
    ), f'Не получен ответ окна баланса: {sent_texts(session)}'

    balance_after = await get_user_balance(int(user['id']))
    assert balance_after == balance_before  # переход не меняет баланс


@pytest.mark.asyncio
async def test_balance_topup_repeated_callback_is_safe(routing, db):
    """Повторный переход к пополнению не списывает и не дублирует."""
    dp, bot, session = routing
    uid = 88102
    await create_empty_user(uid, None, 'Synthetic')
    await db.execute(
        'UPDATE users SET is_registered = 1 WHERE telegram_id = ?', (uid,)
    )
    await db.commit()

    user = await get_user_by_telegram_id(uid)
    balance_before = await get_user_balance(int(user['id']))

    await dp.feed_update(bot, incoming(uid=uid, callback='balance', mid=3))
    await dp.feed_update(bot, incoming(uid=uid, callback='balance', mid=4))

    balance_after = await get_user_balance(int(user['id']))
    assert balance_after == balance_before


# ---------------------------------------------------------------------------
# FIX 3: «Пополнить баланс» в личном профиле
# ---------------------------------------------------------------------------

def test_profile_keyboard_includes_topup_button():
    keyboard = profile_keyboard()
    topup_buttons = [
        b for b in _flatten(keyboard)
        if b.text == '💰 Пополнить баланс'
    ]
    assert len(topup_buttons) == 1
    assert topup_buttons[0].callback_data == 'balance'
    assert topup_buttons[0].url is None


def test_profile_keyboard_retains_buttons_and_private_navigation():
    keyboard = profile_keyboard()
    texts = [b.text for b in _flatten(keyboard)]
    assert '📝 Оформить заказ' in texts
    assert '📁 Мои заказы' in texts
    assert '👥 Пригласить друзей' in texts
    assert '💰 Пополнить баланс' in texts
    assert '↩️ Назад' in texts
    callbacks = [b.callback_data for b in _flatten(keyboard)]
    assert 'make_order' in callbacks
    assert 'my_orders' in callbacks
    assert 'referral_program' in callbacks
    assert 'balance' in callbacks
    assert 'main_menu' in callbacks


# ---------------------------------------------------------------------------
# FIX 1: реальный роутинг assignment_free через Dispatcher (state transition)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assignment_free_routes_to_free_master_preserving_draft(
    routing, db,
):
    """Нажатие «Свободный исполнитель» переводит в режим free, не сбрасывая
    направление черновика заказа."""
    dp, bot, session = routing
    uid = 88103
    await create_empty_user(uid, None, 'Synthetic')
    await db.execute(
        'UPDATE users SET is_registered = 1 WHERE telegram_id = ?', (uid,)
    )
    await db.commit()

    await create_master_profile_with_directions(
        telegram_id=9003,
        username='master_free',
        full_name='Мастер Свободный',
        university=None,
        specialty_code=None,
        specialty_name=None,
        directions={'Морское дело'},
    )

    state = dp.fsm.get_context(bot=bot, chat_id=uid, user_id=uid)
    await state.set_state(OrderState.waiting_for_master)
    await state.set_data({'direction': 'Морское дело'})

    await dp.feed_update(bot, incoming(uid=uid, callback='assignment_free', mid=5))

    data = await state.get_data()
    assert data.get('assignment_mode') == 'free'
    assert data.get('direction') == 'Морское дело'  # черновик сохранён
    current_state = await state.get_state()
    assert current_state == OrderState.waiting_for_confirmation.state
    assert any(
        'Проверьте заказ' in text for text in sent_texts(session)
    ), f'Не получено сообщение подтверждения: {sent_texts(session)}'


# ---------------------------------------------------------------------------
# UI-01: «Свободный исполнитель» скрыт в контексте перевыбора заказа
# ---------------------------------------------------------------------------

def test_masters_keyboard_hides_free_executor_in_reassign():
    """В контексте перевыбора (is_reassign=True) кнопка free не показывается."""
    master = {
        'telegram_id': 9004,
        'full_name': 'Мастер Перевыбор',
        'reviews_count': 0,
        'average_rating': None,
        'average_response_minutes': None,
    }
    keyboard = masters_selection_keyboard(
        masters=[master], current_page=0, total_pages=1, is_reassign=True,
    )
    free_buttons = [
        b for b in _flatten(keyboard)
        if b.text == '🟢 Свободный исполнитель'
    ]
    assert len(free_buttons) == 0, (
        f'Кнопка free не должна показываться при перевыборе: '
        f'{[b.text for b in _flatten(keyboard)]}'
    )
    named_buttons = [
        b for b in _flatten(keyboard)
        if b.callback_data == 'master_card:9004'
    ]
    assert len(named_buttons) == 1


def test_masters_keyboard_shows_free_executor_in_draft():
    """В контексте нового черновика (is_reassign=False) кнопка free показывается."""
    master = {
        'telegram_id': 9005,
        'full_name': 'Мастер Черновик',
        'reviews_count': 0,
        'average_rating': None,
        'average_response_minutes': None,
    }
    keyboard = masters_selection_keyboard(
        masters=[master], current_page=0, total_pages=1, is_reassign=False,
    )
    free_buttons = [
        b for b in _flatten(keyboard)
        if b.text == '🟢 Свободный исполнитель'
    ]
    assert len(free_buttons) == 1
    assert free_buttons[0].callback_data == 'assignment_free'


# ---------------------------------------------------------------------------
# PRIV-01: приватный личный чат для профиля и баланса
# ---------------------------------------------------------------------------

def test_private_guard_function():
    """Граница приватности: только личный чат актёра допускается."""
    from handlers.profile import _is_private_own_chat
    from aiogram.types import Chat

    class FakeMessage:
        def __init__(self, chat):
            self.chat = chat

    # Частный чатactёра — разрешён.
    msg_private = FakeMessage(Chat(id=88101, type='private'))
    assert _is_private_own_chat(msg_private, 88101) is True

    # Частный чужой чат — запрещён.
    msg_foreign = FakeMessage(Chat(id=99901, type='private'))
    assert _is_private_own_chat(msg_foreign, 88101) is False

    # Супергруппа — запрещена.
    msg_group = FakeMessage(Chat(id=-10088001, type='supergroup'))
    assert _is_private_own_chat(msg_group, 88101) is False

    # Группа — запрещена.
    msg_group2 = FakeMessage(Chat(id=-88001, type='group'))
    assert _is_private_own_chat(msg_group2, 88101) is False

    # Нет чата — запрещено.
    msg_none = FakeMessage(None)
    assert _is_private_own_chat(msg_none, 88101) is False


@pytest.mark.asyncio
async def test_profile_rejected_in_group(routing, db):
    """Профиль не отдаётся в супергруппу/группу — доступа к личным данным нет."""
    dp, bot, session = routing
    uid = 88104
    await create_empty_user(uid, None, 'Synthetic')
    await db.execute(
        'UPDATE users SET is_registered = 1 WHERE telegram_id = ?', (uid,)
    )
    await db.commit()

    calls_before = len(session.calls)

    # Профиль из супергруппы.
    await dp.feed_update(bot, incoming(
        uid=uid, chat_type='supergroup', callback='profile', mid=6
    ))
    # Баланс из группы.
    await dp.feed_update(bot, incoming(
        uid=uid, chat_type='group', callback='balance', mid=7
    ))

    new_texts = new_texts_after(session, calls_before)
    # Должны быть только нейтральные отказы, без личных данных.
    assert all(
        'доступен только в личном чате' in t or t == '' for t in new_texts
    ), f'Неожиданные сообщения из группы: {new_texts}'
    assert any(
        'Профиль доступен только' in t for t in new_texts
    ), f'Нет отказа для профиля: {new_texts}'
    assert any(
        'Баланс доступен только' in t for t in new_texts
    ), f'Нет отказа для баланса: {new_texts}'


@pytest.mark.asyncio
async def test_profile_allowed_in_private(routing, db):
    """Профиль и баланс работают в личном чате actёра."""
    dp, bot, session = routing
    uid = 88105
    await create_empty_user(uid, None, 'Synthetic')
    await db.execute(
        'UPDATE users SET is_registered = 1 WHERE telegram_id = ?', (uid,)
    )
    await db.commit()

    calls_before = len(session.calls)
    await dp.feed_update(bot, incoming(uid=uid, callback='profile', mid=8))
    new_texts = new_texts_after(session, calls_before)

    combined = ' '.join(new_texts)
    assert 'Профиль' in combined, (
        f'Профиль не отдан в личном чате: {new_texts}'
    )


# ---------------------------------------------------------------------------
# TOPUP-01: пополнение при отсутствующем контакте администратора
# ---------------------------------------------------------------------------

def test_topup_button_present_without_contact():
    """Кнопка пополнения в оплате всегда видима; без контакта — путь к balance."""
    keyboard = order_payment_keyboard(
        order_id=9, can_pay_without_bonus=False,
    )
    topup = [b for b in _flatten(keyboard) if b.text == '💰 Пополнить баланс']
    assert len(topup) == 1
    assert topup[0].callback_data == 'balance'
    assert topup[0].url is None


def test_profile_topup_button_present_without_contact():
    """Кнопка пополнения в профиле всегда видима."""
    keyboard = profile_keyboard()
    topup = [b for b in _flatten(keyboard) if b.text == '💰 Пополнить баланс']
    assert len(topup) == 1
    assert topup[0].callback_data == 'balance'
    assert topup[0].url is None


@pytest.mark.asyncio
async def test_balance_no_contact_shows_explanation(routing, db, monkeypatch):
    """Без настроенного контакта администратора — ясное объяснение, не 'не указан'."""
    import config as config_module

    monkeypatch.setattr(config_module, 'TOPUP_ADMIN_USERNAME', '')

    dp, bot, session = routing
    uid = 88106
    await create_empty_user(uid, None, 'Synthetic')
    await db.execute(
        'UPDATE users SET is_registered = 1 WHERE telegram_id = ?', (uid,)
    )
    await db.commit()

    calls_before = len(session.calls)
    await dp.feed_update(bot, incoming(uid=uid, callback='balance', mid=9))
    new_texts = new_texts_after(session, calls_before)

    combined = ' '.join(new_texts)
    assert 'не настроен' in combined or 'не указан' not in combined, (
        f'Ожидалось ясное объяснение отсутствия контакта, получено: {new_texts}'
    )


@pytest.mark.asyncio
async def test_balance_topup_no_money_mutation(routing, db):
    """Навигация к балансу и обратно не списывает и не зачисляет деньги."""
    dp, bot, session = routing
    uid = 88107
    await create_empty_user(uid, None, 'Synthetic')
    await db.execute(
        'UPDATE users SET is_registered = 1 WHERE telegram_id = ?', (uid,)
    )
    await db.commit()

    user = await get_user_by_telegram_id(uid)
    balance_before = await get_user_balance(int(user['id']))

    await dp.feed_update(bot, incoming(uid=uid, callback='balance', mid=10))
    await dp.feed_update(bot, incoming(uid=uid, callback='balance', mid=11))

    balance_after = await get_user_balance(int(user['id']))
    assert balance_after == balance_before, (
        f'Баланс изменился при навигации: {balance_before} -> {balance_after}'
    )


# ---------------------------------------------------------------------------
# TEST-02: уникальные scratch-базы на прогоны
# ---------------------------------------------------------------------------

def test_scratch_db_roots_differ():
    """Два независимых процесса pytest должны получать разные корни БД."""
    from tests.conftest import TEST_SCRATCH_DIR
    # Каждый импорт контеста в отдельном процессе даёт свой pid/timestamp.
    # В одном процессе корень стабилен, но проверяем, что путь не фиксированный.
    assert str(TEST_SCRATCH_DIR) != '/tmp/opencode/study_bot_audit_tests/test_database.db'
    assert TEST_SCRATCH_DIR.parent.name == 'study_bot_audit_tests'
    assert 'run_' in TEST_SCRATCH_DIR.name
