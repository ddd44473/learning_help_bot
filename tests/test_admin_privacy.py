"""SEC-02: реальные типы aiogram, все транспортные методы заглушены."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message, User

from handlers import admin_archive, admin_balance, admin_logs, transactions
from utils import admin_access

ADMIN = 88001


def message(chat_type="private", actor=ADMIN, text="/balance @synthetic", mid=1):
    return Message(
        message_id=mid, date=datetime.now(timezone.utc), text=text,
        chat=Chat(id=actor if chat_type == "private" else -88001, type=chat_type),
        from_user=User(id=actor, is_bot=False, first_name="Synthetic"),
    ).as_(Bot("123456:synthetic-token"))


@pytest.fixture
def transport(monkeypatch):
    for module in (admin_access, admin_archive, admin_balance, admin_logs, transactions):
        monkeypatch.setattr(module, "ADMIN_IDS", {ADMIN})
    calls = {}
    for cls, methods in (
        (Message, ("answer", "reply", "answer_document", "edit_text")),
        (CallbackQuery, ("answer",)), (Bot, ("send_message",)),
    ):
        for method in methods:
            mock = AsyncMock()
            monkeypatch.setattr(cls, method, mock)
            calls[f"{cls.__name__}.{method}"] = mock
    return calls


COMMANDS = [
    admin_balance.show_user_balance, admin_balance.give_money, admin_balance.take_money,
    admin_logs.request_user_logs, transactions.admin_transactions_command,
    transactions.admin_transaction_details, admin_archive.archive_command,
]
CALLBACKS = [
    admin_logs.export_user_logs, transactions.paginate_admin_transactions,
    transactions.export_transactions_csv, admin_archive.archive_callbacks,
]


@pytest.mark.parametrize("handler", COMMANDS)
@pytest.mark.parametrize("chat_type,actor", [
    ("group", ADMIN), ("supergroup", ADMIN), ("private", 88002), ("group", 88002),
])
async def test_commands_deny_unsafe_context(handler, chat_type, actor, transport):
    await handler(message(chat_type, actor))
    transport["Message.answer_document"].assert_not_called()
    transport["Bot.send_message"].assert_not_called()
    for call in transport["Message.answer"].call_args_list:
        assert call.args == ("Откройте эту команду в личном чате с ботом.",)


@pytest.mark.parametrize("handler", CALLBACKS)
@pytest.mark.parametrize("context", ["group", "supergroup", "nonadmin", "missing", "inaccessible"])
async def test_old_callbacks_deny_unsafe_context(handler, context, transport):
    actor = 88002 if context == "nonadmin" else ADMIN
    msg = message(context if context in {"group", "supergroup"} else "private", actor)
    if context == "missing":
        msg = None
    elif context == "inaccessible":
        msg = InaccessibleMessage(chat=Chat(id=ADMIN, type="private"), message_id=1, date=0)
    callback = CallbackQuery(id="synthetic", from_user=User(
        id=actor, is_bot=False, first_name="Synthetic"),
        chat_instance="test", message=msg, data="archive:export:1",
    )
    await handler(callback)
    for method in ("Message.answer", "Message.answer_document", "Message.edit_text", "Bot.send_message"):
        transport[method].assert_not_called()


async def test_private_admin_allowed(transport):
    assert await admin_access.require_private_admin(message())
    assert not any(mock.called for mock in transport.values())


@pytest.mark.parametrize("handler,command,sign", [
    (admin_balance.give_money, "give_money", 1),
    (admin_balance.take_money, "take_money", -1),
])
async def test_balance_handler_replay_after_response_failure(db, monkeypatch, transport, handler, command, sign):
    await db.execute("INSERT INTO users (id, telegram_id, tg_username, balance) VALUES (1, 88003, 'synthetic', 1000)")
    await db.commit()
    monkeypatch.setattr(admin_balance, "log_user_activity", AsyncMock())
    event = message(text=f"/{command} @synthetic 100")
    transport["Message.answer"].side_effect = RuntimeError("response failed after commit")
    with pytest.raises(RuntimeError):
        await handler(event)
    transport["Message.answer"].side_effect = None
    await handler(event)
    row = await (await db.execute("SELECT balance FROM users WHERE id = 1")).fetchone()
    assert row[0] == 1000 + sign * 100
    rows = await (await db.execute("SELECT amount, balance_before, balance_after FROM balance_transactions")).fetchall()
    assert len(rows) == 1 and tuple(rows[0]) == (sign * 100, 1000, 1000 + sign * 100)
    admin_balance.log_user_activity.assert_awaited_once()
    assert "повторного" in transport["Message.answer"].call_args.args[0]
