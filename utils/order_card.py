"""Публикация рабочих тем заказов и восстановление незавершённых публикаций.

Это канонический (авторитетный) путь публикации новой темы заказа и её
durable recovery (checkpoint-таблица order_topic_checkpoints; Phase 2):
- новая тема только через create_order_assignment_topic / безопасную
  первую публикацию по маркеру local_creation_pending;
- неизвестный результат create_forum_topic никогда не повторяется
  вслепую (detail create_outcome_unknown_manual_review);
- recovery -- recover_unfinished_topic_publications, вызывается из main.py.

Не использовать для новых вызовов legacy services.orders.create_order_topic:
у него нет контрольных точек, и он не существует в production call graph
(AST-инвентарь и grep по handlers/services/utils/tests/scripts не находят
ни одного вызова). Файл сохранён по явному решению Phase 3; удаление
требует отдельного решения владельца.
"""
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from database.chat_archive import (
    close_topic as record_archive_topic_closed,
)
from database.chat_archive import (
    delete_topic_checkpoint,
    claim_topic_publication,
    get_topic_checkpoint,
    link_checkpoint_topic,
    list_unfinished_topic_checkpoints,
    mark_topic_checkpoint_state,
    reserve_topic_creation,
    upsert_topic_checkpoint,
)
from config import MASTER_GROUP_ID
from database.masters import (
    get_active_masters,
    get_master_by_telegram_id_any,
)
from database.orders import (
    get_excluded_master_ids,
    get_order_files,
    get_order_with_client,
)
from keyboards.master_order import (
    master_order_status_keyboard,
)
from utils import master_profile as _master_profile


logger = logging.getLogger(__name__)

# Compatibility names; rendering has a single implementation.
_master_public_name = _master_profile.master_public_name
_master_group_mention = _master_profile.master_group_mention


def _master_group_message_link(
    message_id: int,
) -> str | None:
    """
    Возвращает прямую ссылку на сообщение в закрытой supergroup.

    Для Telegram supergroup ID имеет вид -100XXXXXXXXXX.
    В ссылке t.me/c используется часть после -100.
    """

    raw_chat_id = str(
        int(MASTER_GROUP_ID)
    )

    if not raw_chat_id.startswith(
        "-100"
    ):
        return None

    internal_chat_id = raw_chat_id[
        4:
    ]

    if not internal_chat_id:
        return None

    return (
        "https://t.me/c/"
        f"{internal_chat_id}/"
        f"{int(message_id)}"
    )


def _free_order_notification_keyboard(
    message_id: int,
) -> InlineKeyboardMarkup | None:
    link = _master_group_message_link(
        message_id
    )

    if link is None:
        return None

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Открыть заказ в беседе",
                    url=link,
                )
            ]
        ]
    )


