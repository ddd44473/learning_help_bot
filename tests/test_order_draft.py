"""Чистые функции черновика заказа: parse_order_details / get_attached_file.

Phase 3 C5: точный characterization-набор для pure-логики, перенесённой из
handlers/order_form.py в utils/order_draft.py. Поведение не изменилось —
здесь фиксируется текущий пользовательский вывод по каждому классу ввода.
"""
from datetime import datetime, timezone

from aiogram.types import Chat, Document, Message, PhotoSize, User

from utils.order_draft import get_attached_file, parse_order_details


def test_description_with_future_deadline():
    description, deadline, error = parse_order_details(
        "Сформулируйте условия задания точно.\nДедлайн: 15-09-2099"
    )
    assert description == "Сформулируйте условия задания точно."
    assert deadline == "15-09-2099"
    assert error is None


def test_description_without_deadline():
    description, deadline, error = parse_order_details(
        "Короткое описание задания без дедлайна."
    )
    assert "Короткое описание задания" in description
    assert deadline is None
    assert error is None


def test_empty_text_error():
    description, deadline, error = parse_order_details("   \n  ")
    assert (description, deadline, error) == (
        None, None, "Сначала опишите условия задания."
    )


def test_too_short_description_error():
    error = parse_order_details("короче 10 ")[2]
    assert error == (
        "Описание слишком короткое. Напишите условия задания "
        "хотя бы в 10 символах."
    )


def test_too_long_description_error():
    error = parse_order_details("а" * 3001)[2]
    assert error == (
        "Описание слишком длинное. Максимум — 3000 символов."
    )


def test_past_deadline_error():
    error = parse_order_details(
        "Описание достаточно длинное и корректное.\nДедлайн: 01-01-2020"
    )[2]
    assert error == (
        "Некорректный дедлайн. Укажите сегодняшний или будущий день "
        "в формате ДД-ММ-ГГГГ."
    )


def test_malformed_deadline_hint():
    error = parse_order_details(
        "Описание достаточно длинное и корректное.\nДедлайн: 2099"
    )[2]
    assert error is not None and "дедлайн" in error.lower()
    assert "15-09-2026" in error  # пример формата сохранён


def test_deadline_must_be_last_line():
    error = parse_order_details(
        "Дедлайн: 15-09-2099\nОписание достаточно длинное и корректное."
    )[2]
    assert error is None  # без валидного последнего дедлайна это просто описание


def _message(*, photo=False, document=False):
    chat = Chat(id=88201, type="private")
    user = User(id=88201, is_bot=False, first_name="Synthetic")
    kwargs = dict(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
    )
    if photo:
        kwargs["photo"] = [PhotoSize(file_id="photo-1", file_unique_id="u1", width=1, height=1)]
    if document:
        kwargs["document"] = Document(file_id="doc-1", file_unique_id="u2", file_name="task.txt")
    return Message(**kwargs)


def test_get_attached_file_photo():
    assert get_attached_file(_message(photo=True)) == {
        "telegram_file_id": "photo-1",
        "file_type": "photo",
        "file_name": None,
    }


def test_get_attached_file_document():
    assert get_attached_file(_message(document=True)) == {
        "telegram_file_id": "doc-1",
        "file_type": "document",
        "file_name": "task.txt",
    }


def test_get_attached_file_none():
    assert get_attached_file(_message()) is None
