from collections.abc import Collection

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from catalog.directions import DIRECTION_OPTIONS


def admin_master_directions_keyboard(
    selected_directions: Collection[str],
) -> InlineKeyboardMarkup:
    """
    Выбор направлений при полном заполнении карточки
    через /create_master.
    """

    selected = set(selected_directions)

    rows: list[list[InlineKeyboardButton]] = []

    for key, title, emoji in DIRECTION_OPTIONS:
        mark = "☑" if title in selected else "☐"

        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {title} {emoji}",
                    callback_data=f"admin_master_dir:{key}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Сохранить карточку",
                callback_data="admin_master_dirs_save",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def admin_master_manage_keyboard(
    master_telegram_id: int,
    *,
    is_active: bool,
    is_deleted: bool,
    catalog_back_callback: str | None = None,
) -> InlineKeyboardMarkup:
    """
    Основное меню /edit_master.

    Удалённую карточку оставляем только для просмотра:
    её нельзя редактировать или возвращать в работу.
    """

    if is_deleted:
        rows = [
            [
                InlineKeyboardButton(
                    text="⚫ Исполнитель удалён",
                    callback_data="admin_master_noop",
                )
            ]
        ]

        if catalog_back_callback:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="↩️ Назад к списку",
                        callback_data=catalog_back_callback,
                    )
                ]
            )

        return InlineKeyboardMarkup(
            inline_keyboard=rows
        )

    status_text = (
        "🔴 Сделать неактивным"
        if is_active
        else "🟢 Сделать активным"
    )

    rows = [
            [
                InlineKeyboardButton(
                    text="👤 Изменить ФИО",
                    callback_data=(
                        "admin_master_edit_field:"
                        f"{master_telegram_id}:full_name"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎓 Изменить университет",
                    callback_data=(
                        "admin_master_edit_field:"
                        f"{master_telegram_id}:university"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔢 Изменить код специальности",
                    callback_data=(
                        "admin_master_edit_field:"
                        f"{master_telegram_id}:specialty_code"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📚 Изменить специальность",
                    callback_data=(
                        "admin_master_edit_field:"
                        f"{master_telegram_id}:specialty_name"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧭 Изменить направления",
                    callback_data=(
                        "admin_master_edit_directions:"
                        f"{master_telegram_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text=status_text,
                    callback_data=(
                        "admin_master_toggle_active:"
                        f"{master_telegram_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Заполнить карточку заново",
                    callback_data=(
                        "admin_master_refill:"
                        f"{master_telegram_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Удалить исполнителя",
                    callback_data=(
                        "admin_master_delete_request:"
                        f"{master_telegram_id}"
                    ),
                )
            ],
        ]

    if catalog_back_callback:
        rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Назад к списку",
                    callback_data=catalog_back_callback,
                )
            ]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def admin_master_edit_directions_keyboard(
    selected_directions: Collection[str],
) -> InlineKeyboardMarkup:
    selected = set(selected_directions)

    rows: list[list[InlineKeyboardButton]] = []

    for key, title, emoji in DIRECTION_OPTIONS:
        mark = "☑" if title in selected else "☐"

        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {title} {emoji}",
                    callback_data=(
                        f"admin_master_edit_dir:{key}"
                    ),
                )
            ]
        )

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="✅ Сохранить направления",
                    callback_data="admin_master_edit_dirs_save",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data="admin_master_edit_cancel",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def admin_master_refill_directions_keyboard(
    selected_directions: Collection[str],
) -> InlineKeyboardMarkup:
    selected = set(selected_directions)

    rows: list[list[InlineKeyboardButton]] = []

    for key, title, emoji in DIRECTION_OPTIONS:
        mark = "☑" if title in selected else "☐"

        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {title} {emoji}",
                    callback_data=(
                        f"admin_master_refill_dir:{key}"
                    ),
                )
            ]
        )

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="✅ Сохранить карточку",
                    callback_data="admin_master_refill_dirs_save",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data="admin_master_refill_cancel",
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def admin_master_keep_value_keyboard(
    field_name: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➡️ Оставить без изменений",
                    callback_data=(
                        f"admin_master_refill_keep:{field_name}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data="admin_master_refill_cancel",
                )
            ],
        ]
    )


def admin_master_delete_confirmation_keyboard(
    master_telegram_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить",
                    callback_data=(
                        "admin_master_delete_confirm:"
                        f"{master_telegram_id}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data=(
                        "admin_master_delete_cancel:"
                        f"{master_telegram_id}"
                    ),
                )
            ],
        ]
    )


