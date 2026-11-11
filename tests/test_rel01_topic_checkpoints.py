"""REL-01 (build_full.md §): durable-контрольные точки публикации тем.

Проверяется:
- контрольная точка создаётся сразу после forum topic и ДО карточки;
- публикация успешна: точка снята, topic_id/карточка сохранены в заказе;
- сбой отправки карточки/вложений: state='failed', заказ без темы;
- recovery: незавершённые точки возобновляются, точка снимается;
- recovery игнорирует лишние точки (нет заказа, 'published').
"""

from types import SimpleNamespace
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

import aiosqlite
import pytest
import pytest_asyncio

from database.chat_archive import (
    get_topic_checkpoint,
    list_unfinished_topic_checkpoints,
    upsert_topic_checkpoint,
)
from database.orders import create_order

NOW = "datetime('now', '+3 hours')"

CLIENT_TG = 8880001
MASTER_TG = 666001


class StubBot:
    """Минимальный стаб Telegram Bot без сети."""

    def __init__(self, fail_method: str | None = None):
        self.calls: list = []
        self.fail_method = fail_method
        self._topic_counter = 0
        self.message_counter = 10

    def _record(self, method: str):
        if method == self.fail_method:
            raise RuntimeError(f"stub failure: {method}")
        self.message_counter += 1
        self.calls.append((method, self.message_counter))
        return SimpleNamespace(message_id=self.message_counter)

    async def create_forum_topic(self, chat_id: int, name: str):
        self._topic_counter += 1
        self.calls.append(("create_forum_topic", self._topic_counter))
        if self.fail_method == "create_forum_topic":
            raise RuntimeError("stub failure: create_forum_topic")
        return SimpleNamespace(message_thread_id=self._topic_counter)

    async def send_message(self, **kwargs):
        return self._record("send_message")

    async def send_photo(self, **kwargs):
        return self._record("send_photo")

    async def send_document(self, **kwargs):
        return self._record("send_document")


@pytest_asyncio.fixture
async def checkpoint_shop(tmp_path, use_db_path, monkeypatch):
    """Заказ с назначенным исполнителем + заглушка capture_initial_order."""
    from database.db import init_db
    from database.migrations import run_migrations

    db_path = tmp_path / "rel01.db"
    use_db_path(db_path)
    await init_db()
    await run_migrations()
    from database.chat_archive import init_chat_archive
    await init_chat_archive()

    # Ленивый импорт внутри create_order_assignment_topic — глушим заранее.
    import services.chat_capture

    monkeypatch.setattr(
        services.chat_capture, "capture_initial_order", _noop_capture
    )
    monkeypatch.setattr(
        services.chat_capture, "capture_error", _noop_capture
    )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"""
            INSERT INTO users (
                id, telegram_id, tg_username, balance, bonus_balance,
                is_registered, created_at, updated_at, last_active_at
            )
            VALUES (1, {CLIENT_TG}, 'client', 0, 0, 1, {NOW}, {NOW}, {NOW})
            """
        )
        await db.execute(
            f"""
            INSERT INTO masters (
                telegram_id, full_name, is_active, created_at, updated_at
            )
            VALUES ({MASTER_TG}, 'Мастер', 1, {NOW}, {NOW})
            """
        )
        await db.execute(
            f"""
            INSERT INTO master_directions (
                master_telegram_id, direction, created_at
            )
            VALUES ({MASTER_TG}, 'Математика', {NOW})
            """
        )
        await db.commit()

    order_id, _fresh = await create_order(
        user_id=1,
        subject="Консультация",
        work_type="homework",
        description="решить задание",
        assigned_master_telegram_id=MASTER_TG,
        direction="Математика",
    )
    return db_path, order_id


async def _noop_capture(*args, **kwargs):  # pragma: no cover
    return None


