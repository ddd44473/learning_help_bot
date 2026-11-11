from aiogram import Router, F
from aiogram.types import CallbackQuery

from keyboards.help import help_keyboard
from utils.section_images import send_section_message

router = Router()


HELP_TEXT = """
🛟 <b>Помощь</b>

Здесь вы можете найти ответы на частые вопросы или напрямую написать менеджеру.

Выберите нужный раздел ниже 👇
"""


@router.callback_query(F.data == "help_settings")
async def open_help_settings(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await send_section_message(
        callback.message,
        "help.png",
        HELP_TEXT,
        reply_markup=help_keyboard(),
        parse_mode="HTML",
        replace_photo=True,
    )
    await callback.answer()
