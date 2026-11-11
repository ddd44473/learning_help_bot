"""Отмена сценариев редактирования/заполнения и no-op callback."""

import logging

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from handlers.admin_master_profile._shared import (
    _deny_non_admin_callback,
    _restore_catalog_context,
    render_master_management_message,
    router,
)

logger = logging.getLogger("handlers.admin_master_profile")


@router.callback_query(
    F.data == "admin_master_edit_cancel"
)
async def cancel_edit_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    data = await state.get_data()

    master_id = data.get(
        "master_telegram_id"
    )
    management_message_id = data.get(
        "management_message_id"
    )
    catalog_back_callback = data.get(
        "catalog_back_callback"
    )

    await state.clear()

    if (
        callback.message is not None
        and master_id is not None
        and management_message_id is not None
        and callback.message.message_id
        == int(management_message_id)
    ):
        await render_master_management_message(
            callback.bot,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            master_telegram_id=int(master_id),
            catalog_back_callback=(
                str(catalog_back_callback)
                if catalog_back_callback
                else None
            ),
        )
    elif callback.message is not None:
        try:
            await callback.message.edit_text(
                "Редактирование отменено."
            )
        except TelegramBadRequest:
            pass

    await _restore_catalog_context(
        state,
        (
            str(catalog_back_callback)
            if catalog_back_callback
            else None
        ),
    )

    await callback.answer(
        "Редактирование отменено."
    )

@router.callback_query(
    F.data == "admin_master_noop"
)
async def admin_master_noop(
    callback: CallbackQuery,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    await callback.answer(
        "Карточка исполнителя сохранена только для истории."
    )