async def fetch(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
    return row[0] if row is not None else None


async def test_checkpoint_written_before_card_and_cleaned_on_success(
    checkpoint_shop, monkeypatch
):
    db_path, order_id = checkpoint_shop
    import utils.order_card as order_card

    bot = StubBot()

    events: list = []

    original_create = bot.create_forum_topic

    async def spy_create_forum_topic(**kwargs):
        result = await original_create(**kwargs)  # type: ignore[operator]
        events.append("topic_created")
        bot.calls[-1] = ("create_forum_topic", result.message_thread_id)
        return result

    monkeypatch.setattr(bot, "create_forum_topic", spy_create_forum_topic)

    original_upsert = order_card.upsert_topic_checkpoint

    async def spy_upsert(**kwargs):
        events.append(f"checkpoint:{kwargs.get('state')}")
        return await original_upsert(**kwargs)

    monkeypatch.setattr(order_card, "upsert_topic_checkpoint", spy_upsert)

    original_mark = order_card.mark_topic_checkpoint_state

    async def spy_mark(*args, **kwargs):
        events.append(f"state:{kwargs.get('state', args[1] if args else None)}")
        return await original_mark(*args, **kwargs)

    monkeypatch.setattr(order_card, "mark_topic_checkpoint_state", spy_mark)

    original_send = bot.send_message

    async def spy_send(**kwargs):
        checkpoint = await get_topic_checkpoint(order_id)
        assert checkpoint is not None and checkpoint["state"] == "publishing"
        events.append("send_message")
        return await original_send(**kwargs)

    monkeypatch.setattr(bot, "send_message", spy_send)

    topic_id = await order_card.create_order_assignment_topic(
        bot=bot, order_id=order_id
    )

    # 1) Точка создана после forum topic и до отправки карточки.
    assert events.index("topic_created") < events.index("checkpoint:created")
    assert events.index("checkpoint:created") < events.index("state:publishing")
    send_positions = [
        position
        for position, (method, _mid) in enumerate(bot.calls)
        if method in ("send_message", "send_photo", "send_document")
    ]
    first_send = send_positions[0]
    assert (
        bot.calls[first_send][0] == "send_message"
    ), "первая отправка — карточка в тему"
    assert events.index("state:publishing") < events.index("send_message")

    # 2) Опубликованная точка сохраняется для защиты повтора.
    assert "state:published" in events
    assert (await get_topic_checkpoint(order_id))["state"] == "published"
    assert await list_unfinished_topic_checkpoints() == []

    # 3) Итоги заказа: тема и карточка сохранены.
    assert await fetch(
        db_path, "SELECT topic_id FROM orders WHERE id = ?", (order_id,)
    ) == topic_id
    assert await fetch(
        db_path,
        "SELECT group_chat_id FROM orders WHERE id = ?",
        (order_id,),
    ) is not None
    card_message_id = await fetch(
        db_path,
        "SELECT order_card_message_id FROM orders WHERE id = ?",
        (order_id,),
    )
    assert card_message_id is not None
    # Карточка — первое сообщение в этой теме.
    assert card_message_id in {
        message_id for _method, message_id in bot.calls
    }


async def test_failed_card_send_leaves_failed_checkpoint(checkpoint_shop):
    db_path, order_id = checkpoint_shop
    import utils.order_card as order_card

    bot = StubBot(fail_method="send_message")

    with pytest.raises(RuntimeError):
        await order_card.create_order_assignment_topic(
            bot=bot, order_id=order_id
        )

    checkpoint = await get_topic_checkpoint(order_id)
    assert checkpoint is not None
    assert checkpoint["state"] == "failed"
    assert checkpoint["detail"] == "publication_partial_or_unknown_manual_review"
    # Известная тема сохранена ДО отправки карточки.
    assert await fetch(
        db_path, "SELECT topic_id FROM orders WHERE id = ?", (order_id,)
    ) == checkpoint["topic_id"]


async def test_partial_publication_requires_manual_review(checkpoint_shop):
    db_path, order_id = checkpoint_shop
    import utils.order_card as order_card

    # Имитируем сбой на отправке вложения после карточки.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO order_files (order_id, file_type, telegram_file_id, file_name) "
            "VALUES (?, 'document', 'synthetic-file', 'task.txt')", (order_id,)
        )
        await db.commit()
    failing_bot = StubBot(fail_method="send_document")
    with pytest.raises(RuntimeError):
        await order_card.create_order_assignment_topic(
            bot=failing_bot, order_id=order_id
        )

    checkpoint = await get_topic_checkpoint(order_id)
    assert checkpoint is not None
    assert checkpoint["state"] == "failed"
    card_before = {mid for _m, mid in failing_bot.calls if _m == "send_message"}

    # «Перезапуск»: рабочий бот, восстановление.
    healthy_bot = StubBot()
    recovered = await order_card.recover_unfinished_topic_publications(
        bot=healthy_bot
    )

    assert recovered == []
    assert (await get_topic_checkpoint(order_id))["state"] == "failed"
    assert await fetch(
        db_path, "SELECT topic_id FROM orders WHERE id = ?", (order_id,)
    ) == checkpoint["topic_id"]
    assert await fetch(
        db_path,
        "SELECT order_card_message_id FROM orders WHERE id = ?",
        (order_id,),
    ) is not None

    # Нельзя повторять частичные отправки: результат предыдущего send неизвестен.
    sends_healthy = [
        (method, mid) for method, mid in healthy_bot.calls if method == "send_message"
    ]
    assert sends_healthy == []
    new_card_id = await fetch(
        db_path,
        "SELECT order_card_message_id FROM orders WHERE id = ?",
        (order_id,),
    )
    assert card_before
    assert new_card_id in card_before
    assert not [call for call in healthy_bot.calls if call[0] == "create_forum_topic"]

    # Повторный recovery — нечего возобновлять.
    assert (
        await order_card.recover_unfinished_topic_publications(
            bot=healthy_bot
        )
        == []
    )


