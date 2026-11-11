#!/usr/bin/env python3
"""Restore a Coliseum runtime backup into a stopped deployment.

The script verifies the backup manifest, every SHA-256 checksum and the SQLite
integrity of the backup before changing anything. Existing targets are never
deleted: with ``--force`` they are renamed to ``*.pre-restore-<stamp>`` so an
operator can go back manually. Use disposable target paths for rehearsal.

Example rehearsal (never point this at live data)::

    python3 deploy/scripts/restore_runtime.py \
        --backup /opt/coliseum/backups/coliseum-backup-YYYYmmdd-HHMMSS \
        --db /tmp/coliseum-restore/data/database.db \
        --storage /tmp/coliseum-restore/storage \
        --confirm-stopped

Live restore after a fresh backup and ``docker compose stop bot``::

    python3 deploy/scripts/restore_runtime.py \
        --backup /opt/coliseum/backups/<verified-backup> \
        --db /opt/coliseum/data/database.db \
        --storage /opt/coliseum/storage \
        --force --confirm-stopped

Only the Python standard library is used. No network access occurs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


class RestoreError(RuntimeError):
    """Verification or a safety check failed; nothing should be trusted."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RestoreError(f"Unsafe path in backup manifest: {value!r}")
    return relative


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def _check_integrity(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    finally:
        connection.close()
    if rows != ["ok"]:
        raise RestoreError("SQLite integrity_check failed: " + ", ".join(rows[:5]))


def verify_backup(backup: Path) -> dict:
    manifest_path = backup / "manifest.json"
    manifest_hash_path = backup / "MANIFEST.sha256"
    checksums_path = backup / "SHA256SUMS"
    incomplete = backup / "INCOMPLETE.txt"
    if incomplete.exists():
        raise RestoreError("Backup directory is marked INCOMPLETE")
    for required in (manifest_path, manifest_hash_path, checksums_path):
        if not required.is_file():
            raise RestoreError(f"Missing backup file: {required.name}")

    expected_manifest_hash = manifest_hash_path.read_text(encoding="utf-8").split()[0]
    if sha256_file(manifest_path) != expected_manifest_hash:
        raise RestoreError("manifest.json does not match MANIFEST.sha256")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "coliseum-runtime-backup-v1":
        raise RestoreError("Unsupported backup format")
    database = manifest.get("database") or {}
    storage = manifest.get("storage") or {}
    if database.get("file") != "database.db":
        raise RestoreError("Unexpected database filename in manifest")

    expected: dict[str, str] = {"database.db": str(database.get("sha256", ""))}
    for item in storage.get("files", []):
        relative = _safe_relative(str(item["path"]))
        expected[f"storage/{relative.as_posix()}"] = str(item.get("sha256", ""))

    listed: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise RestoreError("Malformed SHA256SUMS line")
        digest, name = parts
        _safe_relative(name)
        if name in listed:
            raise RestoreError(f"Duplicate checksum entry: {name}")
        listed[name] = digest

    if set(listed) != set(expected):
        raise RestoreError("SHA256SUMS does not match the manifest file set")
    for name, digest in listed.items():
        candidate = backup / name
        if not candidate.is_file():
            raise RestoreError(f"Missing backup payload: {name}")
        if sha256_file(candidate) != digest:
            raise RestoreError(f"Checksum mismatch: {name}")

    db_path = backup / "database.db"
    _check_integrity(db_path)
    if db_path.stat().st_size != int(database.get("size", -1)):
        raise RestoreError("Database size does not match manifest")

    actual_count = 0
    actual_bytes = 0
    for item in storage.get("files", []):
        candidate = backup / "storage" / _safe_relative(str(item["path"]))
        actual_count += 1
        actual_bytes += candidate.stat().st_size
    if actual_count != int(storage.get("file_count", -1)):
        raise RestoreError("Storage file count does not match manifest")
    if actual_bytes != int(storage.get("total_bytes", -1)):
        raise RestoreError("Storage byte count does not match manifest")
    return manifest


def _move_aside(path: Path, stamp: str) -> Path | None:
    if path.is_symlink():
        raise RestoreError(f"Target must not be a symlink: {path}")
    if not path.exists():
        return None
    destination = path.with_name(f"{path.name}.pre-restore-{stamp}")
    if destination.exists() or destination.is_symlink():
        raise RestoreError(f"Pre-restore target already exists: {destination}")
    os.replace(path, destination)
    return destination


def restore(
    backup: Path,
    db_target: Path,
    storage_target: Path,
    *,
    force: bool,
) -> tuple[dict, list[Path]]:
    manifest = verify_backup(backup)
    if _is_within(db_target, backup) or _is_within(storage_target, backup):
        raise RestoreError("Restore targets must be outside the backup directory")
    if _is_within(backup, storage_target):
        raise RestoreError("Backup directory must not be inside the storage target")
    if _is_within(db_target, storage_target) or _is_within(storage_target, db_target):
        raise RestoreError("Database and storage targets must not overlap")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    database_sidecars = [
        Path(str(db_target) + suffix) for suffix in ("-wal", "-shm", "-journal")
    ]
    existing_database = db_target.exists() or any(p.exists() for p in database_sidecars)
    existing_storage = storage_target.exists()
    if (existing_database or existing_storage) and not force:
        raise RestoreError(
            "Target database or storage already exists; use --force after a fresh backup"
        )

    moved: list[Path] = []
    for path in [db_target, *database_sidecars]:
        moved_path = _move_aside(path, stamp)
        if moved_path is not None:
            moved.append(moved_path)
    moved_storage = _move_aside(storage_target, stamp)
    if moved_storage is not None:
        moved.append(moved_storage)

    db_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(backup / "database.db", db_target)
    os.chmod(db_target, 0o600)
    _check_integrity(db_target)

    storage_target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(storage_target, 0o700)
    for item in manifest["storage"]["files"]:
        relative = _safe_relative(str(item["path"]))
        source = backup / "storage" / relative
        destination = storage_target / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        if sha256_file(destination) != str(item["sha256"]):
            raise RestoreError(f"Restored file checksum mismatch: {relative}")
    return manifest, moved


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and restore a Coliseum runtime backup."
    )
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/opt/coliseum/data/database.db"),
        help="Target SQLite path (default: %(default)s)",
    )
    parser.add_argument(
        "--storage",
        type=Path,
        default=Path("/opt/coliseum/storage"),
        help="Target storage root (default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rename existing targets aside instead of refusing",
    )
    parser.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="Required: confirm the bot container is stopped",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.confirm_stopped:
        print(
            "Refusing to run: stop the bot container first, then pass --confirm-stopped.",
            file=sys.stderr,
        )
        return 2
    backup_path = args.backup.absolute()
    try:
        manifest, moved = restore(
            backup_path,
            args.db.absolute(),
            args.storage.absolute(),
            force=args.force,
        )
    except (RestoreError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"Restore failed: {error}", file=sys.stderr)
        return 1

    print(f"Restore completed from: {backup_path}")
    print(f"Database integrity_check: {manifest['database']['integrity_check']}")
    print(f"Storage files restored: {manifest['storage']['file_count']}")
    if moved:
        print("Previous targets renamed aside:")
        for path in moved:
            print(f"  {path}")
    else:
        print("No previous target existed; nothing was overwritten.")
    print("Start the bot, inspect logs, and keep the pre-restore paths until verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
