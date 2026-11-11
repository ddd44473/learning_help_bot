"""Повторное заполнение существующей карточки исполнителя."""

import logging

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from catalog.directions import DIRECTION_BY_KEY
from database.masters import (
    get_master_by_telegram_id_any,
    get_master_directions,
    update_master_profile_with_directions,
)
from handlers.admin_master_profile._shared import (
    REFILL_NEXT_FIELD,
    REFILL_STATE_BY_FIELD,
    _deny_non_admin_callback,
    _field_prompt,
    _get_catalog_back_callback,
    _restore_catalog_context,
    _validate_profile_value,
    is_admin,
    render_master_management_message,
    router,
)
from keyboards.admin_master_profile import (
    admin_master_keep_value_keyboard,
    admin_master_refill_directions_keyboard,
)
from states.admin_master_profile import (
    AdminMasterProfileState,
)

logger = logging.getLogger("handlers.admin_master_profile")


async def _render_refill_prompt(
    bot: Bot,
    state: FSMContext,
    field_name: str,
) -> None:
    data = await state.get_data()

    chat_id = data.get(
        "management_chat_id"
    )
    message_id = data.get(
        "management_message_id"
    )

    if (
        chat_id is None
        or message_id is None
    ):
        raise RuntimeError(
            "Не найдено сообщение редактора."
        )

    await bot.edit_message_text(
        chat_id=int(chat_id),
        message_id=int(message_id),
        text=(
            "🔄 <b>Повторное заполнение карточки</b>\n\n"
            + _field_prompt(
                field_name,
                data.get(field_name),
                refill=True,
            )
        ),
        reply_markup=(
            admin_master_keep_value_keyboard(
                field_name
            )
        ),
        parse_mode="HTML",
    )

async def _go_to_refill_next(
    bot: Bot,
    state: FSMContext,
    field_name: str,
) -> None:
    next_field = REFILL_NEXT_FIELD[
        field_name
    ]

    if next_field is None:
        await state.set_state(
            AdminMasterProfileState
            .refill_directions
        )

        data = await state.get_data()
        selected = set(
            data.get(
                "selected_directions",
                [],
            )
        )

        await bot.edit_message_text(
            chat_id=int(
                data["management_chat_id"]
            ),
            message_id=int(
                data["management_message_id"]
            ),
            text=(
                "🔄 <b>Повторное заполнение карточки</b>\n\n"
                "🧭 Выберите направления исполнителя. "
                "После этого нажмите "
                "«✅ Сохранить карточку»."
            ),
            reply_markup=(
                admin_master_refill_directions_keyboard(
                    selected
                )
            ),
            parse_mode="HTML",
        )
        return

    await state.set_state(
        REFILL_STATE_BY_FIELD[
            next_field
        ]
    )

    await _render_refill_prompt(
        bot,
        state,
        next_field,
    )

