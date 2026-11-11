from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone


MOSCOW_TZ = timezone(timedelta(hours=3), name="MSK")
DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_moscow() -> datetime:
    """Текущее timezone-aware время Москвы (UTC+3)."""
    return datetime.now(MOSCOW_TZ)


def today_moscow() -> date:
    """Текущая календарная дата в Москве."""
    return now_moscow().date()


def format_db_datetime(
    value: object,
    *,
    output_format: str = "%d.%m.%Y %H:%M:%S",
    empty_value: str = "",
) -> str:
    """
    Форматирует timestamp, который хранится в БД уже в московском времени.

    После миграции все timestamp-поля БД используют формат
    YYYY-MM-DD HH:MM:SS и московское время, поэтому дополнительный +3
    при отображении делать нельзя.
    """
    if value is None:
        return empty_value

    raw = str(value)

    try:
        parsed = datetime.strptime(raw, DB_DATETIME_FORMAT)
    except ValueError:
        return raw

    return parsed.strftime(output_format)


class MoscowLogFormatter(logging.Formatter):
    """Форматтер стандартных Python-логов с московским временем (UTC+3)."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 - API logging.Formatter
        current = datetime.fromtimestamp(record.created, MOSCOW_TZ)
        if datefmt:
            return current.strftime(datefmt)
        return current.strftime("%Y-%m-%d %H:%M:%S")


def configure_moscow_logging(level: int = logging.INFO) -> None:
    """Настраивает консольные логи приложения на московское время."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        MoscowLogFormatter(
            "%(asctime)s MSK | %(levelname)s | %(name)s | %(message)s"
        )
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)
