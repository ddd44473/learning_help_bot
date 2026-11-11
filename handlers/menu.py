from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

from keyboards.main_menu import main_menu_keyboard
from utils.section_images import send_section_message

router = Router()


MAIN_MENU_TEXT = """
👋 Добро пожаловать!

Здесь вы можете оформить заказ на учебную работу, посмотреть информацию о сервисе и перейти в профиль.

📝 Чтобы начать оформление, нажмите кнопку «Сделать заказ».

👤 В профиле позже будут отображаться ваши заказы и данные.
"""


async def show_main_menu(message: Message, *, new_message: bool = False):
    await send_section_message(
        message,
        "main_menu.png",
        MAIN_MENU_TEXT,
        reply_markup=main_menu_keyboard(),
        replace_photo=not new_message,
    )


@router.callback_query(F.data == "main_menu")
async def open_main_menu(callback: CallbackQuery):
    await callback.answer()
    await show_main_menu(callback.message)


@router.callback_query(F.data == "start_main_menu")
async def open_main_menu_from_welcome(callback: CallbackQuery):
    """Из приветствия /start открываем отдельное сообщение с меню."""
    await callback.answer()

    if callback.message is None:
        return

    await show_main_menu(callback.message, new_message=True)
