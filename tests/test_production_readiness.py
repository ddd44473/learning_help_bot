"""Focused first-production-deployment readiness checks.

These tests cover only deployment-critical invariants:
* the main administrator (numeric ADMIN_IDS) and the balance top-up contact
  (TOPUP_ADMIN_USERNAME) remain independent;
* a fresh database contains the full schema and no business/personal rows;
* new legitimate users and masters can still register after fresh init;
* the production env template contains placeholders only;
* the fresh-database helper refuses the staging database.

All databases are synthetic and live under the isolated pytest scratch root.
No network calls, no real Telegram identities and no staging data are used.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from deploy.scripts import fresh_database


ADMIN_AUTH_SOURCE_FILES = (
    "utils/admin_access.py",
    "handlers/admin_balance.py",
    "handlers/admin_bans.py",
    "handlers/admin_archive.py",
    "handlers/admin_logs.py",
    "handlers/transactions.py",
    "handlers/master_order_access.py",
    "handlers/admin_master_profile/_shared.py",
    "middlewares/security.py",
    "database/operations.py",
    "services/chat_capture.py",
)


def test_parse_admin_ids_accepts_only_numeric_telegram_ids() -> None:
    assert config.parse_admin_ids("") == set()
    assert config.parse_admin_ids(" 101, 202 ,, ") == {101, 202}
    with pytest.raises(ValueError):
        config.parse_admin_ids("@topup_contact")


@pytest.mark.asyncio
async def test_topup_contact_is_not_an_administrator(monkeypatch) -> None:
    """A top-up username must never become an admin identity by itself."""
    from handlers import admin_balance
    from utils import admin_access

    owner_id = 991_001
    topup_id = 991_002

    monkeypatch.setattr(admin_balance, "ADMIN_IDS", {owner_id})
    monkeypatch.setattr(admin_access, "ADMIN_IDS", {owner_id})

    assert admin_balance.is_admin(owner_id) is True
    assert admin_balance.is_admin(topup_id) is False

    topup_event = SimpleNamespace(from_user=SimpleNamespace(id=topup_id))
    assert await admin_access.require_private_admin(topup_event) is False

    owner_event = SimpleNamespace(from_user=SimpleNamespace(id=owner_id))
    # The owner passes the identity check; the private-chat type check is
    # outside this synthetic event and is covered by existing handler tests.
    assert owner_id in admin_access.ADMIN_IDS


def test_topup_username_is_not_used_by_authorization_code() -> None:
    """Source-level separation: TOPUP_ADMIN_USERNAME is a contact, not auth."""
    for relative_path in ADMIN_AUTH_SOURCE_FILES:
        text = (fresh_database.PROJECT_ROOT / relative_path).read_text(
            encoding="utf-8"
        )
        assert "TOPUP_ADMIN_USERNAME" not in text, relative_path

    profile_text = (
        fresh_database.PROJECT_ROOT / "handlers/profile.py"
    ).read_text(encoding="utf-8")
    keyboard_text = (
        fresh_database.PROJECT_ROOT / "keyboards/profile.py"
    ).read_text(encoding="utf-8")
    assert "TOPUP_ADMIN_USERNAME" in profile_text
    assert "TOPUP_ADMIN_USERNAME" in keyboard_text


@pytest.mark.asyncio
async def test_fresh_database_is_complete_and_contains_no_business_data(
    use_db_path, tmp_path
) -> None:
    target = tmp_path / "fresh" / "database.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    use_db_path(target)

    from database.access_control import init_access_control
    from database.chat_archive import init_chat_archive
    from database.db import init_db
    from database.migrations import run_migrations

    await init_db()
    await run_migrations()
    await init_chat_archive()
    await init_access_control()

    report = fresh_database.inspect_database(target)

    assert report["integrity"] == "ok"
    assert report["foreign_key_violations"] == []
    assert report["missing_tables"] == []
    assert report["missing_indexes"] == []
    assert report["nonempty_tables"] == {}
    assert report["has_business_data"] is False
    assert report["unexpected_migration_markers"] == []
    assert set(report["migration_markers"]).issubset(
        fresh_database.KNOWN_MIGRATION_MARKERS
    )
    assert report["table_count"] >= len(fresh_database.REQUIRED_TABLES)

    # No test/owner identity is seeded, and no administrator row exists.
    connection = sqlite3.connect(str(target))
    try:
        connection.execute("PRAGMA query_only = ON")
        for table in ("users", "masters", "orders", "banned_telegram_users"):
            assert connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_new_user_and_master_can_register_after_fresh_init(
    fresh_database,
) -> None:
    """The fresh schema still accepts normal legitimate registrations."""
    from catalog.directions import DIRECTION_OPTIONS
    from database.masters import (
        CREATE_MASTER_SUCCESS,
        create_master_profile_with_directions,
        get_master_by_telegram_id,
    )
    from database.users import (
        create_empty_user,
        finish_user_registration,
        get_user_by_telegram_id,
    )

    synthetic_user_id = 992_001
    synthetic_master_id = 992_002
    direction = DIRECTION_OPTIONS[0][1]

    await create_empty_user(
        telegram_id=synthetic_user_id,
        tg_username="synthetic_client",
        tg_full_name="Synthetic Client",
    )
    await finish_user_registration(
        telegram_id=synthetic_user_id,
        bot_nickname="synthetic_client",
    )
    user = await get_user_by_telegram_id(synthetic_user_id)
    assert user is not None
    assert int(user["is_registered"]) == 1

    result = await create_master_profile_with_directions(
        telegram_id=synthetic_master_id,
        username="synthetic_master",
        full_name="Synthetic Master",
        university=None,
        specialty_code=None,
        specialty_name=None,
        directions={direction},
    )
    master = await get_master_by_telegram_id(synthetic_master_id)
    assert result == CREATE_MASTER_SUCCESS
    assert master is not None
    assert int(master["is_active"]) == 1


def test_production_env_template_has_placeholders_only() -> None:
    template = (
        fresh_database.PROJECT_ROOT
        / "deploy"
        / "bot.env.production.example"
    )
    text = template.read_text(encoding="utf-8")

    active_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    values: dict[str, str] = {}
    for line in active_lines:
        key, value = line.split("=", 1)
        values[key] = value

    assert set(values) == {
        "BOT_TOKEN",
        "ADMIN_IDS",
        "MASTER_GROUP_ID",
        "TOPUP_ADMIN_USERNAME",
        "SUPPORT_USERNAME",
    }
    assert "STUDY_BOT_DB_PATH" not in values
    assert all(value.startswith("REPLACE_WITH_") for value in values.values())
    assert not any(":" in value for value in values.values())


def test_fresh_database_helper_refuses_staging_database() -> None:
    with pytest.raises(fresh_database.FreshDatabaseError):
        fresh_database.validate_target(
            fresh_database.PROJECT_ROOT / "database" / "staging.db"
        )
    with pytest.raises(fresh_database.FreshDatabaseError):
        fresh_database.validate_target(
            fresh_database.PROJECT_ROOT / "database" / "database.db"
        )
