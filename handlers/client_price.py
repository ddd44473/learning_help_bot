import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
)
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from config import MASTER_GROUP_ID
from database.orders import (
    get_order_with_client,
)
from database.price_offers import (
    OFFERED_BY_CLIENT,
    OFFERED_BY_MASTER,
    PRICE_STATUS_WAITING_CLIENT,
    PRICE_STATUS_WAITING_MASTER_RESPONSE,
    accept_price_offer,
    create_price_offer,
    get_pending_price_offer,
    reject_price_negotiation,
)
from database.users import get_internal_user_id
from database.payments import (
    PAYMENT_RESULT_INSUFFICIENT_FUNDS,
    PAYMENT_RESULT_NOT_AVAILABLE,
    PAYMENT_RESULT_ORDER_NOT_FOUND,
    PAYMENT_RESULT_SUCCESS,
    pay_order_from_balance,
    get_order_payment_preview,
)
from utils.order_statuses import (
    ORDER_STATUS_NEW,
)
from utils.order_card import (
    close_and_record_topic,
    refresh_order_card,
)
from keyboards.price_offers import (
    master_counter_offer_keyboard,
)
from states.client_price import ClientPriceState
from utils.payment_preview import build_payment_preview_text, payment_keyboard_from_preview
from database.activity_logs import log_user_activity
from database.referrals import get_bonus_balance


from handlers.client_reassignment import (
    cancel_order_after_failed_assignment,
    start_order_reassignment,
    switch_order_to_free_master,
)


