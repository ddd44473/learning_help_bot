"""Пути private storage: отказываем symlink/escape до чтения/записи."""
import hashlib
import os
from pathlib import Path


class StorageBoundaryError(ValueError):
    pass


def contained_path(root: Path, relative: str, *, mkdir: bool = False) -> Path:
    root = root.absolute()
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise StorageBoundaryError("Некорректный путь приватного файла")
    # Проверяем также предков root, чтобы сам root не был ссылкой.
    for parent in (*reversed(root.parents), root):
        if parent.is_symlink():
            raise StorageBoundaryError("Символьные ссылки в private storage запрещены")
    if mkdir:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = root
    for index, part in enumerate(rel.parts):
        current = current / part
        if current.is_symlink():
            raise StorageBoundaryError("Символьные ссылки в private storage запрещены")
        if mkdir and index < len(rel.parts) - 1:
            current.mkdir(exist_ok=True, mode=0o700)
    if not current.resolve().is_relative_to(root.resolve()):
        raise StorageBoundaryError("Путь выходит за private storage")
    return current


def storage_bytes(root: Path, *, max_entries: int = 100000) -> int:
    """Только metadata; не следуем ссылкам, ограничиваем обход."""
    contained_path(root, "boundary-check", mkdir=True)
    total = count = 0
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > max_entries or entry.is_symlink():
                    raise StorageBoundaryError("Хранилище требует ручной проверки")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                else:
                    raise StorageBoundaryError("Неподдерживаемый объект в хранилище")
    return total


def file_digest(path: Path, max_bytes: int) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    import stat
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise StorageBoundaryError("Ожидался обычный файл")
        for chunk in iter(lambda: stream.read(65536), b""):
            size += len(chunk)
            if size > max_bytes:
                raise StorageBoundaryError("Файл превышает разрешённый размер")
            digest.update(chunk)
    return digest.hexdigest(), size
