"""Диагностический catch-all handler.

СТАТУС: dormant/compatibility. Router намеренно НЕ подключён в main.py,
потому что `@router.message()` перехватывает любые сообщения и способен
сломать рабочие FSM-сценарии и заглушки. Использовать только вручную
для локальной диагностики chat_id; не подключать без owner review.
"""
from aiogram import Router
from aiogram.types import Message

router = Router()


@router.message()
async def show_chat_id(message: Message):
    print(
        f"CHAT_ID: {message.chat.id}, "
        f"TYPE: {message.chat.type}, "
        f"TITLE: {message.chat.title}"
    )
