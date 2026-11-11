from contextlib import nullcontext
import asyncio
import html
import hashlib
import json
from uuid import uuid4
from aiogram import F, Router, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import logging
from aiogram.exceptions import TelegramBadRequest
from database.orders import (
    assign_waiting_order_to_master,
    create_order,
    get_excluded_master_ids,
    get_order_with_client,
    release_assignment_after_topic_failure,
    get_reviews_by_master,
)
from database.users import get_internal_user_id
from keyboards.order_form import (
    confirm_order_keyboard,
    master_assignment_mode_keyboard,
    unavailable_masters_keyboard,
    change_order_direction_keyboard,
)
from states.order import OrderState
from catalog.directions import DIRECTION_BY_KEY
from aiogram.fsm.storage.base import BaseEventIsolation
from utils.order_draft import parse_order_details, get_attached_file
from database.chat_archive import get_topic_checkpoint
from keyboards.order_chat import order_chat_keyboard
from utils.order_card import (
    archive_order_topic,
    create_order_assignment_topic,
)


from database.masters import (
    get_active_masters,
    get_master_by_telegram_id,
    get_active_masters_count,
    get_master_directions,
    master_has_direction,
)
from database.reviews import (
    get_master_rating_stats,
)
from keyboards.masters import (
    master_card_keyboard,
    masters_selection_keyboard,
)
from keyboards.reviews import (
    master_reviews_keyboard,
)
from database.activity_logs import log_user_activity
from utils.section_images import edit_message_content, send_section_message

router = Router()
logger = logging.getLogger(__name__)


async def get_selection_direction(
    state_data: dict,
) -> str | None:
    """
    Возвращает направление текущего оформления/перевыбора.

    При обычном оформлении направление уже лежит в FSM.
    При перевыборе дополнительно умеем восстановить его из заказа.
    """

    direction = str(
        state_data.get("direction")
        or ""
    ).strip()

    if direction:
        return direction

    reassign_order_id = state_data.get(
        "reassign_order_id"
    )

    if isinstance(
        reassign_order_id,
        int,
    ):
        order = await get_order_with_client(
            reassign_order_id
        )

        if order is not None:
            direction = str(
                order["direction"]
                or ""
            ).strip()

            if direction:
                return direction

    return None



ASSIGNMENT_MODE_TEXT = (
    "👨‍💻 <b>Выберите способ назначения исполнителя</b>\n\n"
    "Вы можете выбрать конкретного исполнителя "
    "или открыть заказ для свободного исполнителя.\n\n"
    "В режиме «Свободный исполнитель» заказ получит "
    "первый доступный исполнитель, который его возьмёт."
)


async def show_assignment_mode(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    prefix_text: str | None = None,
) -> None:
    if callback.message is None:
        return

    await state.set_state(
        OrderState.waiting_for_master
    )

    text = ASSIGNMENT_MODE_TEXT

    if prefix_text:
        text = (
            f"{prefix_text}\n\n"
            f"{ASSIGNMENT_MODE_TEXT}"
        )

    await send_section_message(
        callback.message,
        "choose_master.png",
        text,
        reply_markup=(
            master_assignment_mode_keyboard()
        ),
        parse_mode="HTML",
        replace_photo=True,
    )


async def show_no_available_masters(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    direction: str,
) -> None:
    if callback.message is None:
        return

    data = await state.get_data()

    # Для уже созданного заказа направление менять здесь нельзя:
    # это отдельное бизнес-действие и оно должно менять сам orders.
    can_change_direction = not isinstance(
        data.get("reassign_order_id"),
        int,
    )

    await edit_message_content(callback.message, 
        "⚠️ <b>Нет доступных исполнителей</b>\n\n"
        f"Для направления "
        f"<b>{html.escape(direction)}</b> "
        "сейчас нет доступных исполнителей.\n\n"
        + (
            "Выберите другое направление "
            "или вернитесь к выбору способа назначения."
            if can_change_direction
            else
            "Вернитесь назад и выберите другой "
            "доступный вариант назначения."
        ),
        reply_markup=unavailable_masters_keyboard(
            allow_direction_change=(
                can_change_direction
            )
        ),
        parse_mode="HTML",
    )


