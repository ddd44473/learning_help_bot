import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


BOT_TOKEN = os.getenv("BOT_TOKEN")

MASTER_GROUP_ID = int(
    os.getenv("MASTER_GROUP_ID", "0")
)


def parse_admin_ids(value: str) -> set[int]:
    admin_ids: set[int] = set()

    for raw_id in value.split(","):
        normalized_id = raw_id.strip()

        if not normalized_id:
            continue

        try:
            admin_ids.add(int(normalized_id))
        except ValueError as error:
            raise ValueError(
                "ADMIN_IDS должен содержать Telegram ID "
                "через запятую."
            ) from error

    return admin_ids


ADMIN_IDS = parse_admin_ids(
    os.getenv("ADMIN_IDS", "")
)

TOPUP_ADMIN_USERNAME = (
    os.getenv("TOPUP_ADMIN_USERNAME", "")
    .strip()
    .lstrip("@")
)

# Контакт менеджера для кнопок «Написать менеджеру».
# Можно изменить в .env: SUPPORT_USERNAME=another_username
SUPPORT_USERNAME = (
    os.getenv("SUPPORT_USERNAME")
    or os.getenv("TOPUP_ADMIN_USERNAME")
    or ""
).strip().lstrip("@")

SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}" if SUPPORT_USERNAME else ""

# Единая точка правды для корня приватного хранилища архивов переписок.
# services/archive_worker.py и services/archive_export.py читают этот
# атрибут динамически (config.ARCHIVE_STORAGE_ROOT); дублирующего
# определения в services.chat_capture больше нет, что убирает
# циклический импорт services.chat_capture <-> services.archive_worker.
ARCHIVE_STORAGE_ROOT = (
    Path(__file__).resolve().parent / "storage" / "order_archives"
)


def validate_deployment_config() -> None:
    """Проверка обязательных параметров без вывода их значений."""
    if not isinstance(BOT_TOKEN, str) or ":" not in BOT_TOKEN:
        raise ValueError("Задайте корректный BOT_TOKEN перед запуском.")
    prefix, secret = BOT_TOKEN.split(":", 1)
    if not prefix.isdecimal() or not secret or any(char.isspace() for char in BOT_TOKEN):
        raise ValueError("Некорректный формат BOT_TOKEN.")
    if not ADMIN_IDS or any(value <= 0 or value > 9223372036854775807 for value in ADMIN_IDS):
        raise ValueError("ADMIN_IDS должен содержать положительные Telegram ID администраторов.")
    if not str(MASTER_GROUP_ID).startswith("-100") or MASTER_GROUP_ID >= 0:
        raise ValueError("MASTER_GROUP_ID должен указывать рабочую форум-супергруппу (-100…).")
