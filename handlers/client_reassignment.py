"""Перевыбор и переназначение исполнителя клиентом.

Выделено из handlers.client_price (workstream C): сценарии не связаны
с ценой/оплатой и принадлежат lifecycle заказа. Router и порядок
регистрации остаются в handlers.client_price, поэтому callback-контракты
`reassign_choose:`, `reassign_free:`, `reassign_cancel:` не меняются.

logger сохраняет историческое имя handlers.client_price.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from database.activity_logs import log_user_activity
from database.masters import (
    get_active_masters,
    get_active_masters_count,
)
from database.orders import (
    cancel_unpaid_order_by_client,
    get_excluded_master_ids,
    get_order_with_client,
    release_assignment_after_topic_failure,
    set_order_open_for_free_master,
)
from database.users import get_internal_user_id
from keyboards.masters import masters_selection_keyboard
from states.order import OrderState
from utils.order_card import (
    archive_order_topic,
    create_order_assignment_topic,
)
from utils.order_statuses import ORDER_STATUS_NEW

logger = logging.getLogger("handlers.client_price")


async def start_order_reassignment(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    try:
        _, raw = callback.data.split(":", maxsplit=1)
        order_id = int(raw)
    except (ValueError, IndexError):
        await callback.answer("Некорректный заказ.", show_alert=True)
        return

    internal_user_id = await get_internal_user_id(callback.from_user.id)
    order = await get_order_with_client(order_id)

    if (
        internal_user_id is None
        or order is None
        or int(order["user_id"]) != internal_user_id
        or str(order["status"]) != ORDER_STATUS_NEW
        or str(order["assignment_status"]) not in {"waiting_selection", "open"}
    ):
        await callback.answer(
            "Заказ сейчас недоступен для перевыбора исполнителя.",
            show_alert=True,
        )
        return

    excluded = await get_excluded_master_ids(order_id)
    direction = str(
        order["direction"]
        or ""
    ).strip()

    if not direction:
        await callback.answer(
            "У заказа не указано направление.",
            show_alert=True,
        )
        return

    total_count = await get_active_masters_count(
        exclude_telegram_ids=excluded,
        direction=direction,
    )

    if total_count == 0:
        await callback.answer(
            "Других доступных исполнителей сейчас нет.",
            show_alert=True,
        )
        return

    masters = await get_active_masters(
        limit=10,
        offset=0,
        exclude_telegram_ids=excluded,
        direction=direction,
    )
    total_pages = max((total_count + 9) // 10, 1)

    await state.set_state(OrderState.waiting_for_master)
    await state.set_data(
        {
            "reassign_order_id": order_id,
            "assignment_mode": "specific",
            "direction": direction,
        }
    )

    await callback.message.answer(
        "👨‍💻 <b>Выберите другого исполнителя</b>\n\n"
        "Исполнители, с которыми по этому заказу уже не удалось "
        "продолжить работу, исключены из списка.",
        reply_markup=masters_selection_keyboard(
            masters=masters,
            current_page=0,
            total_pages=total_pages,
            is_reassign=True,
        ),
        parse_mode="HTML",
    )
    await callback.answer()

async def switch_order_to_free_master(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    try:
        _, raw = callback.data.split(":", maxsplit=1)
        order_id = int(raw)
    except (ValueError, IndexError):
        await callback.answer("Некорректный заказ.", show_alert=True)
        return

    internal_user_id = await get_internal_user_id(callback.from_user.id)
    order = await get_order_with_client(order_id)

    if (
        internal_user_id is None
        or order is None
        or int(order["user_id"]) != internal_user_id
    ):
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    if str(order["status"]) != ORDER_STATUS_NEW:
        await callback.answer(
            "Заказ уже нельзя переназначить.",
            show_alert=True,
        )
        return

    assignment_status = str(order["assignment_status"])

    if assignment_status == "open":
        await callback.answer(
            "Заказ уже ожидает свободного исполнителя.",
            show_alert=True,
        )
        return

    if assignment_status != "waiting_selection":
        await callback.answer(
            "Заказ сейчас нельзя перевести в режим «Свободный исполнитель».",
            show_alert=True,
        )
        return

    excluded = await get_excluded_master_ids(order_id)
    direction = str(
        order["direction"]
        or ""
    ).strip()

    if not direction:
        await callback.answer(
            "У заказа не указано направление.",
            show_alert=True,
        )
        return

    eligible_count = await get_active_masters_count(
        exclude_telegram_ids=excluded,
        direction=direction,
    )

    if eligible_count == 0:
        await callback.answer(
            "Сейчас не осталось доступных исполнителей для этого заказа.",
            show_alert=True,
        )
        return

    changed, captured_generation = await set_order_open_for_free_master(
        order_id=order_id,
        client_telegram_id=callback.from_user.id,
        capture_generation=True,
    )

    if not changed:
        await callback.answer(
            "Состояние заказа уже изменилось.",
            show_alert=True,
        )
        return

    # ORD-02: фиксируем генерацию свободного режима до Telegram-операции.
    try:
        await create_order_assignment_topic(
            bot=bot,
            order_id=order_id,
            expected_assignment_generation=captured_generation,
        )
    except Exception:
        logger.exception(
            "Не удалось создать тему свободного заказа %s",
            order_id,
        )
        await release_assignment_after_topic_failure(
            order_id=order_id,
            master_telegram_id=None,
            captured_assignment_generation=captured_generation,
        )
        await state.clear()
        await callback.answer(
            "Создать рабочую тему не удалось. "
            "Заказ возвращён к выбору исполнителя.",
            show_alert=True,
        )
        return

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            user_id=internal_user_id,
            action="assignment_mode_changed",
            entity_type="order",
            entity_id=order_id,
            details={"mode": "free_master"},
        )
    except Exception:
        logger.exception(
            "Не удалось записать свободный режим заказа %s",
            order_id,
        )

    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        f"🟢 Заказ <b>№{order_id}</b> открыт для свободного исполнителя.\n\n"
        "Первый подходящий исполнитель, который возьмёт заказ, "
        "будет закреплён за ним.",
        parse_mode="HTML",
    )
    await callback.answer("Ожидаем свободного исполнителя.")

async def cancel_order_after_failed_assignment(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
) -> None:
    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    try:
        _, raw = callback.data.split(":", maxsplit=1)
        order_id = int(raw)
    except (ValueError, IndexError):
        await callback.answer("Некорректный заказ.", show_alert=True)
        return

    internal_user_id = await get_internal_user_id(callback.from_user.id)
    order = await get_order_with_client(order_id)

    if (
        internal_user_id is None
        or order is None
        or int(order["user_id"]) != internal_user_id
    ):
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    if (
        str(order["status"]) != ORDER_STATUS_NEW
        or str(order["assignment_status"]) not in {"waiting_selection", "open"}
    ):
        await callback.answer(
            "Заказ уже изменился или сейчас не может быть отменён.",
            show_alert=True,
        )
        return

    cancelled = await cancel_unpaid_order_by_client(
        order_id=order_id,
        client_telegram_id=callback.from_user.id,
    )

    if not cancelled:
        await callback.answer(
            "Заказ уже изменился или больше не может быть отменён этим способом.",
            show_alert=True,
        )
        return

    await archive_order_topic(
        bot=bot,
        group_chat_id=order["group_chat_id"],
        topic_id=order["topic_id"],
        order_id=order_id,
    )

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            user_id=internal_user_id,
            action="order_cancelled",
            entity_type="order",
            entity_id=order_id,
            details={
                "cancelled_by": "client",
                "reason": "client_cancelled_after_failed_assignment",
            },
        )
    except Exception:
        logger.exception(
            "Не удалось записать отмену заказа %s",
            order_id,
        )

    await state.clear()

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        f"❌ Заказ <b>№{order_id}</b> отменён.",
        parse_mode="HTML",
    )
    await callback.answer("Заказ отменён.")