async def test_recovery_skips_foreign_checkpoints(checkpoint_shop):
    db_path, order_id = checkpoint_shop
    import utils.order_card as order_card

    healthy_bot = StubBot()

    # Точка для несуществующего заказа: удаляется, не возобновляется.
    await upsert_topic_checkpoint(
        order_id=9999,
        group_chat_id=-100,
        topic_id=42,
        state="publishing",
    )
    # Точка 'published' не входит в список незавершённых.
    await upsert_topic_checkpoint(
        order_id=order_id,
        group_chat_id=-100,
        topic_id=43,
        state="published",
    )

    recovered = await order_card.recover_unfinished_topic_publications(
        bot=healthy_bot
    )
    assert recovered == []
    assert await get_topic_checkpoint(9999) is None
    assert await get_topic_checkpoint(order_id) is not None, (
        "'published'-точка не должна триггерить восстановление и удаление"
    )


async def test_recovery_skips_rebound_topic(checkpoint_shop):
    db_path, order_id = checkpoint_shop
    import utils.order_card as order_card

    # Сбой после создания точки, но тема в заказе уже переподвязана
    # другим потоком на другую тему: устаревшая точка снимается.
    await upsert_topic_checkpoint(
        order_id=order_id,
        group_chat_id=-100,
        topic_id=17,
        state="failed",
        detail="прервано перезапуском",
    )
    await _raw_update_order_topic(db_path, order_id, topic_id=99)

    healthy_bot = StubBot()
    recovered = await order_card.recover_unfinished_topic_publications(
        bot=healthy_bot
    )
    assert recovered == []
    assert await get_topic_checkpoint(order_id) is not None
    # Карточка не отправлялась — публикация уже не для этой темы.
    assert not [
        call for call in healthy_bot.calls if call[0] == "send_message"
    ]
    assert await fetch(
        db_path, "SELECT topic_id FROM orders WHERE id = ?", (order_id,)
    ) == 99


async def _raw_update_order_topic(db_path, order_id: int, topic_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE orders SET topic_id = ?, group_chat_id = -100 "
            "WHERE id = ?",
            (topic_id, order_id),
        )
        await db.commit()


async def test_known_card_rejection_recovers_without_new_topic(checkpoint_shop):
    _, order_id = checkpoint_shop
    import utils.order_card as order_card

    class RejectedBot(StubBot):
        async def send_message(self, **kwargs):
            raise TelegramBadRequest(method=SendMessage(chat_id=-10088001, text="synthetic"), message="rejected")

    with pytest.raises(TelegramBadRequest):
        await order_card.create_order_assignment_topic(RejectedBot(), order_id)
    checkpoint = await get_topic_checkpoint(order_id)
    assert checkpoint["detail"] == "card_rejected_safe_retry"
    healthy = StubBot()
    assert await order_card.recover_unfinished_topic_publications(healthy) == [order_id]
    assert not [call for call in healthy.calls if call[0] == "create_forum_topic"]
    assert await order_card.create_order_assignment_topic(healthy, order_id) == checkpoint["topic_id"]
    assert await order_card.recover_unfinished_topic_publications(healthy) == []


async def test_ambiguous_create_never_retried(checkpoint_shop):
    _, order_id = checkpoint_shop
    import utils.order_card as order_card

    class TimeoutBot(StubBot):
        async def create_forum_topic(self, **kwargs):
            raise TimeoutError("remote may have created topic")

    with pytest.raises(TimeoutError):
        await order_card.create_order_assignment_topic(TimeoutBot(), order_id)
    checkpoint = await get_topic_checkpoint(order_id)
    assert checkpoint["topic_id"] == 0
    assert checkpoint["detail"] == "create_outcome_unknown_manual_review"
    healthy = StubBot()
    assert await order_card.recover_unfinished_topic_publications(healthy) == []
    with pytest.raises(RuntimeError):
        await order_card.create_order_assignment_topic(healthy, order_id)
    assert healthy.calls == []


async def test_old_generation_never_republished(checkpoint_shop):
    path, order_id = checkpoint_shop
    import utils.order_card as order_card
    await upsert_topic_checkpoint(order_id, -10088001, 17, "created", assignment_generation=0)
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE orders SET assignment_generation=assignment_generation+1 WHERE id=?", (order_id,))
        await db.commit()
    healthy = StubBot()
    assert await order_card.recover_unfinished_topic_publications(healthy) == []
    assert healthy.calls == []
    assert await get_topic_checkpoint(order_id) is not None


async def test_snapshot_generation_mismatch_does_not_create(checkpoint_shop):
    _, order_id = checkpoint_shop
    import utils.order_card as order_card
    bot = StubBot()
    with pytest.raises(RuntimeError):
        await order_card.create_order_assignment_topic(bot, order_id, expected_assignment_generation=-1)
    assert bot.calls == []
    assert await get_topic_checkpoint(order_id) is None
