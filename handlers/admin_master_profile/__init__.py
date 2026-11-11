"""Административное управление карточками исполнителей.

Совместимый entrypoint: `handlers.admin_master_profile.router` остаётся
единственным Router с тем же порядком observers, что и до разделения.
Публичные имена прежнего модуля реэкспортируются отсюда.

Внутренняя структура:
- setup     — создание карточки (/create_master);
- catalog   — каталог и поиск (/masters);
- edit      — редактирование полей/направлений/статуса (/edit_master);
- delete    — мягкое удаление;
- refill    — повторное заполнение;
- navigation— отмена сценариев и no-op;
- _shared   — единые admin-guards, константы, рендеринг и Router.
"""

# Compatibility surface: прежние внешние имена остаются доступны.
import html
import logging

from aiogram import F, Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from catalog.directions import (
    DIRECTION_BY_KEY,
    DIRECTION_OPTIONS,
)
from config import ADMIN_IDS
from database.masters import (
    MASTER_STATUS_ACTIVE_ORDERS,
    MASTER_STATUS_DELETED,
    MASTER_STATUS_NO_DIRECTIONS,
    MASTER_STATUS_NOT_FOUND,
    MASTER_STATUS_SUCCESS,
    MASTER_STATUS_UNCHANGED,
    ADMIN_MASTER_STATUS_ACTIVE,
    ADMIN_MASTER_STATUS_DELETED,
    ADMIN_MASTER_STATUS_INACTIVE,
    CREATE_MASTER_ALREADY_EXISTS,
    CREATE_MASTER_DELETED,
    CREATE_MASTER_SUCCESS,
    change_master_active_status,
    create_master_profile_with_directions,
    get_admin_masters,
    get_admin_masters_count,
    get_admin_masters_stats,
    get_master_by_telegram_id_any,
    get_master_by_username,
    get_master_directions,
    replace_master_directions,
    soft_delete_master,
    update_master_field,
    update_master_profile_with_directions,
)
from database.users import get_user_by_tg_username
from keyboards.admin_master_profile import (
    admin_master_cancel_keyboard,
    admin_master_delete_confirmation_keyboard,
    admin_master_directions_keyboard,
    admin_master_edit_directions_keyboard,
    admin_master_keep_value_keyboard,
    admin_master_manage_keyboard,
    admin_master_refill_directions_keyboard,
    admin_masters_directions_keyboard,
    admin_masters_list_keyboard,
    admin_masters_menu_keyboard,
    admin_masters_search_cancel_keyboard,
)
from states.admin_master_profile import (
    AdminMasterProfileState,
)

from handlers.admin_master_profile._shared import (
    ADMIN_MASTERS_PAGE_SIZE,
    ADMIN_MASTER_STATUS_BY_KEY,
    ADMIN_MASTER_STATUS_TITLE_BY_KEY,
    EDITABLE_FIELDS,
    REFILL_NEXT_FIELD,
    REFILL_STATE_BY_FIELD,
    _catalog_back_callback,
    _deny_non_admin_callback,
    _display_value,
    _edit_catalog_message,
    _field_prompt,
    _get_catalog_back_callback,
    _render_masters_menu,
    _render_masters_page,
    _restore_catalog_context,
    _status_title,
    _validate_profile_value,
    build_directions_summary,
    build_master_management,
    is_admin,
    logger,
    parse_username_argument,
    render_master_management_message,
    router,
)


# Порядок импорта = прежний порядок декораторов в едином модуле.
from handlers.admin_master_profile import setup as _setup  # noqa: E402,F401
from handlers.admin_master_profile import catalog as _catalog  # noqa: E402,F401
from handlers.admin_master_profile import edit as _edit  # noqa: E402,F401
from handlers.admin_master_profile import delete as _delete  # noqa: E402,F401
from handlers.admin_master_profile import refill as _refill  # noqa: E402,F401
from handlers.admin_master_profile import navigation as _navigation  # noqa: E402,F401

from handlers.admin_master_profile.setup import (
    _process_setup_field,
    directions_text_fallback,
    process_master_full_name,
    process_master_specialty_code,
    process_master_specialty_name,
    process_master_university,
    save_master_profile,
    start_master_profile_setup,
    toggle_master_direction,
)
from handlers.admin_master_profile.catalog import (
    masters_catalog_all,
    masters_catalog_by_direction,
    masters_catalog_by_status,
    masters_catalog_command,
    masters_catalog_directions,
    masters_catalog_menu,
    masters_catalog_open_master,
    masters_catalog_page_info,
    masters_catalog_search_process,
    masters_catalog_search_start,
)
from handlers.admin_master_profile.edit import (
    edit_directions_text_fallback,
    edit_master_command,
    process_edit_master_field,
    save_edit_master_directions,
    start_edit_master_directions,
    start_edit_master_field,
    toggle_edit_master_direction,
    toggle_master_active_status,
)
from handlers.admin_master_profile.delete import (
    _send_delete_confirmation,
    cancel_delete_master,
    confirm_delete_master,
    delete_master_command,
    request_delete_master,
)
from handlers.admin_master_profile.refill import (
    _go_to_refill_next,
    _process_refill_field_message,
    _render_refill_prompt,
    cancel_refill_callback,
    keep_refill_value,
    refill_directions_text_fallback,
    refill_full_name_message,
    refill_specialty_code_message,
    refill_specialty_name_message,
    refill_university_message,
    save_refilled_master_profile,
    start_master_refill,
    toggle_refill_direction,
)
from handlers.admin_master_profile.navigation import (
    admin_master_noop,
    cancel_edit_callback,
)