router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(
    F.data.startswith("accept_price:")
)
async def accept_master_price(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    """
    Клиент принимает актуальное предложение исполнителя.

    Итоговая стоимость сохраняется в orders.agreed_price,
    а переговоры переходят в состояние agreed.

    Дополнительно проверяется, что:
    - заказ принадлежит текущему клиенту;
    - предложение всё ещё актуально;
    - предложение создано исполнителем;
    - клиент действительно имеет право принять
      это предложение.

    После успешного принятия:
    - событие записывается в журнал активности;
    - клиенту показывается актуальный экран оплаты.
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
        _, order_id_raw, offer_id_raw = (
            callback.data.split(
                ":",
                maxsplit=2,
            )
        )

        order_id = int(
            order_id_raw
        )

        offer_id = int(
            offer_id_raw
        )

    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные предложения.",
            show_alert=True,
        )
        return

    internal_user_id = await get_internal_user_id(
        callback.from_user.id
    )

    if internal_user_id is None:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    order = await get_order_with_client(
        order_id
    )

    if order is None:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Проверяем владельца заказа.
    # -------------------------------------------------

    if (
        int(order["user_id"])
        != internal_user_id
    ):
        await callback.answer(
            "Это предложение относится "
            "к другому заказу.",
            show_alert=True,
        )
        return

    if (
        str(order["status"])
        != ORDER_STATUS_NEW
    ):
        await callback.answer(
            "Этот заказ уже не находится "
            "на этапе согласования цены.",
            show_alert=True,
        )
        return

    if (
        str(order["price_status"])
        != PRICE_STATUS_WAITING_CLIENT
    ):
        await callback.answer(
            "Это предложение уже неактуально.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Проверяем актуальное предложение.
    # -------------------------------------------------

    pending_offer = await get_pending_price_offer(
        order_id
    )

    if (
        pending_offer is None
        or int(
            pending_offer["id"]
        ) != offer_id
        or str(
            pending_offer["offered_by"]
        ) != OFFERED_BY_MASTER
    ):
        await callback.answer(
            "Это предложение уже неактуально.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Принимаем предложение.
    # -------------------------------------------------

    try:
        accepted, agreed_price = (
            await accept_price_offer(
                order_id=order_id,
                offer_id=offer_id,
                accepted_by=OFFERED_BY_CLIENT,
                accepted_by_telegram_id=(
                    callback.from_user.id
                ),
                expected_price_status=(
                    PRICE_STATUS_WAITING_CLIENT
                ),
            )
        )

    except Exception:
        logger.exception(
            "Ошибка принятия цены "
            "по заказу %s клиентом %s",
            order_id,
            callback.from_user.id,
        )

        await callback.answer(
            "Не удалось подтвердить стоимость.",
            show_alert=True,
        )
        return

    if (
        not accepted
        or agreed_price is None
    ):
        await callback.answer(
            "Предложение уже было изменено, "
            "обработано или больше недоступно.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Журнал активности.
    #
    # Цена уже действительно согласована в БД,
    # поэтому именно здесь фиксируем событие.
    # -------------------------------------------------

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            user_id=internal_user_id,
            action="price_accepted",
            entity_type="order",
            entity_id=order_id,
            details={
                "offer_id": offer_id,
                "amount": int(
                    agreed_price
                ),
                "offer_created_by": "master",
                "accepted_by": "client",
            },
        )

    except Exception:
        # Ошибка журнала не должна отменять
        # уже успешно согласованную стоимость.
        logger.exception(
            "Не удалось записать событие "
            "price_accepted для заказа %s",
            order_id,
        )

    # -------------------------------------------------
    # Убираем устаревшие кнопки предложения.
    # -------------------------------------------------

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None,
        )

    except TelegramBadRequest as error:
        logger.warning(
            "Не удалось удалить кнопки предложения "
            "по заказу %s: %s",
            order_id,
            error,
        )

    # -------------------------------------------------
    # Показываем актуальный расчёт оплаты.
    # -------------------------------------------------

    preview = await get_order_payment_preview(
        order_id=order_id,
        user_id=internal_user_id,
    )

    if preview is None:
        await callback.answer(
            "Не удалось подготовить оплату заказа.",
            show_alert=True,
        )
        return

    direction = html.escape(
        str(order["direction"] or "Не указано")
    )

    await callback.message.answer(
        "✅ <b>Стоимость согласована</b>\n\n"
        f"Заказ: <b>№{order_id}</b>\n"
        f"Направление: <b>{direction}</b>\n"
        f"Итоговая стоимость: <b>{agreed_price} ₽</b>\n\n"
        f"{build_payment_preview_text(preview)}",
        parse_mode="HTML",
        reply_markup=payment_keyboard_from_preview(
            order_id,
            preview,
        ),
    )

    # -------------------------------------------------
    # Уведомляем исполнителя.
    # -------------------------------------------------

    topic_id = order[
        "topic_id"
    ]

    if topic_id is not None:
        try:
            await bot.send_message(
                chat_id=MASTER_GROUP_ID,
                message_thread_id=int(
                    topic_id
                ),
                text=(
                    "✅ <b>Клиент согласился "
                    "на стоимость</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n"
                    f"Итоговая цена: "
                    f"<b>{agreed_price} ₽</b>\n\n"
                    "Теперь ожидается оплата заказа."
                ),
                parse_mode="HTML",
            )

        except Exception:
            logger.exception(
                "Не удалось уведомить исполнителя "
                "о согласовании цены заказа %s",
                order_id,
            )

    await callback.answer(
        "Стоимость согласована."
    )


@router.callback_query(
    F.data.startswith("refresh_order_payment:")
)
async def refresh_order_payment(
    callback: CallbackQuery,
) -> None:
    """
    Повторно проверяет баланс клиента
    и обновляет клавиатуру оплаты заказа.
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
        _, order_id_raw = callback.data.split(
            ":",
            maxsplit=1,
        )
        order_id = int(order_id_raw)

    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный номер заказа.",
            show_alert=True,
        )
        return

    internal_user_id = await get_internal_user_id(
        callback.from_user.id
    )

    if internal_user_id is None:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    order = await get_order_with_client(
        order_id
    )

    if order is None:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    if int(order["user_id"]) != internal_user_id:
        await callback.answer(
            "Этот заказ принадлежит другому пользователю.",
            show_alert=True,
        )
        return

    if (
        str(order["status"]) != "new"
        or str(order["price_status"]) != "agreed"
        or order["agreed_price"] is None
    ):
        await callback.answer(
            "Этот заказ сейчас недоступен для оплаты.",
            show_alert=True,
        )
        return

    agreed_price = int(
        order["agreed_price"]
    )

    preview = await get_order_payment_preview(
        order_id=order_id,
        user_id=internal_user_id,
    )

    if preview is None:
        await callback.answer(
            "Этот заказ сейчас недоступен для оплаты.",
            show_alert=True,
        )
        return

    direction = html.escape(
        str(order["direction"] or "Не указано")
    )

    try:
        await callback.message.edit_text(
            "💳 <b>Оплата заказа</b>\n\n"
            f"Заказ: <b>№{order_id}</b>\n"
            f"Направление: <b>{direction}</b>\n"
            f"Стоимость: <b>{agreed_price} ₽</b>\n\n"
            f"{build_payment_preview_text(preview)}",
            parse_mode="HTML",
            reply_markup=payment_keyboard_from_preview(
                order_id,
                preview,
            ),
        )
    except TelegramBadRequest as error:
        logger.warning(
            "Не удалось обновить экран оплаты заказа %s: %s",
            order_id,
            error,
        )
        await callback.answer("Баланс не изменился.")
        return

    if bool(preview["can_pay_without_bonus"]) or bool(
        preview["can_pay_with_bonus"]
    ):
        await callback.answer("Средств достаточно для оплаты.")
    else:
        best_price = (
            int(preview["cash_with_bonus"])
            if int(preview["max_bonus_available"]) > 0
            else int(preview["cash_without_bonus"])
        )
        missing_amount = max(
            best_price - int(preview["balance"]),
            0,
        )
        await callback.answer(
            f"Не хватает {missing_amount} ₽.",
            show_alert=True,
        )


