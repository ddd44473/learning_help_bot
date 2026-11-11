"""Редактирование полей, направлений и статуса карточки (/edit_master)."""

import logging

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from catalog.directions import DIRECTION_BY_KEY
from database.masters import (
    MASTER_STATUS_ACTIVE_ORDERS,
    MASTER_STATUS_DELETED,
    MASTER_STATUS_NOT_FOUND,
    MASTER_STATUS_NO_DIRECTIONS,
    MASTER_STATUS_SUCCESS,
    MASTER_STATUS_UNCHANGED,
    change_master_active_status,
    get_master_by_telegram_id_any,
    get_master_by_username,
    get_master_directions,
    replace_master_directions,
    update_master_field,
)
from handlers.admin_master_profile._shared import (
    EDITABLE_FIELDS,
    _deny_non_admin_callback,
    _display_value,
    _field_prompt,
    _get_catalog_back_callback,
    _restore_catalog_context,
    _validate_profile_value,
    build_master_management,
    is_admin,
    parse_username_argument,
    render_master_management_message,
    router,
)
from keyboards.admin_master_profile import (
    admin_master_cancel_keyboard,
    admin_master_edit_directions_keyboard,
)
from states.admin_master_profile import (
    AdminMasterProfileState,
)

logger = logging.getLogger("handlers.admin_master_profile")


@router.message(Command("edit_master"))
async def edit_master_command(
    message: Message,
    state: FSMContext,
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
            "<code>/edit_master @username</code>",
            parse_mode="HTML",
        )
        return

    master = await get_master_by_username(
        username
    )

    if master is None:
        await message.answer(
            "⚠️ Исполнитель с таким Telegram username "
            "не найден."
        )
        return

    await state.clear()

    payload = await build_master_management(
        int(master["telegram_id"])
    )

    if payload is None:
        await message.answer(
            "⚠️ Не удалось загрузить карточку исполнителя."
        )
        return

    text, keyboard = payload

    await message.answer(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )

