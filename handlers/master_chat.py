import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.types import Message

from config import ADMIN_IDS, MASTER_GROUP_ID
from database.orders import get_order_by_topic_id
from utils.order_statuses import is_order_active
from database.master_response_times import (
    finish_master_response_waiting,
    update_master_average_response,
)
from database.masters import is_master_available
from utils.chat_content import is_relay_content


router = Router()
logger = logging.getLogger(__name__)


def _is_admin(telegram_id: int) -> bool:
    return int(telegram_id) in ADMIN_IDS


@router.message(
    F.chat.id == MASTER_GROUP_ID,
    F.message_thread_id,
    is_relay_content,
)
async def relay_master_message_to_client(
    message: Message,
    bot: Bot,
) -> None:
    """
    Передаёт клиенту сообщения только от исполнителя,
    который закреплён за заказом.

    Сообщения остальных участников остаются
    в рабочей теме, но клиенту не отправляются.

    После успешной отправки сообщения клиенту
    завершает текущий замер времени ответа исполнителя
    и пересчитывает его среднее время ответа.
    """

    # Не обрабатываем сообщения без автора
    # и сообщения ботов.
    if (
        message.from_user is None
        or message.from_user.is_bot
    ):
        return

    topic_id = message.message_thread_id

    if topic_id is None:
        return

    # Игнорируем служебные события форума и группы.
    if (
        message.forum_topic_created is not None
        or message.forum_topic_closed is not None
        or message.forum_topic_reopened is not None
        or message.forum_topic_edited is not None
        or message.new_chat_members
        or message.left_chat_member is not None
        or message.pinned_message is not None
    ):
        return

    order = await get_order_by_topic_id(
        group_chat_id=message.chat.id,
        topic_id=topic_id,
    )

    if order is None:
        await message.reply(
            "⚠️ Не удалось найти заказ, "
            "связанный с этой темой."
        )
        return

    order_id = int(
        order["id"]
    )

    order_status = str(
        order["status"]
    )

    client_telegram_id = order[
        "client_telegram_id"
    ]

    assigned_master_telegram_id = order[
        "assigned_master_telegram_id"
    ]

    is_admin_actor = _is_admin(
        message.from_user.id
    )

    # До назначения исполнителя обычные участники
    # клиенту писать не могут. Администратор может
    # вмешаться в любую тему заказа.
    if (
        assigned_master_telegram_id is None
        and not is_admin_actor
    ):
        await message.reply(
            f"⚠️ Заказ №{order_id} ещё не взят "
            "в работу.\n\n"
            "Сначала нажмите кнопку "
            "«Взять в работу». "
            "Сообщение клиенту не отправлено."
        )
        return

    normalized_master_id = (
        int(assigned_master_telegram_id)
        if assigned_master_telegram_id is not None
        else None
    )

    # Обычный исполнитель может писать только по своему заказу.
    # Администратор может отвечать из любой темы, не становясь
    # назначенным исполнителем.
    if (
        not is_admin_actor
        and (
            normalized_master_id is None
            or message.from_user.id
            != normalized_master_id
        )
    ):
        await message.reply(
            f"⚠️ Заказ №{order_id} закреплён "
            "за другим исполнителем.\n\n"
            "Ваше сообщение осталось "
            "в рабочей теме, "
            "но клиенту не отправлено."
        )
        return

    # Активность карточки проверяем только для исполнителя.
    if (
        not is_admin_actor
        and normalized_master_id is not None
        and not await is_master_available(
            normalized_master_id
        )
    ):
        await message.reply(
            "⛔ Ваш статус исполнителя сейчас неактивен "
            "или карточка удалена.\n\n"
            "Сообщение клиенту не отправлено."
        )
        return

    # Повторно проверяем актуальный статус
    # заказа перед каждой отправкой.
    if not is_order_active(
        order_status
    ):
        await message.reply(
            f"⚠️ Заказ №{order_id} уже завершён "
            "или отменён.\n\n"
            "Отправка сообщений клиенту "
            "по этому заказу недоступна."
        )
        return

    if client_telegram_id is None:
        await message.reply(
            "⚠️ У клиента не найден Telegram ID."
        )
        return

    client_telegram_id = int(
        client_telegram_id
    )

    subject = html.escape(
        str(
            order["subject"]
            or "Не указан"
        )
    )

    work_type = html.escape(
        str(
            order["work_type"]
            or "Не указан"
        )
    )

    try:
        if is_admin_actor:
            header_text = (
                f"🛡 <b>Сообщение администратора "
                f"по заказу №{order_id}</b>"
            )
        else:
            header_text = (
                f"💬 <b>Новое сообщение "
                f"по заказу №{order_id}</b>"
            )

        await bot.send_message(
            chat_id=client_telegram_id,
            text=(
                f"{header_text}\n\n"
                f"📘 Предмет: "
                f"<b>{subject}</b>\n"
                f"📝 Тип работы: "
                f"<b>{work_type}</b>"
            ),
            parse_mode="HTML",
        )

        # Копируем сообщение исполнителя
        # без раскрытия его профиля.
        sent_message = await bot.copy_message(
            chat_id=client_telegram_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )

        # Ответ администратора не должен закрывать таймер
        # реального исполнителя и менять его статистику.
        if (
            not is_admin_actor
            and normalized_master_id is not None
        ):
            try:
                response_seconds = (
                    await finish_master_response_waiting(
                        order_id=order_id,
                        master_telegram_id=(
                            normalized_master_id
                        ),
                    )
                )

                if response_seconds is not None:
                    average_minutes = (
                        await update_master_average_response(
                            master_telegram_id=(
                                normalized_master_id
                            ),
                        )
                    )

                    logger.info(
                        "Зафиксировано время ответа исполнителя. "
                        "Заказ: %s, исполнитель: %s, "
                        "ответ: %s сек., "
                        "новое среднее: %s мин.",
                        order_id,
                        normalized_master_id,
                        response_seconds,
                        average_minutes,
                    )

            except Exception:
                logger.exception(
                    "Не удалось сохранить статистику "
                    "времени ответа исполнителя. "
                    "Заказ: %s, исполнитель: %s",
                    order_id,
                    normalized_master_id,
                )

    except TelegramForbiddenError:
        logger.warning(
            "Клиент запретил боту отправлять сообщения. "
            "Заказ: %s, Telegram ID клиента: %s",
            order_id,
            client_telegram_id,
        )

        await message.reply(
            "⚠️ Не удалось отправить сообщение "
            "клиенту.\n\n"
            "Возможно, клиент заблокировал бота "
            "или удалил диалог."
        )
        return

    except TelegramBadRequest as error:
        logger.warning(
            "Telegram отклонил сообщение исполнителя. "
            "Заказ: %s, ошибка: %s",
            order_id,
            error,
        )

        await message.reply(
            "⚠️ Telegram не смог передать "
            "это сообщение клиенту.\n"
            "Попробуйте отправить его "
            "в другом формате."
        )
        return

    except Exception:
        logger.exception(
            "Ошибка передачи сообщения "
            "исполнителя клиенту. "
            "Заказ: %s",
            order_id,
        )

        await message.reply(
            "⚠️ Во время отправки произошла ошибка.\n"
            "Попробуйте ещё раз немного позже."
        )
        return

    if is_admin_actor:
        await message.reply(
            "✅ Сообщение администратора отправлено клиенту."
        )
    else:
        await message.reply(
            "✅ Сообщение отправлено клиенту."
        )

    logger.info(
        "Сообщение из рабочей темы передано клиенту. "
        "Заказ: %s, отправитель: %s, роль: %s, "
        "сообщение в теме: %s, "
        "сообщение клиенту: %s",
        order_id,
        message.from_user.id,
        (
            "admin"
            if is_admin_actor
            else "master"
        ),
        message.message_id,
        sent_message.message_id,
    )
