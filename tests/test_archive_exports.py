"""Ограниченная синтетика: экспорт, CSV, общий lifecycle закрытия."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import CloseForumTopic

from database.activity_logs import get_user_activity_logs_for_export
from database.chat_archive import init_chat_archive, get_archive_contents, get_topic
from database.transactions import get_transactions_for_export
from handlers.transactions import _build_transactions_csv
from utils import export_limits
from utils.order_card import close_and_record_topic


async def prepare(db):
    await init_chat_archive()
    await db.execute("INSERT INTO users(id, telegram_id) VALUES (1, 88101)")
    await db.execute("INSERT INTO orders(id, user_id, subject, work_type, description) VALUES (1, 1, 'synthetic', 'solution', 'synthetic')")
    await db.execute("INSERT INTO chat_archive_topics(order_id, group_chat_id, topic_id) VALUES (1, -10088001, 1)")
    await db.commit()


@pytest.mark.parametrize("kind", ["logs", "transactions", "archive"])
async def test_exports_reject_oversize_without_silent_truncation(db, monkeypatch, kind):
    await prepare(db)
    monkeypatch.setattr(export_limits, "MAX_EXPORT_ROWS", 2)
    for _ in range(3):
        await db.execute("INSERT INTO user_activity_logs(telegram_id, action) VALUES (88101, 'synthetic')")
        await db.execute("INSERT INTO balance_transactions(user_id, amount, balance_before, balance_after, transaction_type) VALUES (1, 1, 0, 1, 'admin_deposit')")
        await db.execute("INSERT INTO chat_archive_events(order_id, event_type) VALUES (1, 'synthetic')")
    await db.commit()
    with pytest.raises(export_limits.ExportLimitError):
        if kind == "logs":
            await get_user_activity_logs_for_export(88101, "all")
        elif kind == "transactions":
            await get_transactions_for_export(period="all")
        else:
            await get_archive_contents(1)


@pytest.mark.parametrize("outcome", ["success", "already_closed", "network_failure", "bad_request"])
async def test_topic_closed_at_matches_remote_outcome(db, outcome):
    await prepare(db)
    error = None
    if outcome == "network_failure":
        error = TimeoutError("unknown remote outcome")
    elif outcome in {"already_closed", "bad_request"}:
        error = TelegramBadRequest(method=CloseForumTopic(chat_id=-10088001, message_thread_id=1),
                                   message="TOPIC_NOT_MODIFIED" if outcome == "already_closed" else "invalid topic")
    bot = SimpleNamespace(close_forum_topic=AsyncMock(side_effect=error))
    if outcome in {"network_failure", "bad_request"}:
        with pytest.raises(type(error)):
            await close_and_record_topic(bot, -10088001, 1)
    else:
        await close_and_record_topic(bot, -10088001, 1)
    topic = await get_topic(-10088001, 1)
    assert bool(topic['closed_at']) == (outcome in {"success", "already_closed"})


async def test_transaction_csv_escapes_user_formulas():
    row = dict(created_at="2026-01-01 12:00:00", id=1, transaction_type="payment", amount=-1,
               balance_before=2, balance_after=1, user_id=1, telegram_id=88101,
               tg_username="=HYPERLINK(1)", bot_nickname=" +SUM(1)", order_id=1,
               admin_telegram_id=None, comment="@formula")
    csv = _build_transactions_csv([row]).decode('utf-8-sig')
    assert "'=HYPERLINK(1)" in csv and "' +SUM(1)" in csv and "'@formula" in csv
    assert ";-1;" in csv  # Настоящие числовые суммы не превращаются в текст.
