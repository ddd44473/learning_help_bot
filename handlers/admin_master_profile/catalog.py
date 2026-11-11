"""Административный каталог исполнителей (/masters) и поиск."""

import logging

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from database.masters import (
    get_admin_masters_stats,
    get_master_by_telegram_id_any,
    get_master_by_username,
)
from handlers.admin_master_profile._shared import (
    _catalog_back_callback,
    _deny_non_admin_callback,
    _edit_catalog_message,
    _render_masters_menu,
    _render_masters_page,
    build_master_management,
    is_admin,
    router,
)
from keyboards.admin_master_profile import (
    admin_masters_directions_keyboard,
    admin_masters_menu_keyboard,
    admin_masters_search_cancel_keyboard,
)
from states.admin_master_profile import (
    AdminMasterProfileState,
)

logger = logging.getLogger("handlers.admin_master_profile")


@router.message(Command("masters"))
async def masters_catalog_command(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Открывает каталог всех исполнителей.

    Команда доступна только администраторам.
    Списки исполнителей всегда пагинируются по 6 человек.
    """

    if (
        message.from_user is None
        or not is_admin(message.from_user.id)
    ):
        return

    stats = await get_admin_masters_stats()

    await state.clear()

    await message.answer(
        "👥 <b>Управление исполнителями</b>\n\n"
        f"Всего: <b>{stats['total']}</b>\n"
        f"🟢 Активных: <b>{stats['active']}</b>\n"
        f"🔴 Неактивных: <b>{stats['inactive']}</b>\n"
        f"⚫ Удалённых: <b>{stats['deleted']}</b>\n\n"
        "Выберите способ просмотра.",
        reply_markup=(
            admin_masters_menu_keyboard()
        ),
        parse_mode="HTML",
    )

@router.callback_query(
    F.data == "adm_m_menu"
)
async def masters_catalog_menu(
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

    await _render_masters_menu(
        callback,
        state,
    )

    await callback.answer()

@router.callback_query(
    F.data.startswith("adm_m_all:")
)
async def masters_catalog_all(
    callback: CallbackQuery,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    if callback.data is None:
        await callback.answer()
        return

    try:
        page = int(
            callback.data.split(
                ":",
                maxsplit=1,
            )[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректная страница.",
            show_alert=True,
        )
        return

    await _render_masters_page(
        callback,
        mode="all",
        argument="x",
        page=page,
    )

    await callback.answer()

@router.callback_query(
    F.data == "adm_m_dirs"
)
async def masters_catalog_directions(
    callback: CallbackQuery,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    if callback.message is None:
        await callback.answer()
        return

    await _edit_catalog_message(
        callback,
        text=(
            "🧭 <b>Фильтр по направлению</b>\n\n"
            "Выберите направление. "
            "После выбора будут показаны все "
            "исполнители этого направления — "
            "активные, неактивные и удалённые."
        ),
        reply_markup=(
            admin_masters_directions_keyboard()
        ),
    )

    await callback.answer()

@router.callback_query(
    F.data.startswith("adm_m_dir:")
)
async def masters_catalog_by_direction(
    callback: CallbackQuery,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    if callback.data is None:
        await callback.answer()
        return

    try:
        _, direction_key, page_raw = (
            callback.data.split(
                ":",
                maxsplit=2,
            )
        )
        page = int(page_raw)
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные направления.",
            show_alert=True,
        )
        return

    await _render_masters_page(
        callback,
        mode="direction",
        argument=direction_key,
        page=page,
    )

    await callback.answer()

@router.callback_query(
    F.data.startswith("adm_m_status:")
)
async def masters_catalog_by_status(
    callback: CallbackQuery,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    if callback.data is None:
        await callback.answer()
        return

    try:
        _, status_key, page_raw = (
            callback.data.split(
                ":",
                maxsplit=2,
            )
        )
        page = int(page_raw)
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные статуса.",
            show_alert=True,
        )
        return

    await _render_masters_page(
        callback,
        mode="status",
        argument=status_key,
        page=page,
    )

    await callback.answer()

@router.callback_query(
    F.data.startswith("adm_m_open:")
)
async def masters_catalog_open_master(
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
        (
            _,
            master_id_raw,
            mode,
            argument,
            page_raw,
        ) = callback.data.split(
            ":",
            maxsplit=4,
        )

        master_telegram_id = int(
            master_id_raw
        )
        page = int(
            page_raw
        )

    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные исполнителя.",
            show_alert=True,
        )
        return

    if mode not in {
        "all",
        "status",
        "direction",
    }:
        await callback.answer(
            "Неизвестный источник списка.",
            show_alert=True,
        )
        return

    back_callback = _catalog_back_callback(
        mode=mode,
        argument=argument,
        page=page,
    )

    await state.clear()
    await state.update_data(
        catalog_back_callback=(
            back_callback
        )
    )

    payload = await build_master_management(
        master_telegram_id,
        catalog_back_callback=(
            back_callback
        ),
    )

    if payload is None:
        await callback.answer(
            "Исполнитель больше не найден.",
            show_alert=True,
        )
        return

    text, keyboard = payload

    await _edit_catalog_message(
        callback,
        text=text,
        reply_markup=keyboard,
    )

    await callback.answer()

@router.callback_query(
    F.data == "adm_m_search"
)
async def masters_catalog_search_start(
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

    await state.clear()
    await state.set_state(
        AdminMasterProfileState
        .searching_master
    )

    await callback.message.edit_text(
        "🔎 <b>Поиск исполнителя</b>\n\n"
        "Отправьте:\n"
        "• <code>@username</code> исполнителя;\n"
        "• либо его Telegram ID.\n\n"
        "Поиск работает в том числе по "
        "неактивным и удалённым исполнителям.",
        reply_markup=(
            admin_masters_search_cancel_keyboard()
        ),
        parse_mode="HTML",
    )

    await callback.answer()

@router.message(
    AdminMasterProfileState.searching_master
)
async def masters_catalog_search_process(
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
            "Отправьте Telegram username "
            "или Telegram ID текстом."
        )
        return

    query = message.text.strip()

    if query.lower() == "/cancel":
        await state.clear()

        stats = await get_admin_masters_stats()

        await message.answer(
            "👥 <b>Управление исполнителями</b>\n\n"
            f"Всего: <b>{stats['total']}</b>\n"
            f"🟢 Активных: <b>{stats['active']}</b>\n"
            f"🔴 Неактивных: <b>{stats['inactive']}</b>\n"
            f"⚫ Удалённых: <b>{stats['deleted']}</b>",
            reply_markup=(
                admin_masters_menu_keyboard()
            ),
            parse_mode="HTML",
        )
        return

    master = None

    normalized_id = query.lstrip("+")

    if normalized_id.isdigit():
        try:
            master = (
                await get_master_by_telegram_id_any(
                    int(normalized_id)
                )
            )
        except ValueError:
            master = None

    if master is None:
        master = await get_master_by_username(
            query
        )

    if master is None:
        await message.answer(
            "⚠️ Исполнитель не найден.\n\n"
            "Проверьте @username или Telegram ID "
            "и попробуйте ещё раз."
        )
        return

    master_id = int(
        master["telegram_id"]
    )

    await state.clear()
    await state.update_data(
        catalog_back_callback="adm_m_menu"
    )

    payload = await build_master_management(
        master_id,
        catalog_back_callback="adm_m_menu",
    )

    if payload is None:
        await message.answer(
            "⚠️ Не удалось открыть карточку."
        )
        return

    text, keyboard = payload

    await message.answer(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )

@router.callback_query(
    F.data == "adm_m_page_info"
)
async def masters_catalog_page_info(
    callback: CallbackQuery,
) -> None:
    if await _deny_non_admin_callback(
        callback
    ):
        return

    await callback.answer(
        "Текущая страница списка."
    )
