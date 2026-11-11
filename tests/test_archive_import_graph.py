"""Цикл импорта capture/worker и единая точка правды корня архивов.

Phase 3 C2: раньше services/archive_worker.py внутри свойства storage_root
делал lazy-импорт из services.chat_capture, а chat_capture при этом
импортировал воркер -- замкнутый цикл с динамическим workaround. Теперь:
- единственный authoritative источник пути -- config.ARCHIVE_STORAGE_ROOT;
- archive_worker больше вообще не ссылается на services.chat_capture;
- storage_root воркера продолжает следовать за runtime-значением config
  (динамическое чтение) -- инвариант тестовой изоляции сохранён.
"""
from pathlib import Path

import services.archive_worker
import config

# Значение на момент импорта модуля (до autouse-патчей conftest).
RAW_ARCHIVE_ROOT = config.ARCHIVE_STORAGE_ROOT


def test_worker_source_no_longer_references_chat_capture():
    source = Path(services.archive_worker.__file__).read_text()
    assert "services.chat_capture" not in source
    assert "chat_capture" not in source


def test_capture_module_no_longer_defines_duplicate_storage_root():
    from services import chat_capture

    source = Path(chat_capture.__file__).read_text()
    assert "STORAGE_ROOT" not in source
    assert not hasattr(chat_capture, "STORAGE_ROOT")


def test_archive_export_reads_config_authority():
    from services import archive_export

    source = Path(archive_export.__file__).read_text()
    assert "from services.chat_capture" not in source
    assert "config.ARCHIVE_STORAGE_ROOT" in source
    assert not hasattr(archive_export, "STORAGE_ROOT")


def test_worker_default_storage_root_follows_config(monkeypatch, tmp_path):
    from services.archive_worker import ArchiveDownloadWorker

    root = tmp_path / "synthetic_archives"
    monkeypatch.setattr(config, "ARCHIVE_STORAGE_ROOT", root)
    worker = ArchiveDownloadWorker()
    assert worker.storage_root == root


def test_explicit_storage_root_still_wins(monkeypatch, tmp_path):
    from services.archive_worker import ArchiveDownloadWorker

    monkeypatch.setattr(config, "ARCHIVE_STORAGE_ROOT", tmp_path / "config_root")
    worker = ArchiveDownloadWorker(storage_root=tmp_path / "explicit_root")
    assert worker.storage_root == tmp_path / "explicit_root"


def test_config_root_points_to_runtime_locations():
    # Значение по умолчанию эквивалентно прежнему const в chat_capture:
    # <проект>/storage/order_archives. Сравниваем значение, захваченное
    # до autouse-патчей conftest.
    expected = Path(__file__).resolve().parent.parent / "storage" / "order_archives"
    assert RAW_ARCHIVE_ROOT == expected
