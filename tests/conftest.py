"""Конфигурация изолированной тестовой среды BUILD-аудита.

БЕЗОПАСНОСТЬ (§8 build_full.md):
- все тесты работают только с синтетическими одноразовыми базами в
  изолированном каталоге /tmp/opencode/study_bot_audit_tests;
- STUDY_BOT_DB_PATH устанавливается ДО первого импорта database.*,
  потому что модули импортируют DB_PATH по значению;
- автouse-фикстура блокирует aiosqlite.connect для любых путей вне
  изолированного каталога;
- реальные Telegram-вызовы запрещены, start_polling не вызывается.
"""

import os
import sys
import socket
from datetime import datetime
from pathlib import Path

import dotenv

# Не загружать даже локальные .env при импорте config в тестах.
dotenv.load_dotenv = lambda *args, **kwargs: False

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Изолированный каталог тестовых данных (одобренный временный корень).
# Уникальный подкаталог на каждый процесс pytest, чтобы независимые прогоны
# не делили и не удаляли файлы базы данных друг друга.
TEST_SCRATCH_DIR = (
    Path("/tmp/opencode") / "study_bot_audit_tests"
    / f"run_{os.getpid()}_{int(datetime.now().timestamp() * 1000)}"
)
TEST_SCRATCH_DIR.mkdir(parents=True, exist_ok=True)


def _configure_db_path():
    if os.environ.get("STUDY_BOT_DB_PATH"):
        raise RuntimeError(
            "STUDY_BOT_DB_PATH уже задан до тестов: значения внешнего "
            "окружения в тестовом сейфе не допускаются."
        )
    os.environ["STUDY_BOT_DB_PATH"] = str(TEST_SCRATCH_DIR / "test_database.db")


if os.environ.get("STUDY_BOT_RUNNING_TESTS") != "1":
    os.environ["STUDY_BOT_RUNNING_TESTS"] = "1"
    _configure_db_path()

# Тесты не должны зависеть от продуктивных значений секретов.
for _secret_var in ("BOT_TOKEN", "ADMIN_IDS", "MASTER_GROUP_ID"):
    os.environ.pop(_secret_var, None)
os.environ["BOT_TOKEN"] = "123456:synthetic-test-token"
os.environ["ADMIN_IDS"] = "88001"
os.environ["MASTER_GROUP_ID"] = "-10088001"


FORBIDDEN_DB_PATHS = (
    Path("/mnt/d/tgbot/database/database.db"),
    Path("/home/igor/study_bot/database/database.db"),
    Path("/home/igor/study_bot_audit/database/database.db"),
)


import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402


@pytest.fixture
def use_db_path(monkeypatch):
    """
    Перенацеливает ВСЕ модули, импортировавшие DB_PATH по значению,
    на указанный тестовый файл базы (§8 build_full.md).
    """

    def _apply(path):
        import importlib

        import database

        module_names = [
            name
            for name in database.__dict__
            if not name.startswith("_")
        ]
        # Прямые импорты DB_PATH по значению.
        for name in module_names:
            module = importlib.import_module(f"database.{name}")
            if hasattr(module, "DB_PATH"):
                monkeypatch.setattr(module, "DB_PATH", path)
        import database.db as db_module

        monkeypatch.setattr(db_module, "DB_PATH", path)

    return _apply


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise RuntimeError("ЗАЩИТА ТЕСТОВ: сетевые соединения запрещены")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "gethostbyname", blocked)


@pytest.fixture(autouse=True)
def block_forbidden_paths(monkeypatch):
    """Все соединения тестов обязаны жить в изолированном каталоге."""
    import aiosqlite
    import sqlite3

    # Разрешаем любой путь внутри общий временной корень аудита (включая
    # подкаталоги pytest-tmp и уникальный run-каталог), но blokируем реальные
    # базы за его пределами.
    allowed_root = Path("/tmp/opencode/study_bot_audit_tests").resolve()
    forbidden = {
        path.resolve() for path in FORBIDDEN_DB_PATHS if path.exists()
    }
    original_connect = aiosqlite.connect
    original_sqlite_connect = sqlite3.connect

    def validate_path(path):
        try:
            resolved = Path(str(path)).resolve()
        except (TypeError, OSError) as error:  # pragma: no cover
            raise RuntimeError(
                "aiosqlite.connect: невозможно определить путь"
            ) from error
        if resolved in forbidden:
            raise RuntimeError(
                "ЗАЩИТА ТЕСТОВ: попытка открыть запрещённую базу "
                f"{resolved}"
            )
        if not resolved.is_relative_to(allowed_root):
            raise RuntimeError(
                "ЗАЩИТА ТЕСТОВ: путь базы вне изолированного каталога: "
                f"{resolved}"
            )

    def guarded_connect(path, *args, **kwargs):
        validate_path(path)
        return original_connect(path, *args, **kwargs)

    def guarded_sqlite_connect(path, *args, **kwargs):
        validate_path(path)
        return original_sqlite_connect(path, *args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", guarded_connect)
    monkeypatch.setattr(sqlite3, "connect", guarded_sqlite_connect)
    yield


@pytest.fixture(autouse=True)
def isolated_archive_storage(monkeypatch, tmp_path):
    import config
    import services.archive_export
    import utils.section_images

    root = tmp_path / "synthetic_archives"
    # Единая точка правды: worker и exporter читают config.ARCHIVE_STORAGE_ROOT
    # динамически, одного патча достаточно для обеих подсистем.
    monkeypatch.setattr(config, "ARCHIVE_STORAGE_ROOT", root)
    monkeypatch.setattr(services.archive_export, "EXPORT_TMP_ROOT", tmp_path / 'synthetic_exports')
    monkeypatch.setattr(utils.section_images, "IMAGES_DIR", tmp_path / 'synthetic_images')


@pytest_asyncio.fixture(autouse=True)
async def fresh_database():
    """Чистая синтетическая база перед каждым тестом."""
    from database.db import init_db
    from database.db import DB_PATH as db_path
    from database.migrations import run_migrations

    db_file = Path(str(db_path))
    if not db_file.resolve().is_relative_to(TEST_SCRATCH_DIR.resolve()):
        raise RuntimeError("Refusing fixture cleanup outside this test run")
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_file) + suffix)
        if candidate.exists():
            candidate.unlink()

    await init_db()
    await run_migrations()

    yield db_file

    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_file) + suffix)
        if candidate.exists():
            candidate.unlink()


@pytest_asyncio.fixture
async def db(fresh_database):
    """Открытое соединение с текущей тестовой базой (Row-фабрика)."""
    import aiosqlite

    connection = await aiosqlite.connect(fresh_database)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        await connection.close()
