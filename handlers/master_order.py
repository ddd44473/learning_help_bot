import html
import logging

from aiogram import F, Router, Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.types import (
    CallbackQuery,
)

from config import MASTER_GROUP_ID
from database.orders import (
    cancel_new_order_by_master,
    claim_open_order,
    get_order_with_client,
    update_order_status,
)
from keyboards.master_order import master_order_status_keyboard
from utils.order_statuses import (
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_COMPLETED,
    ORDER_STATUS_IN_PROGRESS,
    ORDER_STATUS_NEW,
    ORDER_STATUS_TITLES,
    ORDER_STATUS_WAITING_CLIENT,
)
from utils.order_card import (
    close_and_record_topic,
    archive_order_topic,
    refresh_order_card,
)
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from keyboards.my_orders import (
    completion_confirmation_keyboard,
)
from keyboards.price_offers import (
    order_reassignment_keyboard,
)
from states.master_price import MasterPriceState
from states.master_order import MasterOrderState
from database.payments import (
    REFUND_RESULT_ALREADY_REFUNDED,
    REFUND_RESULT_NOT_AVAILABLE,
    REFUND_RESULT_ORDER_NOT_FOUND,
    REFUND_RESULT_SUCCESS,
    cancel_paid_order_with_refund,
    refund_paid_order_for_master_reassignment,
)
from database.activity_logs import log_user_activity
from database.masters import (
    get_master_by_telegram_id_any,
    is_master_available,
    master_has_direction,
)
from utils.master_profile import (
    master_public_name as _master_public_name,
    master_group_mention as _master_group_mention,
    claimed_master_client_keyboard as _claimed_master_client_keyboard,
    claimed_master_profile_keyboard as _claimed_master_profile_keyboard,
    build_claimed_master_profile_text as _build_claimed_master_profile_text,
)
from utils import master_profile as _master_profile

# Keep the historical helper import available without duplicating rendering.
_format_public_response_time = _master_profile.format_public_response_time


from handlers.master_order_access import (
    _ensure_operational_master_callback,
    _is_admin,
)
from handlers.master_order_pricing import (
    master_accept_client_price,
    master_reject_client_price,
    process_master_price_offer,
    start_master_price_offer,
)


router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(
    F.data.startswith("claimed_master_profile:")
)
async def show_claimed_master_profile(
    callback: CallbackQuery,
) -> None:
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
            "Некорректные данные исполнителя.",
            show_alert=True,
        )
        return

    text = await _build_claimed_master_profile_text(
        master_telegram_id
    )

    if text is None:
        await callback.answer(
            "Профиль исполнителя сейчас недоступен.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=(
            _claimed_master_profile_keyboard(
                master_telegram_id
            )
        ),
    )

    await callback.answer()






ALLOWED_STATUSES = {
    ORDER_STATUS_IN_PROGRESS,
    ORDER_STATUS_WAITING_CLIENT,
    ORDER_STATUS_CANCELLED,
}


ALLOWED_TRANSITIONS = {
    ORDER_STATUS_NEW: {
        ORDER_STATUS_IN_PROGRESS,
        ORDER_STATUS_CANCELLED,
    },
    ORDER_STATUS_IN_PROGRESS: {
        ORDER_STATUS_WAITING_CLIENT,
        ORDER_STATUS_CANCELLED,
    },
    # waiting_client теперь означает:
    # "работа готова и ждёт решения клиента".
    # Исполнитель не может менять этот статус.
    ORDER_STATUS_WAITING_CLIENT: set(),
    ORDER_STATUS_COMPLETED: set(),
    ORDER_STATUS_CANCELLED: set(),
}


CLIENT_STATUS_MESSAGES = {
    ORDER_STATUS_IN_PROGRESS: (
        "🔵 <b>Заказ принят в работу</b>\n\n"
        "Исполнитель продолжает работу над вашим заказом."
    ),
    ORDER_STATUS_WAITING_CLIENT: (
        "✅ <b>Исполнитель сообщил о готовности работы</b>\n\n"
        "Проверьте результат и подтвердите завершение заказа. "
        "Если требуется доработка, верните заказ в работу."
    ),
    ORDER_STATUS_CANCELLED: (
        "❌ <b>Заказ отменён</b>\n\n"
        "Работа по этому заказу была отменена."
    ),
}


@router.callback_query(F.data == "master_status_info")
async def show_current_status(
    callback: CallbackQuery,
) -> None:
    if not await _ensure_operational_master_callback(
        callback,
        allow_admin=True,
    ):
        return

    await callback.answer(
        "Это текущий статус заказа."
    )


