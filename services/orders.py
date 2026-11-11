"""Legacy publisher публикации темы заказа (БЕЗ durable контрольных точек).

СТАТУС (Phase 3): production-код не вызывается. Единственный авторитетный
путь создания темы/восстановления -- utils.order_card (create_order_topic
здесь сохранён для истории: см. STAGING_ARCHITECTURE_PHASE2_REPORT.md
раздел 3 "AST-инвентарь ... единственный production-вызов create_order --
handlers/order_form.py"). Никогда не использовать в новом коде: эта
реализация не пишет order_topic_checkpoints и не различает неизвестный
remote outcome. Удаление -- отдельное решение владельца; до тех пор
запрет на повторное подключение без owner review.
"""
import html
from aiogram import Bot
from config import MASTER_GROUP_ID
from database.orders import update_order_topic, update_order_card_message_id
from keyboards.master_order import master_order_status_keyboard
from utils.order_statuses import ORDER_STATUS_NEW
import logging
from aiogram.exceptions import TelegramForbiddenError
logger = logging.getLogger(__name__)


def build_group_order_card(order_id: int, data: dict) -> str:
    direction = html.escape(
        str(data.get("direction") or "Не указано")
    )
    subject = html.escape(
        str(data.get("subject") or "Не указан")
    )
    work_type = html.escape(
        str(data.get("work_type") or "Не указан")
    )
    deadline = html.escape(
        str(data.get("deadline") or "Не указан")
    )
    description = html.escape(
        str(data.get("description") or "Описание отсутствует")
    )

    files_count = len(data.get("files", []))

    return (
        f"📋 <b>Новый заказ №{order_id}</b>\n\n"
        f"🧭 Направление: <b>{direction}</b>\n"
        f"📘 Предмет: <b>{subject}</b>\n"
        f"📝 Тип работы: <b>{work_type}</b>\n"
        f"📅 Дедлайн: <b>{deadline}</b>\n"
        f"📎 Файлов: <b>{files_count}</b>\n\n"
        f"💬 <b>Описание:</b>\n"
        f"{description}\n\n"
        "💬 <b>Как отвечать клиенту:</b>\n"
        "Просто отправляйте сообщения в этой теме.\n"
        "Бот автоматически передаст их заказчику."
    )


async def create_order_topic(
    bot: Bot,
    order_id: int,
    data: dict,
) -> int:
    """
    Создаёт тему заказа в группе исполнителей,
    сохраняет её ID в базе,
    публикует основную карточку заказа,
    сохраняет message_id этой карточки,
    отмечает выбранного исполнителя,
    отправляет ему личное уведомление
    и пересылает вложения клиента.
    """

    subject = str(
        data.get("subject") or "Без предмета"
    )

    assigned_master_telegram_id = data.get(
        "assigned_master_telegram_id"
    )
    assigned_master_username = data.get(
        "assigned_master_username"
    )
    assigned_master_full_name = data.get(
        "assigned_master_full_name"
    )

    if assigned_master_telegram_id is None:
        raise RuntimeError(
            f"Для заказа №{order_id} не выбран исполнитель."
        )

    safe_topic_subject = subject[:50]

    # 1. Создаём отдельную тему заказа.
    topic = await bot.create_forum_topic(
        chat_id=MASTER_GROUP_ID,
        name=(
            f"Заказ №{order_id} — "
            f"{safe_topic_subject}"
        ),
    )

    topic_id = topic.message_thread_id

    # 2. Сохраняем связь заказа с группой и темой.
    topic_saved = await update_order_topic(
        order_id=order_id,
        group_chat_id=MASTER_GROUP_ID,
        topic_id=topic_id,
    )

    if not topic_saved:
        raise RuntimeError(
            f"Не удалось сохранить тему "
            f"для заказа №{order_id}."
        )

    # 3. Публикуем основную карточку заказа.
    order_card_message = await bot.send_message(
        chat_id=MASTER_GROUP_ID,
        message_thread_id=topic_id,
        text=build_group_order_card(
            order_id,
            data,
        ),
        parse_mode="HTML",
        reply_markup=master_order_status_keyboard(
            order_id=order_id,
            current_status=ORDER_STATUS_NEW,
        ),
    )

    # 4. Сохраняем message_id основной карточки заказа.
    card_saved = await update_order_card_message_id(
        order_id=order_id,
        order_card_message_id=(
            order_card_message.message_id
        ),
    )

    if not card_saved:
        raise RuntimeError(
            f"Не удалось сохранить ID карточки "
            f"заказа №{order_id}."
        )

    # 5. Формируем упоминание выбранного исполнителя.
    if assigned_master_username:
        normalized_username = (
            str(assigned_master_username)
            .strip()
            .lstrip("@")
        )

        master_mention = (
            f"@{html.escape(normalized_username)}"
        )

    else:
        safe_master_name = html.escape(
            str(
                assigned_master_full_name
                or "Выбранный исполнитель"
            )
        )

        master_mention = (
            f'<a href="tg://user?id='
            f'{int(assigned_master_telegram_id)}">'
            f"{safe_master_name}</a>"
        )

    # 6. Отмечаем выбранного исполнителя в рабочей теме.
    await bot.send_message(
        chat_id=MASTER_GROUP_ID,
        message_thread_id=topic_id,
        text=(
            f"📩 <b>Заказ №{order_id} "
            "передан выбранному исполнителю</b>\n\n"
            f"👨‍💻 Исполнитель: {master_mention}\n\n"
            "Пожалуйста, ознакомьтесь с заданием "
            "и предложите клиенту стоимость выполнения."
        ),
        parse_mode="HTML",
    )

    # 7. Дополнительно отправляем исполнителю
    # личное уведомление о назначенном заказе.
    try:
        await bot.send_message(
            chat_id=int(
                assigned_master_telegram_id
            ),
            text=(
                "🔔 <b>Вам назначен новый заказ</b>\n\n"
                f"Заказ: <b>№{order_id}</b>\n"
                f"Предмет: "
                f"<b>{html.escape(subject)}</b>\n\n"
                "Заказ уже опубликован "
                "в рабочей группе исполнителей.\n\n"
                "Ознакомьтесь с заданием "
                "и предложите клиенту "
                "стоимость выполнения."
            ),
            parse_mode="HTML",
        )

    except TelegramForbiddenError:
        logger.warning(
            "Не удалось отправить личное уведомление "
            "исполнителю %s о заказе %s. "
            "Возможно, исполнитель не запускал бота "
            "или заблокировал его.",
            assigned_master_telegram_id,
            order_id,
        )

    except Exception:
        logger.exception(
            "Не удалось отправить личное уведомление "
            "исполнителю %s о новом заказе %s.",
            assigned_master_telegram_id,
            order_id,
        )

    # 8. Отправляем прикреплённые клиентом файлы.
    for file_data in data.get("files", []):
        file_type = file_data.get(
            "file_type"
        )
        telegram_file_id = file_data.get(
            "telegram_file_id"
        )

        if not telegram_file_id:
            continue

        if file_type == "photo":
            await bot.send_photo(
                chat_id=MASTER_GROUP_ID,
                message_thread_id=topic_id,
                photo=telegram_file_id,
            )

        elif file_type == "document":
            file_name = file_data.get(
                "file_name"
            )

            await bot.send_document(
                chat_id=MASTER_GROUP_ID,
                message_thread_id=topic_id,
                document=telegram_file_id,
                caption=(
                    str(file_name)
                    if file_name
                    else None
                ),
            )

    return topic_id


