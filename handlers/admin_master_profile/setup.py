"""Создание новой карточки исполнителя (/create_master) и её полей."""

import html
import logging

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from catalog.directions import DIRECTION_BY_KEY
from database.masters import (
    CREATE_MASTER_ALREADY_EXISTS,
    CREATE_MASTER_DELETED,
    CREATE_MASTER_SUCCESS,
    create_master_profile_with_directions,
    get_master_by_telegram_id_any,
    get_master_directions,
    update_master_profile_with_directions,
)
from database.users import get_user_by_tg_username
from handlers.admin_master_profile._shared import (
    _deny_non_admin_callback,
    _display_value,
    _field_prompt,
    _validate_profile_value,
    build_directions_summary,
    is_admin,
    parse_username_argument,
    router,
)
from keyboards.admin_master_profile import (
    admin_master_directions_keyboard,
)
from states.admin_master_profile import (
    AdminMasterProfileState,
)

logger = logging.getLogger("handlers.admin_master_profile")


@router.message(Command("create_master"))
async def start_master_profile_setup(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Создаёт нового исполнителя из уже известного боту пользователя.

    Telegram Bot API не позволяет получить Telegram ID произвольного
    человека только по @username, поэтому пользователь должен хотя бы
    один раз открыть бота и попасть в таблицу users.
    """

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
            "<code>/create_master @username</code>",
            parse_mode="HTML",
        )
        return

    user = await get_user_by_tg_username(
        username
    )

    if user is None:
        await message.answer(
            "⚠️ <b>Пользователь не найден в сервисе</b>\n\n"
            f"Telegram: <b>@{html.escape(username)}</b>\n\n"
            "Попросите пользователя сначала открыть бота "
            "и запустить его, после чего повторите:\n"
            f"<code>/create_master @{html.escape(username)}</code>",
            parse_mode="HTML",
        )
        return

    master_telegram_id = int(
        user["telegram_id"]
    )

    existing_master = (
        await get_master_by_telegram_id_any(
            master_telegram_id
        )
    )

    if existing_master is not None:
        if existing_master["deleted_at"] is not None:
            await message.answer(
                "⚫ Этот пользователь уже был удалён "
                "из исполнителей.\n\n"
                "Удалённая карточка сохраняется в истории "
                "и через /create_master не восстанавливается."
            )
            return

        await message.answer(
            "ℹ️ Этот пользователь уже является исполнителем.\n\n"
            "Для изменения карточки используйте:\n"
            f"<code>/edit_master @{html.escape(username)}</code>",
            parse_mode="HTML",
        )
        return

    telegram_username = str(
        user["tg_username"]
        or username
    ).strip().lstrip("@")

    telegram_full_name = str(
        user["tg_full_name"]
        or telegram_username
        or "Новый исполнитель"
    ).strip()

    await state.clear()

    await state.set_state(
        AdminMasterProfileState
        .waiting_for_full_name
    )

    await state.set_data(
        {
            "create_master_mode": True,
            "master_telegram_id": (
                master_telegram_id
            ),
            "master_username": (
                telegram_username
            ),
        }
    )

    await message.answer(
        "👨‍💻 <b>Создание нового исполнителя</b>\n\n"
        f"Telegram: "
        f"<b>@{html.escape(telegram_username)}</b>\n\n"
        "Сейчас заполним карточку исполнителя.\n\n"
        + _field_prompt(
            "full_name",
            telegram_full_name,
            refill=False,
        ),
        parse_mode="HTML",
    )

async def _process_setup_field(
    message: Message,
    state: FSMContext,
    *,
    field_name: str,
    next_state,
    next_field: str | None,
) -> None:
    if (
        message.from_user is None
        or not is_admin(message.from_user.id)
    ):
        return

    if not message.text:
        await message.answer(
            "Значение нужно отправить текстом."
        )
        return

    if (
        message.text.strip().lower()
        == "/cancel"
    ):
        await state.clear()
        await message.answer(
            "Заполнение карточки отменено."
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

    if next_state is not None:
        await state.set_state(
            next_state
        )

    if next_field is not None:
        data = await state.get_data()

        await message.answer(
            _field_prompt(
                next_field,
                data.get(next_field),
                refill=False,
            ),
            parse_mode="HTML",
        )

@router.message(
    AdminMasterProfileState.waiting_for_full_name
)
async def process_master_full_name(
    message: Message,
    state: FSMContext,
) -> None:
    await _process_setup_field(
        message,
        state,
        field_name="full_name",
        next_state=(
            AdminMasterProfileState
            .waiting_for_university
        ),
        next_field="university",
    )

@router.message(
    AdminMasterProfileState.waiting_for_university
)
async def process_master_university(
    message: Message,
    state: FSMContext,
) -> None:
    await _process_setup_field(
        message,
        state,
        field_name="university",
        next_state=(
            AdminMasterProfileState
            .waiting_for_specialty_code
        ),
        next_field="specialty_code",
    )

@router.message(
    AdminMasterProfileState.waiting_for_specialty_code
)
async def process_master_specialty_code(
    message: Message,
    state: FSMContext,
) -> None:
    await _process_setup_field(
        message,
        state,
        field_name="specialty_code",
        next_state=(
            AdminMasterProfileState
            .waiting_for_specialty_name
        ),
        next_field="specialty_name",
    )

@router.message(
    AdminMasterProfileState.waiting_for_specialty_name
)
async def process_master_specialty_name(
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
            "Название специальности нужно "
            "отправить текстом."
        )
        return

    if (
        message.text.strip().lower()
        == "/cancel"
    ):
        await state.clear()
        await message.answer(
            "Заполнение карточки отменено."
        )
        return

    valid, value, error = _validate_profile_value(
        "specialty_name",
        message.text,
    )

    if not valid:
        await message.answer(
            error or "Некорректное значение."
        )
        return

    await state.update_data(
        specialty_name=value
    )

    data = await state.get_data()
    master_telegram_id = data.get(
        "master_telegram_id"
    )

    if master_telegram_id is None:
        await state.clear()
        await message.answer(
            "Не удалось определить исполнителя. "
            "Начните заполнение заново."
        )
        return

    if bool(
        data.get("create_master_mode")
    ):
        existing_directions: list[str] = []
    else:
        existing_directions = (
            await get_master_directions(
                int(master_telegram_id)
            )
        )

    await state.update_data(
        selected_directions=list(
            existing_directions
        )
    )

    await state.set_state(
        AdminMasterProfileState
        .waiting_for_directions
    )

    await message.answer(
        "🧭 <b>Направления исполнителя</b>\n\n"
        "Выберите любое количество направлений. "
        "Повторное нажатие снимает выбор.\n\n"
        "Когда закончите, нажмите "
        "«✅ Сохранить карточку».",
        reply_markup=admin_master_directions_keyboard(
            existing_directions
        ),
        parse_mode="HTML",
    )

@router.callback_query(
    AdminMasterProfileState.waiting_for_directions,
    F.data.startswith("admin_master_dir:"),
)
async def toggle_master_direction(
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
        _, direction_key = (
            callback.data.split(
                ":",
                maxsplit=1,
            )
        )
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
        action_text = "Направление снято."
    else:
        selected.add(direction)
        action_text = "Направление добавлено."

    await state.update_data(
        selected_directions=list(selected)
    )

    await callback.message.edit_reply_markup(
        reply_markup=admin_master_directions_keyboard(
            selected
        )
    )

    await callback.answer(
        action_text
    )

@router.callback_query(
    AdminMasterProfileState.waiting_for_directions,
    F.data == "admin_master_dirs_save",
)
async def save_master_profile(
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

    required_fields = (
        "full_name",
        "university",
        "specialty_code",
        "specialty_name",
    )

    if (
        master_telegram_id is None
        or any(
            field not in data
            for field in required_fields
        )
    ):
        await state.clear()
        await callback.answer(
            "Данные карточки потеряны. "
            "Запустите команду заново.",
            show_alert=True,
        )
        return

    selected_directions = set(
        data.get(
            "selected_directions",
            [],
        )
    )

    # Новый исполнитель сразу должен быть полноценным и активным.
    # Поэтому пустой список направлений для /create_master не сохраняем.
    if (
        bool(data.get("create_master_mode"))
        and not selected_directions
    ):
        await callback.answer(
            "⚠️ Выберите хотя бы одно направление "
            "для нового исполнителя.",
            show_alert=True,
        )
        return

    username = str(
        data.get("master_username")
        or ""
    ).strip().lstrip("@")

    if bool(
        data.get("create_master_mode")
    ):
        try:
            result = (
                await create_master_profile_with_directions(
                    telegram_id=int(
                        master_telegram_id
                    ),
                    username=(
                        username
                        or None
                    ),
                    full_name=str(
                        data["full_name"]
                    ),
                    university=data[
                        "university"
                    ],
                    specialty_code=data[
                        "specialty_code"
                    ],
                    specialty_name=data[
                        "specialty_name"
                    ],
                    directions=(
                        selected_directions
                    ),
                )
            )

        except Exception:
            logger.exception(
                "Ошибка создания исполнителя %s",
                master_telegram_id,
            )
            await callback.answer(
                "⚠️ Не удалось создать исполнителя.",
                show_alert=True,
            )
            return

        if result == CREATE_MASTER_ALREADY_EXISTS:
            await state.clear()
            await callback.answer(
                "Этот пользователь уже стал исполнителем.",
                show_alert=True,
            )
            return

        if result == CREATE_MASTER_DELETED:
            await state.clear()
            await callback.answer(
                "Этот исполнитель был удалён и "
                "не может быть создан повторно.",
                show_alert=True,
            )
            return

        if result != CREATE_MASTER_SUCCESS:
            await callback.answer(
                "Исполнитель не был создан.",
                show_alert=True,
            )
            return

        await state.clear()

        try:
            await callback.message.edit_reply_markup(
                reply_markup=None
            )
        except TelegramBadRequest:
            pass

        username_text = (
            f"@{html.escape(username)}"
            if username
            else "Без username"
        )

        await callback.message.answer(
            "✅ <b>Исполнитель создан</b>\n\n"
            f"Telegram: <b>{username_text}</b>\n"
            f"Имя: "
            f"<b>{_display_value(data['full_name'])}</b>\n"
            f"Университет: "
            f"<b>{_display_value(data['university'])}</b>\n"
            f"Специальность: "
            f"<b>{_display_value(data['specialty_code'])} — "
            f"{_display_value(data['specialty_name'])}</b>\n\n"
            "🧭 <b>Направления:</b>\n"
            f"{build_directions_summary(selected_directions)}\n\n"
            "🟢 Статус: <b>Активен</b>",
            parse_mode="HTML",
        )

        await callback.answer(
            "Исполнитель создан."
        )
        return

    # Совместимый fallback для уже начатых старых FSM-сессий.
    try:
        updated = (
            await update_master_profile_with_directions(
                telegram_id=int(
                    master_telegram_id
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
                directions=selected_directions,
            )
        )

    except Exception:
        logger.exception(
            "Ошибка обновления карточки "
            "исполнителя %s",
            master_telegram_id,
        )
        await callback.answer(
            "⚠️ Не удалось сохранить карточку исполнителя.",
            show_alert=True,
        )
        return

    if not updated:
        await state.clear()
        await callback.answer(
            "⚠️ Исполнитель не найден или уже удалён.",
            show_alert=True,
        )
        return

    await state.clear()

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None
        )
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        "✅ <b>Карточка исполнителя сохранена</b>\n\n"
        f"Имя: <b>{_display_value(data['full_name'])}</b>\n"
        "🧭 <b>Направления:</b>\n"
        f"{build_directions_summary(selected_directions)}",
        parse_mode="HTML",
    )

    await callback.answer(
        "Карточка сохранена."
    )

@router.message(
    AdminMasterProfileState.waiting_for_directions
)
async def directions_text_fallback(
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
        await state.clear()
        await message.answer(
            "Заполнение карточки отменено."
        )
        return

    await message.answer(
        "Выберите направления кнопками "
        "в предыдущем сообщении или отправьте "
        "<code>/cancel</code> для отмены.",
        parse_mode="HTML",
    )