@router.callback_query(
    F.data.startswith("claim_free_order:")
)
async def claim_free_order_handler(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    if not await _ensure_operational_master_callback(
        callback
    ):
        return

    """Первый подходящий исполнитель атомарно забирает свободный заказ."""

    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    if callback.message.chat.id != MASTER_GROUP_ID:
        await callback.answer(
            "Взять заказ можно только в рабочей группе.",
            show_alert=True,
        )
        return

    try:
        _, raw = callback.data.split(":", maxsplit=1)
        order_id = int(raw)
    except (ValueError, IndexError):
        await callback.answer("Некорректный заказ.", show_alert=True)
        return

    order = await get_order_with_client(order_id)
    if order is None:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    if (
        order["group_chat_id"] is None
        or int(order["group_chat_id"]) != callback.message.chat.id
        or order["topic_id"] is None
        or (
            callback.message.message_thread_id is not None
            and int(order["topic_id"]) != callback.message.message_thread_id
        )
    ):
        await callback.answer(
            "Эта кнопка относится к другой теме.",
            show_alert=True,
        )
        return

    direction = str(
        order["direction"]
        or ""
    ).strip()

    if (
        not direction
        or not await master_has_direction(
            telegram_id=callback.from_user.id,
            direction=direction,
        )
    ):
        await callback.answer(
            "Этот заказ недоступен для ваших направлений.",
            show_alert=True,
        )
        return

    claimed = await claim_open_order(
        order_id=order_id,
        master_telegram_id=callback.from_user.id,
    )
    if not claimed:
        await callback.answer(
            "Заказ уже взял другой исполнитель или он больше недоступен вам.",
            show_alert=True,
        )
        return

    try:
        await refresh_order_card(bot=bot, order_id=order_id)
    except Exception:
        logger.exception("Не удалось обновить карточку свободного заказа %s", order_id)

    # После успешного claim берём публичное имя и Telegram username
    # из таблицы masters. Для клиента и рабочей группы используются
    # разные представления одного исполнителя.
    master = await get_master_by_telegram_id_any(
        callback.from_user.id
    )

    public_master_name = _master_public_name(
        master,
        callback.from_user.full_name,
    )

    group_master_mention = _master_group_mention(
        master,
        callback.from_user.id,
        callback.from_user.full_name,
    )

    try:
        await callback.message.answer(
            "🔔 <b>Заказ закреплён за исполнителем</b>\n\n"
            f"Заказ: <b>№{order_id}</b>\n"
            f"Исполнитель: {group_master_mention}\n\n"
            "Вам назначен этот заказ. "
            "Ознакомьтесь с заданием и предложите стоимость.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(
            "Не удалось отправить mention исполнителя "
            "после взятия заказа %s",
            order_id,
        )

    client_telegram_id = order["client_telegram_id"]

    if client_telegram_id is not None:
        try:
            await bot.send_message(
                chat_id=int(
                    client_telegram_id
                ),
                text=(
                    "✅ <b>Свободный заказ принят</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n\n"
                    "Ваш заказ будет выполнять "
                    f"<b>{html.escape(public_master_name)}</b>.\n\n"
                    "Вы можете открыть профиль исполнителя, "
                    "посмотреть отзывы о его работе или "
                    "написать ему по заказу через сервис."
                ),
                reply_markup=(
                    _claimed_master_client_keyboard(
                        order_id=order_id,
                        master_telegram_id=(
                            callback.from_user.id
                        ),
                    )
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Не удалось уведомить клиента "
                "о взятии заказа %s",
                order_id,
            )

    try:
        await log_user_activity(
            telegram_id=int(client_telegram_id) if client_telegram_id is not None else callback.from_user.id,
            user_id=int(order["user_id"]),
            action="master_selected",
            entity_type="order",
            entity_id=order_id,
            details={
                "master_telegram_id": callback.from_user.id,
                "master_username": (
                    master["username"]
                    if master is not None
                    else callback.from_user.username
                ),
                "master_full_name": public_master_name,
                "selection_mode": "free_master_claim",
            },
        )
    except Exception:
        logger.exception("Не удалось записать взятие свободного заказа %s", order_id)

    await callback.answer("Заказ закреплён за вами.")


@router.callback_query(
    F.data.startswith("master_decline_order:")
)
async def master_decline_order(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """
    Запрашивает у назначенного исполнителя обязательную причину отказа.

    Сам заказ на этом шаге ещё не изменяется. Отказ применяется только
    после того, как исполнитель отправит причину отдельным сообщением.
    """

    if not await _ensure_operational_master_callback(
        callback
    ):
        return

    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    if callback.message.chat.id != MASTER_GROUP_ID:
        await callback.answer(
            "Отказаться от заказа можно только в рабочей группе.",
            show_alert=True,
        )
        return

    try:
        _, raw = callback.data.split(":", maxsplit=1)
        order_id = int(raw)
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный заказ.",
            show_alert=True,
        )
        return

    order = await get_order_with_client(order_id)

    if order is None:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    if (
        order["group_chat_id"] is None
        or int(order["group_chat_id"]) != callback.message.chat.id
        or order["topic_id"] is None
        or (
            callback.message.message_thread_id is not None
            and int(order["topic_id"])
            != callback.message.message_thread_id
        )
    ):
        await callback.answer(
            "Эта кнопка относится к другой теме.",
            show_alert=True,
        )
        return

    assigned_master_telegram_id = order[
        "assigned_master_telegram_id"
    ]

    if (
        assigned_master_telegram_id is None
        or int(assigned_master_telegram_id)
        != callback.from_user.id
    ):
        await callback.answer(
            "Этот заказ закреплён за другим исполнителем.",
            show_alert=True,
        )
        return

    if str(order["status"]) not in {
        ORDER_STATUS_NEW,
        ORDER_STATUS_IN_PROGRESS,
    }:
        await callback.answer(
            "Отказаться от завершённого или отменённого заказа нельзя.",
            show_alert=True,
        )
        return

    await state.set_state(
        MasterOrderState.waiting_for_decline_reason
    )
    await state.set_data(
        {
            "decline_order_id": order_id,
            "decline_assignment_generation": int(order["assignment_generation"]),
            "decline_group_chat_id": callback.message.chat.id,
            "decline_topic_id": order["topic_id"],
            "decline_source_message_id": callback.message.message_id,
        }
    )

    await callback.message.answer(
        "❌ <b>Отказ от заказа "
        f"№{order_id}</b>\n\n"
        "Напишите <b>одним сообщением причину отказа</b>. "
        "Эту причину увидит клиент.\n\n"
        "Например: <i>Не успеваю выполнить работу до указанного срока.</i>\n\n"
        "Чтобы не отказываться от заказа, отправьте <code>/cancel</code>.",
        parse_mode="HTML",
    )

    await callback.answer(
        "Напишите причину отказа."
    )


@router.message(
    MasterOrderState.waiting_for_decline_reason
)
async def master_decline_reason_received(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    """
    Принимает обязательную причину отказа и только после этого
    освобождает заказ от текущего исполнителя.
    """

    data = await state.get_data()

    order_id_raw = data.get("decline_order_id")
    group_chat_id_raw = data.get("decline_group_chat_id")
    topic_id_raw = data.get("decline_topic_id")
    source_message_id_raw = data.get("decline_source_message_id")

    try:
        order_id = int(order_id_raw)
        group_chat_id = int(group_chat_id_raw)
        topic_id = int(topic_id_raw)
    except (TypeError, ValueError):
        await state.clear()
        await message.answer(
            "Не удалось определить заказ. Нажмите «Отказаться» ещё раз."
        )
        return

    if (
        message.chat.id != group_chat_id
        or message.message_thread_id is None
        or int(message.message_thread_id) != topic_id
    ):
        await message.answer(
            "Причину отказа нужно отправить в той же теме заказа, "
            "где вы нажали кнопку «Отказаться»."
        )
        return

    raw_reason = (message.text or "").strip()

    if raw_reason.lower() == "/cancel":
        await state.clear()
        await message.answer(
            f"Отказ от заказа №{order_id} отменён. Заказ остаётся за вами."
        )
        return

    if not raw_reason:
        await message.answer(
            "Напишите причину отказа текстом. Она будет показана клиенту."
        )
        return

    if len(raw_reason) > 1000:
        await message.answer(
            "Причина слишком длинная. Сократите её до 1000 символов."
        )
        return

    if not await is_master_available(message.from_user.id):
        await state.clear()
        await message.answer(
            "⛔ Рабочее действие недоступно. "
            "Для исполнителя требуется активная карточка."
        )
        return

    # Повторно читаем заказ: между нажатием кнопки и отправкой причины
    # его состояние могло измениться.
    order = await get_order_with_client(order_id)

    if order is None:
        await state.clear()
        await message.answer(
            "Заказ не найден."
        )
        return

    if data.get("decline_assignment_generation") != int(order["assignment_generation"]):
        await state.clear()
        await message.answer("Назначение изменилось. Нажмите «Отказаться» ещё раз.")
        return

    assigned_master_telegram_id = order[
        "assigned_master_telegram_id"
    ]

    if (
        assigned_master_telegram_id is None
        or int(assigned_master_telegram_id)
        != message.from_user.id
    ):
        await state.clear()
        await message.answer(
            "Этот заказ уже не закреплён за вами."
        )
        return

    if (
        order["group_chat_id"] is None
        or int(order["group_chat_id"]) != group_chat_id
        or order["topic_id"] is None
        or int(order["topic_id"]) != topic_id
    ):
        await state.clear()
        await message.answer(
            "Состояние темы заказа изменилось. Нажмите «Отказаться» ещё раз."
        )
        return

    current_status = str(order["status"])
    client_telegram_id = order["client_telegram_id"]

    refund_amount: int | None = None
    refund_balance: int | None = None
    refund_bonus_amount = int(order["bonus_used"] or 0)

    # -------------------------------------------------
    # Отказ ДО оплаты.
    # -------------------------------------------------

    if current_status == ORDER_STATUS_NEW:
        try:
            released = await cancel_new_order_by_master(
                order_id=order_id,
                master_telegram_id=message.from_user.id,
                decline_reason=raw_reason,
                expected_assignment_generation=int(order["assignment_generation"]),
            )
        except Exception:
            logger.exception(
                "Ошибка отказа исполнителя %s от нового заказа %s",
                message.from_user.id,
                order_id,
            )
            await message.answer(
                "Не удалось отказаться от заказа. Попробуйте отправить причину ещё раз."
            )
            return

        if not released:
            await state.clear()
            await message.answer(
                "Состояние заказа уже изменилось."
            )
            return

    # -------------------------------------------------
    # Отказ ПОСЛЕ оплаты.
    # -------------------------------------------------

    elif current_status == ORDER_STATUS_IN_PROGRESS:
        try:
            (
                refund_result,
                refund_balance,
                refund_amount,
            ) = await refund_paid_order_for_master_reassignment(
                order_id=order_id,
                master_telegram_id=message.from_user.id,
                decline_reason=raw_reason,
                expected_assignment_generation=int(order["assignment_generation"]),
            )
        except Exception:
            logger.exception(
                "Ошибка возврата средств и освобождения заказа %s "
                "после отказа исполнителя %s",
                order_id,
                message.from_user.id,
            )
            await message.answer(
                "Не удалось отказаться от заказа и вернуть средства. "
                "Попробуйте отправить причину ещё раз."
            )
            return

        if refund_result == REFUND_RESULT_ORDER_NOT_FOUND:
            await state.clear()
            await message.answer(
                "Заказ не найден."
            )
            return

        if refund_result == REFUND_RESULT_ALREADY_REFUNDED:
            await state.clear()
            await message.answer(
                "Средства по этой оплате уже были возвращены."
            )
            return

        if refund_result == REFUND_RESULT_NOT_AVAILABLE:
            await state.clear()
            await message.answer(
                "Этот заказ сейчас нельзя вернуть на перевыбор исполнителя."
            )
            return

        if refund_result != REFUND_RESULT_SUCCESS:
            await message.answer(
                "Не удалось выполнить возврат средств. Попробуйте ещё раз."
            )
            return

    else:
        await state.clear()
        await message.answer(
            "Отказаться от завершённого или отменённого заказа нельзя."
        )
        return

    # БД успешно обновлена. После этой точки FSM можно завершить.
    await state.clear()

    # Убираем рабочие кнопки с исходной карточки заказа.
    if source_message_id_raw is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=group_chat_id,
                message_id=int(source_message_id_raw),
                reply_markup=None,
            )
        except (TelegramBadRequest, ValueError, TypeError):
            pass
        except Exception:
            logger.exception(
                "Не удалось убрать кнопки после отказа по заказу %s",
                order_id,
            )

    try:
        await log_user_activity(
            telegram_id=message.from_user.id,
            action="master_declined_order",
            entity_type="order",
            entity_id=order_id,
            details={
                "master_telegram_id": message.from_user.id,
                "reason": (
                    "master_declined_after_payment"
                    if refund_amount is not None
                    else "master_declined_order"
                ),
                "decline_reason": raw_reason,
                "refund_amount": refund_amount,
                "refund_bonus_amount": (
                    refund_bonus_amount
                    if refund_amount is not None
                    else 0
                ),
            },
        )
    except Exception:
        logger.exception(
            "Не удалось записать отказ исполнителя от заказа %s",
            order_id,
        )

    if (
        refund_amount is not None
        and client_telegram_id is not None
    ):
        try:
            await log_user_activity(
                telegram_id=int(client_telegram_id),
                user_id=int(order["user_id"]),
                action="balance_refunded",
                entity_type="order",
                entity_id=order_id,
                details={
                    "amount": int(refund_amount),
                    "bonus_restored": refund_bonus_amount,
                    "balance_before": (
                        int(refund_balance) - int(refund_amount)
                        if refund_balance is not None
                        else None
                    ),
                    "balance_after": (
                        int(refund_balance)
                        if refund_balance is not None
                        else None
                    ),
                    "reason": "master_declined_after_payment",
                    "decline_reason": raw_reason,
                    "initiated_by": "master",
                    "master_telegram_id": message.from_user.id,
                },
            )
        except Exception:
            logger.exception(
                "Не удалось записать возврат после отказа исполнителя "
                "по заказу %s",
                order_id,
            )

    safe_reason = html.escape(raw_reason)

    try:
        master_text = (
            "❌ <b>Вы отказались от заказа</b>\n\n"
            f"Заказ <b>№{order_id}</b> возвращён клиенту "
            "для выбора другого исполнителя.\n\n"
            f"<b>Указанная причина:</b>\n{safe_reason}"
        )

        if refund_amount is not None:
            master_text += (
                "\n\n"
                f"Клиенту возвращено <b>{refund_amount} ₽</b> "
                "на обычный баланс."
            )
            if refund_bonus_amount > 0:
                master_text += (
                    f"\nТакже восстановлено бонусов: "
                    f"<b>{refund_bonus_amount} ₽</b>."
                )

        await message.answer(
            master_text,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception(
            "Не удалось отправить подтверждение отказа по заказу %s",
            order_id,
        )

    await archive_order_topic(
        bot=bot,
        group_chat_id=order["group_chat_id"],
        topic_id=order["topic_id"],
        order_id=order_id,
    )

    if client_telegram_id is not None:
        try:
            if refund_amount is None:
                client_text = (
                    "❌ <b>Исполнитель отказался от заказа</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n\n"
                    f"<b>Причина отказа:</b>\n{safe_reason}\n\n"
                    "Заказ не отменён. Выберите другого исполнителя, "
                    "откройте его для свободного исполнителя или отмените заказ."
                )
            else:
                client_text = (
                    "❌ <b>Исполнитель отказался от заказа</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n"
                    f"💰 Возвращено на баланс: <b>{refund_amount} ₽</b>\n"
                    f"💳 Текущий баланс: <b>{refund_balance} ₽</b>\n"
                    + (
                        f"🎁 Восстановлено бонусов: <b>{refund_bonus_amount} ₽</b>\n"
                        if refund_bonus_amount > 0
                        else ""
                    )
                    + "\n"
                    f"<b>Причина отказа:</b>\n{safe_reason}\n\n"
                    "Сам заказ сохранён. Выберите другого исполнителя, "
                    "режим «Свободный исполнитель» или отмените заказ. "
                    "С новым исполнителем стоимость будет согласована заново."
                )

            await bot.send_message(
                chat_id=int(client_telegram_id),
                text=client_text,
                reply_markup=order_reassignment_keyboard(order_id),
                parse_mode="HTML",
            )
        except TelegramForbiddenError:
            logger.warning(
                "Клиент %s заблокировал бота",
                client_telegram_id,
            )
        except Exception:
            logger.exception(
                "Не удалось уведомить клиента по заказу %s",
                order_id,
            )

router.callback_query(
    F.data.startswith("offer_price:")
)(start_master_price_offer)


router.message(
    MasterPriceState.waiting_for_price
)(process_master_price_offer)


@router.callback_query(
    F.data.startswith("master_status:")
)
async def change_order_status(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    if not await _ensure_operational_master_callback(
        callback,
        allow_admin=True,
    ):
        return

    """
    Изменяет статус оплаченного заказа.

    Управлять заказом может только исполнитель,
    которого клиент выбрал при оформлении.

    При отмене оплаченного заказа:
    - возвращает клиенту средства;
    - записывает возврат в историю транзакций;
    - переводит заказ в cancelled атомарно.

    После успешного изменения статуса
    действие записывается в журнал активности.
    """

    if callback.message is None:
        await callback.answer(
            "Сообщение заказа недоступно.",
            show_alert=True,
        )
        return

    if callback.data is None:
        await callback.answer(
            "Некорректные данные кнопки.",
            show_alert=True,
        )
        return

    if callback.message.chat.id != MASTER_GROUP_ID:
        await callback.answer(
            "Управление заказом доступно "
            "только в рабочей группе.",
            show_alert=True,
        )
        return

    try:
        _, order_id_raw, new_status = (
            callback.data.split(
                ":",
                maxsplit=2,
            )
        )

        order_id = int(order_id_raw)

    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные кнопки.",
            show_alert=True,
        )
        return

    if new_status not in ALLOWED_STATUSES:
        await callback.answer(
            "Неизвестный статус заказа.",
            show_alert=True,
        )
        return

    order = await get_order_with_client(
        order_id
    )

    is_admin_actor = _is_admin(
        callback.from_user.id
    )

    if order is None:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    if (
        order["group_chat_id"]
        != callback.message.chat.id
    ):
        await callback.answer(
            "Эта кнопка не относится "
            "к данной группе.",
            show_alert=True,
        )
        return

    order_topic_id = order[
        "topic_id"
    ]

    if order_topic_id is None:
        await callback.answer(
            "У заказа не найдена связанная тема.",
            show_alert=True,
        )
        return

    message_thread_id = (
        callback.message.message_thread_id
    )

    if (
        message_thread_id is not None
        and int(order_topic_id)
        != message_thread_id
    ):
        await callback.answer(
            "Эта кнопка не относится "
            "к данной теме.",
            show_alert=True,
        )
        return

    assigned_master_telegram_id = order[
        "assigned_master_telegram_id"
    ]

    if (
        assigned_master_telegram_id is None
        and not is_admin_actor
    ):
        await callback.answer(
            "У заказа не указан "
            "выбранный исполнитель.",
            show_alert=True,
        )
        return

    if (
        not is_admin_actor
        and assigned_master_telegram_id is not None
        and int(assigned_master_telegram_id)
        != callback.from_user.id
    ):
        await callback.answer(
            "Этот заказ закреплён "
            "за другим исполнителем.",
            show_alert=True,
        )
        return

    current_status = str(
        order["status"]
    )

    # Новый заказ ещё проходит согласование
    # стоимости и оплату.
    if current_status == ORDER_STATUS_NEW:
        await callback.answer(
            "Сначала необходимо согласовать "
            "стоимость и получить оплату заказа.",
            show_alert=True,
        )
        return

    if current_status == new_status:
        await callback.answer(
            "У заказа уже установлен "
            "этот статус.",
            show_alert=True,
        )
        return

    allowed_next_statuses = (
        ALLOWED_TRANSITIONS.get(
            current_status,
            set(),
        )
    )

    if new_status not in allowed_next_statuses:
        current_label = (
            ORDER_STATUS_TITLES.get(
                current_status,
                current_status,
            )
        )

        new_label = (
            ORDER_STATUS_TITLES.get(
                new_status,
                new_status,
            )
        )

        await callback.answer(
            f"Нельзя изменить статус "
            f"«{current_label}» "
            f"на «{new_label}».",
            show_alert=True,
        )
        return

    # Если заказ отменяется после оплаты,
    # здесь будут сохранены данные возврата.
    refund_amount: int | None = None
    refund_balance: int | None = None
    refund_bonus_amount = int(order["bonus_used"] or 0)

    try:
        if new_status == ORDER_STATUS_CANCELLED:
            (
                refund_result,
                refund_balance,
                refund_amount,
            ) = await cancel_paid_order_with_refund(
                order_id=order_id,
                actor_telegram_id=callback.from_user.id,
                expected_assignment_generation=int(order["assignment_generation"]),
            )

            if (
                refund_result
                == REFUND_RESULT_ORDER_NOT_FOUND
            ):
                await callback.answer(
                    "Заказ не найден.",
                    show_alert=True,
                )
                return

            if (
                refund_result
                == REFUND_RESULT_ALREADY_REFUNDED
            ):
                await callback.answer(
                    "Средства по этому заказу "
                    "уже были возвращены.",
                    show_alert=True,
                )
                return

            if (
                refund_result
                == REFUND_RESULT_NOT_AVAILABLE
            ):
                await callback.answer(
                    "Этот заказ сейчас нельзя "
                    "отменить с возвратом средств.",
                    show_alert=True,
                )
                return

            if (
                refund_result
                != REFUND_RESULT_SUCCESS
            ):
                await callback.answer(
                    "Не удалось выполнить возврат.",
                    show_alert=True,
                )
                return

        else:
            updated = await update_order_status(
                order_id=order_id,
                old_status=current_status,
                new_status=new_status,
                actor_telegram_id=callback.from_user.id,
                expected_assignment_generation=int(order["assignment_generation"]),
            )

            if not updated:
                await callback.answer(
                    "Статус заказа уже был изменён.",
                    show_alert=True,
                )
                return

    except Exception:
        logger.exception(
            "Ошибка изменения статуса "
            "заказа %s",
            order_id,
        )

        await callback.answer(
            "Во время изменения статуса "
            "произошла ошибка.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Изменение уже успешно сохранено в БД.
    # Записываем действие исполнителя в журнал.
    # -------------------------------------------------

    activity_details = {
        "old_status": current_status,
        "new_status": new_status,
        "actor_role": (
            "admin"
            if is_admin_actor
            else "master"
        ),
    }

    if (
        new_status == ORDER_STATUS_CANCELLED
        and refund_amount is not None
    ):
        activity_details[
            "refund_amount"
        ] = int(refund_amount)
        activity_details[
            "refund_bonus_amount"
        ] = refund_bonus_amount

        if refund_balance is not None:
            activity_details[
                "balance_after_refund"
            ] = int(refund_balance)

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            action="order_status_changed",
            entity_type="order",
            entity_id=order_id,
            details=activity_details,
        )

    except Exception:
        # Ошибка журнала не должна откатывать
        # уже выполненное изменение заказа.
        logger.exception(
            "Не удалось записать изменение "
            "статуса заказа %s в журнал активности.",
            order_id,
        )

    if (
        new_status == ORDER_STATUS_CANCELLED
        and refund_amount is not None
        and order["client_telegram_id"] is not None
    ):
        try:
            balance_before_refund = (
                int(refund_balance) - int(refund_amount)
                if refund_balance is not None
                else None
            )

            await log_user_activity(
                telegram_id=int(order["client_telegram_id"]),
                user_id=int(order["user_id"]),
                action="balance_refunded",
                entity_type="order",
                entity_id=order_id,
                details={
                    "amount": int(refund_amount),
                    "bonus_restored": refund_bonus_amount,
                    "balance_before": balance_before_refund,
                    "balance_after": (
                        int(refund_balance)
                        if refund_balance is not None
                        else None
                    ),
                    "reason": "order_cancelled",
                    "initiated_by": (
                        "admin"
                        if is_admin_actor
                        else "master"
                    ),
                    "master_telegram_id": (
                        int(assigned_master_telegram_id)
                        if assigned_master_telegram_id
                        is not None
                        else None
                    ),
                    "admin_telegram_id": (
                        callback.from_user.id
                        if is_admin_actor
                        else None
                    ),
                },
            )
        except Exception:
            logger.exception(
                "Не удалось записать возврат средств "
                "по заказу %s в журнал клиента",
                order_id,
            )

    # С этого момента изменение уже сохранено в БД.
    # Ошибки Telegram не должны отменять
    # изменение статуса или возврат.
    warnings: list[str] = []

    try:
        await callback.message.edit_reply_markup(
            reply_markup=(
                master_order_status_keyboard(
                    order_id=order_id,
                    current_status=new_status,
                )
            )
        )

    except TelegramBadRequest as error:
        logger.warning(
            "Не удалось обновить клавиатуру "
            "заказа %s: %s",
            order_id,
            error,
        )

    except Exception:
        logger.exception(
            "Неожиданная ошибка при "
            "обновлении клавиатуры заказа %s",
            order_id,
        )

    new_status_label = (
        ORDER_STATUS_TITLES.get(
            new_status,
            new_status,
        )
    )

    actor_name = html.escape(
        callback.from_user.full_name
    )

    actor_label = (
        "Администратор"
        if is_admin_actor
        else "Исполнитель"
    )

    # Сообщение в рабочую тему.
    try:
        status_text = (
            f"🔄 <b>Статус заказа №{order_id} "
            "изменён</b>\n\n"
            f"Новый статус: "
            f"<b>{new_status_label}</b>\n"
            f"{actor_label}: <b>{actor_name}</b>"
        )

        if (
            new_status == ORDER_STATUS_CANCELLED
            and refund_amount is not None
        ):
            status_text += (
                "\n\n"
                f"💰 Клиенту возвращено: "
                f"<b>{refund_amount} ₽</b>"
            )
            if refund_bonus_amount > 0:
                status_text += (
                    f"\n🎁 Восстановлено бонусов: "
                    f"<b>{refund_bonus_amount} ₽</b>"
                )

        await callback.message.answer(
            status_text,
            parse_mode="HTML",
        )

    except TelegramBadRequest as error:
        logger.warning(
            "Не удалось отправить сообщение "
            "об изменении статуса заказа %s: %s",
            order_id,
            error,
        )

    except Exception:
        logger.exception(
            "Неожиданная ошибка при отправке "
            "сообщения об изменении статуса "
            "заказа %s",
            order_id,
        )

    client_telegram_id = order[
        "client_telegram_id"
    ]

    if client_telegram_id is None:
        logger.warning(
            "У заказа %s отсутствует "
            "Telegram ID клиента.",
            order_id,
        )

        warnings.append(
            "уведомить клиента не удалось"
        )

    else:
        direction = html.escape(
            str(
                order["direction"]
                or "Не указано"
            )
        )

        client_status_message = (
            CLIENT_STATUS_MESSAGES.get(
                new_status
            )
        )

        if client_status_message is None:
            logger.warning(
                "Не найден текст уведомления "
                "для статуса %s",
                new_status,
            )

            warnings.append(
                "не найден текст уведомления "
                "для клиента"
            )

        else:
            refund_text = ""

            if (
                new_status
                == ORDER_STATUS_CANCELLED
                and refund_amount is not None
                and refund_balance is not None
            ):
                refund_text = (
                    "\n\n"
                    "💰 <b>Средства возвращены</b>\n"
                    f"Возвращено: "
                    f"<b>{refund_amount} ₽</b>\n"
                    f"Баланс: "
                    f"<b>{refund_balance} ₽</b>"
                )
                if refund_bonus_amount > 0:
                    refund_text += (
                        f"\n🎁 Восстановлено бонусов: "
                        f"<b>{refund_bonus_amount} ₽</b>"
                    )

            try:
                reply_markup = (
                    completion_confirmation_keyboard(
                        order_id
                    )
                    if new_status
                    == ORDER_STATUS_WAITING_CLIENT
                    else None
                )

                await bot.send_message(
                    chat_id=int(
                        client_telegram_id
                    ),
                    text=(
                        f"{client_status_message}\n\n"
                        f"Номер заказа: "
                        f"<b>№{order_id}</b>\n"
                        f"Направление: "
                        f"<b>{direction}</b>"
                        f"{refund_text}"
                    ),
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )

            except TelegramForbiddenError:
                logger.warning(
                    "Клиент %s заблокировал бота. "
                    "Заказ: %s",
                    client_telegram_id,
                    order_id,
                )

                warnings.append(
                    "клиент заблокировал бота"
                )

            except TelegramBadRequest as error:
                logger.warning(
                    "Не удалось уведомить клиента %s "
                    "об изменении заказа %s: %s",
                    client_telegram_id,
                    order_id,
                    error,
                )

                warnings.append(
                    "Telegram не смог "
                    "уведомить клиента"
                )

            except Exception:
                logger.exception(
                    "Неожиданная ошибка "
                    "при уведомлении клиента %s "
                    "по заказу %s",
                    client_telegram_id,
                    order_id,
                )

                warnings.append(
                    "при уведомлении клиента "
                    "произошла ошибка"
                )

    # Финальное completed теперь ставит только клиент.
    # Исполнитель архивирует тему здесь лишь при отмене.
    should_close_topic = (
        new_status == ORDER_STATUS_CANCELLED
    )

    if should_close_topic:
        archived_topic_name = (
            f"📦 Архив | Заказ №{order_id}"
        )

        try:
            await bot.edit_forum_topic(
                chat_id=(
                    callback.message.chat.id
                ),
                message_thread_id=int(
                    order_topic_id
                ),
                name=archived_topic_name,
            )

            logger.info(
                "Тема заказа переименована "
                "в архивную. Заказ: %s, "
                "группа: %s, тема: %s",
                order_id,
                callback.message.chat.id,
                order_topic_id,
            )

        except TelegramBadRequest as error:
            logger.warning(
                "Не удалось переименовать "
                "тему заказа %s: %s",
                order_id,
                error,
            )

            warnings.append(
                "тему не удалось переименовать"
            )

        except Exception:
            logger.exception(
                "Неожиданная ошибка при "
                "переименовании темы заказа %s",
                order_id,
            )

            warnings.append(
                "при переименовании темы "
                "произошла ошибка"
            )

        try:
            await close_and_record_topic(bot, callback.message.chat.id, int(order_topic_id))

            logger.info(
                "Тема заказа закрыта. "
                "Заказ: %s, "
                "группа: %s, тема: %s",
                order_id,
                callback.message.chat.id,
                order_topic_id,
            )

        except TelegramBadRequest as error:
            logger.warning(
                "Не удалось закрыть тему "
                "заказа %s: %s",
                order_id,
                error,
            )

            warnings.append(
                "тему заказа не удалось закрыть"
            )

        except Exception:
            logger.exception(
                "Неожиданная ошибка при "
                "закрытии темы заказа %s",
                order_id,
            )

            warnings.append(
                "при закрытии темы "
                "произошла ошибка"
            )

    if warnings:
        await callback.answer(
            "Статус обновлён, но "
            + "; ".join(warnings)
            + ".",
            show_alert=True,
        )
        return

    if (
        new_status == ORDER_STATUS_CANCELLED
        and refund_amount is not None
    ):
        await callback.answer(
            f"Заказ отменён. "
            f"Клиенту возвращено "
            f"{refund_amount} ₽."
        )
        return

    if should_close_topic:
        await callback.answer(
            "Статус обновлён. "
            "Тема переименована и закрыта."
        )
        return

    if new_status == ORDER_STATUS_WAITING_CLIENT:
        await callback.answer(
            "Работа отправлена клиенту на подтверждение."
        )
        return

    await callback.answer(
        "Статус заказа обновлён."
    )


router.callback_query(
    F.data.startswith("master_accept_price:")
)(master_accept_client_price)


router.callback_query(
    F.data.startswith("master_reject_price:")
)(master_reject_client_price)