@router.callback_query(
    F.data.startswith("pay_order:")
    | F.data.startswith("pay_order_bonus:")
)
async def pay_order(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    """
    Оплачивает заказ с внутреннего баланса клиента.

    При успешной оплате:
    - списывает согласованную стоимость;
    - сохраняет транзакцию;
    - переводит заказ в статус in_progress;
    - записывает событие оплаты в журнал активности;
    - уведомляет клиента и выбранного исполнителя;
    - обновляет основную карточку заказа.
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

    use_bonus = callback.data.startswith(
        "pay_order_bonus:"
    )

    try:
        _, order_id_raw = callback.data.split(
            ":",
            maxsplit=1,
        )

        order_id = int(
            order_id_raw
        )

    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный номер заказа.",
            show_alert=True,
        )
        return

    internal_user_id = await get_internal_user_id(
        callback.from_user.id
    )

    if internal_user_id is None:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    order = await get_order_with_client(
        order_id
    )

    if order is None:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    if (
        int(order["user_id"])
        != internal_user_id
    ):
        await callback.answer(
            "Этот заказ принадлежит "
            "другому пользователю.",
            show_alert=True,
        )
        return

    try:
        result, new_balance, paid_amount = (
            await pay_order_from_balance(
                order_id=order_id,
                user_id=internal_user_id,
                use_bonus=use_bonus,
            )
        )

    except Exception:
        logger.exception(
            "Ошибка оплаты заказа %s",
            order_id,
        )

        await callback.answer(
            "Во время оплаты произошла ошибка.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Заказ не найден
    # -------------------------------------------------

    if result == PAYMENT_RESULT_ORDER_NOT_FOUND:
        await callback.answer(
            "Заказ не найден или "
            "не принадлежит вам.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Недостаточно средств
    # -------------------------------------------------

    if result == PAYMENT_RESULT_INSUFFICIENT_FUNDS:
        current_balance = (
            int(new_balance)
            if new_balance is not None
            else 0
        )

        price = (
            int(paid_amount)
            if paid_amount is not None
            else 0
        )

        missing_amount = max(
            price - current_balance,
            0,
        )

        # Неудачная попытка оплаты тоже полезна
        # для аудита действий пользователя.
        try:
            await log_user_activity(
                telegram_id=callback.from_user.id,
                user_id=internal_user_id,
                action="payment_failed",
                entity_type="order",
                entity_id=order_id,
                details={
                    "reason": "insufficient_funds",
                    "required_amount": price,
                    "current_balance": current_balance,
                    "missing_amount": missing_amount,
                },
            )

        except Exception:
            logger.exception(
                "Не удалось записать событие "
                "payment_failed для заказа %s",
                order_id,
            )

        try:
            await callback.message.edit_reply_markup(
                reply_markup=(
                    payment_keyboard_from_preview(order_id, preview)
                    if (preview := await get_order_payment_preview(
                        order_id=order_id,
                        user_id=internal_user_id,
                    )) is not None
                    else None
                )
            )

        except TelegramBadRequest as error:
            logger.warning(
                "Не удалось обновить клавиатуру "
                "неоплаченного заказа %s: %s",
                order_id,
                error,
            )

        await callback.answer(
            f"Недостаточно средств. "
            f"Не хватает {missing_amount} ₽.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Заказ уже нельзя оплатить
    # -------------------------------------------------

    if result == PAYMENT_RESULT_NOT_AVAILABLE:
        await callback.answer(
            "Заказ уже оплачен или сейчас "
            "недоступен для оплаты.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Неизвестный/некорректный результат
    # -------------------------------------------------

    if (
        result != PAYMENT_RESULT_SUCCESS
        or new_balance is None
        or paid_amount is None
    ):
        await callback.answer(
            "Оплату не удалось выполнить.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Успешная оплата
    # -------------------------------------------------

    paid_order = await get_order_with_client(order_id)
    if paid_order is None:
        await callback.answer(
            "Оплата прошла, но не удалось обновить карточку заказа.",
            show_alert=True,
        )
        return

    full_price = int(paid_order["agreed_price"] or 0)
    referral_discount = int(paid_order["referral_discount"] or 0)
    bonus_used = int(paid_order["bonus_used"] or 0)
    bonus_balance_after = await get_bonus_balance(internal_user_id)
    if bonus_balance_after is None:
        bonus_balance_after = 0

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            user_id=internal_user_id,
            action="payment_succeeded",
            entity_type="order",
            entity_id=order_id,
            details={
                "amount": int(paid_amount),
                "full_price": full_price,
                "referral_discount": referral_discount,
                "bonus_used": bonus_used,
                "balance_before": int(new_balance) + int(paid_amount),
                "balance_after": int(new_balance),
                "bonus_balance_after": int(bonus_balance_after),
            },
        )

    except Exception:
        logger.exception(
            "Не удалось записать событие "
            "payment_succeeded для заказа %s",
            order_id,
        )

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            user_id=internal_user_id,
            action="order_started",
            entity_type="order",
            entity_id=order_id,
            details={
                "reason": "payment_succeeded",
                "assigned_master_telegram_id": order[
                    "assigned_master_telegram_id"
                ],
            },
        )
    except Exception:
        logger.exception(
            "Не удалось записать событие "
            "order_started для заказа %s",
            order_id,
        )

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None,
        )

    except TelegramBadRequest as error:
        logger.warning(
            "Не удалось убрать клавиатуру оплаты "
            "заказа %s: %s",
            order_id,
            error,
        )

    direction = html.escape(
        str(
            order["direction"]
            or "Не указано"
        )
    )

    discount_lines = ""
    if referral_discount > 0:
        discount_lines += (
            f"🎉 Реферальная скидка: <b>−{referral_discount} ₽</b>\n"
        )
    if bonus_used > 0:
        discount_lines += (
            f"🎁 Использовано бонусов: <b>{bonus_used} ₽</b>\n"
        )

    await callback.message.answer(
        "✅ <b>Заказ успешно оплачен</b>\n\n"
        f"Заказ: <b>№{order_id}</b>\n"
        f"Направление: <b>{direction}</b>\n"
        f"Полная стоимость: <b>{full_price} ₽</b>\n"
        f"{discount_lines}"
        f"💳 Списано с баланса: <b>{paid_amount} ₽</b>\n"
        f"💳 Остаток на балансе: <b>{new_balance} ₽</b>\n"
        f"🎁 Остаток бонусов: <b>{bonus_balance_after} бонусов</b>\n\n"
        "Средства зарезервированы сервисом.\n"
        "Заказ передан исполнителю в работу.",
        parse_mode="HTML",
    )

    topic_id = order[
        "topic_id"
    ]

    group_chat_id = order[
        "group_chat_id"
    ]

    if (
        topic_id is not None
        and group_chat_id is not None
    ):
        try:
            await bot.send_message(
                chat_id=int(
                    group_chat_id
                ),
                message_thread_id=int(
                    topic_id
                ),
                text=(
                    "💳 <b>Заказ оплачен</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n"
                    f"Согласованная стоимость: <b>{full_price} ₽</b>\n"
                    + (
                        f"🎁 Клиент использовал бонусы: <b>{bonus_used} бонусов</b>\n"
                        if bonus_used > 0
                        else ""
                    )
                    + (
                        f"🎉 Скидка приглашённого: <b>{referral_discount} ₽</b>\n"
                        if referral_discount > 0
                        else ""
                    )
                    + f"💳 С баланса клиента списано: <b>{paid_amount} ₽</b>\n\n"
                    + (
                        "⚠️ <b>Важно:</b> бонусы и скидка клиента не уменьшают "
                        "согласованную стоимость для исполнителя. "
                        f"Для вас сумма заказа остаётся <b>{full_price} ₽</b>.\n\n"
                        if bonus_used > 0 or referral_discount > 0
                        else ""
                    )
                    + "Средства зарезервированы сервисом.\n"
                    "Можно приступать к выполнению заказа."
                ),
                parse_mode="HTML",
            )

        except Exception:
            logger.exception(
                "Не удалось уведомить исполнителя "
                "об оплате заказа %s",
                order_id,
            )

    try:
        card_updated = await refresh_order_card(
            bot=bot,
            order_id=order_id,
        )

        if not card_updated:
            logger.warning(
                "Не удалось обновить основную "
                "карточку заказа %s после оплаты.",
                order_id,
            )

    except Exception:
        logger.exception(
            "Неожиданная ошибка при обновлении "
            "карточки заказа %s после оплаты.",
            order_id,
        )

    await callback.answer(
        "Заказ оплачен."
    )


@router.callback_query(
    F.data.startswith("counter_price:")
)
async def start_client_counter_price(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """
    Запускает ввод встречной цены клиентом.
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
        _, order_id_raw, offer_id_raw = callback.data.split(
            ":",
            maxsplit=2,
        )

        order_id = int(order_id_raw)
        offer_id = int(offer_id_raw)

    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные предложения.",
            show_alert=True,
        )
        return

    internal_user_id = await get_internal_user_id(
        callback.from_user.id
    )

    if internal_user_id is None:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    order = await get_order_with_client(
        order_id
    )

    if order is None:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    if int(order["user_id"]) != internal_user_id:
        await callback.answer(
            "Это предложение относится к другому заказу.",
            show_alert=True,
        )
        return

    if str(order["status"]) != "new":
        await callback.answer(
            "Этот заказ уже не находится "
            "на этапе согласования цены.",
            show_alert=True,
        )
        return

    if (
        str(order["price_status"])
        != PRICE_STATUS_WAITING_CLIENT
    ):
        await callback.answer(
            "Это предложение уже неактуально.",
            show_alert=True,
        )
        return

    pending_offer = await get_pending_price_offer(
        order_id
    )

    if (
        pending_offer is None
        or int(pending_offer["id"]) != offer_id
        or str(pending_offer["offered_by"])
        != OFFERED_BY_MASTER
    ):
        await callback.answer(
            "Это предложение уже неактуально.",
            show_alert=True,
        )
        return

    await state.set_state(
        ClientPriceState.waiting_for_counter_price
    )

    await state.set_data(
        {
            "order_id": order_id,
            "offer_id": offer_id,
            "internal_user_id": internal_user_id,
            "expected_price_status": (
                PRICE_STATUS_WAITING_CLIENT
            ),
            "master_telegram_id": order[
                "assigned_master_telegram_id"
            ],
            "group_chat_id": order[
                "group_chat_id"
            ],
            "topic_id": order["topic_id"],
            "direction": order["direction"],
        }
    )

    await callback.message.answer(
        "✏️ <b>Предложите свою стоимость</b>\n\n"
        "Отправьте целое число в рублях.\n"
        "Например: <code>1300</code>\n\n"
        "Чтобы отменить ввод, отправьте "
        "<code>/cancel</code>.",
        parse_mode="HTML",
    )

    await callback.answer()


