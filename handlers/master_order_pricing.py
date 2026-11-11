"""Ценовые переговоры исполнителя по заказу.

Выделено из handlers.master_order (workstream A). Модуль владеет только
сценариями предложения цены, принятия и отклонения встречной цены клиента.
Router и порядок регистрации остаются в handlers.master_order, поэтому
callback-контракты и порядок observers не меняются.

logger сохраняет историческое имя handlers.master_order, чтобы категория
журнала не изменилась после структурного переноса.
"""

import html
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import MASTER_GROUP_ID
from database.activity_logs import log_user_activity
from database.masters import is_master_available
from database.orders import get_order_with_client
from database.payments import get_order_payment_preview
from database.price_offers import (
    OFFERED_BY_CLIENT,
    OFFERED_BY_MASTER,
    PRICE_STATUS_WAITING_CLIENT,
    PRICE_STATUS_WAITING_MASTER,
    PRICE_STATUS_WAITING_MASTER_RESPONSE,
    accept_price_offer,
    create_price_offer,
    get_pending_price_offer,
    reject_client_price_by_master,
)
from handlers.master_order_access import (
    _ensure_operational_master_callback,
)
from keyboards.price_offers import (
    client_price_offer_keyboard,
    order_reassignment_keyboard,
)
from states.master_price import MasterPriceState
from utils.order_card import archive_order_topic
from utils.order_statuses import ORDER_STATUS_NEW
from utils.payment_preview import (
    build_payment_preview_text,
    payment_keyboard_from_preview,
)

logger = logging.getLogger("handlers.master_order")


