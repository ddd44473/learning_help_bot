from aiogram.types import Message


def is_relay_content(message: Message) -> bool:
    """Slash-команды обрабатываются command routers, не копируются собеседнику."""
    return not (message.text and message.text.startswith('/'))
