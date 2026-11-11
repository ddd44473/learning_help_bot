import html
import logging
from datetime import datetime
from typing import Any, Sequence

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database.orders import (
    add_review_text,
    create_review,
    get_active_orders_by_user_id,
    confirm_order_completion_by_client,
    get_client_review_for_order,
    get_order_with_client,
    get_review_by_order_id,
    get_user_order,
    return_order_to_work_by_client,
    get_completed_orders_by_user_id,
    get_active_orders_count_by_user_id,
    get_completed_orders_count_by_user_id,
)
from database.users import get_internal_user_id
from keyboards.my_orders import (
    active_order_details_keyboard,
    active_orders_keyboard,
    my_orders_keyboard,
    completed_order_details_keyboard,
    completed_orders_keyboard,
    review_rating_keyboard,
)
from states.order_chat import ReviewState
from utils.order_statuses import (
    ORDER_STATUS_COMPLETED,
    ORDER_STATUS_IN_PROGRESS,
    ORDER_STATUS_WAITING_CLIENT,
    get_order_status_title,
    is_order_active,
)
from utils.order_card import (
    archive_order_topic,
    refresh_order_card,
)
from database.activity_logs import log_user_activity
from database.referrals import get_referral_reward_for_order
from keyboards.profile import referral_reward_keyboard


router = Router()
logger = logging.getLogger(__name__)


def _format_referral_expiry(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return value or "неизвестно"


MY_ORDERS_TEXT = """
📂 <b>Мои заказы</b>

Здесь собраны все оформленные вами заказы.

📌 В активных находятся новые заказы и работы, которые ещё выполняются.

✅ В завершённых позже можно будет посмотреть выполненные и закрытые заказы.
"""

MAX_REVIEW_TEXT_LENGTH = 2000


def review_comment_keyboard(
    order_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пропустить",
                    callback_data=f"review_skip:{order_id}",
                )
            ]
        ]
    )


