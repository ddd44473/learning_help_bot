"""Характеризационные тесты пакета admin_master_profile (workstream B)."""
import importlib

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation

import handlers.admin_master_profile as profile_package
from middlewares.activity_audit import ActivityAuditMiddleware
from middlewares.security import SecurityMiddleware
from test_dispatcher_phase2 import LocalSession, cloned_router, incoming, new_texts_after

ADMIN_TG = 88001
MASTER_TG = 88901
USERNAME = "newmaster"

EXPECTED_CALLBACKS = [
    "toggle_master_direction", "save_master_profile", "masters_catalog_menu",
    "masters_catalog_all", "masters_catalog_directions", "masters_catalog_by_direction",
    "masters_catalog_by_status", "masters_catalog_open_master", "masters_catalog_search_start",
    "masters_catalog_page_info", "start_edit_master_field", "start_edit_master_directions",
    "toggle_edit_master_direction", "save_edit_master_directions", "toggle_master_active_status",
    "request_delete_master", "confirm_delete_master", "cancel_delete_master",
    "start_master_refill", "keep_refill_value", "toggle_refill_direction",
    "save_refilled_master_profile", "cancel_refill_callback", "cancel_edit_callback",
    "admin_master_noop",
]

EXPECTED_MESSAGES = [
    "start_master_profile_setup", "process_master_full_name", "process_master_university",
    "process_master_specialty_code", "process_master_specialty_name", "directions_text_fallback",
    "masters_catalog_command", "masters_catalog_search_process", "edit_master_command",
    "process_edit_master_field", "edit_directions_text_fallback", "delete_master_command",
    "refill_full_name_message", "refill_university_message", "refill_specialty_code_message",
    "refill_specialty_name_message", "refill_directions_text_fallback",
]


@pytest.fixture
async def admin_routing():
    session = LocalSession()
    bot = Bot("123456:synthetic-test-token", session=session)
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    dp.update.outer_middleware(SecurityMiddleware())
    dp.update.outer_middleware(ActivityAuditMiddleware())
    dp["fsm_events_isolation"] = dp.fsm.events_isolation
    dp.include_router(cloned_router(importlib.import_module("handlers.admin_master_profile")))
    yield dp, bot, session
    await dp.fsm.close()
    await session.close()


async def insert_registered_user(db):
    await db.execute(
        """
        INSERT INTO users(id, telegram_id, tg_username, tg_full_name, is_registered)
        VALUES (1, ?, ?, 'Новый Исполнитель', 1)
        """,
        (MASTER_TG, USERNAME),
    )
    await db.commit()


def test_package_preserves_router_order_and_public_names():
    assert [h.callback.__name__ for h in profile_package.router.callback_query.handlers] == EXPECTED_CALLBACKS
    assert [h.callback.__name__ for h in profile_package.router.message.handlers] == EXPECTED_MESSAGES
    assert profile_package.router is profile_package._shared.router
    assert profile_package.logger.name == "handlers.admin_master_profile"
    for name in EXPECTED_CALLBACKS + EXPECTED_MESSAGES:
        assert callable(getattr(profile_package, name))


