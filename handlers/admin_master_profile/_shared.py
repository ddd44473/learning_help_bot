"""Общие константы, проверки и UI-хелперы административного профиля.

Пакет handlers.admin_master_profile разделён по workflow (setup, catalog,
edit, delete, refill, navigation). Этот модуль хранит единственный Router,
единые admin-проверки и рендеринг карточки, чтобы не дублировать guards.
"""

import html
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from catalog.directions import (
    DIRECTION_BY_KEY,
    DIRECTION_OPTIONS,
)
from config import ADMIN_IDS
from database.masters import (
    ADMIN_MASTER_STATUS_ACTIVE,
    ADMIN_MASTER_STATUS_DELETED,
    ADMIN_MASTER_STATUS_INACTIVE,
    get_admin_masters,
    get_admin_masters_count,
    get_admin_masters_stats,
    get_master_by_telegram_id_any,
    get_master_directions,
)
from keyboards.admin_master_profile import (
    admin_master_manage_keyboard,
    admin_masters_list_keyboard,
    admin_masters_menu_keyboard,
)
from states.admin_master_profile import (
    AdminMasterProfileState,
)


router = Router()
logger = logging.getLogger("handlers.admin_master_profile")


EDITABLE_FIELDS = {
    "full_name": "👤 ФИО",
    "university": "🎓 Университет",
    "specialty_code": "🔢 Код специальности",
    "specialty_name": "📚 Специальность",
}

REFILL_STATE_BY_FIELD = {
    "full_name": AdminMasterProfileState.refill_full_name,
    "university": AdminMasterProfileState.refill_university,
    "specialty_code": AdminMasterProfileState.refill_specialty_code,
    "specialty_name": AdminMasterProfileState.refill_specialty_name,
}

REFILL_NEXT_FIELD = {
    "full_name": "university",
    "university": "specialty_code",
    "specialty_code": "specialty_name",
    "specialty_name": None,
}


ADMIN_MASTERS_PAGE_SIZE = 6

ADMIN_MASTER_STATUS_BY_KEY = {
    "a": ADMIN_MASTER_STATUS_ACTIVE,
    "i": ADMIN_MASTER_STATUS_INACTIVE,
    "d": ADMIN_MASTER_STATUS_DELETED,
}

ADMIN_MASTER_STATUS_TITLE_BY_KEY = {
    "a": "🟢 Активные",
    "i": "🔴 Неактивные",
    "d": "⚫ Удалённые",
}


def is_admin(
    telegram_id: int,
) -> bool:
    return telegram_id in ADMIN_IDS

def parse_username_argument(
    message: Message,
) -> str | None:
    if not message.text:
        return None

    parts = message.text.split(
        maxsplit=1,
    )

    if len(parts) != 2:
        return None

    username = (
        parts[1]
        .strip()
        .lstrip("@")
    )

    if not username:
        return None

    return username

def build_directions_summary(
    selected_directions: set[str] | list[str],
) -> str:
    selected = set(
        selected_directions
    )

    ordered = [
        f"• {html.escape(title)} {emoji}"
        for _, title, emoji in DIRECTION_OPTIONS
        if title in selected
    ]

    if not ordered:
        return "• Не назначены"

    return "\n".join(
        ordered
    )

def _status_title(
    master,
) -> str:
    if master["deleted_at"] is not None:
        return "⚫ Удалён"

    if bool(int(master["is_active"])):
        return "🟢 Активен"

    return "🔴 Неактивен"

def _display_value(
    value: object,
) -> str:
    if value is None:
        return "Не указано"

    normalized = str(value).strip()

    if not normalized:
        return "Не указано"

    return html.escape(normalized)

