from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from keyboards.order import catalog_directions_keyboard
from states.order import OrderState
from catalog.directions import DIRECTION_TITLES, DIRECTION_DESCRIPTIONS
from utils.section_images import send_section_message


router = Router()


CATALOG_TEXT = """
📚 <b>Оформление заказа</b>

Выберите направление, к которому относится ваше задание.

После выбора направления останется одним сообщением отправить условия. Дедлайн и файлы можно добавить при необходимости.
"""


@router.callback_query(F.data == "make_order")
async def open_order_menu(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await send_section_message(
        callback.message,
        "make_order.png",
        CATALOG_TEXT,
        reply_markup=catalog_directions_keyboard(),
        parse_mode="HTML",
        replace_photo=True,
    )
    await callback.answer()


@router.callback_query(F.data == "catalog_order")
async def open_catalog(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await send_section_message(
        callback.message,
        "list_of_directions.png",
        CATALOG_TEXT,
        reply_markup=catalog_directions_keyboard(),
        parse_mode="HTML",
        replace_photo=True,
    )
    await callback.answer()


@router.callback_query(
    F.data.in_(set(DIRECTION_TITLES))
)
async def select_direction(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """
    Сохраняет выбранное направление и переводит клиента
    к единому сообщению с условиями и необязательными дедлайном/файлами.
    """

    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    direction = DIRECTION_TITLES.get(
        callback.data
    )

    if direction is None:
        await callback.answer(
            "Не удалось определить направление.",
            show_alert=True,
        )
        return

    # Начинаем новое оформление заказа.
    await state.clear()
    await state.update_data(
        direction=direction,
        files=[],
    )

    direction_key = callback.data.removeprefix("direction_")
    description = DIRECTION_DESCRIPTIONS.get(direction_key)
    description_line = (
        f"Включает: {description}\n\n"
        if description else ""
    )

    await callback.message.answer(
        f"🧭 Вы выбрали направление: <b>{direction}</b>\n\n"
        f"{description_line}"
        "Теперь отправьте <b>одно сообщение</b> с условиями задания. "
        "При необходимости к нему можно добавить дедлайн и файлы.\n\n"
        "<b>Как оформить:</b>\n"
        "1. Опишите задачу и все требования.\n"
        "2. Если нужны файлы — прикрепите их к этому сообщению.\n"
        "3. Если нужен дедлайн — укажите его последней строкой строго так:\n"
        "<code>Дедлайн: ДД-ММ-ГГГГ</code>\n\n"
        "<b>Пример:</b>\n"
        "Решить задания 1–10, оформить с подробными решениями.\n"
        "Дедлайн: 15-09-2026",
        parse_mode="HTML",
    )

    await state.set_state(
        OrderState.waiting_for_order_details
    )

    await callback.answer()