async def _notify_assigned_master_about_order(
    bot: Bot,
    *,
    order,
    card_message_id: int,
) -> None:
    """
    Отправляет выбранному клиентом исполнителю личное уведомление
    о новом назначенном заказе с прямой ссылкой на карточку заказа
    в рабочей беседе.

    Ошибка доставки уведомления не влияет на созданный заказ.
    """

    master_telegram_id = order[
        "assigned_master_telegram_id"
    ]

    if master_telegram_id is None:
        return

    order_id = int(
        order["id"]
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
            or "Не указан"
        )
    )

    deadline = html.escape(
        str(
            order["deadline"]
            or "Не указан"
        )
    )

    keyboard = (
        _free_order_notification_keyboard(
            card_message_id
        )
    )

    try:
        await bot.send_message(
            chat_id=int(
                master_telegram_id
            ),
            text=(
                "🔔 <b>Вам назначен новый заказ</b>\n\n"
                f"Заказ: <b>№{order_id}</b>\n"
                f"🧭 Направление: <b>{direction}</b>\n"
                f"📝 Тип работы: <b>{work_type}</b>\n"
                f"📅 Дедлайн: <b>{deadline}</b>\n\n"
                "Клиент выбрал вас исполнителем этого заказа. "
                "Откройте заказ в рабочей беседе, чтобы "
                "посмотреть подробности и предложить стоимость."
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    except Exception:
        logger.exception(
            "Не удалось уведомить исполнителя %s "
            "о назначенном заказе %s",
            master_telegram_id,
            order_id,
        )


async def _notify_eligible_masters_about_open_order(
    bot: Bot,
    *,
    order,
    card_message_id: int,
) -> None:
    """
    Уведомляет в ЛС только тех активных исполнителей,
    которые работают с направлением свободного заказа
    и не исключены из него после прошлой неудачной попытки.

    Ошибка доставки одному исполнителю не влияет ни на заказ,
    ни на уведомления остальным.
    """

    order_id = int(
        order["id"]
    )

    direction = str(
        order["direction"]
        or ""
    ).strip()

    if not direction:
        return

    excluded = await get_excluded_master_ids(
        order_id
    )

    masters = await get_active_masters(
        limit=None,
        offset=0,
        exclude_telegram_ids=excluded,
        direction=direction,
    )

    if not masters:
        return

    safe_direction = html.escape(
        direction
    )

    safe_work_type = html.escape(
        str(
            order["work_type"]
            or "Не указан"
        )
    )

    safe_deadline = html.escape(
        str(
            order["deadline"]
            or "Не указан"
        )
    )

    keyboard = (
        _free_order_notification_keyboard(
            card_message_id
        )
    )

    for master in masters:
        try:
            await bot.send_message(
                chat_id=int(
                    master["telegram_id"]
                ),
                text=(
                    "🟢 <b>Появился свободный заказ</b>\n\n"
                    f"Заказ: <b>№{order_id}</b>\n"
                    f"🧭 Направление: "
                    f"<b>{safe_direction}</b>\n"
                    f"📝 Тип работы: "
                    f"<b>{safe_work_type}</b>\n"
                    f"📅 Дедлайн: "
                    f"<b>{safe_deadline}</b>\n\n"
                    "Заказ доступен исполнителям вашего "
                    "направления. Откройте его в рабочей "
                    "беседе, чтобы посмотреть подробности "
                    "и при желании взять заказ."
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )

        except Exception:
            # Нельзя допустить, чтобы заблокировавший бота
            # исполнитель сорвал создание свободного заказа.
            logger.exception(
                "Не удалось уведомить исполнителя %s "
                "о свободном заказе %s",
                master["telegram_id"],
                order_id,
            )
            continue


def build_group_order_card(
    order,
    files_count: int,
    master=None,
) -> str:
    direction = html.escape(
        str(
            order["direction"]
            or "Не указано"
        )
    )

    work_type = html.escape(
        str(
            order["work_type"]
            or "Не указан"
        )
    )

    deadline = html.escape(
        str(
            order["deadline"]
            or "Не указан"
        )
    )

    description = html.escape(
        str(
            order["description"]
            or "Описание отсутствует"
        )
    )

    assignment_status = str(
        order["assignment_status"]
        or "assigned"
    )

    if assignment_status == "open":
        assignment_text = (
            "🟢 <b>Свободный исполнитель</b>\n"
            "Первый доступный исполнитель, который нажмёт "
            "«Взять заказ», закрепит заказ за собой."
        )

    else:
        master_id = order[
            "assigned_master_telegram_id"
        ]

        if master_id is None:
            assignment_text = (
                "👨‍💻 <b>Исполнитель пока не определён</b>"
            )

        else:
            master_mention = (
                _master_group_mention(
                    master,
                    int(master_id),
                )
            )

            assignment_text = (
                "👨‍💻 <b>Заказ закреплён за исполнителем</b>\n"
                f"Исполнитель: {master_mention}"
            )

    return (
        f"📋 <b>Новый заказ №{order['id']}</b>\n\n"
        f"🧭 Направление: <b>{direction}</b>\n"
        f"📝 Тип работы: <b>{work_type}</b>\n"
        f"📅 Дедлайн: <b>{deadline}</b>\n"
        f"📎 Файлов: <b>{files_count}</b>\n\n"
        f"{assignment_text}\n\n"
        f"💬 <b>Описание:</b>\n"
        f"{description}\n\n"
        "💬 <b>Как отвечать клиенту:</b>\n"
        "После закрепления заказа просто отправляйте "
        "сообщения в этой теме. Бот автоматически "
        "передаст их заказчику."
    )


async def _get_assigned_master(
    order,
):
    master_id = order[
        "assigned_master_telegram_id"
    ]

    if master_id is None:
        return None

    return await get_master_by_telegram_id_any(
        int(master_id)
    )


async def create_order_assignment_topic(
    bot: Bot,
    order_id: int,
    *,
    expected_assignment_generation: int | None = None,
) -> int:
    """
    Создаёт рабочую тему текущего назначения.

    Для конкретного исполнителя:
    - в карточке показывается @username вместо цифрового ID;
    - отдельное сообщение с mention уведомляет исполнителя.

    Для свободного заказа mention появится после того,
    как конкретный исполнитель заберёт заказ.

    REL-01: сразу после создания темы фиксируется durable-контрольная
    точка, поэтому прерванная публикация возобновляется при перезапуске
    бота (recover_unfinished_topic_publications).
    """

    order = await get_order_with_client(
        order_id
    )

    if order is None:
        raise RuntimeError(
            "Не удалось найти заказ для создания темы."
        )

    files = await get_order_files(
        order_id
    )

    direction = str(
        order["direction"]
        or "Без направления"
    )

    assignment_status = str(
        order["assignment_status"]
        or "assigned"
    )

    master = await _get_assigned_master(
        order
    )

    generation = int(order["assignment_generation"])
    if expected_assignment_generation is not None and generation != expected_assignment_generation:
        raise RuntimeError("Назначение изменилось до создания темы.")
    if not await reserve_topic_creation(order_id, MASTER_GROUP_ID, generation):
        checkpoint = await get_topic_checkpoint(order_id)
        if (
            checkpoint and checkpoint["state"] == "published"
            and int(checkpoint["assignment_generation"]) == generation
        ):
            return int(checkpoint["topic_id"])
        raise RuntimeError("Создание темы уже начато. Требуется восстановление или ручная сверка.")

    if assignment_status == "open":
        topic_name = (
            f"🟢 Свободный | Заказ №{order_id} — "
            f"{direction[:38]}"
        )
    else:
        topic_name = (
            f"Заказ №{order_id} — {direction[:50]}"
        )

    topic = await bot.create_forum_topic(
        chat_id=MASTER_GROUP_ID,
        name=topic_name,
    )

    topic_id = int(
        topic.message_thread_id
    )

    # REL-01: контрольная точка ДО отправки карточки и вложений —
    # подтверждение хотя бы одного известного topic_id.
    await upsert_topic_checkpoint(
        order_id=order_id,
        group_chat_id=MASTER_GROUP_ID,
        topic_id=topic_id,
        state="created",
        assignment_generation=generation,
    )

    if not await link_checkpoint_topic(order_id, MASTER_GROUP_ID, topic_id, generation):
        raise RuntimeError("Назначение изменилось; известная тема сохранена для ручной сверки.")

    if not await claim_topic_publication(order_id, generation):
        raise RuntimeError("Публикация темы уже начата другим обработчиком.")

    try:
        await _publish_order_in_topic(
            bot,
            order=order,
            files=files,
            topic_id=topic_id,
            assignment_status=assignment_status,
            master=master,
        )
    except BaseException as error:
        # Контрольные точки нужны и при отмене задачи asyncio
        # (CancelledError) — поэтому BaseException, а не Exception.
        try:
            checkpoint = await get_topic_checkpoint(order_id)
            safe_retry = (
                isinstance(error, TelegramBadRequest)
                and checkpoint and checkpoint["detail"] == "card_requested"
            )
            await mark_topic_checkpoint_state(
                order_id=order_id,
                state="failed",
                detail="card_rejected_safe_retry" if safe_retry else "publication_partial_or_unknown_manual_review",
            )
        except Exception:
            logger.exception(
                "Ошибка перезаписи контрольной точки темы заказа %s",
                order_id,
            )
        raise

    await mark_topic_checkpoint_state(
        order_id=order_id,
        state="published",
    )

    return topic_id


async def _publish_order_in_topic(
    bot: Bot,
    *,
    order,
    files,
    topic_id: int,
    assignment_status: str,
    master,
) -> int:
    """
    Публикует карточку заказа, упоминание исполнителя и вложения
    в уже созданную тему, сохраняет ссылки в БД и отправляет
    уведомления. Вызывается и из create_order_assignment_topic,
    и из восстановления незавершённых публикаций (REL-01).

    Возвращает message_id карточки.
    """

    order_id = int(order["id"])

    await mark_topic_checkpoint_state(order_id, "publishing", "card_requested")
    card_message = await bot.send_message(
        chat_id=MASTER_GROUP_ID,
        message_thread_id=topic_id,
        text=build_group_order_card(
            order=order,
            files_count=len(files),
            master=master,
        ),
        parse_mode="HTML",
        reply_markup=master_order_status_keyboard(
            order_id=order_id,
            current_status=str(
                order["status"]
            ),
            assignment_status=assignment_status,
        ),
    )

    await mark_topic_checkpoint_state(order_id, "publishing", "card_sent_partial_manual_review")
    card_saved = await link_checkpoint_topic(
        order_id, MASTER_GROUP_ID, topic_id, int(order["assignment_generation"]), int(card_message.message_id)
    )
    if not card_saved:
        raise RuntimeError("Не удалось сохранить карточку заказа.")

    # Отдельное сообщение с настоящим Telegram mention.
    # Именно это сообщение должно дать исполнителю уведомление
    # о назначенном заказе при обычных настройках Telegram.
    assigned_master_id = order[
        "assigned_master_telegram_id"
    ]

    if (
        assignment_status == "assigned"
        and assigned_master_id is not None
    ):
        master_mention = _master_group_mention(
            master,
            int(
                assigned_master_id
            ),
        )

        await bot.send_message(
            chat_id=MASTER_GROUP_ID,
            message_thread_id=topic_id,
            text=(
                f"🔔 {master_mention}, "
                f"<b>вам назначен заказ №{order_id}</b>.\n\n"
                "Ознакомьтесь с заданием и предложите "
                "стоимость выполнения."
            ),
            parse_mode="HTML",
        )

    for file_data in files:
        telegram_file_id = file_data[
            "telegram_file_id"
        ]

        file_type = file_data[
            "file_type"
        ]

        if file_type == "photo":
            await bot.send_photo(
                chat_id=MASTER_GROUP_ID,
                message_thread_id=topic_id,
                photo=telegram_file_id,
            )

        elif file_type == "document":
            await bot.send_document(
                chat_id=MASTER_GROUP_ID,
                message_thread_id=topic_id,
                document=telegram_file_id,
                caption=(
                    file_data["file_name"]
                    or None
                ),
            )

    # Исходные материалы заказа пришли до включения клиентского диалога:
    # сохраняем их отдельно, не полагаясь на историю Telegram-темы.
    try:
        from services.chat_capture import capture_initial_order
        await capture_initial_order(bot, order_id)
    except Exception as error:
        logger.exception('Ошибка сохранения исходных материалов заказа %s', order_id)
        from services.chat_capture import capture_error
        await capture_error(MASTER_GROUP_ID, topic_id, str(error))

    # Уведомляем только для режима свободного исполнителя.
    # Уведомления отправляются после сохранения темы и карточки,
    # чтобы кнопка сразу вела на существующее сообщение.
    if assignment_status == "open":
        await _notify_eligible_masters_about_open_order(
            bot,
            order=order,
            card_message_id=int(
                card_message.message_id
            ),
        )

    elif assignment_status == "assigned":
        await _notify_assigned_master_about_order(
            bot,
            order=order,
            card_message_id=int(
                card_message.message_id
            ),
        )

    return int(
        card_message.message_id
    )


async def recover_unfinished_topic_publications(
    bot: Bot,
) -> list[int]:
    """
    REL-01: возобновляет незавершённые публикации тем заказов
    после перезапуска бота.

    Автоматически обрабатываются только known-topic без отправок и
    явный отказ Telegram на первой карточке. Частичные/неизвестные
    отправки и старые генерации сохраняются для ручной сверки.

    Возвращает список заказов, публикация которых была завершена.
    """

    checkpoints = await list_unfinished_topic_checkpoints()

    if not checkpoints:
        return []

    recovered_orders: list[int] = []

    for checkpoint in checkpoints:
        order_id = int(
            checkpoint["order_id"]
        )

        try:
            order = await get_order_with_client(
                order_id
            )

            if order is None:
                # Заказ исчез после перезапуска — точка неактуальна.
                await delete_topic_checkpoint(
                    order_id=order_id,
                )
                continue

            stored_topic_id = order[
                "topic_id"
            ]

            # Never infer "not attempted" from NULL topic fields on legacy
            # orders. Only the explicit atomic creation marker is retryable.
            if (
                checkpoint["state"] == "failed"
                and checkpoint["detail"] == "local_creation_pending"
                and int(checkpoint["topic_id"]) == 0
                and int(checkpoint["group_chat_id"]) in (0, MASTER_GROUP_ID)
                and int(checkpoint["assignment_generation"]) == int(order["assignment_generation"])
                and order["status"] == "new"
                and order["assignment_status"] in ("assigned", "open")
                and stored_topic_id is None and order["group_chat_id"] is None
            ):
                await create_order_assignment_topic(
                    bot, order_id, expected_assignment_generation=int(order["assignment_generation"]),
                )
                recovered_orders.append(order_id)
                continue

            if (
                (stored_topic_id is not None
                and int(stored_topic_id)
                != int(checkpoint["topic_id"]))
                or int(checkpoint["assignment_generation"]) != int(order["assignment_generation"])
                or order["status"] != "new"
                or order["assignment_status"] not in ("assigned", "open")
                or int(checkpoint["group_chat_id"]) != MASTER_GROUP_ID
                or int(checkpoint["topic_id"]) <= 0
            ):
                logger.warning("Тема заказа %s требует ручной сверки; автоматическая публикация запрещена", order_id)
                continue

            generation = int(order["assignment_generation"])
            if not await claim_topic_publication(order_id, generation):
                continue
            if not await link_checkpoint_topic(order_id, MASTER_GROUP_ID, int(checkpoint["topic_id"]), generation):
                continue

            files = await get_order_files(
                order_id
            )

            assignment_status = str(
                order["assignment_status"]
                or "assigned"
            )

            master = await _get_assigned_master(
                order
            )

            await _publish_order_in_topic(
                bot,
                order=order,
                files=files,
                topic_id=int(
                    checkpoint["topic_id"]
                ),
                assignment_status=assignment_status,
                master=master,
            )

        except Exception as error:
            logger.error("Не удалось возобновить публикацию заказа %s: %s", order_id, type(error).__name__)
            continue

        await mark_topic_checkpoint_state(order_id, "published")

        recovered_orders.append(
            order_id
        )

    return recovered_orders


async def archive_order_topic(
    bot: Bot,
    group_chat_id: int | None,
    topic_id: int | None,
    order_id: int,
) -> None:
    """
    Архивирует старую попытку обсуждения.
    Ошибка Telegram не должна менять состояние заказа в БД.
    """

    if (
        group_chat_id is None
        or topic_id is None
    ):
        return

    try:
        await bot.edit_forum_topic(
            chat_id=int(
                group_chat_id
            ),
            message_thread_id=int(
                topic_id
            ),
            name=(
                f"📦 Архив | Заказ №{order_id}"
            ),
        )
    except TelegramBadRequest:
        pass

    try:
        await close_and_record_topic(bot, int(group_chat_id), int(topic_id))
    except TelegramBadRequest:
        pass


async def close_and_record_topic(bot: Bot, group_chat_id: int, topic_id: int) -> None:
    """Единый close lifecycle; сетевой сбой не означает закрытие."""
    try:
        await bot.close_forum_topic(chat_id=group_chat_id, message_thread_id=topic_id)
    except TelegramBadRequest as error:
        if "topic_not_modified" not in str(error).lower():
            raise
    try:
        await record_archive_topic_closed(group_chat_id, topic_id)
    except Exception:
        logger.exception("Telegram-тема закрыта; запись closed_at требует сверки")


async def refresh_order_card(
    bot: Bot,
    order_id: int,
) -> bool:
    """
    Полностью обновляет основную карточку заказа.

    Раньше обновлялась только клавиатура. Теперь после взятия
    свободного заказа текст тоже меняется: вместо режима
    «Свободный исполнитель» появляется @username исполнителя.
    """

    order = await get_order_with_client(
        order_id
    )

    if order is None:
        return False

    if (
        order["group_chat_id"] is None
        or order["topic_id"] is None
        or order[
            "order_card_message_id"
        ] is None
    ):
        return False

    files = await get_order_files(
        order_id
    )

    master = await _get_assigned_master(
        order
    )

    try:
        await bot.edit_message_text(
            chat_id=int(
                order["group_chat_id"]
            ),
            message_id=int(
                order[
                    "order_card_message_id"
                ]
            ),
            text=build_group_order_card(
                order=order,
                files_count=len(files),
                master=master,
            ),
            parse_mode="HTML",
            reply_markup=(
                master_order_status_keyboard(
                    order_id=order_id,
                    current_status=str(
                        order["status"]
                    ),
                    assignment_status=str(
                        order[
                            "assignment_status"
                        ]
                        or "assigned"
                    ),
                )
            ),
        )

        return True

    except TelegramBadRequest as exc:
        # Повторный refresh без реальных изменений —
        # нормальный сценарий, а не ошибка.
        if (
            "message is not modified"
            in str(exc).lower()
        ):
            return True

        return False