async def build_master_management(
    master_telegram_id: int,
    *,
    catalog_back_callback: str | None = None,
) -> tuple[str, object] | None:
    master = await get_master_by_telegram_id_any(
        master_telegram_id
    )

    if master is None:
        return None

    directions = await get_master_directions(
        master_telegram_id
    )

    username = (
        f"@{html.escape(str(master['username']))}"
        if master["username"]
        else "Не указан"
    )

    average_response = (
        f"{int(master['average_response_minutes'])} мин."
        if master["average_response_minutes"] is not None
        else "Пока нет данных"
    )

    text = (
        "✏️ <b>Управление исполнителем</b>\n\n"
        f"🔗 Telegram: <b>{username}</b>\n"
        f"🆔 Telegram ID: "
        f"<code>{int(master['telegram_id'])}</code>\n\n"
        f"👤 ФИО: <b>{_display_value(master['full_name'])}</b>\n"
        f"🎓 Университет: "
        f"<b>{_display_value(master['university'])}</b>\n"
        f"🔢 Код специальности: "
        f"<b>{_display_value(master['specialty_code'])}</b>\n"
        f"📚 Специальность: "
        f"<b>{_display_value(master['specialty_name'])}</b>\n\n"
        "🧭 <b>Направления:</b>\n"
        f"{build_directions_summary(directions)}\n\n"
        f"⏱ Среднее время ответа: "
        f"<b>{html.escape(average_response)}</b>\n"
        f"📌 Статус: <b>{_status_title(master)}</b>"
    )

    if master["deleted_at"] is not None:
        text += (
            "\n\n"
            "ℹ️ Карточка сохранена в истории сервиса. "
            "Удалённый исполнитель не может получать заказы "
            "или пользоваться рабочими функциями."
        )

    keyboard = admin_master_manage_keyboard(
        int(master["telegram_id"]),
        is_active=bool(int(master["is_active"])),
        is_deleted=(
            master["deleted_at"] is not None
        ),
        catalog_back_callback=(
            catalog_back_callback
        ),
    )

    return text, keyboard

async def render_master_management_message(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    master_telegram_id: int,
    catalog_back_callback: str | None = None,
) -> bool:
    payload = await build_master_management(
        master_telegram_id,
        catalog_back_callback=(
            catalog_back_callback
        ),
    )

    if payload is None:
        return False

    text, keyboard = payload

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    except TelegramBadRequest as exc:
        if (
            "message is not modified"
            not in str(exc).lower()
        ):
            raise

    return True

async def _deny_non_admin_callback(
    callback: CallbackQuery,
) -> bool:
    if is_admin(callback.from_user.id):
        return False

    await callback.answer(
        "Недостаточно прав.",
        show_alert=True,
    )
    return True

async def _get_catalog_back_callback(
    state: FSMContext,
) -> str | None:
    data = await state.get_data()
    value = data.get(
        "catalog_back_callback"
    )

    if not value:
        return None

    return str(value)

async def _restore_catalog_context(
    state: FSMContext,
    catalog_back_callback: str | None,
) -> None:
    if catalog_back_callback:
        await state.update_data(
            catalog_back_callback=(
                catalog_back_callback
            )
        )

def _catalog_back_callback(
    *,
    mode: str,
    argument: str,
    page: int,
) -> str:
    if mode == "all":
        return f"adm_m_all:{page}"

    if mode == "status":
        return (
            f"adm_m_status:{argument}:{page}"
        )

    if mode == "direction":
        return (
            f"adm_m_dir:{argument}:{page}"
        )

    return "adm_m_menu"

async def _edit_catalog_message(
    callback: CallbackQuery,
    *,
    text: str,
    reply_markup,
) -> None:
    if callback.message is None:
        return

    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if (
            "message is not modified"
            not in str(exc).lower()
        ):
            raise