@router.callback_query(
    F.data.startswith("admin_master_edit_field:")
)
async def start_edit_master_field(
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
        _, master_id_raw, field_name = (
            callback.data.split(
                ":",
                maxsplit=2,
            )
        )
        master_telegram_id = int(
            master_id_raw
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные кнопки.",
            show_alert=True,
        )
        return

    if field_name not in EDITABLE_FIELDS:
        await callback.answer(
            "Это поле нельзя редактировать.",
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
            "Удалённую карточку редактировать нельзя.",
            show_alert=True,
        )
        return

    current_value = master[
        field_name
    ]

    catalog_back_callback = (
        await _get_catalog_back_callback(
            state
        )
    )

    await state.clear()
    await state.set_state(
        AdminMasterProfileState
        .editing_field_value
    )
    await state.set_data(
        {
            "catalog_back_callback": (
                catalog_back_callback
            ),
            "master_telegram_id": (
                master_telegram_id
            ),
            "edit_field": field_name,
            "old_value": current_value,
            "management_chat_id": (
                callback.message.chat.id
            ),
            "management_message_id": (
                callback.message.message_id
            ),
        }
    )

    await callback.message.answer(
        _field_prompt(
            field_name,
            current_value,
            refill=False,
        ),
        reply_markup=admin_master_cancel_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()

@router.message(
    AdminMasterProfileState.editing_field_value
)
async def process_edit_master_field(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        message.from_user is None
        or not is_admin(message.from_user.id)
    ):
        return

    if not message.text:
        await message.answer(
            "Новое значение нужно отправить текстом."
        )
        return

    data = await state.get_data()

    master_telegram_id = data.get(
        "master_telegram_id"
    )
    field_name = data.get(
        "edit_field"
    )

    if (
        master_telegram_id is None
        or field_name not in EDITABLE_FIELDS
    ):
        await state.clear()
        await message.answer(
            "Данные редактора потеряны. "
            "Откройте /edit_master заново."
        )
        return

    if (
        message.text.strip().lower()
        == "/cancel"
    ):
        await state.clear()
        await message.answer(
            "Редактирование поля отменено."
        )
        return

    valid, value, error = _validate_profile_value(
        str(field_name),
        message.text,
    )

    if not valid:
        await message.answer(
            error or "Некорректное значение."
        )
        return

    try:
        updated = await update_master_field(
            telegram_id=int(
                master_telegram_id
            ),
            field_name=str(
                field_name
            ),
            value=value,
        )
    except Exception:
        logger.exception(
            "Ошибка изменения поля %s "
            "исполнителя %s",
            field_name,
            master_telegram_id,
        )
        await message.answer(
            "⚠️ Не удалось сохранить изменение."
        )
        return

    if not updated:
        await state.clear()
        await message.answer(
            "⚠️ Исполнитель не найден или уже удалён."
        )
        return

    old_value = data.get(
        "old_value"
    )

    management_chat_id = data.get(
        "management_chat_id"
    )
    management_message_id = data.get(
        "management_message_id"
    )

    catalog_back_callback = data.get(
        "catalog_back_callback"
    )

    await state.clear()

    if (
        management_chat_id is not None
        and management_message_id is not None
    ):
        try:
            await render_master_management_message(
                message.bot,
                chat_id=int(
                    management_chat_id
                ),
                message_id=int(
                    management_message_id
                ),
                master_telegram_id=int(
                    master_telegram_id
                ),
                catalog_back_callback=(
                    str(catalog_back_callback)
                    if catalog_back_callback
                    else None
                ),
            )
        except Exception:
            logger.exception(
                "Не удалось обновить карточку "
                "управления исполнителем %s",
                master_telegram_id,
            )

    await _restore_catalog_context(
        state,
        (
            str(catalog_back_callback)
            if catalog_back_callback
            else None
        ),
    )

    await message.answer(
        "✅ <b>Поле обновлено</b>\n\n"
        f"{EDITABLE_FIELDS[str(field_name)]}\n"
        f"Было: <b>{_display_value(old_value)}</b>\n"
        f"Стало: <b>{_display_value(value)}</b>",
        parse_mode="HTML",
    )

@router.callback_query(
    F.data.startswith(
        "admin_master_edit_directions:"
    )
)
async def start_edit_master_directions(
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
            "Некорректные данные кнопки.",
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
            "Удалённую карточку редактировать нельзя.",
            show_alert=True,
        )
        return

    directions = await get_master_directions(
        master_telegram_id
    )

    catalog_back_callback = (
        await _get_catalog_back_callback(
            state
        )
    )

    await state.clear()
    await state.set_state(
        AdminMasterProfileState
        .editing_directions
    )
    await state.set_data(
        {
            "catalog_back_callback": (
                catalog_back_callback
            ),
            "master_telegram_id": (
                master_telegram_id
            ),
            "selected_directions": list(
                directions
            ),
            "management_chat_id": (
                callback.message.chat.id
            ),
            "management_message_id": (
                callback.message.message_id
            ),
        }
    )

    await callback.message.edit_text(
        "🧭 <b>Изменение направлений</b>\n\n"
        "Выберите направления исполнителя. "
        "Повторное нажатие снимает выбор.",
        reply_markup=(
            admin_master_edit_directions_keyboard(
                directions
            )
        ),
        parse_mode="HTML",
    )

    await callback.answer()

@router.callback_query(
    AdminMasterProfileState.editing_directions,
    F.data.startswith("admin_master_edit_dir:"),
)
async def toggle_edit_master_direction(
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
        direction_key = callback.data.split(
            ":",
            maxsplit=1,
        )[1]
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректное направление.",
            show_alert=True,
        )
        return

    direction = DIRECTION_BY_KEY.get(
        direction_key
    )

    if direction is None:
        await callback.answer(
            "Неизвестное направление.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    selected = set(
        data.get(
            "selected_directions",
            [],
        )
    )

    if direction in selected:
        selected.remove(direction)
        result_text = "Направление снято."
    else:
        selected.add(direction)
        result_text = "Направление добавлено."

    await state.update_data(
        selected_directions=list(selected)
    )

    await callback.message.edit_reply_markup(
        reply_markup=(
            admin_master_edit_directions_keyboard(
                selected
            )
        )
    )

    await callback.answer(
        result_text
    )

@router.callback_query(
    AdminMasterProfileState.editing_directions,
    F.data == "admin_master_edit_dirs_save",
)
async def save_edit_master_directions(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    if callback.message is None:
        await callback.answer()
        return

    data = await state.get_data()
    master_telegram_id = data.get(
        "master_telegram_id"
    )

    if master_telegram_id is None:
        await state.clear()
        await callback.answer(
            "Данные редактора потеряны.",
            show_alert=True,
        )
        return

    selected = set(
        data.get(
            "selected_directions",
            [],
        )
    )

    updated = await replace_master_directions(
        int(master_telegram_id),
        selected,
    )

    if not updated:
        await state.clear()
        await callback.answer(
            "Исполнитель не найден или уже удалён.",
            show_alert=True,
        )
        return

    catalog_back_callback = data.get(
        "catalog_back_callback"
    )

    await state.clear()

    await render_master_management_message(
        callback.bot,
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        master_telegram_id=int(
            master_telegram_id
        ),
        catalog_back_callback=(
            str(catalog_back_callback)
            if catalog_back_callback
            else None
        ),
    )

    await _restore_catalog_context(
        state,
        (
            str(catalog_back_callback)
            if catalog_back_callback
            else None
        ),
    )

    await callback.answer(
        "Направления сохранены."
    )

@router.message(
    AdminMasterProfileState.editing_directions
)
async def edit_directions_text_fallback(
    message: Message,
    state: FSMContext,
) -> None:
    if (
        message.from_user is None
        or not is_admin(message.from_user.id)
    ):
        return

    if (
        message.text
        and message.text.strip().lower()
        == "/cancel"
    ):
        data = await state.get_data()
        catalog_back_callback = data.get(
            "catalog_back_callback"
        )
        await state.clear()

        chat_id = data.get(
            "management_chat_id"
        )
        message_id = data.get(
            "management_message_id"
        )
        master_id = data.get(
            "master_telegram_id"
        )

        if (
            chat_id is not None
            and message_id is not None
            and master_id is not None
        ):
            await render_master_management_message(
                message.bot,
                chat_id=int(chat_id),
                message_id=int(message_id),
                master_telegram_id=int(master_id),
                catalog_back_callback=(
                    str(catalog_back_callback)
                    if catalog_back_callback
                    else None
                ),
            )

        await _restore_catalog_context(
            state,
            (
                str(catalog_back_callback)
                if catalog_back_callback
                else None
            ),
        )

        await message.answer(
            "Редактирование направлений отменено."
        )
        return

    await message.answer(
        "Измените направления кнопками "
        "в карточке или отправьте /cancel."
    )

@router.callback_query(
    F.data.startswith("admin_master_toggle_active:")
)
async def toggle_master_active_status(
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
            "Некорректные данные кнопки.",
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
            "Удалённого исполнителя нельзя активировать.",
            show_alert=True,
        )
        return

    make_active = not bool(
        int(master["is_active"])
    )

    try:
        result, active_orders = (
            await change_master_active_status(
                telegram_id=master_telegram_id,
                make_active=make_active,
            )
        )
    except Exception:
        logger.exception(
            "Не удалось изменить статус "
            "исполнителя %s",
            master_telegram_id,
        )
        await callback.answer(
            "Не удалось изменить статус.",
            show_alert=True,
        )
        return

    if result == MASTER_STATUS_ACTIVE_ORDERS:
        await callback.answer(
            "⚠️ Нельзя сделать исполнителя "
            f"неактивным: активных заказов — {active_orders}. "
            "Сначала завершите или переназначьте их.",
            show_alert=True,
        )
        return

    if result == MASTER_STATUS_NO_DIRECTIONS:
        await callback.answer(
            "⚠️ Нельзя активировать исполнителя "
            "без направлений. Сначала назначьте "
            "хотя бы одно направление.",
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

    if result not in {
        MASTER_STATUS_SUCCESS,
        MASTER_STATUS_UNCHANGED,
    }:
        await callback.answer(
            "Статус не был изменён.",
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
        (
            "Исполнитель активирован."
            if make_active
            else "Исполнитель временно отключён."
        )
    )