def build_order_preview(data: dict) -> str:
    direction = html.escape(
        str(data.get("direction") or "Не указано")
    )
    deadline = html.escape(
        str(data.get("deadline") or "Не указан")
    )
    description = html.escape(
        str(data.get("description") or "Не указано")
    )

    master_full_name = data.get(
        "assigned_master_full_name"
    )

    assignment_mode = data.get(
        "assignment_mode"
    )

    # В клиентской части Telegram username исполнителя
    # намеренно никогда не показывается.
    if assignment_mode == "free":
        master_display = "Свободный исполнитель"
    elif master_full_name:
        master_display = str(
            master_full_name
        )
    else:
        master_display = "Не выбран"

    safe_master_display = html.escape(
        master_display
    )

    files_count = len(
        data.get("files", [])
    )

    return (
        "📋 <b>Проверьте заказ</b>\n\n"
        f"🧭 Направление: <b>{direction}</b>\n"
        f"📅 Дедлайн: <b>{deadline}</b>\n"
        f"📎 Файлов: <b>{files_count}</b>\n"
        f"👨‍💻 Исполнитель: <b>{safe_master_display}</b>\n\n"
        f"💬 <b>Описание:</b>\n{description}"
    )


async def show_confirmation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("draft_id"):
        await state.update_data(draft_id=uuid4().hex)

    confirmation = await message.answer(
        build_order_preview(data),
        reply_markup=confirm_order_keyboard(),
        parse_mode="HTML",
    )

    await state.update_data(confirmation_message_id=confirmation.message_id)
    await state.set_state(OrderState.waiting_for_confirmation)


# Telegram присылает каждый элемент альбома отдельным update. На короткое
# время собираем такие сообщения, чтобы для клиента это оставалось одним
# этапом оформления, а в заказ попадали все приложенные файлы.
_pending_media_groups: dict[tuple[int, int, str], dict] = {}


async def finish_order_details_messages(
    messages: list[Message],
    state: FSMContext,
) -> None:
    """Сохраняет единый пакет данных заказа и продолжает оформление."""

    text = next(
        (
            (item.caption or item.text or "").strip()
            for item in messages
            if (item.caption or item.text or "").strip()
        ),
        "",
    )

    description, deadline, error = parse_order_details(text)

    if error is not None:
        await messages[-1].answer(
            f"⚠️ {error}\n\n"
            "Отправьте условия заново одним сообщением."
        )
        return

    files = []
    for item in messages:
        file_data = get_attached_file(item)
        if file_data is not None:
            files.append(file_data)

    await state.update_data(
        # Поле work_type остаётся в БД и старых карточках для совместимости,
        # но клиент больше не выбирает его отдельным шагом.
        work_type="По описанию",
        description=description,
        deadline=deadline,
        files=files,
    )

    await state.set_state(OrderState.waiting_for_master)

    await send_section_message(
        messages[-1],
        "choose_master.png",
        ASSIGNMENT_MODE_TEXT,
        reply_markup=master_assignment_mode_keyboard(),
        parse_mode="HTML",
    )


async def finalize_media_group(
    key: tuple[int, int, str],
    state: FSMContext,
    *,
    events_isolation: BaseEventIsolation | None = None,
) -> None:
    # Даём Telegram прислать остальные элементы той же группы.
    await asyncio.sleep(0.8)

    group = _pending_media_groups.get(key)
    if group is None:
        return
    try:
        # Изоляция приходит от того Dispatcher, который реально принял
        # update (main.py инжектирует workflow-зависимость
        # "fsm_events_isolation"). Глобальный dp больше не используется:
        # тесты собирают отдельный Dispatcher с собственной изоляцией.
        if events_isolation is None:
            locked = nullcontext()
        else:
            locked = events_isolation.lock(key=state.key)
        async with locked:
            data = await state.get_data()
            if (
                data.get("details_session_id") != group["session_id"]
                or await state.get_state() != OrderState.waiting_for_order_details.state
            ):
                return
            await finish_order_details_messages(group["messages"], state)
    except Exception:
        logger.exception("Не удалось завершить сборку альбома заказа")
    finally:
        if _pending_media_groups.get(key) is group:
            _pending_media_groups.pop(key, None)


@router.message(OrderState.waiting_for_order_details)
async def process_order_details(
    message: Message,
    state: FSMContext,
    fsm_events_isolation: BaseEventIsolation | None = None,
) -> None:
    """
    Принимает условия и, при наличии, дедлайн и файлы одним этапом.

    Поддерживаются обычное текстовое сообщение, сообщение с фото/документом
    и Telegram media group с несколькими вложениями.

    ``fsm_events_isolation`` инжектируется Dispatcher-ом из его
    workflow-данных (см. main.py): финализатор альбома работает вне
    обработки update и обязан синхронизироваться по той же изоляции
    событий FSM, что и обычные обработчики.
    """

    if message.media_group_id:
        key = (
            message.chat.id,
            message.from_user.id,
            str(message.media_group_id),
        )

        data = await state.get_data()
        session_id = data.get("details_session_id")
        if not session_id:
            session_id = uuid4().hex
            await state.update_data(details_session_id=session_id)
        if key not in _pending_media_groups and len(_pending_media_groups) >= 512:
            await message.answer("Слишком много загружаемых альбомов. Попробуйте немного позже.")
            return
        group = _pending_media_groups.setdefault(
            key, {"messages": [], "task": None, "session_id": session_id},
        )
        if group["session_id"] != session_id or len(group["messages"]) >= 10:
            return
        group["messages"].append(message)

        if group["task"] is None:
            group["task"] = asyncio.create_task(
                finalize_media_group(
                    key,
                    state,
                    events_isolation=fsm_events_isolation,
                )
            )
            def cleanup_album(task):
                if _pending_media_groups.get(key) is group:
                    _pending_media_groups.pop(key, None)
                if not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        logger.error("Фоновая сборка альбома не завершена: %s", type(error).__name__)
            group["task"].add_done_callback(cleanup_album)
        return

    await finish_order_details_messages(
        [message],
        state,
    )