async def start_master_price_offer(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if not await _ensure_operational_master_callback(
        callback
    ):
        return

    """
    Запускает ввод стоимости выбранным исполнителем.
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
            "Предложить стоимость можно только "
            "в рабочей группе.",
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

    order = await get_order_with_client(
        order_id
    )

    if order is None:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    if order["group_chat_id"] != callback.message.chat.id:
        await callback.answer(
            "Эта кнопка не относится к данной группе.",
            show_alert=True,
        )
        return

    topic_id = order["topic_id"]
    message_thread_id = (
        callback.message.message_thread_id
    )

    if topic_id is None:
        await callback.answer(
            "У заказа не найдена тема.",
            show_alert=True,
        )
        return

    if (
        message_thread_id is not None
        and int(topic_id) != message_thread_id
    ):
        await callback.answer(
            "Эта кнопка не относится к данной теме.",
            show_alert=True,
        )
        return

    assigned_master_telegram_id = order[
        "assigned_master_telegram_id"
    ]

    if assigned_master_telegram_id is None:
        await callback.answer(
            "У заказа не указан исполнитель.",
            show_alert=True,
        )
        return

    if (
        int(assigned_master_telegram_id)
        != callback.from_user.id
    ):
        await callback.answer(
            "Этот заказ закреплён за другим исполнителем.",
            show_alert=True,
        )
        return

    if str(order["status"]) != ORDER_STATUS_NEW:
        await callback.answer(
            "Предложить цену можно только "
            "для нового заказа.",
            show_alert=True,
        )
        return

    price_status = str(
        order["price_status"]
    )

    if price_status not in {
        PRICE_STATUS_WAITING_MASTER,
        PRICE_STATUS_WAITING_MASTER_RESPONSE,
    }:
        await callback.answer(
            "Сейчас предложение цены от исполнителя "
            "не ожидается.",
            show_alert=True,
        )
        return

    await state.set_state(
        MasterPriceState.waiting_for_price
    )

    await state.set_data(
        {
            "order_id": order_id,
            "group_chat_id": callback.message.chat.id,
            "topic_id": int(topic_id),
            "expected_price_status": price_status,
            "client_telegram_id": order[
                "client_telegram_id"
            ],
            "direction": order["direction"],
        }
    )

    await callback.message.answer(
        "💰 <b>Введите стоимость выполнения</b>\n\n"
        "Отправьте целое число в рублях.\n"
        "Например: <code>1500</code>\n\n"
        "Чтобы отменить ввод, отправьте "
        "<code>/cancel</code>.",
        parse_mode="HTML",
    )

    await callback.answer()

async def process_master_price_offer(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    """
    Сохраняет предложение цены исполнителя
    и отправляет его клиенту.

    После успешного создания предложения
    записывает действие исполнителя в журнал активности.
    """

    if message.from_user is None:
        return

    if not await is_master_available(
        message.from_user.id
    ):
        await state.clear()
        await message.answer(
            "⛔ Ваш статус исполнителя сейчас неактивен "
            "или карточка удалена. Предлагать стоимость нельзя."
        )
        return

    data = await state.get_data()

    order_id = data.get(
        "order_id"
    )

    group_chat_id = data.get(
        "group_chat_id"
    )

    topic_id = data.get(
        "topic_id"
    )

    expected_price_status = data.get(
        "expected_price_status"
    )

    client_telegram_id = data.get(
        "client_telegram_id"
    )

    if (
        order_id is None
        or group_chat_id is None
        or topic_id is None
        or expected_price_status is None
        or client_telegram_id is None
    ):
        await state.clear()

        await message.answer(
            "Не удалось определить заказ. "
            "Попробуйте нажать кнопку предложения "
            "цены ещё раз."
        )
        return

    # -------------------------------------------------
    # Проверяем, что цена отправлена
    # именно в теме нужного заказа.
    # -------------------------------------------------

    if (
        message.chat.id
        != int(group_chat_id)
        or message.message_thread_id
        != int(topic_id)
    ):
        await message.answer(
            "Стоимость нужно отправить в теме "
            "соответствующего заказа."
        )
        return

    # -------------------------------------------------
    # Проверяем введённое значение.
    # -------------------------------------------------

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
            "Ввод стоимости отменён."
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
            "Например: <code>1500</code>.",
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
    # Получаем актуальное состояние заказа.
    # -------------------------------------------------

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
    # Проверяем, что цену вводит именно
    # выбранный для этого заказа исполнитель.
    # -------------------------------------------------

    if (
        order[
            "assigned_master_telegram_id"
        ]
        is None
        or int(
            order[
                "assigned_master_telegram_id"
            ]
        )
        != message.from_user.id
    ):
        await state.clear()

        await message.answer(
            "Этот заказ закреплён "
            "за другим исполнителем."
        )
        return

    if (
        str(order["status"])
        != ORDER_STATUS_NEW
    ):
        await state.clear()

        await message.answer(
            "Заказ уже не находится на этапе "
            "согласования цены."
        )
        return

    # -------------------------------------------------
    # Создаём предложение цены.
    # -------------------------------------------------

    try:
        offer_id = await create_price_offer(
            order_id=int(
                order_id
            ),
            offered_by=OFFERED_BY_MASTER,
            offered_by_telegram_id=(
                message.from_user.id
            ),
            amount=amount,
            expected_current_price_status=str(
                expected_price_status
            ),
            new_price_status=(
                PRICE_STATUS_WAITING_CLIENT
            ),
        )

    except Exception:
        logger.exception(
            "Ошибка создания предложения цены "
            "по заказу %s",
            order_id,
        )

        await message.answer(
            "Не удалось сохранить предложение цены."
        )
        return

    if offer_id is None:
        await state.clear()

        await message.answer(
            "Состояние переговоров уже изменилось. "
            "Обновите заказ и попробуйте снова."
        )
        return

    # -------------------------------------------------
    # Логируем успешное предложение исполнителя.
    #
    # user_id=None намеренно:
    # действие совершает исполнитель, а не клиент
    # из таблицы users.
    # -------------------------------------------------

    try:
        await log_user_activity(
            telegram_id=message.from_user.id,
            user_id=None,
            action="price_offered",
            entity_type="order",
            entity_id=int(
                order_id
            ),
            details={
                "offered_by": "master",
                "amount": amount,
                "offer_id": int(
                    offer_id
                ),
            },
        )

    except Exception:
        # Ошибка журнала активности не должна
        # отменять уже созданное предложение.
        logger.exception(
            "Не удалось записать событие "
            "price_offered для заказа %s",
            order_id,
        )

    direction = html.escape(
        str(
            order["direction"]
            or "Не указано"
        )
    )

    await state.clear()

    # -------------------------------------------------
    # Подтверждаем исполнителю сохранение цены.
    # -------------------------------------------------

    await message.answer(
        f"✅ Стоимость <b>{amount} ₽</b> "
        f"предложена клиенту.",
        parse_mode="HTML",
    )

    # -------------------------------------------------
    # Отправляем предложение клиенту.
    # -------------------------------------------------

    try:
        await bot.send_message(
            chat_id=int(
                client_telegram_id
            ),
            text=(
                "💰 <b>Исполнитель предложил "
                "стоимость</b>\n\n"
                f"Заказ: <b>№{order_id}</b>\n"
                f"Направление: <b>{direction}</b>\n"
                f"Стоимость: "
                f"<b>{amount} ₽</b>\n\n"
                "Вы можете согласиться, "
                "предложить свою цену "
                "или отказаться."
            ),
            parse_mode="HTML",
            reply_markup=(
                client_price_offer_keyboard(
                    order_id=int(
                        order_id
                    ),
                    offer_id=offer_id,
                )
            ),
        )

    except TelegramForbiddenError:
        logger.warning(
            "Клиент %s заблокировал бота. "
            "Предложение цены по заказу %s "
            "сохранено, но не доставлено.",
            client_telegram_id,
            order_id,
        )

        await message.answer(
            "⚠️ Цена сохранена, но клиент "
            "заблокировал бота."
        )

    except Exception:
        logger.exception(
            "Не удалось отправить клиенту "
            "предложение цены по заказу %s",
            order_id,
        )

        await message.answer(
            "⚠️ Цена сохранена, но уведомить "
            "клиента не удалось."
        )

async def master_accept_client_price(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    if not await _ensure_operational_master_callback(
        callback
    ):
        return

    """
    Выбранный исполнитель принимает встречную цену клиента.

    Цена фиксируется как agreed_price,
    после чего клиент получает экран оплаты заказа.

    Дополнительно проверяется:
    - действие выполняется в рабочей группе;
    - кнопка относится к нужной теме заказа;
    - заказ закреплён за текущим исполнителем;
    - заказ находится на этапе согласования цены;
    - предложение всё ещё актуально;
    - предложение создано клиентом;
    - исполнитель имеет право принять его и на уровне БД.
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

    if callback.message.chat.id != MASTER_GROUP_ID:
        await callback.answer(
            "Управление ценой доступно только "
            "в рабочей группе.",
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

    order = await get_order_with_client(
        order_id
    )

    if order is None:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    # Проверяем, что заказ относится
    # к текущей рабочей группе.
    if (
        order["group_chat_id"] is None
        or int(order["group_chat_id"])
        != callback.message.chat.id
    ):
        await callback.answer(
            "Эта кнопка не относится "
            "к данной группе.",
            show_alert=True,
        )
        return

    topic_id = order[
        "topic_id"
    ]

    if topic_id is None:
        await callback.answer(
            "У заказа не найдена "
            "связанная тема.",
            show_alert=True,
        )
        return

    # Проверяем, что кнопка нажата
    # именно в теме этого заказа.
    if (
        callback.message.message_thread_id
        is not None
        and int(topic_id)
        != callback.message.message_thread_id
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

    # Первая линия защиты:
    # handler проверяет закреплённого исполнителя.
    if (
        assigned_master_telegram_id is None
        or int(
            assigned_master_telegram_id
        )
        != callback.from_user.id
    ):
        await callback.answer(
            "Этот заказ закреплён "
            "за другим исполнителем.",
            show_alert=True,
        )
        return

    if (
        str(order["status"])
        != ORDER_STATUS_NEW
    ):
        await callback.answer(
            "Заказ уже не находится "
            "на этапе согласования цены.",
            show_alert=True,
        )
        return

    if (
        str(order["price_status"])
        != PRICE_STATUS_WAITING_MASTER_RESPONSE
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
        or int(
            pending_offer["id"]
        )
        != offer_id
        or str(
            pending_offer["offered_by"]
        )
        != OFFERED_BY_CLIENT
    ):
        await callback.answer(
            "Это предложение уже неактуально.",
            show_alert=True,
        )
        return

    try:
        # Вторая линия защиты:
        # accept_price_offer() повторно проверяет
        # закреплённого исполнителя внутри транзакции БД.
        accepted, agreed_price = (
            await accept_price_offer(
                order_id=order_id,
                offer_id=offer_id,
                accepted_by=OFFERED_BY_MASTER,
                accepted_by_telegram_id=(
                    callback.from_user.id
                ),
                expected_price_status=(
                    PRICE_STATUS_WAITING_MASTER_RESPONSE
                ),
            )
        )

    except Exception:
        logger.exception(
            "Ошибка принятия встречной цены "
            "по заказу %s исполнителем %s",
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

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            action="price_accepted",
            entity_type="order",
            entity_id=order_id,
            details={
                "offer_id": offer_id,
                "amount": int(agreed_price),
                "offer_created_by": "client",
                "accepted_by": "master",
            },
        )
    except Exception:
        logger.exception(
            "Не удалось записать принятие встречной "
            "цены исполнителем по заказу %s",
            order_id,
        )

    # Удаляем кнопки уже обработанного
    # встречного предложения.
    try:
        await callback.message.edit_reply_markup(
            reply_markup=None,
        )

    except TelegramBadRequest as error:
        logger.warning(
            "Не удалось удалить кнопки "
            "встречного предложения "
            "заказа %s: %s",
            order_id,
            error,
        )

    master_name = html.escape(
        callback.from_user.full_name
    )

    await callback.message.answer(
        "✅ <b>Встречная цена принята</b>\n\n"
        f"Заказ: <b>№{order_id}</b>\n"
        f"Итоговая стоимость: "
        f"<b>{agreed_price} ₽</b>\n"
        f"Подтвердил: "
        f"<b>{master_name}</b>\n\n"
        "Теперь ожидается оплата клиента.",
        parse_mode="HTML",
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

        await callback.answer(
            "Цена согласована, "
            "но уведомить клиента не удалось.",
            show_alert=True,
        )
        return

    preview = await get_order_payment_preview(
        order_id=order_id,
        user_id=int(order["user_id"]),
    )

    if preview is None:
        logger.warning(
            "Не удалось подготовить экран оплаты заказа %s",
            order_id,
        )
        await callback.answer(
            "Цена согласована, но подготовить оплату не удалось.",
            show_alert=True,
        )
        return

    direction = html.escape(
        str(order["direction"] or "Не указано")
    )

    try:
        await bot.send_message(
            chat_id=int(client_telegram_id),
            text=(
                "✅ <b>Исполнитель согласился на вашу стоимость</b>\n\n"
                f"Заказ: <b>№{order_id}</b>\n"
                f"Направление: <b>{direction}</b>\n"
                f"Итоговая стоимость: <b>{agreed_price} ₽</b>\n\n"
                f"{build_payment_preview_text(preview)}"
            ),
            parse_mode="HTML",
            reply_markup=payment_keyboard_from_preview(
                order_id,
                preview,
            ),
        )

    except Exception:
        logger.exception(
            "Не удалось отправить клиенту экран оплаты заказа %s",
            order_id,
        )

        await callback.answer(
            "Цена согласована, но уведомить клиента не удалось.",
            show_alert=True,
        )
        return

    await callback.answer(
        "Встречная цена принята."
    )

async def master_reject_client_price(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    if not await _ensure_operational_master_callback(
        callback
    ):
        return

    """Исполнитель отклоняет встречную цену; заказ возвращается клиенту."""

    if callback.message is None or callback.data is None:
        await callback.answer()
        return
    if callback.message.chat.id != MASTER_GROUP_ID:
        await callback.answer("Действие доступно только в рабочей группе.", show_alert=True)
        return
    try:
        _, order_raw, offer_raw = callback.data.split(":", maxsplit=2)
        order_id = int(order_raw)
        offer_id = int(offer_raw)
    except (ValueError, IndexError):
        await callback.answer("Некорректное предложение.", show_alert=True)
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
        await callback.answer("Эта кнопка относится к другой теме.", show_alert=True)
        return
    if (
        order["assigned_master_telegram_id"] is None
        or int(order["assigned_master_telegram_id"]) != callback.from_user.id
    ):
        await callback.answer("Этот заказ закреплён за другим исполнителем.", show_alert=True)
        return
    if str(order["status"]) != ORDER_STATUS_NEW or str(order["price_status"]) != PRICE_STATUS_WAITING_MASTER_RESPONSE:
        await callback.answer("Предложение уже неактуально.", show_alert=True)
        return

    pending = await get_pending_price_offer(order_id)
    if (
        pending is None
        or int(pending["id"]) != offer_id
        or str(pending["offered_by"]) != OFFERED_BY_CLIENT
    ):
        await callback.answer("Предложение уже неактуально.", show_alert=True)
        return

    rejected = await reject_client_price_by_master(
        order_id=order_id,
        offer_id=offer_id,
        master_telegram_id=callback.from_user.id,
        expected_price_status=PRICE_STATUS_WAITING_MASTER_RESPONSE,
    )
    if not rejected:
        await callback.answer("Предложение уже обработано.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            action="price_rejected",
            entity_type="order",
            entity_id=order_id,
            details={
                "offer_id": offer_id,
                "rejected_by": "master",
                "offer_created_by": "client",
            },
        )
    except Exception:
        logger.exception("Не удалось записать отказ по цене заказа %s", order_id)

    try:
        await callback.message.answer(
            "❌ <b>Встречная цена отклонена</b>\n\n"
            f"Заказ <b>№{order_id}</b> возвращён клиенту для выбора другого исполнителя.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Не удалось подтвердить отказ по цене заказа %s", order_id)

    await archive_order_topic(
        bot=bot,
        group_chat_id=order["group_chat_id"],
        topic_id=order["topic_id"],
        order_id=order_id,
    )

    client_telegram_id = order["client_telegram_id"]
    if client_telegram_id is not None:
        try:
            await bot.send_message(
                chat_id=int(client_telegram_id),
                text=(
                    "❌ <b>Исполнитель не согласился на вашу встречную цену</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n\n"
                    "Заказ сохранён. Выберите другого исполнителя, "
                    "режим «Свободный исполнитель» или отмените заказ."
                ),
                reply_markup=order_reassignment_keyboard(order_id),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Не удалось уведомить клиента по заказу %s", order_id)

    await callback.answer("Заказ возвращён клиенту на перевыбор.")