@router.callback_query(
    F.data.startswith("admin_master_refill:")
)
async def start_master_refill(
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
            "Удалённую карточку заполнять нельзя.",
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
        .refill_full_name
    )
    await state.set_data(
        {
            "catalog_back_callback": (
                catalog_back_callback
            ),
            "master_telegram_id": (
                master_telegram_id
            ),
            "master_username": (
                master["username"]
            ),
            "full_name": (
                master["full_name"]
            ),
            "university": (
                master["university"]
            ),
            "specialty_code": (
                master["specialty_code"]
            ),
            "specialty_name": (
                master["specialty_name"]
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

    await _render_refill_prompt(
        callback.bot,
        state,
        "full_name",
    )

    await callback.answer()

async def _process_refill_field_message(
    message: Message,
    state: FSMContext,
    *,
    field_name: str,
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

    if (
        message.text.strip().lower()
        == "/cancel"
    ):
        master_id = data.get(
            "master_telegram_id"
        )
        catalog_back_callback = data.get(
            "catalog_back_callback"
        )
        chat_id = data.get(
            "management_chat_id"
        )
        message_id = data.get(
            "management_message_id"
        )

        await state.clear()

        if (
            master_id is not None
            and chat_id is not None
            and message_id is not None
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
            "Повторное заполнение отменено."
        )
        return

    valid, value, error = _validate_profile_value(
        field_name,
        message.text,
    )

    if not valid:
        await message.answer(
            error or "Некорректное значение."
        )
        return

    await state.update_data(
        **{field_name: value}
    )

    await _go_to_refill_next(
        message.bot,
        state,
        field_name,
    )

@router.message(
    AdminMasterProfileState.refill_full_name
)
async def refill_full_name_message(
    message: Message,
    state: FSMContext,
) -> None:
    await _process_refill_field_message(
        message,
        state,
        field_name="full_name",
    )

@router.message(
    AdminMasterProfileState.refill_university
)
async def refill_university_message(
    message: Message,
    state: FSMContext,
) -> None:
    await _process_refill_field_message(
        message,
        state,
        field_name="university",
    )

@router.message(
    AdminMasterProfileState.refill_specialty_code
)
async def refill_specialty_code_message(
    message: Message,
    state: FSMContext,
) -> None:
    await _process_refill_field_message(
        message,
        state,
        field_name="specialty_code",
    )

@router.message(
    AdminMasterProfileState.refill_specialty_name
)
async def refill_specialty_name_message(
    message: Message,
    state: FSMContext,
) -> None:
    await _process_refill_field_message(
        message,
        state,
        field_name="specialty_name",
    )

@router.callback_query(
    F.data.startswith(
        "admin_master_refill_keep:"
    )
)
async def keep_refill_value(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    if callback.data is None:
        await callback.answer()
        return

    field_name = callback.data.split(
        ":",
        maxsplit=1,
    )[1]

    if field_name not in REFILL_STATE_BY_FIELD:
        await callback.answer(
            "Некорректное поле.",
            show_alert=True,
        )
        return

    current_state = await state.get_state()

    if (
        current_state
        != REFILL_STATE_BY_FIELD[
            field_name
        ].state
    ):
        await callback.answer(
            "Этот шаг уже неактуален.",
            show_alert=True,
        )
        return

    await _go_to_refill_next(
        callback.bot,
        state,
        field_name,
    )

    await callback.answer(
        "Значение оставлено без изменений."
    )

@router.callback_query(
    AdminMasterProfileState.refill_directions,
    F.data.startswith(
        "admin_master_refill_dir:"
    ),
)
async def toggle_refill_direction(
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

    direction_key = callback.data.split(
        ":",
        maxsplit=1,
    )[1]

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
            admin_master_refill_directions_keyboard(
                selected
            )
        )
    )

    await callback.answer(
        result_text
    )

@router.callback_query(
    AdminMasterProfileState.refill_directions,
    F.data == "admin_master_refill_dirs_save",
)
async def save_refilled_master_profile(
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

    required = {
        "master_telegram_id",
        "full_name",
        "university",
        "specialty_code",
        "specialty_name",
        "selected_directions",
    }

    if not required.issubset(
        data.keys()
    ):
        await state.clear()
        await callback.answer(
            "Данные заполнения потеряны.",
            show_alert=True,
        )
        return

    try:
        updated = (
            await update_master_profile_with_directions(
                telegram_id=int(
                    data["master_telegram_id"]
                ),
                full_name=str(
                    data["full_name"]
                ),
                university=data["university"],
                specialty_code=data[
                    "specialty_code"
                ],
                specialty_name=data[
                    "specialty_name"
                ],
                directions=set(
                    data["selected_directions"]
                ),
            )
        )
    except Exception:
        logger.exception(
            "Ошибка полного повторного "
            "заполнения исполнителя %s",
            data["master_telegram_id"],
        )
        await callback.answer(
            "Не удалось сохранить карточку.",
            show_alert=True,
        )
        return

    master_id = int(
        data["master_telegram_id"]
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
        master_telegram_id=master_id,
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
        "Карточка заполнена заново."
    )

@router.message(
    AdminMasterProfileState.refill_directions
)
async def refill_directions_text_fallback(
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
        master_id = data.get(
            "master_telegram_id"
        )
        catalog_back_callback = data.get(
            "catalog_back_callback"
        )
        chat_id = data.get(
            "management_chat_id"
        )
        message_id = data.get(
            "management_message_id"
        )

        await state.clear()

        if (
            master_id is not None
            and chat_id is not None
            and message_id is not None
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
            "Повторное заполнение отменено."
        )
        return

    await message.answer(
        "Выберите направления кнопками "
        "или отправьте /cancel."
    )

@router.callback_query(
    F.data == "admin_master_refill_cancel"
)
async def cancel_refill_callback(
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
    catalog_back_callback = data.get(
        "catalog_back_callback"
    )

    await state.clear()

    if (
        callback.message is not None
        and master_id is not None
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

    await _restore_catalog_context(
        state,
        (
            str(catalog_back_callback)
            if catalog_back_callback
            else None
        ),
    )

    await callback.answer(
        "Повторное заполнение отменено."
    )
