"""Проверка личности и назначения приватного административного ответа."""

from aiogram.types import CallbackQuery, Message

from config import ADMIN_IDS


async def require_private_admin(event: Message | CallbackQuery) -> bool:
    user = event.from_user
    if user is None or user.id not in ADMIN_IDS:
        if isinstance(event, CallbackQuery):
            await event.answer("Недостаточно прав.", show_alert=True)
        return False
    message = event.message if isinstance(event, CallbackQuery) else event
    if (
        not isinstance(message, Message)
        or message.chat.type != "private"
        or message.chat.id != user.id
    ):
        if isinstance(event, CallbackQuery):
            await event.answer("Доступно только в личном чате с ботом.", show_alert=True)
        elif isinstance(message, Message):
            await message.answer("Откройте эту команду в личном чате с ботом.")
        return False
    return True