@pytest.mark.asyncio
async def test_full_admin_lifecycle_create_edit_refill_delete(admin_routing, db):
    await insert_registered_user(db)
    dp, bot, session = admin_routing

    # Создание: /create_master -> 4 поля -> направление -> сохранение.
    await dp.feed_update(bot, incoming(uid=ADMIN_TG, text=f"/create_master @{USERNAME}", mid=1))
    state = dp.fsm.get_context(bot=bot, chat_id=ADMIN_TG, user_id=ADMIN_TG)
    assert await state.get_state() == "AdminMasterProfileState:waiting_for_full_name"

    for mid, text in enumerate(("Иван Тестов", "МГУ", "01.03.02", "Прикладная математика"), start=2):
        await dp.feed_update(bot, incoming(uid=ADMIN_TG, text=text, mid=mid))

    assert await state.get_state() == "AdminMasterProfileState:waiting_for_directions"
    await dp.feed_update(bot, incoming(uid=ADMIN_TG, callback="admin_master_dir:science", mid=6))
    await dp.feed_update(bot, incoming(uid=ADMIN_TG, callback="admin_master_dirs_save", mid=7))

    master = await (await db.execute(
        "SELECT full_name, university, specialty_code, specialty_name, is_active, deleted_at "
        "FROM masters WHERE telegram_id=?",
        (MASTER_TG,),
    )).fetchone()
    assert tuple(master) == (
        "Иван Тестов", "МГУ", "01.03.02", "Прикладная математика", 1, None,
    )
    directions = await (await db.execute(
        "SELECT direction FROM master_directions WHERE master_telegram_id=?",
        (MASTER_TG,),
    )).fetchall()
    assert [row[0] for row in directions] == ["Естественные науки"]
    assert await state.get_state() is None

    # Редактирование одного поля.
    before_edit = len(session.calls)
    await dp.feed_update(bot, incoming(uid=ADMIN_TG, text=f"/edit_master @{USERNAME}", mid=8))
    assert any("Управление исполнителем" in value for value in new_texts_after(session, before_edit))
    await dp.feed_update(bot, incoming(
        uid=ADMIN_TG, callback=f"admin_master_edit_field:{MASTER_TG}:full_name", mid=9,
    ))
    assert await state.get_state() == "AdminMasterProfileState:editing_field_value"
    await dp.feed_update(bot, incoming(uid=ADMIN_TG, text="Новое Имя", mid=10))
    assert tuple(await (await db.execute(
        "SELECT full_name FROM masters WHERE telegram_id=?", (MASTER_TG,)
    )).fetchone()) == ("Новое Имя",)

    # Повторное заполнение: вход и отмена.
    await dp.feed_update(bot, incoming(
        uid=ADMIN_TG, callback=f"admin_master_refill:{MASTER_TG}", mid=11,
    ))
    assert await state.get_state() == "AdminMasterProfileState:refill_full_name"
    await dp.feed_update(bot, incoming(
        uid=ADMIN_TG, callback="admin_master_refill_cancel", mid=12,
    ))
    assert await state.get_state() is None

    # Мягкое удаление.
    before_delete = len(session.calls)
    await dp.feed_update(bot, incoming(
        uid=ADMIN_TG, callback=f"admin_master_delete_request:{MASTER_TG}", mid=13,
    ))
    assert any("Удалить исполнителя?" in value for value in new_texts_after(session, before_delete))
    await dp.feed_update(bot, incoming(
        uid=ADMIN_TG, callback=f"admin_master_delete_confirm:{MASTER_TG}", mid=14,
    ))
    assert (await (await db.execute(
        "SELECT deleted_at FROM masters WHERE telegram_id=?", (MASTER_TG,)
    )).fetchone())[0] is not None


@pytest.mark.asyncio
async def test_non_admin_is_denied(admin_routing):
    dp, bot, session = admin_routing

    await dp.feed_update(bot, incoming(uid=88002, callback="adm_m_menu", mid=20))

    replies = [call for call in session.calls if call.__api_method__ == "answerCallbackQuery"]
    assert replies and replies[-1].text == "Недостаточно прав."
    assert not [call for call in session.calls if call.__api_method__ in {"sendMessage", "editMessageText"}]


@pytest.mark.asyncio
async def test_setup_allows_cancel_without_creating_master(admin_routing, db):
    await insert_registered_user(db)
    dp, bot, _ = admin_routing
    state = dp.fsm.get_context(bot=bot, chat_id=ADMIN_TG, user_id=ADMIN_TG)

    await dp.feed_update(bot, incoming(uid=ADMIN_TG, text=f"/create_master @{USERNAME}", mid=21))
    await dp.feed_update(bot, incoming(uid=ADMIN_TG, text="/cancel", mid=22))

    assert await state.get_state() is None
    assert tuple(await (await db.execute("SELECT COUNT(*) FROM masters")).fetchone()) == (0,)