def admin_master_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Отмена",
                    callback_data="admin_master_edit_cancel",
                )
            ]
        ]
    )

def admin_masters_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Главное меню административного каталога.

    Кнопок здесь намеренно мало: каталог исполнителей и направления
    открываются отдельными страницами, чтобы не раздувать одну клавиатуру.
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Все исполнители",
                    callback_data="adm_m_all:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧭 Фильтр по направлению",
                    callback_data="adm_m_dirs",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Активные",
                    callback_data="adm_m_status:a:0",
                ),
                InlineKeyboardButton(
                    text="🔴 Неактивные",
                    callback_data="adm_m_status:i:0",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚫ Удалённые",
                    callback_data="adm_m_status:d:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔎 Найти исполнителя",
                    callback_data="adm_m_search",
                )
            ],
        ]
    )


def admin_masters_directions_keyboard() -> InlineKeyboardMarkup:
    """
    Фиксированный каталог направлений.

    Сейчас направлений 15 — это существенно ниже лимита Telegram
    на inline-клавиатуру, поэтому отдельная пагинация направлений
    не нужна. Списки самих исполнителей пагинируются отдельно.
    """

    rows: list[list[InlineKeyboardButton]] = []

    for key, title, emoji in DIRECTION_OPTIONS:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{title} {emoji}",
                    callback_data=f"adm_m_dir:{key}:0",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="↩️ К управлению исполнителями",
                callback_data="adm_m_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def _admin_master_list_button_text(
    master,
) -> str:
    if master["deleted_at"] is not None:
        icon = "⚫"
    elif bool(int(master["is_active"])):
        icon = "🟢"
    else:
        icon = "🔴"

    full_name = str(
        master["full_name"]
        or "Без имени"
    ).strip()

    username = (
        f"@{master['username']}"
        if master["username"]
        else None
    )

    if username:
        text = f"{icon} {full_name} · {username}"
    else:
        text = f"{icon} {full_name}"

    # Очень длинные имена плохо выглядят внутри inline-кнопки.
    # Callback при этом содержит только ID и короткий фильтр.
    if len(text) > 58:
        text = text[:57] + "…"

    return text


def admin_masters_list_keyboard(
    masters,
    *,
    mode: str,
    argument: str,
    current_page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """
    Одна страница списка исполнителей.

    В handler используется 6 исполнителей на страницу.
    Даже вместе с навигацией клавиатура остаётся небольшой.
    """

    rows: list[list[InlineKeyboardButton]] = []

    for master in masters:
        master_id = int(
            master["telegram_id"]
        )

        rows.append(
            [
                InlineKeyboardButton(
                    text=_admin_master_list_button_text(
                        master
                    ),
                    callback_data=(
                        f"adm_m_open:{master_id}:"
                        f"{mode}:{argument}:{current_page}"
                    ),
                )
            ]
        )

    if total_pages > 1:
        pagination: list[InlineKeyboardButton] = []

        if current_page > 0:
            if mode == "all":
                previous_callback = (
                    f"adm_m_all:{current_page - 1}"
                )
            elif mode == "status":
                previous_callback = (
                    f"adm_m_status:{argument}:"
                    f"{current_page - 1}"
                )
            else:
                previous_callback = (
                    f"adm_m_dir:{argument}:"
                    f"{current_page - 1}"
                )

            pagination.append(
                InlineKeyboardButton(
                    text="⬅️",
                    callback_data=previous_callback,
                )
            )

        pagination.append(
            InlineKeyboardButton(
                text=(
                    f"{current_page + 1} / "
                    f"{total_pages}"
                ),
                callback_data="adm_m_page_info",
            )
        )

        if current_page + 1 < total_pages:
            if mode == "all":
                next_callback = (
                    f"adm_m_all:{current_page + 1}"
                )
            elif mode == "status":
                next_callback = (
                    f"adm_m_status:{argument}:"
                    f"{current_page + 1}"
                )
            else:
                next_callback = (
                    f"adm_m_dir:{argument}:"
                    f"{current_page + 1}"
                )

            pagination.append(
                InlineKeyboardButton(
                    text="➡️",
                    callback_data=next_callback,
                )
            )

        rows.append(
            pagination
        )

    if mode == "direction":
        rows.append(
            [
                InlineKeyboardButton(
                    text="🧭 Другое направление",
                    callback_data="adm_m_dirs",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="↩️ К управлению исполнителями",
                callback_data="adm_m_menu",
            )
        ]
    )

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


def admin_masters_search_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ К управлению исполнителями",
                    callback_data="adm_m_menu",
                )
            ]
        ]
    )

