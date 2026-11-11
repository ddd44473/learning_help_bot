#!/usr/bin/env python3
"""Initialise or verify a FRESH Coliseum SQLite database safely.

The script does not implement a second schema bootstrap. It calls the same
startup functions as ``main.py``:

    init_db() -> run_migrations() -> init_chat_archive() -> init_access_control()

``init`` is intended for a disposable verification directory or for a
maintenance window before the first production start. ``verify`` uses only the
Python standard library, so it can be run on the VPS host without a project
virtualenv.

Safety rules:
* the repository ``database/`` directory, ``storage/`` and ``staging.db`` are
  refused as targets;
* symlinks and non-file targets are refused;
* an existing database that already contains business/personal rows is refused,
  never truncated or overwritten;
* no users, masters, orders, balances, archives or administrator rows are
  inserted;
* no network access is performed.

Examples::

    # On a build/verification machine with the project virtualenv:
    .venv/bin/python -B deploy/scripts/fresh_database.py init \
        --db /tmp/opencode/coliseum-fresh/database.db

    # On the VPS, with the container stopped, using only the standard library:
    python3 deploy/scripts/fresh_database.py verify \
        --db /opt/coliseum/data/database.db

The preferred production first-start mechanism remains the existing container
startup: an empty ``/data/database.db`` is created by ``main.py`` itself. This
script only prepares and verifies that state before/after the first start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

REQUIRED_TABLES = (
    "users",
    "masters",
    "master_directions",
    "orders",
    "order_files",
    "reviews",
    "balance_transactions",
    "order_price_offers",
    "master_response_times",
    "user_activity_logs",
    "order_master_attempts",
    "referrals",
    "bonus_transactions",
    "bonus_grants",
    "bonus_usage_allocations",
    "operation_log",
    "order_topic_checkpoints",
    "chat_archive_download_jobs",
    "chat_archive_topics",
    "chat_archive_messages",
    "chat_archive_attachments",
    "chat_archive_events",
    "chat_archive_admin_audit",
    "banned_telegram_users",
    "ban_business_audit",
    "app_migrations",
)

# Tables that must contain no rows in a fresh production database. Reference /
# migration metadata lives in app_migrations and is checked separately.
EMPTY_TABLES = tuple(table for table in REQUIRED_TABLES if table != "app_migrations")

REQUIRED_INDEXES = (
    "idx_users_referral_code",
    "idx_orders_user_id",
    "idx_balance_transactions_user_id",
    "idx_bonus_grants_expiry",
    "idx_operation_log_created",
    "idx_chat_archive_topics_order",
    "idx_banned_telegram_users_username",
    "idx_download_jobs_status",
)

KNOWN_MIGRATION_MARKERS = {
    "timestamps_to_moscow_v1",
    "bonus_grants_v1",
    "direction_titles_2026_10_v1",
    "bonus_schema_legacy_to_current_v1",
}


class FreshDatabaseError(RuntimeError):
    """A safety or schema check failed; nothing was overwritten."""


def _detect_project_root() -> Path | None:
    """Find the checkout that contains database/db.py (works in a container).

    ``init`` needs the project source; ``verify`` is stdlib-only and must also
    work on a VPS where only deploy/scripts was copied.
    """
    candidates: list[Path] = []
    try:
        candidates.append(Path(__file__).resolve().parents[2])
    except IndexError:
        pass
    candidates.append(Path.cwd())
    for candidate in candidates:
        if (candidate / "database" / "db.py").is_file() and (
            candidate / "main.py"
        ).is_file():
            return candidate.resolve()
    return None


PROJECT_ROOT = _detect_project_root()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def validate_target(raw_path: str | Path) -> Path:
    """Return an absolute safe target path or raise without touching files."""
    path = Path(raw_path).expanduser()
    if path.is_symlink():
        raise FreshDatabaseError(f"Путь БД не должен быть symlink: {path}")

    resolved = path.resolve(strict=False)

    if resolved.name in {"staging.db", "staging.db-wal", "staging.db-shm"}:
        raise FreshDatabaseError("Запрещено использовать staging-базу как цель.")
    if PROJECT_ROOT is not None:
        if _is_within(resolved, PROJECT_ROOT / "database"):
            raise FreshDatabaseError(
                "Запрещено создавать/изменять БД внутри repository/database."
            )
        if _is_within(resolved, PROJECT_ROOT / "storage"):
            raise FreshDatabaseError(
                "Запрещено создавать/изменять БД внутри repository/storage."
            )
    if resolved.exists() and not resolved.is_file():
        raise FreshDatabaseError(f"Цель БД должна быть файлом: {resolved}")
    return resolved


def _ro_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FreshDatabaseError(f"Файл БД не найден: {path}")
    connection = sqlite3.connect(str(path), timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    # query_only makes every write on this connection fail. The file is not
    # created here because existence is checked above.
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _count_rows(connection: sqlite3.Connection, table: str) -> int:
    if not _table_exists(connection, table):
        return -1
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def inspect_database(path: Path) -> dict[str, object]:
    """Read-only inspection used by both `init` and `verify`."""
    connection = _ro_connection(path)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [
            tuple(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        counts = {
            table: _count_rows(connection, table)
            for table in EMPTY_TABLES
            if table in tables
        }
        migrations = []
        if "app_migrations" in tables:
            migrations = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM app_migrations ORDER BY name"
                ).fetchall()
            ]
    finally:
        connection.close()

    return {
        "path": str(path),
        "integrity": integrity,
        "foreign_key_violations": foreign_keys,
        "missing_tables": sorted(set(REQUIRED_TABLES) - tables),
        "missing_indexes": sorted(set(REQUIRED_INDEXES) - indexes),
        "nonempty_tables": {
            table: count for table, count in counts.items() if count > 0
        },
        "table_count": len(tables),
        "index_count": len(indexes),
        "migration_markers": migrations,
        "unexpected_migration_markers": sorted(
            set(migrations) - KNOWN_MIGRATION_MARKERS
        ),
        "has_business_data": any(count > 0 for count in counts.values()),
    }


def _assert_fresh_report(report: dict[str, object]) -> None:
    problems: list[str] = []
    if report["integrity"] != "ok":
        problems.append(f"integrity_check={report['integrity']}")
    if report["foreign_key_violations"]:
        problems.append(
            f"foreign_key_check={len(report['foreign_key_violations'])} нарушений"
        )
    if report["missing_tables"]:
        problems.append(f"нет таблиц: {report['missing_tables']}")
    if report["missing_indexes"]:
        problems.append(f"нет индексов: {report['missing_indexes']}")
    if report["nonempty_tables"]:
        problems.append(f"есть данные: {report['nonempty_tables']}")
    if report["unexpected_migration_markers"]:
        problems.append(
            "неизвестные маркеры миграций: "
            f"{report['unexpected_migration_markers']}"
        )
    if problems:
        raise FreshDatabaseError(
            "Проверка свежей БД не пройдена: " + "; ".join(problems)
        )


async def initialise(path: Path) -> dict[str, object]:
    """Create the schema with the real startup functions and verify it."""
    if PROJECT_ROOT is None:
        raise FreshDatabaseError(
            "Для init нужен исходный код проекта (database/db.py и main.py). "
            "Запустите скрипт из checkout или смонтируйте его в контейнер."
        )
    if path.exists():
        report = inspect_database(path)
        if report["has_business_data"]:
            raise FreshDatabaseError(
                "Целевая БД уже содержит пользовательские/рабочие данные. "
                "Инициализация остановлена; файл не изменён."
            )

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.environ["STUDY_BOT_DB_PATH"] = str(path)
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    import database.db as database
    from database.access_control import init_access_control
    from database.chat_archive import init_chat_archive
    from database.db import init_db
    from database.migrations import run_migrations

    if Path(database.DB_PATH).resolve() != path.resolve():
        raise FreshDatabaseError(
            "DB_PATH не совпал с запрошенной целью; инициализация отменена."
        )

    await init_db()
    await run_migrations()
    await init_chat_archive()
    await init_access_control()

    report = inspect_database(path)
    _assert_fresh_report(report)
    path.chmod(0o600)
    return report


def _print_report(report: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"DB: {report['path']}")
    print(
        "OK: schema complete, no business data, "
        f"integrity={report['integrity']}, "
        f"tables={report['table_count']}, indexes={report['index_count']}, "
        f"migration_markers={len(report['migration_markers'])}"
    )
    print("No users, masters, orders, balances, archives or administrators inserted.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely initialise or verify a fresh Coliseum SQLite database."
    )
    parser.add_argument("command", choices=("init", "verify"))
    parser.add_argument("--db", required=True, help="Path to the SQLite database file.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    try:
        target = validate_target(args.db)
        if args.command == "init":
            report = asyncio.run(initialise(target))
        else:
            if not target.exists():
                raise FreshDatabaseError(f"Файл БД не найден: {target}")
            report = inspect_database(target)
            _assert_fresh_report(report)
        _print_report(report, as_json=args.json)
        return 0
    except FreshDatabaseError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