async def _render_masters_menu(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    stats = await get_admin_masters_stats()

    await state.clear()

    text = (
        "👥 <b>Управление исполнителями</b>\n\n"
        f"Всего: <b>{stats['total']}</b>\n"
        f"🟢 Активных: <b>{stats['active']}</b>\n"
        f"🔴 Неактивных: <b>{stats['inactive']}</b>\n"
        f"⚫ Удалённых: <b>{stats['deleted']}</b>\n\n"
        "Выберите способ просмотра."
    )

    await _edit_catalog_message(
        callback,
        text=text,
        reply_markup=(
            admin_masters_menu_keyboard()
        ),
    )

async def _render_masters_page(
    callback: CallbackQuery,
    *,
    mode: str,
    argument: str,
    page: int,
) -> None:
    status_filter: str | None = None
    direction: str | None = None

    if mode == "all":
        heading = "👥 <b>Все исполнители</b>"
        description = (
            "Показаны активные, неактивные "
            "и удалённые исполнители."
        )

    elif mode == "status":
        status_filter = (
            ADMIN_MASTER_STATUS_BY_KEY.get(
                argument
            )
        )

        if status_filter is None:
            await callback.answer(
                "Неизвестный фильтр статуса.",
                show_alert=True,
            )
            return

        heading = (
            f"{ADMIN_MASTER_STATUS_TITLE_BY_KEY[argument]}"
        )
        description = (
            "Выберите исполнителя, чтобы открыть "
            "его административную карточку."
        )

    elif mode == "direction":
        direction = DIRECTION_BY_KEY.get(
            argument
        )

        if direction is None:
            await callback.answer(
                "Неизвестное направление.",
                show_alert=True,
            )
            return

        heading = (
            "🧭 <b>"
            + html.escape(direction)
            + "</b>"
        )
        description = (
            "Показаны все исполнители с этим "
            "направлением независимо от статуса."
        )

    else:
        await callback.answer(
            "Неизвестный режим каталога.",
            show_alert=True,
        )
        return

    total_count = await get_admin_masters_count(
        status_filter=status_filter,
        direction=direction,
    )

    total_pages = max(
        (
            total_count
            + ADMIN_MASTERS_PAGE_SIZE
            - 1
        )
        // ADMIN_MASTERS_PAGE_SIZE,
        1,
    )

    safe_page = min(
        max(
            int(page),
            0,
        ),
        total_pages - 1,
    )

    masters = await get_admin_masters(
        limit=ADMIN_MASTERS_PAGE_SIZE,
        offset=(
            safe_page
            * ADMIN_MASTERS_PAGE_SIZE
        ),
        status_filter=status_filter,
        direction=direction,
    )

    text = (
        f"{heading}\n\n"
        f"{description}\n\n"
        f"Найдено: <b>{total_count}</b>\n"
        f"Страница: "
        f"<b>{safe_page + 1} из {total_pages}</b>"
    )

    if total_count == 0:
        text += (
            "\n\nПо выбранному фильтру "
            "исполнителей нет."
        )

    await _edit_catalog_message(
        callback,
        text=text,
        reply_markup=admin_masters_list_keyboard(
            masters,
            mode=mode,
            argument=argument,
            current_page=safe_page,
            total_pages=total_pages,
        ),
    )

def _validate_profile_value(
    field_name: str,
    raw_value: str,
) -> tuple[bool, str | None, str | None]:
    value = raw_value.strip()

    if (
        field_name != "full_name"
        and value.lower() == "/clear"
    ):
        return True, None, None

    if field_name == "full_name":
        if len(value) < 2 or len(value) > 100:
            return (
                False,
                None,
                "ФИО должно содержать от 2 до 100 символов.",
            )

    elif field_name == "university":
        if len(value) > 200:
            return (
                False,
                None,
                "Название университета слишком длинное.",
            )

    elif field_name == "specialty_code":
        if len(value) > 30:
            return (
                False,
                None,
                "Код специальности слишком длинный.",
            )

    elif field_name == "specialty_name":
        if len(value) > 200:
            return (
                False,
                None,
                "Название специальности слишком длинное.",
            )

    else:
        return (
            False,
            None,
            "Неизвестное поле карточки.",
        )

    if not value:
        if field_name == "full_name":
            return (
                False,
                None,
                "ФИО не может быть пустым.",
            )

        return True, None, None

    return True, value, None

def _field_prompt(
    field_name: str,
    current_value: object,
    *,
    refill: bool,
) -> str:
    title = EDITABLE_FIELDS[field_name]
    current = _display_value(
        current_value
    )

    optional_note = (
        "\n\nЧтобы очистить поле, отправьте "
        "<code>/clear</code>."
        if field_name != "full_name"
        else ""
    )

    keep_note = (
        "\n\nИли нажмите "
        "«➡️ Оставить без изменений»."
        if refill
        else ""
    )

    return (
        f"{title}\n\n"
        f"Текущее значение:\n"
        f"<b>{current}</b>\n\n"
        "Отправьте новое значение."
        f"{optional_note}"
        f"{keep_note}\n\n"
        "Для отмены можно отправить "
        "<code>/cancel</code>."
    )
