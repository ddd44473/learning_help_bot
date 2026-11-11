"""Golden characterization: established imports, exact copy and callback routing."""
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from handlers import master_order as subject
from utils import order_card
from test_dispatcher_phase2 import incoming, routing as dispatcher_routing

routing = dispatcher_routing


@pytest.mark.parametrize('master,fallback,expected', [
    (None, None, 'Исполнитель'),
    (None, '  Резерв <&>  ', 'Резерв <&>'),
    ({'full_name': '  Иван <&>  '}, 'Fallback', 'Иван <&>'),
    ({'full_name': ''}, ' Резерв ', 'Резерв'),
    ({'full_name': None}, ' ', 'Исполнитель'),
])
def test_public_name(master, fallback, expected):
    assert subject._master_public_name(master, fallback) == expected
    if fallback is None:
        assert order_card._master_public_name(master) == expected


@pytest.mark.parametrize('master,fallback,expected', [
    (None, None, '<a href="tg://user?id=88901">Исполнитель</a>'),
    (None, '<Резерв>', '<a href="tg://user?id=88901">&lt;Резерв&gt;</a>'),
    ({'full_name': '<Иван>', 'username': ''}, None,
     '<a href="tg://user?id=88901">&lt;Иван&gt;</a>'),
    ({'full_name': 'Иван', 'username': ' @@name<&> '}, None, '@name&lt;&amp;&gt;'),
])
def test_group_mention(master, fallback, expected):
    assert subject._master_group_mention(master, 88901, fallback) == expected
    if fallback is None:
        assert order_card._master_group_mention(master, 88901) == expected


@pytest.mark.parametrize('minutes,expected', [
    (None, 'Пока нет данных'), (0, 'Около 0 мин.'),
    (59, 'Около 59 мин.'), (60, 'Около 1 ч.'),
    (121, 'Около 2 ч. 1 мин.'),
])
def test_response_time(minutes, expected):
    assert subject._format_public_response_time(minutes) == expected


def buttons(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_keyboard_contracts():
    assert buttons(subject._claimed_master_client_keyboard(
        order_id=42, master_telegram_id=88901,
    )) == [
        [('👨‍💻 Профиль исполнителя', 'claimed_master_profile:88901')],
        [('⭐ Отзывы исполнителя', 'reviews_master:88901')],
        [('💬 Написать по заказу', 'write_order:42')],
        [('📋 Мои заказы', 'my_orders')],
    ]
    assert buttons(subject._claimed_master_profile_keyboard(88901)) == [
        [('⭐ Посмотреть отзывы', 'reviews_master:88901')],
        [('📋 Мои заказы', 'my_orders')],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['missing', 'deleted', 'empty', 'rated'])
async def test_profile_exact_database_output(db, kind):
    if kind != 'missing':
        await db.execute(
            'INSERT INTO masters(telegram_id,full_name,username,deleted_at) VALUES (?,?,?,?)',
            (88901, '<Иван>', 'must_not_leak', 'deleted' if kind == 'deleted' else None),
        )
    if kind == 'rated':
        await db.execute('UPDATE masters SET average_response_minutes=121 WHERE telegram_id=88901')
        await db.execute('INSERT INTO master_directions(master_telegram_id,direction) VALUES (88901,?)', ('Морское дело',))
        await db.execute('INSERT INTO users(id,telegram_id) VALUES (1,88101)')
        await db.execute("INSERT INTO orders(id,user_id,subject,work_type,description,assigned_master_telegram_id) VALUES (1,1,'s','w','d',88901)")
        await db.execute('INSERT INTO reviews(order_id,client_user_id,master_telegram_id,rating) VALUES (1,1,88901,4)')
    await db.commit()
    text = await subject._build_claimed_master_profile_text(88901)
    if kind in {'missing', 'deleted'}:
        assert text is None
        return
    directions = '• Морское дело' if kind == 'rated' else 'Не указаны'
    response = 'Около 2 ч. 1 мин.' if kind == 'rated' else 'Пока нет данных'
    rating = ('⭐ <b>4.00 / 5.00</b>\n💬 Отзывов: <b>1</b>' if kind == 'rated'
              else '⭐ Рейтинг пока не сформирован\n💬 Отзывов пока нет')
    assert text == (
        '👨‍💻 <b>Профиль исполнителя</b>\n\n👤 <b>&lt;Иван&gt;</b>\n\n'
        '🎓 <b>Университет</b>\nНе указано\n\n'
        '📚 <b>Специальность</b>\nКод не указан — Название не указано\n\n'
        f'🧭 <b>Направления</b>\n{directions}\n\n'
        f'⏱ <b>Среднее время ответа</b>\n{response}\n\n{rating}'
    )
    assert 'must_not_leak' not in text and '88901' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,available,answer', [
    ('claimed_master_profile:88901', True, None),
    ('claimed_master_profile:88901', False, 'Профиль исполнителя сейчас недоступен.'),
    ('claimed_master_profile:bad', False, 'Некорректные данные исполнителя.'),
])
async def test_actual_callback_route(routing, monkeypatch, payload, available, answer):
    dp, bot, session = routing
    builder = AsyncMock(return_value='Golden profile' if available else None)
    monkeypatch.setattr(subject, '_build_claimed_master_profile_text', builder)
    await dp.feed_update(bot, incoming(callback=payload))
    sends = [c for c in session.calls if c.__api_method__ == 'sendMessage']
    replies = [c for c in session.calls if c.__api_method__ == 'answerCallbackQuery']
    assert len(replies) == 1 and replies[0].text == answer
    if available:
        assert len(sends) == 1 and sends[0].text == 'Golden profile'
        assert sends[0].parse_mode == 'HTML'
        assert buttons(sends[0].reply_markup) == buttons(subject._claimed_master_profile_keyboard(88901))
    else:
        assert not sends
        assert replies[0].show_alert is True
    if payload.endswith('bad'):
        builder.assert_not_awaited()
    else:
        builder.assert_awaited_once_with(88901)


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['message', 'data'])
async def test_incomplete_callback_is_only_acknowledged(monkeypatch, missing):
    builder = AsyncMock()
    monkeypatch.setattr(subject, '_build_claimed_master_profile_text', builder)
    callback = SimpleNamespace(
        message=None if missing == 'message' else SimpleNamespace(answer=AsyncMock()),
        data=None if missing == 'data' else 'claimed_master_profile:88901',
        answer=AsyncMock(),
    )
    await subject.show_claimed_master_profile(callback)
    callback.answer.assert_awaited_once_with()
    builder.assert_not_awaited()


@pytest.mark.asyncio
async def test_populated_profile_html_and_inactive_visibility(db):
    await db.execute(
        '''INSERT INTO masters(telegram_id,full_name,username,university,
           specialty_code,specialty_name,is_active) VALUES (88901,?,?,?,?,?,0)''',
        ('<Имя>', 'private_username', '<Вуз &>', '<01>', '<Спец>'),
    )
    await db.commit()
    text = await subject._build_claimed_master_profile_text(88901)
    assert '👤 <b>&lt;Имя&gt;</b>' in text
    assert '🎓 <b>Университет</b>\n&lt;Вуз &amp;&gt;' in text
    assert '📚 <b>Специальность</b>\n&lt;01&gt; — &lt;Спец&gt;' in text
    assert 'private_username' not in text and '88901' not in text
