"""Мягкое удаление карточки исполнителя и подтверждения."""

import html
import logging

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from database.masters import (
    MASTER_STATUS_ACTIVE_ORDERS,
    MASTER_STATUS_DELETED,
    MASTER_STATUS_NOT_FOUND,
    MASTER_STATUS_SUCCESS,
    get_master_by_telegram_id_any,
    get_master_by_username,
    soft_delete_master,
)
from handlers.admin_master_profile._shared import (
    _deny_non_admin_callback,
    _display_value,
    _get_catalog_back_callback,
    build_master_management,
    is_admin,
    parse_username_argument,
    render_master_management_message,
    router,
)
from keyboards.admin_master_profile import (
    admin_master_delete_confirmation_keyboard,
)

logger = logging.getLogger("handlers.admin_master_profile")


async def _send_delete_confirmation(
    message: Message,
    master_telegram_id: int,
) -> None:
    master = await get_master_by_telegram_id_any(
        master_telegram_id
    )

    if master is None:
        await message.answer(
            "⚠️ Исполнитель не найден."
        )
        return

    if master["deleted_at"] is not None:
        await message.answer(
            "⚫ Этот исполнитель уже удалён."
        )
        return

    username = (
        f"@{html.escape(str(master['username']))}"
        if master["username"]
        else str(master_telegram_id)
    )

    await message.answer(
        "⚠️ <b>Удаление исполнителя</b>\n\n"
        f"Исполнитель: <b>{username}</b>\n"
        f"ФИО: <b>{_display_value(master['full_name'])}</b>\n\n"
        "Удаление сохраняет отзывы, завершённые заказы "
        "и исторические данные сохранятся, но исполнитель "
        "больше не сможет получать заказы или пользоваться "
        "рабочими функциями.\n\n"
        "Если у него есть активные заказы, удаление "
        "будет автоматически запрещено.",
        reply_markup=(
            admin_master_delete_confirmation_keyboard(
                master_telegram_id
            )
        ),
        parse_mode="HTML",
    )

@router.message(Command("delete_master"))
async def delete_master_command(
    message: Message,
) -> None:
    if (
        message.from_user is None
        or not is_admin(message.from_user.id)
    ):
        return

    username = parse_username_argument(
        message
    )

    if username is None:
        await message.answer(
            "Использование команды:\n\n"
            "<code>/delete_master @username</code>",
            parse_mode="HTML",
        )
        return

    master = await get_master_by_username(
        username
    )

    if master is None:
        await message.answer(
            "⚠️ Исполнитель не найден."
        )
        return

    await _send_delete_confirmation(
        message,
        int(master["telegram_id"]),
    )

@router.callback_query(
    F.data.startswith(
        "admin_master_delete_request:"
    )
)
async def request_delete_master(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    if (
        callback.message is None
        or callback.data is None
    ):
        await callback.answer()
        return

    try:
        master_telegram_id = int(
            callback.data.split(
                ":",
                maxsplit=1,
            )[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные.",
            show_alert=True,
        )
        return

    master = await get_master_by_telegram_id_any(
        master_telegram_id
    )

    if master is None:
        await callback.answer(
            "Исполнитель не найден.",
            show_alert=True,
        )
        return

    if master["deleted_at"] is not None:
        await callback.answer(
            "Исполнитель уже удалён.",
            show_alert=True,
        )
        return

    username = (
        f"@{html.escape(str(master['username']))}"
        if master["username"]
        else str(master_telegram_id)
    )

    await callback.message.edit_text(
        "⚠️ <b>Удалить исполнителя?</b>\n\n"
        f"Исполнитель: <b>{username}</b>\n"
        f"ФИО: <b>{_display_value(master['full_name'])}</b>\n\n"
        "Отзывы и история заказов сохранятся. "
        "Исполнитель будет исключён из рабочего контура.\n\n"
        "При наличии активных заказов удаление "
        "будет запрещено.",
        reply_markup=(
            admin_master_delete_confirmation_keyboard(
                master_telegram_id
            )
        ),
        parse_mode="HTML",
    )

    await callback.answer()

@router.callback_query(
    F.data.startswith(
        "admin_master_delete_confirm:"
    )
)
async def confirm_delete_master(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    if (
        callback.message is None
        or callback.data is None
    ):
        await callback.answer()
        return

    try:
        master_telegram_id = int(
            callback.data.split(
                ":",
                maxsplit=1,
            )[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные.",
            show_alert=True,
        )
        return

    try:
        result, active_orders = (
            await soft_delete_master(
                master_telegram_id
            )
        )
    except Exception:
        logger.exception(
            "Ошибка удаления исполнителя %s",
            master_telegram_id,
        )
        await callback.answer(
            "Не удалось удалить исполнителя.",
            show_alert=True,
        )
        return

    if result == MASTER_STATUS_ACTIVE_ORDERS:
        await callback.answer(
            "⚠️ Удаление запрещено: "
            f"активных заказов — {active_orders}. "
            "Сначала завершите или переназначьте их.",
            show_alert=True,
        )
        return

    if result == MASTER_STATUS_DELETED:
        await callback.answer(
            "Исполнитель уже удалён.",
            show_alert=True,
        )
        return

    if result == MASTER_STATUS_NOT_FOUND:
        await callback.answer(
            "Исполнитель не найден.",
            show_alert=True,
        )
        return

    if result != MASTER_STATUS_SUCCESS:
        await callback.answer(
            "Исполнитель не был удалён.",
            show_alert=True,
        )
        return

    catalog_back_callback = (
        await _get_catalog_back_callback(
            state
        )
    )

    await render_master_management_message(
        callback.bot,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        master_telegram_id=master_telegram_id,
        catalog_back_callback=(
            catalog_back_callback
        ),
    )

    await callback.answer(
        "Исполнитель удалён."
    )

@router.callback_query(
    F.data.startswith(
        "admin_master_delete_cancel:"
    )
)
async def cancel_delete_master(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    if (
        callback.message is None
        or callback.data is None
    ):
        await callback.answer()
        return

    try:
        master_telegram_id = int(
            callback.data.split(
                ":",
                maxsplit=1,
            )[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные.",
            show_alert=True,
        )
        return

    # Если подтверждение было открыто из /edit_master,
    # возвращаем карточку в том же сообщении.
    catalog_back_callback = (
        await _get_catalog_back_callback(
            state
        )
    )

    payload = await build_master_management(
        master_telegram_id,
        catalog_back_callback=(
            catalog_back_callback
        ),
    )

    if payload is not None:
        text, keyboard = payload
        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    await callback.answer(
        "Удаление отменено."
    )
