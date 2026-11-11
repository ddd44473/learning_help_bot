#!/usr/bin/env python3
"""Create a consistent maintenance-window backup of Coliseum runtime data.

Scope: the SQLite database and the mutable storage tree only. The private
bot.env file is deliberately outside both paths and is never read or copied.

Safety model:
* Run only while the bot container is stopped (``--confirm-stopped``).
* Use the SQLite backup API, never a bare copy of a live database file.
* Copy the storage tree with structure, rejecting symlinks and special files.
* Write per-file SHA-256 hashes, a manifest and a database integrity report.
* Refuse to write a backup inside the live storage tree or database directory.

Example (on the VPS, after ``docker compose stop bot``)::

    python3 deploy/scripts/backup_runtime.py \
        --db /opt/coliseum/data/database.db \
        --storage /opt/coliseum/storage \
        --out /opt/coliseum/backups \
        --confirm-stopped

This script uses only the Python standard library. It does not contact
Telegram or any other network service.
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
BACKUP_PREFIX = "coliseum-backup-"


class BackupError(RuntimeError):
    """A safety or consistency check failed; the backup is incomplete."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def _open_source_database(path: Path) -> sqlite3.Connection:
    if path.is_symlink():
        raise BackupError(f"Database path must not be a symlink: {path}")
    if not path.is_file():
        raise BackupError(f"Database file not found: {path}")
    # Read/write connection is used only so SQLite can recover/checkpoint a
    # WAL sidecar after a clean stop; query_only prevents application writes.
    connection = sqlite3.connect(str(path), timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA query_only = ON")
    return connection


def backup_database(source_path: Path, target_path: Path) -> dict[str, object]:
    source = _open_source_database(source_path)
    try:
        source_journal_mode = str(
            source.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        source_page_count = int(
            source.execute("PRAGMA page_count").fetchone()[0]
        )
        target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = sqlite3.connect(str(target_path), timeout=10)
        try:
            target.execute("PRAGMA busy_timeout = 10000")
            source.backup(target)
            target.execute("PRAGMA journal_mode = DELETE").fetchone()
            integrity = [
                str(row[0])
                for row in target.execute("PRAGMA integrity_check").fetchall()
            ]
            if integrity != ["ok"]:
                raise BackupError(
                    "SQLite integrity_check failed on the backup copy: "
                    + ", ".join(integrity[:5])
                )
            foreign_key_rows = len(
                target.execute("PRAGMA foreign_key_check").fetchall()
            )
            target_page_count = int(
                target.execute("PRAGMA page_count").fetchone()[0]
            )
            target.commit()
        finally:
            target.close()
    finally:
        source.close()

    for sidecar in ("-wal", "-shm", "-journal"):
        candidate = Path(str(target_path) + sidecar)
        if candidate.exists():
            raise BackupError(
                f"Unexpected SQLite sidecar after backup: {candidate.name}"
            )

    os.chmod(target_path, 0o600)
    size = target_path.stat().st_size
    if size <= 0:
        raise BackupError("Backup database is empty")

    return {
        "file": target_path.name,
        "size": size,
        "sha256": sha256_file(target_path),
        "integrity_check": "ok",
        "foreign_key_violations": foreign_key_rows,
        "source_journal_mode": source_journal_mode,
        "source_page_count": source_page_count,
        "backup_page_count": target_page_count,
    }


def _copy_storage(source: Path, target: Path) -> dict[str, object]:
    if not source.is_dir():
        raise BackupError(f"Storage directory not found: {source}")
    if source.is_symlink():
        raise BackupError("Storage root must not be a symlink")

    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    files: list[dict[str, object]] = []
    total_bytes = 0
    file_count = 0

    for root, dirs, names in os.walk(source, followlinks=False):
        root_path = Path(root)
        if root_path.is_symlink():
            raise BackupError(f"Symlink directory is not allowed: {root_path}")

        relative_root = root_path.relative_to(source)
        target_root = target / relative_root
        target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target_root, 0o700)

        for directory in list(dirs):
            candidate = root_path / directory
            if candidate.is_symlink():
                raise BackupError(f"Symlink directory is not allowed: {candidate}")
        for name in names:
            candidate = root_path / name
            if candidate.is_symlink():
                raise BackupError(f"Symlink file is not allowed: {candidate}")
            if not candidate.is_file():
                raise BackupError(f"Special file is not allowed: {candidate}")

            relative = candidate.relative_to(source)
            if ".." in relative.parts:
                raise BackupError("Unsafe relative path in storage")
            destination = target / relative
            if not _is_within(destination, target):
                raise BackupError("Storage copy escaped the backup root")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(candidate, destination)
            os.chmod(destination, 0o600)
            size = destination.stat().st_size
            files.append(
                {
                    "path": relative.as_posix(),
                    "size": size,
                    "sha256": sha256_file(destination),
                }
            )
            total_bytes += size
            file_count += 1

    files.sort(key=lambda item: str(item["path"]))
    return {
        "root": "storage",
        "file_count": file_count,
        "total_bytes": total_bytes,
        "files": files,
    }


def _choose_target(out_base: Path, name: str | None) -> Path:
    if out_base.is_symlink():
        raise BackupError(f"Backup destination must not be a symlink: {out_base}")
    out_base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not out_base.is_dir():
        raise BackupError(f"Backup destination is not a directory: {out_base}")
    if name:
        candidate_name = Path(name)
        if (
            candidate_name.is_absolute()
            or len(candidate_name.parts) != 1
            or name in {".", ".."}
        ):
            raise BackupError("--name must be a single directory name")
        candidate = out_base / candidate_name
        if candidate.exists() or candidate.is_symlink():
            raise BackupError(f"Backup target already exists: {candidate}")
        return candidate
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidate = out_base / f"{BACKUP_PREFIX}{stamp}"
    suffix = 1
    while candidate.exists():
        candidate = out_base / f"{BACKUP_PREFIX}{stamp}-{suffix}"
        suffix += 1
    return candidate


def create_backup(db_path: Path, storage_path: Path, out_base: Path, name: str | None) -> Path:
    if _is_within(out_base, storage_path) or _is_within(out_base, db_path.parent):
        raise BackupError(
            "Backup destination must be outside the live storage and data directories"
        )
    if _is_within(db_path, storage_path):
        raise BackupError("Database must not be inside the storage tree")

    target = _choose_target(out_base, name)
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        database = backup_database(db_path, target / "database.db")
        storage = _copy_storage(storage_path, target / "storage")
        manifest = {
            "format": "coliseum-runtime-backup-v1",
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": {
                "database": str(db_path),
                "storage": str(storage_path),
            },
            "database": database,
            "storage": storage,
            "notes": [
                "Backup created with the SQLite backup API after the bot was stopped.",
                "Private bot.env is intentionally excluded.",
                "Keep an encrypted copy off the server and test restore on disposable paths.",
            ],
        }
        manifest_path = target / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        checksum_lines = [f"{database['sha256']}  database.db"]
        checksum_lines.extend(
            f"{item['sha256']}  storage/{item['path']}"
            for item in storage["files"]
        )
        (target / "SHA256SUMS").write_text(
            "\n".join(checksum_lines) + "\n", encoding="utf-8"
        )
        (target / "MANIFEST.sha256").write_text(
            f"{sha256_file(manifest_path)}  manifest.json\n", encoding="utf-8"
        )
        return target
    except Exception as error:  # noqa: BLE001 - record incomplete backup honestly
        marker = target / "INCOMPLETE.txt"
        marker.write_text(
            f"Backup failed: {type(error).__name__}\n"
            "Do not use this directory as a restore source.\n",
            encoding="utf-8",
        )
        os.chmod(marker, 0o600)
        raise


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Consistent maintenance-window backup for Coliseum."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("/opt/coliseum/data/database.db"),
        help="SQLite database path (default: %(default)s)",
    )
    parser.add_argument(
        "--storage",
        type=Path,
        default=Path("/opt/coliseum/storage"),
        help="Mutable storage root (default: %(default)s)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("/opt/coliseum/backups"),
        help="Backup destination base directory (default: %(default)s)",
    )
    parser.add_argument("--name", help="Optional exact backup directory name")
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
    try:
        target = create_backup(
            args.db.absolute(),
            args.storage.absolute(),
            args.out.absolute(),
            args.name,
        )
    except (BackupError, OSError, sqlite3.Error) as error:
        print(f"Backup failed: {error}", file=sys.stderr)
        return 1

    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    print(f"Backup created: {target}")
    print(f"Database integrity_check: {manifest['database']['integrity_check']}")
    print(f"Storage files: {manifest['storage']['file_count']}")
    print(f"Storage bytes: {manifest['storage']['total_bytes']}")
    print("Private bot.env: not included")
    print("Next: copy this directory off-server and verify a test restore.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
