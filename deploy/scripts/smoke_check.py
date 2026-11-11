#!/usr/bin/env python3
"""Offline, synthetic smoke check for the Coliseum container layout.

It never reads .env, never touches staging.db/storage, never starts polling and
blocks all network calls. It runs the startup database/migration subset against
a disposable database and verifies the external paths used by Compose.

Run from a checkout with the accepted virtualenv::

    .venv/bin/python -B deploy/scripts/smoke_check.py

The same checks are described in docs/DOCKER_DEPLOYMENT.md for a `docker run`
invocation with a synthetic environment.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _blocked(*args, **kwargs):
    raise RuntimeError("SMOKE: network access is forbidden")


def _configure_synthetic_environment(run_dir: Path) -> None:
    data_dir = run_dir / "data"
    storage_dir = run_dir / "storage"
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ.update(
        BOT_TOKEN="123456:synthetic-container-smoke",
        ADMIN_IDS="10001",
        MASTER_GROUP_ID="-1001234567890",
        TOPUP_ADMIN_USERNAME="smoke_topup",
        SUPPORT_USERNAME="smoke_support",
        STUDY_BOT_DB_PATH=str(data_dir / "database.db"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
    )
    # Do not let a developer .env influence the smoke run.
    os.chdir(run_dir)


async def _initialise_and_inspect() -> tuple[int, int, str]:
    import config
    import database.db as database
    import services.archive_export as archive_export
    import services.archive_worker as archive_worker
    import utils.section_images as section_images
    from database.access_control import init_access_control
    from database.chat_archive import init_chat_archive
    from database.db import init_db
    from database.migrations import run_migrations

    run_dir = Path.cwd()
    synthetic_storage = run_dir / "storage"
    config.ARCHIVE_STORAGE_ROOT = synthetic_storage / "order_archives"
    archive_export.EXPORT_TMP_ROOT = synthetic_storage / "export_tmp"
    archive_worker.archive_worker._storage_root = config.ARCHIVE_STORAGE_ROOT

    if not database.DB_PATH.is_relative_to(run_dir):
        raise RuntimeError("SMOKE: database path escaped the disposable run directory")
    if not config.ARCHIVE_STORAGE_ROOT.is_relative_to(run_dir):
        raise RuntimeError("SMOKE: archive path escaped the disposable run directory")
    if not section_images.IMAGES_DIR.is_dir():
        raise RuntimeError("SMOKE: bundled imgs/ directory is missing")

    config.validate_deployment_config()
    await init_db()
    await run_migrations()
    await init_chat_archive()
    await init_access_control()

    import main  # noqa: F401 - import-only; polling is never started

    connection = sqlite3.connect(str(database.DB_PATH))
    try:
        table_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        )
        integrity = str(
            connection.execute("PRAGMA integrity_check").fetchone()[0]
        )
    finally:
        connection.close()
    if table_count < 10:
        raise RuntimeError(f"SMOKE: unexpectedly few tables: {table_count}")
    if integrity != "ok":
        raise RuntimeError(f"SMOKE: integrity_check failed: {integrity}")

    image_count = len(list(section_images.IMAGES_DIR.glob("*.png")))
    if image_count < 10:
        raise RuntimeError(f"SMOKE: expected bundled PNG assets, found {image_count}")
    return table_count, image_count, integrity


def main() -> int:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.socket.connect_ex = _blocked  # type: ignore[method-assign]
    socket.create_connection = _blocked  # type: ignore[assignment]
    socket.getaddrinfo = _blocked  # type: ignore[assignment]
    socket.gethostbyname = _blocked  # type: ignore[assignment]

    with tempfile.TemporaryDirectory(
        prefix="coliseum_container_smoke_", dir="/tmp/opencode"
    ) as temporary:
        _configure_synthetic_environment(Path(temporary))
        table_count, image_count, integrity = asyncio.run(
            _initialise_and_inspect()
        )

    print("SMOKE OK: synthetic env, external DB path, startup migrations and imports")
    print(f"tables={table_count} integrity={integrity} bundled_png={image_count}")
    print("No network access, no staging data, no polling, no .env read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