def master_review_notification_keyboard(
    order_id: int,
) -> InlineKeyboardMarkup:
    """Кнопка в личном уведомлении исполнителя."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Посмотреть отзыв",
                    callback_data=(
                        f"master_view_review:{order_id}"
                    ),
                )
            ]
        ]
    )


def build_master_review_text(
    *,
    order,
    review,
) -> str:
    """
    Анонимная карточка отзыва для исполнителя.
    Данные клиента намеренно не выводятся.
    """

    order_id = int(
        order["id"]
    )
    rating = int(
        review["rating"]
    )
    direction = html.escape(
        str(
            order["direction"]
            or "Не указано"
        )
    )
    work_type = html.escape(
        str(
            order["work_type"]
            or "Не указано"
        )
    )
    review_text = html.escape(
        str(
            review["review_text"]
            or "Комментарий пока не оставлен."
        )
    )

    return (
        f"⭐ <b>Отзыв по заказу №{order_id}</b>\n\n"
        f"🧭 Направление: <b>{direction}</b>\n"
        f"📝 Тип работы: <b>{work_type}</b>\n\n"
        f"<b>Оценка:</b> {'⭐' * rating}\n\n"
        f"💬 <b>Комментарий:</b>\n"
        f"{review_text}"
    )


async def notify_master_about_review(
    *,
    bot,
    order_id: int,
    notification_type: str,
) -> None:
    """
    Отправляет исполнителю личное уведомление об отзыве.
    Ошибка Telegram не влияет на уже сохранённый отзыв.
    """

    order = await get_order_with_client(
        order_id
    )

    if (
        order is None
        or order["assigned_master_telegram_id"] is None
    ):
        logger.warning(
            "Не удалось определить исполнителя "
            "для уведомления об отзыве. Заказ: %s",
            order_id,
        )
        return

    review = await get_review_by_order_id(
        order_id
    )

    if review is None:
        logger.warning(
            "Отзыв не найден для уведомления исполнителя. "
            "Заказ: %s",
            order_id,
        )
        return

    master_telegram_id = int(
        order["assigned_master_telegram_id"]
    )
    rating = int(
        review["rating"]
    )

    if notification_type == "comment_added":
        text = (
            "💬 <b>Отзыв дополнен</b>\n\n"
            f"Клиент добавил комментарий "
            f"к отзыву по заказу <b>№{order_id}</b>.\n\n"
            f"Оценка: {'⭐' * rating}"
        )
    else:
        text = (
            "⭐ <b>Вам оставили отзыв</b>\n\n"
            f"Заказ: <b>№{order_id}</b>\n"
            f"Оценка: {'⭐' * rating}\n\n"
            "Клиент может дополнить оценку "
            "текстовым комментарием."
        )

    try:
        await bot.send_message(
            chat_id=master_telegram_id,
            text=text,
            parse_mode="HTML",
            reply_markup=(
                master_review_notification_keyboard(
                    order_id
                )
            ),
        )

    except Exception:
        logger.exception(
            "Не удалось отправить исполнителю %s "
            "уведомление об отзыве по заказу %s",
            master_telegram_id,
            order_id,
        )


def format_date(value: str | None) -> str:
    if not value:
        return "Не указан"

    for source_format in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            date = datetime.strptime(value, source_format)
            return date.strftime("%d-%m-%Y")
        except ValueError:
            continue

    return value


def get_status_text(status: str) -> str:
    """Возвращает безопасное название статуса заказа."""

    status_title = get_order_status_title(status)
    return html.escape(str(status_title))


def build_active_orders_text(
    orders: Sequence[Any],
) -> str:
    """
    Формирует компактный текст раздела
    активных заказов.

    Сами заказы отображаются отдельными
    inline-кнопками.
    """

    return (
        "📌 <b>Активные заказы</b>\n\n"
        "Выберите нужный заказ с помощью кнопок ниже."
    )

def build_completed_orders_text(
    orders: Sequence[Any],
) -> str:
    """
    Формирует компактный текст раздела
    завершённых заказов.

    Сами заказы отображаются отдельными
    inline-кнопками.
    """

    return (
        "✅ <b>Завершённые заказы</b>\n\n"
        "Выберите нужный заказ с помощью кнопок ниже."
    )


def build_order_details_text(
    order: Any,
) -> str:
    order_id = html.escape(str(order["id"]))

    direction = html.escape(
        str(order["direction"] or "Не указано")
    )
    subject = html.escape(
        str(order["subject"] or "Не указан")
    )
    work_type = html.escape(
        str(order["work_type"] or "Не указан")
    )
    deadline = html.escape(
        format_date(order["deadline"])
    )
    status = get_status_text(
        str(order["status"])
    )
    description = html.escape(
        str(order["description"] or "Не указано")
    )

    return (
        f"📋 <b>Заказ №{order_id}</b>\n\n"
        f"🧭 Направление: <b>{direction}</b>\n"
        f"📘 Предмет: <b>{subject}</b>\n"
        f"📝 Тип работы: <b>{work_type}</b>\n"
        f"📅 Дедлайн: <b>{deadline}</b>\n"
        f"📍 Статус: <b>{status}</b>\n\n"
        f"💬 <b>Описание задания:</b>\n"
        f"{description}"
    )


@router.callback_query(
    F.data.startswith("master_view_review:")
)
async def master_view_review(
    callback: CallbackQuery,
) -> None:
    """Показывает отзыв только тому исполнителю, которому он принадлежит."""

    if callback.message is None:
        await callback.answer(
            "Сообщение недоступно.",
            show_alert=True,
        )
        return

    if callback.data is None:
        await callback.answer(
            "Некорректные данные.",
            show_alert=True,
        )
        return

    try:
        order_id = int(
            callback.data.split(
                ":",
                maxsplit=1,
            )[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный номер заказа.",
            show_alert=True,
        )
        return

    review = await get_review_by_order_id(
        order_id
    )
    order = await get_order_with_client(
        order_id
    )

    if review is None or order is None:
        await callback.answer(
            "Отзыв больше не найден.",
            show_alert=True,
        )
        return

    review_master_id = review[
        "master_telegram_id"
    ]
    order_master_id = order[
        "assigned_master_telegram_id"
    ]

    if (
        review_master_id is None
        or order_master_id is None
        or int(review_master_id) != callback.from_user.id
        or int(order_master_id) != callback.from_user.id
    ):
        await callback.answer(
            "Этот отзыв относится к другому исполнителю.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        build_master_review_text(
            order=order,
            review=review,
        ),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(F.data == "my_orders")
async def open_my_orders(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await callback.message.answer(
        MY_ORDERS_TEXT,
        reply_markup=my_orders_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(F.data == "active_orders")
async def open_active_orders(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
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

    page = 0
    page_size = 10
    offset = page * page_size

    total_count = (
        await get_active_orders_count_by_user_id(
            internal_user_id
        )
    )

    if total_count == 0:
        await callback.message.answer(
            "📌 <b>Активные заказы</b>\n\n"
            "Сейчас у вас нет активных заказов.",
            reply_markup=my_orders_keyboard(),
            parse_mode="HTML",
        )

        await callback.answer()
        return

    total_pages = max(
        (total_count + page_size - 1)
        // page_size,
        1,
    )

    orders = await get_active_orders_by_user_id(
        internal_user_id,
        limit=page_size,
        offset=offset,
    )

    await callback.message.answer(
        build_active_orders_text(orders),
        reply_markup=active_orders_keyboard(
            orders=orders,
            current_page=page,
            total_pages=total_pages,
        ),
        parse_mode="HTML",
    )

    await callback.answer()

@router.callback_query(
    F.data.startswith("active_orders_page:")
)
async def change_active_orders_page(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    if callback.data is None:
        await callback.answer(
            "Некорректные данные страницы.",
            show_alert=True,
        )
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

    internal_user_id = await get_internal_user_id(
        callback.from_user.id
    )

    if internal_user_id is None:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    page_size = 10

    total_count = (
        await get_active_orders_count_by_user_id(
            internal_user_id
        )
    )

    if total_count == 0:
        await callback.message.edit_text(
            "📌 <b>Активные заказы</b>\n\n"
            "Сейчас у вас нет активных заказов.",
            reply_markup=my_orders_keyboard(),
            parse_mode="HTML",
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

    offset = page * page_size

    orders = await get_active_orders_by_user_id(
        internal_user_id,
        limit=page_size,
        offset=offset,
    )

    await callback.message.edit_text(
        build_active_orders_text(orders),
        reply_markup=active_orders_keyboard(
            orders=orders,
            current_page=page,
            total_pages=total_pages,
        ),
        parse_mode="HTML",
    )

    await callback.answer()

@router.callback_query(
    F.data == "active_orders_page_info"
)
async def active_orders_page_info(
    callback: CallbackQuery,
) -> None:
    await callback.answer()


@router.callback_query(
    F.data == "completed_orders"
)
async def open_completed_orders(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
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

    page = 0
    page_size = 10
    offset = page * page_size

    total_count = (
        await get_completed_orders_count_by_user_id(
            internal_user_id
        )
    )

    if total_count == 0:
        await callback.message.answer(
            "✅ <b>Завершённые заказы</b>\n\n"
            "У вас пока нет завершённых заказов.",
            reply_markup=my_orders_keyboard(),
            parse_mode="HTML",
        )

        await callback.answer()
        return

    total_pages = max(
        (total_count + page_size - 1)
        // page_size,
        1,
    )

    orders = await get_completed_orders_by_user_id(
        internal_user_id,
        limit=page_size,
        offset=offset,
    )

    await callback.message.answer(
        build_completed_orders_text(
            orders
        ),
        reply_markup=completed_orders_keyboard(
            orders=orders,
            current_page=page,
            total_pages=total_pages,
        ),
        parse_mode="HTML",
    )

    await callback.answer()

@router.callback_query(
    F.data.startswith(
        "completed_orders_page:"
    )
)
async def change_completed_orders_page(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    if callback.data is None:
        await callback.answer(
            "Некорректные данные страницы.",
            show_alert=True,
        )
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

    internal_user_id = await get_internal_user_id(
        callback.from_user.id
    )

    if internal_user_id is None:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    page_size = 10

    total_count = (
        await get_completed_orders_count_by_user_id(
            internal_user_id
        )
    )

    if total_count == 0:
        await callback.message.edit_text(
            "✅ <b>Завершённые заказы</b>\n\n"
            "У вас пока нет завершённых заказов.",
            reply_markup=my_orders_keyboard(),
            parse_mode="HTML",
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

    offset = page * page_size

    orders = await get_completed_orders_by_user_id(
        internal_user_id,
        limit=page_size,
        offset=offset,
    )

    await callback.message.edit_text(
        build_completed_orders_text(
            orders
        ),
        reply_markup=completed_orders_keyboard(
            orders=orders,
            current_page=page,
            total_pages=total_pages,
        ),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(
    F.data == "completed_orders_page_info"
)
async def completed_orders_page_info(
    callback: CallbackQuery,
) -> None:
    await callback.answer()


@router.callback_query(
    F.data.startswith("view_active_order:")
)
async def open_active_order_details(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
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

    order = await get_user_order(
        order_id=order_id,
        user_id=internal_user_id,
    )

    if order is None:
        await callback.answer(
            "Заказ не найден или не принадлежит вам.",
            show_alert=True,
        )
        return

    if not is_order_active(
        str(order["status"])
    ):
        await callback.answer(
            "Этот заказ больше не является активным.",
            show_alert=True,
        )
        return

    details_text = build_order_details_text(
        order
    )

    if (
        str(order["status"])
        == ORDER_STATUS_WAITING_CLIENT
    ):
        details_text += (
            "\n\n✅ <b>Работа ожидает вашего подтверждения</b>\n"
            "Подтвердите завершение, если результат вас устраивает, "
            "или верните заказ исполнителю в работу."
        )

    await callback.message.answer(
        details_text,
        reply_markup=active_order_details_keyboard(
            order_id,
            current_status=str(
                order["status"]
            ),
        ),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith("confirm_completion:")
)
async def confirm_order_completion(
    callback: CallbackQuery,
) -> None:
    """
    Финальное слово по заказу остаётся за клиентом.

    Только клиент конкретного заказа может выполнить переход:
    waiting_client -> completed.
    """

    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    try:
        order_id = int(
            callback.data.split(
                ":",
                maxsplit=1,
            )[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный номер заказа.",
            show_alert=True,
        )
        return

    order = await get_order_with_client(
        order_id
    )

    if (
        order is None
        or order["client_telegram_id"] is None
        or int(order["client_telegram_id"])
        != callback.from_user.id
    ):
        await callback.answer(
            "Заказ не найден или не принадлежит вам.",
            show_alert=True,
        )
        return

    if (
        str(order["status"])
        != ORDER_STATUS_WAITING_CLIENT
    ):
        await callback.answer(
            "Этот запрос на завершение уже неактуален.",
            show_alert=True,
        )
        return

    try:
        confirmed = (
            await confirm_order_completion_by_client(
                order_id=order_id,
                client_telegram_id=(
                    callback.from_user.id
                ),
            )
        )
    except Exception:
        logger.exception(
            "Ошибка клиентского подтверждения "
            "завершения заказа %s",
            order_id,
        )
        await callback.answer(
            "Не удалось подтвердить завершение.",
            show_alert=True,
        )
        return

    if not confirmed:
        await callback.answer(
            "Состояние заказа уже изменилось.",
            show_alert=True,
        )
        return

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        logger.exception(
            "Не удалось убрать кнопки подтверждения "
            "заказа %s",
            order_id,
        )

    internal_user_id = await get_internal_user_id(
        callback.from_user.id
    )

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            user_id=internal_user_id,
            action="order_completion_confirmed",
            entity_type="order",
            entity_id=order_id,
            details={
                "old_status": (
                    ORDER_STATUS_WAITING_CLIENT
                ),
                "new_status": (
                    ORDER_STATUS_COMPLETED
                ),
                "confirmed_by": "client",
            },
        )
    except Exception:
        logger.exception(
            "Не удалось записать подтверждение "
            "завершения заказа %s",
            order_id,
        )

    referral_reward = await get_referral_reward_for_order(
        order_id
    )

    if (
        referral_reward is not None
        and int(referral_reward["reward_amount"]) > 0
    ):
        try:
            await callback.bot.send_message(
                chat_id=int(
                    referral_reward["referrer_telegram_id"]
                ),
                text=(
                    "🎉 <b>+250 бонусов</b>\n\n"
                    "Твой друг успешно завершил первый заказ.\n"
                    "На твой бонусный баланс начислено <b>250 бонусов</b>.\n"
                    f"🪙 Баланс: <b>{int(referral_reward['bonus_balance'])} бонусов</b>\n"
                    f"📅 Действуют до: <b>{_format_referral_expiry(str(referral_reward['expires_at']))}</b>\n\n"
                    "Использовать бонусы можно при следующем заказе."
                ),
                parse_mode="HTML",
                reply_markup=referral_reward_keyboard(),
            )
        except Exception:
            logger.exception(
                "Не удалось уведомить пригласившего о бонусе "
                "по заказу %s",
                order_id,
            )

    master_telegram_id = order[
        "assigned_master_telegram_id"
    ]

    # Сначала сообщаем в рабочую тему, пока она ещё открыта.
    if (
        order["group_chat_id"] is not None
        and order["topic_id"] is not None
    ):
        try:
            await callback.bot.send_message(
                chat_id=int(
                    order["group_chat_id"]
                ),
                message_thread_id=int(
                    order["topic_id"]
                ),
                text=(
                    "✅ <b>Клиент подтвердил завершение заказа</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n"
                    "Работа принята клиентом. "
                    "Заказ окончательно завершён."
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Не удалось сообщить в рабочую тему "
                "о завершении заказа %s",
                order_id,
            )

    if master_telegram_id is not None:
        try:
            await callback.bot.send_message(
                chat_id=int(
                    master_telegram_id
                ),
                text=(
                    "✅ <b>Клиент подтвердил завершение</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n\n"
                    "Работа принята. Заказ окончательно завершён."
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Не удалось лично уведомить исполнителя "
                "о завершении заказа %s",
                order_id,
            )

    try:
        await archive_order_topic(
            bot=callback.bot,
            group_chat_id=(
                order["group_chat_id"]
            ),
            topic_id=order["topic_id"],
            order_id=order_id,
        )
    except Exception:
        logger.exception(
            "Не удалось архивировать тему "
            "завершённого заказа %s",
            order_id,
        )

    await callback.message.answer(
        "✅ <b>Заказ завершён</b>\n\n"
        f"Заказ <b>№{order_id}</b> окончательно завершён "
        "после вашего подтверждения.\n\n"
        "⭐ Теперь вы можете оценить работу исполнителя.",
        parse_mode="HTML",
        reply_markup=review_rating_keyboard(
            order_id
        ),
    )

    await callback.answer(
        "Завершение подтверждено."
    )


@router.callback_query(
    F.data.startswith("return_completion:")
)
async def return_completion_to_work(
    callback: CallbackQuery,
) -> None:
    """
    Клиент не принимает завершение и возвращает заказ:
    waiting_client -> in_progress.
    """

    if callback.message is None or callback.data is None:
        await callback.answer()
        return

    try:
        order_id = int(
            callback.data.split(
                ":",
                maxsplit=1,
            )[1]
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный номер заказа.",
            show_alert=True,
        )
        return

    order = await get_order_with_client(
        order_id
    )

    if (
        order is None
        or order["client_telegram_id"] is None
        or int(order["client_telegram_id"])
        != callback.from_user.id
    ):
        await callback.answer(
            "Заказ не найден или не принадлежит вам.",
            show_alert=True,
        )
        return

    if (
        str(order["status"])
        != ORDER_STATUS_WAITING_CLIENT
    ):
        await callback.answer(
            "Этот запрос уже неактуален.",
            show_alert=True,
        )
        return

    try:
        returned = (
            await return_order_to_work_by_client(
                order_id=order_id,
                client_telegram_id=(
                    callback.from_user.id
                ),
            )
        )
    except Exception:
        logger.exception(
            "Ошибка возврата заказа %s "
            "в работу клиентом",
            order_id,
        )
        await callback.answer(
            "Не удалось вернуть заказ в работу.",
            show_alert=True,
        )
        return

    if not returned:
        await callback.answer(
            "Состояние заказа уже изменилось.",
            show_alert=True,
        )
        return

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        logger.exception(
            "Не удалось убрать кнопки подтверждения "
            "заказа %s",
            order_id,
        )

    internal_user_id = await get_internal_user_id(
        callback.from_user.id
    )

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            user_id=internal_user_id,
            action="order_completion_returned",
            entity_type="order",
            entity_id=order_id,
            details={
                "old_status": (
                    ORDER_STATUS_WAITING_CLIENT
                ),
                "new_status": (
                    ORDER_STATUS_IN_PROGRESS
                ),
                "returned_by": "client",
            },
        )
    except Exception:
        logger.exception(
            "Не удалось записать возврат "
            "заказа %s в работу",
            order_id,
        )

    # Возвращаем рабочие кнопки исполнителю.
    try:
        await refresh_order_card(
            bot=callback.bot,
            order_id=order_id,
        )
    except Exception:
        logger.exception(
            "Не удалось обновить карточку "
            "возвращённого заказа %s",
            order_id,
        )

    if (
        order["group_chat_id"] is not None
        and order["topic_id"] is not None
    ):
        try:
            await callback.bot.send_message(
                chat_id=int(
                    order["group_chat_id"]
                ),
                message_thread_id=int(
                    order["topic_id"]
                ),
                text=(
                    "🔄 <b>Клиент вернул заказ в работу</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n\n"
                    "Завершение не подтверждено. "
                    "Продолжите работу и после готовности "
                    "снова нажмите «✅ Работа готова»."
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Не удалось уведомить рабочую тему "
                "о возврате заказа %s",
                order_id,
            )

    master_telegram_id = order[
        "assigned_master_telegram_id"
    ]

    if master_telegram_id is not None:
        try:
            await callback.bot.send_message(
                chat_id=int(
                    master_telegram_id
                ),
                text=(
                    "🔄 <b>Клиент вернул заказ в работу</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n\n"
                    "Клиент пока не подтвердил завершение. "
                    "Продолжите работу и после готовности "
                    "снова отправьте её на подтверждение."
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Не удалось лично уведомить исполнителя "
                "о возврате заказа %s",
                order_id,
            )

    await callback.message.answer(
        "🔄 <b>Заказ возвращён в работу</b>\n\n"
        f"Заказ <b>№{order_id}</b> снова передан исполнителю.",
        parse_mode="HTML",
    )

    await callback.answer(
        "Заказ возвращён в работу."
    )


@router.callback_query(
    F.data.startswith("view_completed_order:")
)
async def open_completed_order_details(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
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

    order = await get_user_order(
        order_id=order_id,
        user_id=internal_user_id,
    )

    if order is None:
        await callback.answer(
            "Заказ не найден или не принадлежит вам.",
            show_alert=True,
        )
        return

    if str(order["status"]) not in {
        "completed",
        "closed",
    }:
        await callback.answer(
            "Этот заказ ещё не завершён.",
            show_alert=True,
        )
        return

    review = await get_client_review_for_order(
        order_id=order_id,
        client_telegram_id=callback.from_user.id,
    )

    has_review = review is not None

    text = build_order_details_text(order)

    if review is None:
        text += (
            "\n\n⭐ <b>Отзыв:</b>\n"
            "Вы ещё не оставили отзыв по этому заказу."
        )
    else:
        rating = int(review["rating"])
        review_text = (
            review["review_text"]
            or "Комментарий не оставлен."
        )

        text += (
            "\n\n⭐ <b>Ваш отзыв:</b>\n"
            f"{'⭐' * rating}\n"
            f"{html.escape(str(review_text))}"
        )

    await callback.message.answer(
        text,
        reply_markup=completed_order_details_keyboard(
            order_id=order_id,
            has_review=has_review,
        ),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith("review_rating:")
)
async def save_review_rating(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """
    Сохраняет оценку клиента для завершённого заказа.

    После успешного сохранения:
    - записывает событие в журнал активности;
    - убирает кнопки выбора оценки;
    - переводит клиента к вводу комментария.
    """

    if callback.message is None:
        await callback.answer(
            "Сообщение с оценкой недоступно.",
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
        _, order_id_raw, rating_raw = (
            callback.data.split(
                ":",
                maxsplit=2,
            )
        )

        order_id = int(
            order_id_raw
        )

        rating = int(
            rating_raw
        )

    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные оценки.",
            show_alert=True,
        )
        return

    if rating not in {1, 2, 3, 4, 5}:
        await callback.answer(
            "Оценка должна быть от 1 до 5.",
            show_alert=True,
        )
        return

    try:
        created = await create_review(
            order_id=order_id,
            client_telegram_id=(
                callback.from_user.id
            ),
            rating=rating,
        )

    except Exception:
        logger.exception(
            "Ошибка сохранения оценки. "
            "Заказ: %s, клиент: %s",
            order_id,
            callback.from_user.id,
        )

        await callback.answer(
            "Не удалось сохранить оценку. "
            "Попробуйте позже.",
            show_alert=True,
        )
        return

    if not created:
        existing_review = (
            await get_client_review_for_order(
                order_id=order_id,
                client_telegram_id=(
                    callback.from_user.id
                ),
            )
        )

        if existing_review is not None:
            await callback.answer(
                "Вы уже оценили этот заказ.",
                show_alert=True,
            )

        else:
            await callback.answer(
                "Оценить можно только свой "
                "завершённый заказ.",
                show_alert=True,
            )

        return

    # -------------------------------------------------
    # Оценка успешно сохранена.
    # Записываем действие клиента в журнал.
    # -------------------------------------------------

    try:
        await log_user_activity(
            telegram_id=callback.from_user.id,
            action="review_created",
            entity_type="order",
            entity_id=order_id,
            details={
                "rating": rating,
            },
        )

    except Exception:
        # Ошибка журнала не должна влиять
        # на уже сохранённую оценку.
        logger.exception(
            "Не удалось записать создание "
            "отзыва в журнал активности. "
            "Заказ: %s, клиент: %s",
            order_id,
            callback.from_user.id,
        )

    # -------------------------------------------------
    # Уведомляем исполнителя о новой оценке.
    # Ошибка отправки не влияет на уже сохранённый отзыв.
    # -------------------------------------------------

    await notify_master_about_review(
        bot=callback.bot,
        order_id=order_id,
        notification_type="rating_created",
    )

    # -------------------------------------------------
    # Убираем уже использованные кнопки оценки.
    # -------------------------------------------------

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:
        logger.exception(
            "Не удалось убрать кнопки "
            "оценки заказа %s",
            order_id,
        )

    # -------------------------------------------------
    # Переходим к необязательному комментарию.
    # -------------------------------------------------

    await state.set_state(
        ReviewState.waiting_for_comment
    )

    await state.update_data(
        review_order_id=order_id
    )

    await callback.message.answer(
        f"Спасибо! Вы поставили оценку "
        f"<b>{rating} из 5</b>.\n\n"
        "Теперь можете написать короткий "
        "комментарий об исполнителе одним "
        "сообщением или нажать «Пропустить».",
        reply_markup=review_comment_keyboard(
            order_id
        ),
        parse_mode="HTML",
    )

    await callback.answer(
        "Оценка сохранена."
    )


@router.callback_query(
    ReviewState.waiting_for_comment,
    F.data.startswith("review_skip:"),
)
async def skip_review_comment(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if callback.message is None:
        await callback.answer()
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

    state_data = await state.get_data()
    expected_order_id = state_data.get(
        "review_order_id"
    )

    if expected_order_id != order_id:
        await callback.answer(
            "Эта кнопка относится к другому отзыву.",
            show_alert=True,
        )
        return

    await state.clear()

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        logger.exception(
            "Не удалось убрать кнопку пропуска для заказа %s",
            order_id,
        )

    await callback.message.answer(
        "✅ Спасибо! Оценка сохранена без комментария."
    )
    await callback.answer()


@router.message(
    ReviewState.waiting_for_comment
)
async def save_review_comment(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Сохраняет текстовый комментарий клиента
    к ранее созданной оценке заказа.

    После успешного сохранения комментария
    действие записывается в журнал активности.
    """

    if message.from_user is None:
        return

    review_text = (
        message.text
        or ""
    ).strip()

    # -------------------------------------------------
    # Проверяем текст комментария.
    # -------------------------------------------------

    if not review_text:
        await message.answer(
            "Пожалуйста, отправьте комментарий "
            "обычным текстовым сообщением или "
            "нажмите «Пропустить» под предыдущим "
            "сообщением."
        )
        return

    if (
        len(review_text)
        > MAX_REVIEW_TEXT_LENGTH
    ):
        await message.answer(
            "Комментарий слишком длинный. "
            "Максимальная длина — "
            f"{MAX_REVIEW_TEXT_LENGTH} символов."
        )
        return

    # -------------------------------------------------
    # Получаем заказ из состояния пользователя.
    # -------------------------------------------------

    state_data = await state.get_data()

    order_id = state_data.get(
        "review_order_id"
    )

    if not isinstance(
        order_id,
        int,
    ):
        await state.clear()

        await message.answer(
            "Не удалось определить заказ. "
            "Оценка уже сохранена, но комментарий "
            "добавить не получилось."
        )
        return

    # -------------------------------------------------
    # Сохраняем комментарий.
    # -------------------------------------------------

    try:
        updated = await add_review_text(
            order_id=order_id,
            client_telegram_id=(
                message.from_user.id
            ),
            review_text=review_text,
        )

    except Exception:
        logger.exception(
            "Ошибка сохранения комментария. "
            "Заказ: %s, клиент: %s",
            order_id,
            message.from_user.id,
        )

        await message.answer(
            "Не удалось сохранить комментарий. "
            "Попробуйте ещё раз или нажмите "
            "«Пропустить» под предыдущим сообщением."
        )
        return

    if not updated:
        await state.clear()

        await message.answer(
            "Комментарий не сохранён. "
            "Возможно, он уже был добавлен ранее."
        )
        return

    # -------------------------------------------------
    # Комментарий успешно сохранён.
    # Записываем действие клиента в журнал.
    # -------------------------------------------------

    try:
        await log_user_activity(
            telegram_id=message.from_user.id,
            action="review_comment_added",
            entity_type="order",
            entity_id=order_id,
            details={
                "review_text": review_text,
            },
        )

    except Exception:
        # Ошибка журнала не должна влиять
        # на уже сохранённый комментарий.
        logger.exception(
            "Не удалось записать добавление "
            "комментария к отзыву "
            "в журнал активности. "
            "Заказ: %s, клиент: %s",
            order_id,
            message.from_user.id,
        )

    # -------------------------------------------------
    # Уведомляем исполнителя, что отзыв дополнен комментарием.
    # -------------------------------------------------

    await notify_master_about_review(
        bot=message.bot,
        order_id=order_id,
        notification_type="comment_added",
    )

    # -------------------------------------------------
    # Сценарий отзыва полностью завершён.
    # -------------------------------------------------

    await state.clear()

    await message.answer(
        "✅ Спасибо! Ваша оценка "
        "и комментарий сохранены."
    )