@router.callback_query(
    OrderState.waiting_for_master,
    F.data == "assignment_back",
)
async def back_to_assignment_mode(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await show_assignment_mode(
        callback,
        state,
    )

    await callback.answer()


@router.callback_query(
    OrderState.waiting_for_master,
    F.data == "assignment_change_direction",
)
async def open_assignment_direction_change(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    data = await state.get_data()

    if isinstance(
        data.get("reassign_order_id"),
        int,
    ):
        await callback.answer(
            "Направление уже созданного заказа "
            "нельзя менять на этом этапе.",
            show_alert=True,
        )
        return

    await edit_message_content(callback.message, 
        "🧭 <b>Выберите другое направление</b>\n\n"
        "Описание, необязательный дедлайн и уже "
        "прикреплённые файлы сохранятся.",
        reply_markup=(
            change_order_direction_keyboard()
        ),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(
    OrderState.waiting_for_master,
    F.data.startswith("assignment_direction:"),
)
async def change_assignment_direction(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if (
        callback.message is None
        or callback.data is None
    ):
        await callback.answer()
        return

    data = await state.get_data()

    if isinstance(
        data.get("reassign_order_id"),
        int,
    ):
        await callback.answer(
            "Направление уже созданного заказа "
            "нельзя менять на этом этапе.",
            show_alert=True,
        )
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

    # Меняем только направление и сбрасываем прежний выбор исполнителя.
    # Вся уже заполненная часть заказа остаётся в FSM.
    await state.update_data(
        direction=direction,
        assignment_mode=None,
        assigned_master_telegram_id=None,
        assigned_master_username=None,
        assigned_master_full_name=None,
    )

    await show_assignment_mode(
        callback,
        state,
        prefix_text=(
            "✅ Направление изменено: "
            f"<b>{html.escape(direction)}</b>"
        ),
    )

    await callback.answer(
        "Направление изменено."
    )


@router.callback_query(
    OrderState.waiting_for_master,
    F.data == "assignment_specific",
)
async def choose_specific_master(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    page = 0
    page_size = 10
    state_data = await state.get_data()
    reassign_order_id = state_data.get(
        "reassign_order_id"
    )

    direction = await get_selection_direction(
        state_data
    )

    if direction is None:
        await callback.answer(
            "Не удалось определить направление заказа.",
            show_alert=True,
        )
        return

    excluded: set[int] = set()

    if isinstance(
        reassign_order_id,
        int,
    ):
        excluded = await get_excluded_master_ids(
            reassign_order_id
        )

    total_count = await get_active_masters_count(
        exclude_telegram_ids=excluded,
        direction=direction,
    )

    if total_count == 0:
        await show_no_available_masters(
            callback,
            state,
            direction=direction,
        )
        await callback.answer()
        return

    total_pages = max(
        (total_count + page_size - 1)
        // page_size,
        1,
    )

    masters = await get_active_masters(
        limit=page_size,
        offset=0,
        exclude_telegram_ids=excluded,
        direction=direction,
    )

    await state.update_data(
        assignment_mode="specific",
        direction=direction,
    )

    await edit_message_content(callback.message, 
        "👨‍💻 <b>Выберите исполнителя</b>\n\n"
        f"🧭 Направление: <b>{html.escape(direction)}</b>\n\n"
        "Показаны только исполнители, работающие "
        "с этим направлением. Нажмите на исполнителя, "
        "чтобы открыть его карточку.",
        reply_markup=masters_selection_keyboard(
            masters=masters,
            current_page=page,
            total_pages=total_pages,
            is_reassign=isinstance(reassign_order_id, int),
        ),
        parse_mode="HTML",
    )

    await callback.answer()

@router.callback_query(
    OrderState.waiting_for_master,
    F.data == "assignment_free",
)
async def choose_free_master(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    state_data = await state.get_data()

    # Для уже созданного заказа режим свободного исполнителя
    # обрабатывается отдельным reassign_free callback.
    if isinstance(
        state_data.get("reassign_order_id"),
        int,
    ):
        await callback.answer(
            "Используйте кнопку «Свободный исполнитель» "
            "в меню перевыбора заказа.",
            show_alert=True,
        )
        return

    direction = await get_selection_direction(
        state_data
    )

    if direction is None:
        await callback.answer(
            "Не удалось определить направление заказа.",
            show_alert=True,
        )
        return

    total_count = await get_active_masters_count(
        direction=direction,
    )

    if total_count == 0:
        await show_no_available_masters(
            callback,
            state,
            direction=direction,
        )
        await callback.answer()
        return

    await state.update_data(
        assignment_mode="free",
        direction=direction,
        assigned_master_telegram_id=None,
        assigned_master_username=None,
        assigned_master_full_name=None,
    )

    await show_confirmation(
        callback.message,
        state,
    )

    await callback.answer(
        "Выбран режим «Свободный исполнитель»."
    )

@router.callback_query(
    OrderState.waiting_for_master,
    F.data.startswith("masters_page:"),
)
async def change_masters_page(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    try:
        _, page_raw = callback.data.split(
            ":",
            maxsplit=1,
        )
        page = int(page_raw)
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный номер страницы.",
            show_alert=True,
        )
        return

    if page < 0:
        await callback.answer(
            "Такой страницы нет.",
            show_alert=True,
        )
        return

    page_size = 10
    state_data = await state.get_data()
    reassign_order_id = state_data.get(
        "reassign_order_id"
    )
    direction = await get_selection_direction(
        state_data
    )

    if direction is None:
        await callback.answer(
            "Не удалось определить направление заказа.",
            show_alert=True,
        )
        return

    excluded: set[int] = set()

    if isinstance(
        reassign_order_id,
        int,
    ):
        excluded = await get_excluded_master_ids(
            reassign_order_id
        )

    total_count = await get_active_masters_count(
        exclude_telegram_ids=excluded,
        direction=direction,
    )

    if total_count == 0:
        await show_no_available_masters(
            callback,
            state,
            direction=direction,
        )
        await callback.answer()
        return

    total_pages = max(
        (total_count + page_size - 1)
        // page_size,
        1,
    )

    if page >= total_pages:
        page = total_pages - 1

    masters = await get_active_masters(
        limit=page_size,
        offset=page * page_size,
        exclude_telegram_ids=excluded,
        direction=direction,
    )

    await edit_message_content(callback.message, 
        "👨‍💻 <b>Выберите исполнителя</b>\n\n"
        f"🧭 Направление: <b>{html.escape(direction)}</b>\n\n"
        "Показаны только исполнители этого направления.",
        reply_markup=masters_selection_keyboard(
            masters=masters,
            current_page=page,
            total_pages=total_pages,
            is_reassign=isinstance(reassign_order_id, int),
        ),
        parse_mode="HTML",
    )

    await callback.answer()

@router.callback_query(
    OrderState.waiting_for_master,
    F.data == "masters_page_info",
)
async def masters_page_info(
    callback: CallbackQuery,
) -> None:
    await callback.answer()

@router.callback_query(
    OrderState.waiting_for_master,
    F.data.startswith("select_master:"),
)
async def select_master(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    try:
        _, raw = callback.data.split(":", maxsplit=1)
        master_telegram_id = int(raw)
    except (ValueError, IndexError):
        await callback.answer(
            "Не удалось определить исполнителя.",
            show_alert=True,
        )
        return

    master = await get_master_by_telegram_id(master_telegram_id)
    if master is None:
        await callback.answer(
            "Этот исполнитель сейчас недоступен.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    reassign_order_id = data.get("reassign_order_id")

    direction = await get_selection_direction(
        data
    )

    if direction is None:
        await callback.answer(
            "Не удалось определить направление заказа.",
            show_alert=True,
        )
        return

    if isinstance(reassign_order_id, int):
        internal_user_id = await get_internal_user_id(
            callback.from_user.id
        )
        if internal_user_id is None:
            await state.clear()
            await callback.answer(
                "Сначала необходимо зарегистрироваться.",
                show_alert=True,
            )
            return

        order_before = await get_order_with_client(
            reassign_order_id
        )
        if order_before is None or int(order_before["user_id"]) != internal_user_id:
            await state.clear()
            await callback.answer(
                "Заказ не найден или не принадлежит вам.",
                show_alert=True,
            )
            return

        order_direction = str(
            order_before["direction"]
            or ""
        ).strip()

        if (
            not order_direction
            or not await master_has_direction(
                telegram_id=master_telegram_id,
                direction=order_direction,
            )
        ):
            await callback.answer(
                "Этот исполнитель не работает "
                "с направлением заказа.",
                show_alert=True,
            )
            return

        excluded = await get_excluded_master_ids(reassign_order_id)
        if master_telegram_id in excluded:
            await callback.answer(
                "Этот исполнитель уже отказался от данного заказа.",
                show_alert=True,
            )
            return

        assigned, captured_generation = await assign_waiting_order_to_master(
            order_id=reassign_order_id,
            client_telegram_id=callback.from_user.id,
            master_telegram_id=master_telegram_id,
            capture_generation=True,
        )
        if not assigned:
            await callback.answer(
                "Состояние заказа уже изменилось. Обновите меню.",
                show_alert=True,
            )
            return

        # Если до ручного выбора заказ был открыт для свободного исполнителя,
        # закрываем старую общую тему после успешного атомарного назначения.
        await archive_order_topic(
            bot=bot,
            group_chat_id=order_before["group_chat_id"],
            topic_id=order_before["topic_id"],
            order_id=reassign_order_id,
        )

        # Генерация получена внутри транзакции назначения.
        try:
            await create_order_assignment_topic(
                bot=bot,
                order_id=reassign_order_id,
                expected_assignment_generation=captured_generation,
            )
        except Exception:
            logger.exception(
                "Не удалось создать новую тему заказа %s после перевыбора исполнителя",
                reassign_order_id,
            )
            await release_assignment_after_topic_failure(
                order_id=reassign_order_id,
                master_telegram_id=master_telegram_id,
                captured_assignment_generation=captured_generation,
            )
            await state.clear()
            await callback.message.answer(
                "⚠️ Исполнитель выбран, но создать новую рабочую тему не удалось. "
                "Заказ возвращён к выбору исполнителя."
            )
            await callback.answer()
            return

        try:
            await log_user_activity(
                telegram_id=callback.from_user.id,
                user_id=internal_user_id,
                action="master_selected",
                entity_type="order",
                entity_id=reassign_order_id,
                details={
                    "master_telegram_id": master_telegram_id,
                    "master_username": master["username"],
                    "master_full_name": master["full_name"],
                    "selection_mode": "reselected",
                },
            )
        except Exception:
            logger.exception(
                "Не удалось записать перевыбор исполнителя заказа %s",
                reassign_order_id,
            )

        # Клиенту показываем только публичное имя
        # из карточки исполнителя — без @username и Telegram ID.
        master_display = str(
            master["full_name"]
            or "Исполнитель"
        )

        await state.clear()

        await callback.message.answer(
            f"✅ Для заказа <b>№{reassign_order_id}</b> "
            "выбран новый исполнитель: "
            f"<b>{html.escape(master_display)}</b>.\n\n"
            "Создано новое обсуждение. "
            "Исполнитель может предложить стоимость.",
            reply_markup=order_chat_keyboard(
                reassign_order_id
            ),
            parse_mode="HTML",
        )
        await callback.answer("Новый исполнитель выбран.")
        return

    if not await master_has_direction(
        telegram_id=master_telegram_id,
        direction=direction,
    ):
        await callback.answer(
            "Этот исполнитель не работает "
            "с выбранным направлением.",
            show_alert=True,
        )
        return

    await state.update_data(
        assignment_mode="specific",
        direction=direction,
        assigned_master_telegram_id=master_telegram_id,
        assigned_master_username=master["username"],
        assigned_master_full_name=master["full_name"],
    )
    await show_confirmation(callback.message, state)
    await callback.answer("Исполнитель выбран.")

@router.callback_query(
    OrderState.waiting_for_confirmation,
    F.data == "confirm_order",
)
async def confirm_order(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
) -> None:
    if callback.message is None:
        await callback.answer(
            "Сообщение подтверждения недоступно.",
            show_alert=True,
        )
        return

    data = await state.get_data()
    assignment_mode = str(data.get("assignment_mode") or "specific")
    if (
        not data.get("draft_id")
        or data.get("confirmation_message_id") != callback.message.message_id
    ):
        await callback.answer("Это подтверждение устарело. Откройте текущий черновик.", show_alert=True)
        return
    assigned_master_telegram_id = data.get("assigned_master_telegram_id")

    if assignment_mode == "specific" and assigned_master_telegram_id is None:
        await callback.answer(
            "Не удалось определить выбранного исполнителя.",
            show_alert=True,
        )
        return

    await state.set_state(OrderState.creating_order)

    internal_user_id = await get_internal_user_id(callback.from_user.id)
    if internal_user_id is None:
        await state.clear()
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    order_id: int | None = None

    try:
        order_id, freshly_created = await create_order(
            dedup_key=f"order_confirm:{bot.id}:{callback.from_user.id}:{data['draft_id']}",
            dedup_content_hash=hashlib.sha256(json.dumps({
                key: data.get(key) for key in (
                    "direction", "work_type", "description", "deadline", "files",
                    "assignment_mode", "assigned_master_telegram_id",
                )
            }, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            user_id=internal_user_id,
            direction=data.get("direction"),
            # Поле subject пока остаётся в БД для совместимости.
            # Пользователь его больше не выбирает: технически
            # сохраняем туда направление.
            subject=str(data.get("direction") or "Не указано"),
            work_type=data["work_type"],
            description=data["description"],
            deadline=data.get("deadline"),
            files=tuple(data.get("files", [])),
            assigned_master_telegram_id=(
                int(assigned_master_telegram_id)
                if assigned_master_telegram_id is not None
                else None
            ),
            assignment_status=(
                "open" if assignment_mode == "free" else "assigned"
            ),
        )

        if not freshly_created:
            await state.clear()
            await callback.answer(f"Заказ №{order_id} уже сохранён.", show_alert=True)
            return

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

        await create_order_assignment_topic(
            bot=bot,
            order_id=order_id,
        )

    except asyncio.CancelledError:
        if order_id is None:
            await state.set_state(OrderState.waiting_for_confirmation)
        raise
    except Exception as error:
        logger.error("Сбой создания/публикации заказа %s: %s", order_id, type(error).__name__)
        if order_id is None:
            # Keep the same draft/key and confirmation message. A failed local
            # transaction is retryable; an ambiguous COMMIT is deduplicated.
            await state.set_state(OrderState.waiting_for_confirmation)
            await callback.message.answer(
                "⚠️ Не удалось подтвердить сохранение заказа. Черновик сохранён; "
                "повторите подтверждение той же кнопкой."
            )
            await callback.answer()
            return
        checkpoint = await get_topic_checkpoint(order_id) if order_id is not None else None
        if checkpoint is not None:
            await state.clear()
            await callback.message.answer(
                f"⚠️ Заказ №{order_id} сохранён, но публикация обсуждения не завершена. "
                + ("Публикация будет продолжена при восстановлении. Не создавайте заказ повторно."
                   if checkpoint["detail"] == "local_creation_pending"
                   else "Не оформляйте его повторно; требуется проверка администратором.")
            )
            await callback.answer()
            return
        await state.clear()
        await callback.message.answer(
            f"⚠️ Заказ №{order_id} сохранён; публикация требует проверки администратором. "
            "Не создавайте заказ повторно."
        )
        await callback.answer()
        return

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            user_id=internal_user_id,
            action="order_created",
            entity_type="order",
            entity_id=order_id,
            details={
                "work_type": data["work_type"],
                "direction": data.get("direction"),
                "deadline": data.get("deadline"),
                "assigned_master_telegram_id": assigned_master_telegram_id,
                "assignment_mode": assignment_mode,
                "files_count": len(data.get("files", [])),
            },
        )
    except Exception:
        logger.exception("Не удалось записать создание заказа %s", order_id)

    if assignment_mode == "specific":
        try:
            await log_user_activity(
                telegram_id=callback.from_user.id,
                user_id=internal_user_id,
                action="master_selected",
                entity_type="order",
                entity_id=order_id,
                details={
                    "master_telegram_id": int(assigned_master_telegram_id),
                    "master_username": data.get("assigned_master_username"),
                    "master_full_name": data.get("assigned_master_full_name"),
                    "selection_mode": "initial",
                },
            )
        except Exception:
            logger.exception("Не удалось записать выбор исполнителя заказа %s", order_id)

    await state.clear()

    if assignment_mode == "free":
        await callback.message.answer(
            f"✅ Заказ <b>№{order_id}</b> успешно создан.\n\n"
            "🟢 Режим: <b>Свободный исполнитель</b>\n\n"
            "Заказ опубликован для доступных исполнителей. "
            "Первый исполнитель, который возьмёт заказ, будет закреплён за ним. "
            "После этого бот уведомит вас.",
            parse_mode="HTML",
        )
    else:
        # Telegram username — внутреннее поле.
        # Заказчик видит только имя из карточки исполнителя.
        master_display = str(
            data.get(
                "assigned_master_full_name"
            )
            or "Исполнитель"
        )

        await callback.message.answer(
            f"✅ Заказ <b>№{order_id}</b> успешно создан.\n\n"
            f"👨‍💻 Исполнитель: "
            f"<b>{html.escape(master_display)}</b>\n\n"
            "Заказ передан выбранному исполнителю. "
            "Он ознакомится с заданием и предложит "
            "стоимость выполнения.\n\n"
            "Чтобы задать вопрос или дополнить заказ, "
            "нажмите кнопку ниже.",
            reply_markup=order_chat_keyboard(
                order_id
            ),
            parse_mode="HTML",
        )

    await callback.answer("Заказ создан.")

def format_response_time(
    minutes: int | None,
) -> str:
    """
    Преобразует автоматически рассчитанное
    среднее время ответа исполнителя
    в понятный пользователю текст.
    """

    if minutes is None:
        return "Пока нет данных"

    if minutes < 60:
        return f"Около {minutes} мин."

    hours, remaining_minutes = divmod(
        minutes,
        60,
    )

    if remaining_minutes == 0:
        return f"Около {hours} ч."

    return (
        f"около {hours} ч. "
        f"{remaining_minutes} мин."
    )


def build_master_card_text(
    master,
    rating_stats: dict,
    directions: list[str],
) -> str:
    """
    Формирует расширенную карточку исполнителя
    с рейтингом и распределением оценок.
    """

    full_name = html.escape(
        str(master["full_name"] or "Не указано")
    )

    university = html.escape(
        str(master["university"] or "Не указано")
    )

    specialty_code = html.escape(
        str(
            master["specialty_code"]
            or "Код не указан"
        )
    )

    specialty_name = html.escape(
        str(
            master["specialty_name"]
            or "Название не указано"
        )
    )

    response_time = html.escape(
        format_response_time(
            master["average_response_minutes"]
        )
    )

    reviews_count = int(
        rating_stats.get(
            "reviews_count",
            0,
        )
    )

    average_rating = rating_stats.get(
        "average_rating"
    )

    if average_rating is None:
        rating_header = (
            "⭐ Рейтинг пока не сформирован\n"
            "💬 Отзывов пока нет"
        )
    else:
        rating_header = (
            f"⭐ <b>{average_rating:.2f} / 5.00</b>\n"
            f"💬 Отзывов: <b>{reviews_count}</b>"
        )

    def build_rating_bar(
        rating: int,
    ) -> str:
        count = int(
            rating_stats.get(
                f"rating_{rating}",
                0,
            )
        )

        if reviews_count <= 0:
            filled = 0
        else:
            filled = round(
                count / reviews_count * 10
            )

        filled = max(
            0,
            min(
                filled,
                10,
            ),
        )

        empty = 10 - filled

        bar = (
            "█" * filled
            + "░" * empty
        )

        return (
            f"{rating} ⭐  "
            f"<code>{bar}</code>  "
            f"{count}"
        )

    rating_distribution = "\n".join(
        build_rating_bar(rating)
        for rating in range(
            5,
            0,
            -1,
        )
    )

    if directions:
        directions_text = "\n".join(
            f"• {html.escape(direction)}"
            for direction in directions
        )
    else:
        directions_text = "Не назначены"

    return (
        "👨‍💻 <b>Исполнитель</b>\n\n"
        f"👤 <b>{full_name}</b>\n\n"
        f"🎓 <b>Университет</b>\n"
        f"{university}\n\n"
        f"📚 <b>Специальность</b>\n"
        f"{specialty_code} — "
        f"{specialty_name}\n\n"
        f"🧭 <b>Направления</b>\n"
        f"{directions_text}\n\n"
        f"⏱ <b>Среднее время ответа</b>\n"
        f"{response_time}\n\n"
        f"{rating_header}\n\n"
        f"<b>Распределение оценок</b>\n"
        f"{rating_distribution}\n\n"
        "Ознакомьтесь с информацией и отзывами, "
        "после чего выберите исполнителя."
    )


@router.callback_query(
    OrderState.waiting_for_master,
    F.data.startswith("master_card:"),
)
async def open_master_card(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """
    Открывает карточку выбранного исполнителя,
    но ещё не закрепляет его за заказом.
    """

    if callback.message is None:
        await callback.answer(
            "Сообщение недоступно.",
            show_alert=True,
        )
        return

    if callback.data is None:
        await callback.answer(
            "Некорректные данные кнопки.",
            show_alert=True,
        )
        return

    try:
        _, telegram_id_raw = callback.data.split(
            ":",
            maxsplit=1,
        )

        master_telegram_id = int(
            telegram_id_raw
        )

    except (ValueError, IndexError):
        await callback.answer(
            "Не удалось определить исполнителя.",
            show_alert=True,
        )
        return

    master = await get_master_by_telegram_id(
        master_telegram_id
    )

    if master is None:
        await callback.answer(
            "Этот исполнитель сейчас недоступен.",
            show_alert=True,
        )
        return

    state_data = await state.get_data()
    direction = await get_selection_direction(
        state_data
    )

    if (
        direction is None
        or not await master_has_direction(
            telegram_id=master_telegram_id,
            direction=direction,
        )
    ):
        await callback.answer(
            "Этот исполнитель недоступен "
            "для направления заказа.",
            show_alert=True,
        )
        return

    rating_stats = await get_master_rating_stats(
        master_telegram_id
    )
    directions = await get_master_directions(
        master_telegram_id
    )

    card_text = build_master_card_text(
        master=master,
        rating_stats=rating_stats,
        directions=directions,
    )

    try:
        await edit_message_content(callback.message, 
            card_text,
            parse_mode="HTML",
            reply_markup=master_card_keyboard(
                master_telegram_id
            ),
        )

    except Exception:
        await callback.message.answer(
            card_text,
            parse_mode="HTML",
            reply_markup=master_card_keyboard(
                master_telegram_id
            ),
        )

    await callback.answer()


@router.callback_query(
    OrderState.waiting_for_master,
    F.data == "masters_list",
)
async def return_to_masters_list(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    data = await state.get_data()
    reassign_order_id = data.get("reassign_order_id")
    direction = await get_selection_direction(
        data
    )

    if direction is None:
        await callback.answer(
            "Не удалось определить направление заказа.",
            show_alert=True,
        )
        return

    excluded: set[int] = set()

    if isinstance(reassign_order_id, int):
        excluded = await get_excluded_master_ids(
            reassign_order_id
        )

    masters = await get_active_masters(
        limit=10,
        offset=0,
        exclude_telegram_ids=excluded,
        direction=direction,
    )
    total_count = await get_active_masters_count(
        exclude_telegram_ids=excluded,
        direction=direction,
    )

    if not masters:
        await show_no_available_masters(
            callback,
            state,
            direction=direction,
        )
        await callback.answer()
        return

    total_pages = max((total_count + 9) // 10, 1)
    await edit_message_content(callback.message, 
        "👨‍💻 <b>Выберите исполнителя</b>\n\n"
        f"🧭 Направление: <b>{html.escape(direction)}</b>\n\n"
        "Нажмите на имя исполнителя, чтобы открыть его карточку.",
        parse_mode="HTML",
        reply_markup=masters_selection_keyboard(
            masters=masters,
            current_page=0,
            total_pages=total_pages,
            is_reassign=isinstance(reassign_order_id, int),
        ),
    )
    await callback.answer()


@router.callback_query(
    OrderState.waiting_for_master,
    F.data.startswith("master_reviews:"),
)
async def open_master_reviews_from_order(
    callback: CallbackQuery,
) -> None:
    """
    Открывает отзывы исполнителя прямо
    во время оформления заказа.
    """

    if callback.message is None:
        await callback.answer(
            "Сообщение недоступно.",
            show_alert=True,
        )
        return

    if callback.data is None:
        await callback.answer(
            "Некорректные данные кнопки.",
            show_alert=True,
        )
        return

    try:
        _, master_id_raw = callback.data.split(
            ":",
            maxsplit=1,
        )

        master_telegram_id = int(
            master_id_raw
        )

    except (ValueError, IndexError):
        await callback.answer(
            "Не удалось определить исполнителя.",
            show_alert=True,
        )
        return

    master = await get_master_by_telegram_id(
        master_telegram_id
    )

    if master is None:
        await callback.answer(
            "Этот исполнитель сейчас недоступен.",
            show_alert=True,
        )
        return

    reviews = await get_reviews_by_master(
        master_telegram_id=master_telegram_id,
        limit=100,
        offset=0,
    )

    if not reviews:
        await callback.answer(
            "У этого исполнителя пока нет отзывов.",
            show_alert=True,
        )
        return

    review = reviews[0]

    stars = "⭐" * int(review["rating"])
    client_nickname = html.escape(
        str(
            review["client_bot_nickname"]
            or "Пользователь"
        )
    )
    direction = html.escape(
        str(
            review["direction"]
            or "Не указано"
        )
    )
    work_type = html.escape(
        str(
            review["work_type"]
            or "Не указано"
        )
    )
    review_text = html.escape(
        str(
            review["review_text"]
            or "Без комментария"
        )
    )

    text = (
        "⭐ <b>Отзывы об исполнителе</b>\n\n"
        f"👤 <b>Отзыв от:</b> {client_nickname}\n\n"
        f"<b>Оценка:</b> {stars}\n\n"
        f"<b>Направление:</b> {direction}\n\n"
        f"<b>Тип работы:</b> {work_type}\n\n"
        f"<b>Комментарий:</b>\n"
        f"{review_text}"
    )

    await edit_message_content(callback.message, 
        text,
        parse_mode="HTML",
        reply_markup=master_reviews_keyboard(
            master_telegram_id=master_telegram_id,
            current_index=0,
            total_reviews=len(reviews),
            source="order",
        ),
    )

    await callback.answer()

@router.callback_query(
    F.data == "cancel_order"
)
async def cancel_order(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """
    Полностью отменяет текущее оформление заказа
    независимо от того, на каком этапе находится пользователь.
    """

    await state.clear()

    if callback.message is not None:
        await callback.message.answer(
            "❌ Создание заказа отменено."
        )

    await callback.answer(
        "Оформление заказа отменено."
    )