@router.message(
    ClientPriceState.waiting_for_counter_price
)
async def process_client_counter_price(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    """
    Сохраняет встречную цену клиента
    и отправляет её выбранному исполнителю.

    Перед созданием предложения повторно проверяет:
    - текущего пользователя;
    - принадлежность заказа;
    - актуальный статус заказа;
    - состояние переговоров.

    После успешного создания предложения:
    - записывает событие в журнал активности;
    - уведомляет клиента;
    - отправляет встречную цену исполнителю.

    Финальная проверка прав также выполняется
    внутри create_price_offer().
    """

    if message.from_user is None:
        return

    data = await state.get_data()

    order_id = data.get(
        "order_id"
    )

    expected_price_status = data.get(
        "expected_price_status"
    )

    if (
        order_id is None
        or expected_price_status is None
    ):
        await state.clear()

        await message.answer(
            "Не удалось определить заказ. "
            "Попробуйте открыть предложение ещё раз."
        )
        return

    if not message.text:
        await message.answer(
            "Отправьте стоимость текстом, "
            "целым числом."
        )
        return

    if (
        message.text
        .strip()
        .lower()
        == "/cancel"
    ):
        await state.clear()

        await message.answer(
            "Ввод встречной стоимости отменён."
        )
        return

    normalized_amount = (
        message.text
        .strip()
        .replace(" ", "")
    )

    try:
        amount = int(
            normalized_amount
        )

    except ValueError:
        await message.answer(
            "Стоимость должна быть целым числом.\n"
            "Например: <code>1300</code>.",
            parse_mode="HTML",
        )
        return

    if amount <= 0:
        await message.answer(
            "Стоимость должна быть больше нуля."
        )
        return

    if amount > 1_000_000:
        await message.answer(
            "Указана слишком большая стоимость."
        )
        return

    # -------------------------------------------------
    # Определяем текущего пользователя.
    # -------------------------------------------------

    current_internal_user_id = (
        await get_internal_user_id(
            message.from_user.id
        )
    )

    if current_internal_user_id is None:
        await state.clear()

        await message.answer(
            "Не удалось определить ваш профиль.\n\n"
            "Попробуйте пройти регистрацию заново."
        )
        return

    order = await get_order_with_client(
        int(order_id)
    )

    if order is None:
        await state.clear()

        await message.answer(
            "Заказ больше не найден."
        )
        return

    # -------------------------------------------------
    # Проверяем принадлежность заказа.
    # -------------------------------------------------

    if (
        int(order["user_id"])
        != current_internal_user_id
    ):
        await state.clear()

        await message.answer(
            "Этот заказ принадлежит "
            "другому пользователю."
        )
        return

    if (
        str(order["status"])
        != ORDER_STATUS_NEW
    ):
        await state.clear()

        await message.answer(
            "Заказ уже не находится "
            "на этапе согласования цены."
        )
        return

    if (
        str(order["price_status"])
        != PRICE_STATUS_WAITING_CLIENT
    ):
        await state.clear()

        await message.answer(
            "Состояние переговоров "
            "уже изменилось."
        )
        return

    # FSM хранит ожидаемое состояние сценария,
    # но дополнительно убеждаемся, что оно
    # соответствует реальному состоянию заказа.
    if (
        str(expected_price_status)
        != PRICE_STATUS_WAITING_CLIENT
    ):
        await state.clear()

        await message.answer(
            "Данные предложения устарели. "
            "Откройте актуальное предложение ещё раз."
        )
        return

    # -------------------------------------------------
    # Создаём встречное предложение клиента.
    # -------------------------------------------------

    try:
        offer_id = await create_price_offer(
            order_id=int(
                order_id
            ),
            offered_by=OFFERED_BY_CLIENT,
            offered_by_telegram_id=(
                message.from_user.id
            ),
            amount=amount,
            expected_current_price_status=(
                PRICE_STATUS_WAITING_CLIENT
            ),
            new_price_status=(
                PRICE_STATUS_WAITING_MASTER_RESPONSE
            ),
        )

    except Exception:
        logger.exception(
            "Ошибка создания встречного "
            "предложения по заказу %s. "
            "Клиент: %s",
            order_id,
            message.from_user.id,
        )

        await message.answer(
            "Не удалось сохранить встречную цену."
        )
        return

    if offer_id is None:
        await state.clear()

        await message.answer(
            "Предложение уже было изменено, "
            "обработано или больше недоступно."
        )
        return

    # -------------------------------------------------
    # Журнал активности.
    #
    # До этой точки предложение уже успешно
    # создано в БД, поэтому теперь фиксируем
    # реальное действие клиента.
    # -------------------------------------------------

    try:
        await log_user_activity(
            telegram_id=message.from_user.id,
            user_id=current_internal_user_id,
            action="counter_price_offered",
            entity_type="order",
            entity_id=int(
                order_id
            ),
            details={
                "offer_id": int(
                    offer_id
                ),
                "amount": amount,
                "offered_by": "client",
            },
        )

    except Exception:
        # Ошибка журнала не должна ломать
        # уже успешно созданное предложение.
        logger.exception(
            "Не удалось записать событие "
            "counter_price_offered "
            "для заказа %s",
            order_id,
        )

    group_chat_id = order[
        "group_chat_id"
    ]

    topic_id = order[
        "topic_id"
    ]

    direction = html.escape(
        str(
            order["direction"]
            or "Не указано"
        )
    )

    await state.clear()

    # -------------------------------------------------
    # Подтверждаем клиенту.
    # -------------------------------------------------

    await message.answer(
        "✅ <b>Ваша цена отправлена исполнителю</b>\n\n"
        f"Заказ: <b>№{order_id}</b>\n"
        f"Предложенная стоимость: "
        f"<b>{amount} ₽</b>\n\n"
        "Теперь ожидается ответ исполнителя.",
        parse_mode="HTML",
    )

    if (
        group_chat_id is None
        or topic_id is None
    ):
        logger.warning(
            "У заказа %s отсутствует тема "
            "для отправки встречной цены.",
            order_id,
        )

        await message.answer(
            "⚠️ Цена сохранена, "
            "но рабочая тема заказа недоступна."
        )
        return

    # -------------------------------------------------
    # Отправляем встречную цену исполнителю.
    # -------------------------------------------------

    try:
        await bot.send_message(
            chat_id=int(
                group_chat_id
            ),
            message_thread_id=int(
                topic_id
            ),
            text=(
                "✏️ <b>Клиент предложил "
                "свою стоимость</b>\n\n"
                f"Заказ: <b>№{order_id}</b>\n"
                f"Направление: <b>{direction}</b>\n"
                f"Предложение клиента: "
                f"<b>{amount} ₽</b>\n\n"
                "Вы можете согласиться, "
                "предложить новую цену "
                "или отказаться."
            ),
            parse_mode="HTML",
            reply_markup=(
                master_counter_offer_keyboard(
                    order_id=int(
                        order_id
                    ),
                    offer_id=offer_id,
                )
            ),
        )

    except Exception:
        logger.exception(
            "Не удалось отправить исполнителю "
            "встречную цену по заказу %s",
            order_id,
        )

        await message.answer(
            "⚠️ Цена сохранена, "
            "но уведомить исполнителя не удалось."
        )


@router.callback_query(
    F.data.startswith("reject_price:")
)
async def client_reject_price(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    """
    Клиент отклоняет актуальное предложение исполнителя
    и завершает переговоры по заказу.

    Отказ выполняется атомарно:
    - проверяется принадлежность заказа клиенту;
    - проверяется актуальность предложения;
    - price_status переводится в rejected;
    - заказ переводится в cancelled;
    - предложение помечается как rejected;
    - действие записывается в журнал активности.
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
        _, order_id_raw, offer_id_raw = (
            callback.data.split(
                ":",
                maxsplit=2,
            )
        )

        order_id = int(
            order_id_raw
        )

        offer_id = int(
            offer_id_raw
        )

    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные предложения.",
            show_alert=True,
        )
        return

    internal_user_id = await get_internal_user_id(
        callback.from_user.id
    )

    if internal_user_id is None:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    order = await get_order_with_client(
        order_id
    )

    if order is None:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Проверяем владельца заказа.
    # -------------------------------------------------

    if (
        int(order["user_id"])
        != internal_user_id
    ):
        await callback.answer(
            "Этот заказ принадлежит "
            "другому пользователю.",
            show_alert=True,
        )
        return

    if (
        str(order["status"])
        != ORDER_STATUS_NEW
    ):
        await callback.answer(
            "Этот заказ уже не находится "
            "на этапе согласования цены.",
            show_alert=True,
        )
        return

    if (
        str(order["price_status"])
        != PRICE_STATUS_WAITING_CLIENT
    ):
        await callback.answer(
            "Это предложение уже неактуально.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Проверяем актуальное предложение исполнителя.
    # -------------------------------------------------

    pending_offer = await get_pending_price_offer(
        order_id
    )

    if (
        pending_offer is None
        or int(
            pending_offer["id"]
        )
        != offer_id
        or str(
            pending_offer["offered_by"]
        )
        != OFFERED_BY_MASTER
    ):
        await callback.answer(
            "Это предложение уже неактуально.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Атомарно завершаем переговоры.
    # -------------------------------------------------

    try:
        rejected = await reject_price_negotiation(
            order_id=order_id,
            offer_id=offer_id,
            rejected_by=OFFERED_BY_CLIENT,
            rejected_by_telegram_id=(
                callback.from_user.id
            ),
            expected_price_status=(
                PRICE_STATUS_WAITING_CLIENT
            ),
        )

    except Exception:
        logger.exception(
            "Ошибка отказа клиента от цены "
            "по заказу %s. Клиент: %s",
            order_id,
            callback.from_user.id,
        )

        await callback.answer(
            "Не удалось отменить переговоры.",
            show_alert=True,
        )
        return

    if not rejected:
        await callback.answer(
            "Предложение уже было изменено, "
            "обработано или больше недоступно.",
            show_alert=True,
        )
        return

    # -------------------------------------------------
    # Журнал активности.
    #
    # Отказ уже действительно записан в БД,
    # поэтому фиксируем бизнес-событие.
    # -------------------------------------------------

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            user_id=internal_user_id,
            action="price_rejected",
            entity_type="order",
            entity_id=order_id,
            details={
                "offer_id": offer_id,
                "rejected_by": "client",
                "offer_created_by": "master",
            },
        )

    except Exception:
        # Сбой журнала не должен ломать
        # уже успешно отменённый заказ.
        logger.exception(
            "Не удалось записать событие "
            "price_rejected для заказа %s",
            order_id,
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
                "reason": "client_rejected_price",
                "offer_id": offer_id,
            },
        )
    except Exception:
        logger.exception(
            "Не удалось записать отмену заказа %s "
            "клиентом после отказа от цены",
            order_id,
        )

    # -------------------------------------------------
    # Убираем устаревшие inline-кнопки.
    # -------------------------------------------------

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None,
        )

    except TelegramBadRequest as error:
        logger.warning(
            "Не удалось удалить кнопки предложения "
            "по заказу %s: %s",
            order_id,
            error,
        )

    await callback.message.answer(
        "❌ <b>Вы отказались от предложенной цены</b>\n\n"
        f"Заказ <b>№{order_id}</b> отменён.\n"
        "Средства с баланса не списывались.",
        parse_mode="HTML",
    )

    group_chat_id = order[
        "group_chat_id"
    ]

    topic_id = order[
        "topic_id"
    ]

    # -------------------------------------------------
    # Уведомляем исполнителя.
    # -------------------------------------------------

    if (
        group_chat_id is not None
        and topic_id is not None
    ):
        try:
            await bot.send_message(
                chat_id=int(
                    group_chat_id
                ),
                message_thread_id=int(
                    topic_id
                ),
                text=(
                    "❌ <b>Клиент отказался "
                    "от предложенной стоимости</b>\n\n"
                    f"Заказ <b>№{order_id}</b> отменён."
                ),
                parse_mode="HTML",
            )

        except Exception:
            logger.exception(
                "Не удалось уведомить исполнителя "
                "об отказе клиента "
                "по заказу %s",
                order_id,
            )

    # -------------------------------------------------
    # Обновляем основную карточку заказа.
    # -------------------------------------------------

    try:
        card_updated = await refresh_order_card(
            bot=bot,
            order_id=order_id,
        )

        if not card_updated:
            logger.warning(
                "Не удалось обновить карточку "
                "заказа %s после отказа клиента.",
                order_id,
            )

    except Exception:
        logger.exception(
            "Ошибка обновления карточки "
            "заказа %s после отказа клиента.",
            order_id,
        )

    # -------------------------------------------------
    # Архивируем тему заказа.
    # -------------------------------------------------

    if (
        group_chat_id is not None
        and topic_id is not None
    ):
        try:
            await bot.edit_forum_topic(
                chat_id=int(
                    group_chat_id
                ),
                message_thread_id=int(
                    topic_id
                ),
                name=(
                    f"📦 Архив | "
                    f"Заказ №{order_id}"
                ),
            )

            await close_and_record_topic(bot, int(group_chat_id), int(topic_id))

        except TelegramBadRequest as error:
            logger.warning(
                "Не удалось архивировать тему "
                "заказа %s: %s",
                order_id,
                error,
            )

        except Exception:
            logger.exception(
                "Ошибка архивирования "
                "темы заказа %s",
                order_id,
            )

    await callback.answer(
        "Заказ отменён."
    )


router.callback_query(
    F.data.startswith("reassign_choose:")
)(start_order_reassignment)


router.callback_query(
    F.data.startswith("reassign_free:")
)(switch_order_to_free_master)


router.callback_query(
    F.data.startswith("reassign_cancel:")
)(cancel_order_after_failed_assignment)
