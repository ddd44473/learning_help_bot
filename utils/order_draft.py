"""Чистая нормализация черновика заказа (без Telegram UI).

Используется handlers/order_form.py на этапе "условия заказа". Функции не
отправляют сообщения и ничего не знают о FSM/диспетчере: разбор текста,
валидация дедлайна и выборка attached-файла из Message.
"""
from datetime import datetime
import re

from aiogram.types import Message

from utils.moscow_time import today_moscow

DETAILS_DEADLINE_RE = re.compile(
    r"^Дедлайн:\s*(\d{2}-\d{2}-\d{4})$",
    re.IGNORECASE,
)


def validate_deadline(value: str) -> bool:
    try:
        deadline = datetime.strptime(value, "%d-%m-%Y").date()
    except ValueError:
        return False

    return deadline >= today_moscow()


def parse_order_details(raw_text: str) -> tuple[str | None, str | None, str | None]:
    """
    Разбирает единое сообщение заказа.

    Описание обязательно. Дедлайн необязателен; если пользователь его
    указывает, он должен быть последней непустой строкой в формате:
    Дедлайн: ДД-ММ-ГГГГ
    """

    lines = raw_text.strip().splitlines()

    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return None, None, "Сначала опишите условия задания."

    deadline = None
    last_line = lines[-1].strip()
    match = DETAILS_DEADLINE_RE.fullmatch(last_line)

    if match is not None:
        deadline = match.group(1)

        if not validate_deadline(deadline):
            return (
                None,
                None,
                "Некорректный дедлайн. Укажите сегодняшний или будущий день "
                "в формате ДД-ММ-ГГГГ.",
            )

        description_lines = lines[:-1]
    elif last_line.lower().startswith("дедлайн:"):
        return (
            None,
            None,
            "Если указываете дедлайн, используйте формат:\n"
            "Дедлайн: ДД-ММ-ГГГГ\n\n"
            "Например: Дедлайн: 15-09-2026",
        )
    else:
        description_lines = lines

    description = "\n".join(description_lines).strip()

    if len(description) < 10:
        return (
            None,
            None,
            "Описание слишком короткое. Напишите условия задания "
            "хотя бы в 10 символах.",
        )

    if len(description) > 3000:
        return (
            None,
            None,
            "Описание слишком длинное. Максимум — 3000 символов.",
        )

    return description, deadline, None


def get_attached_file(message: Message) -> dict | None:
    if message.photo:
        return {
            "telegram_file_id": message.photo[-1].file_id,
            "file_type": "photo",
            "file_name": None,
        }

    if message.document:
        return {
            "telegram_file_id": message.document.file_id,
            "file_type": "document",
            "file_name": message.document.file_name,
        }

    return None
