import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    Message,
    ReplyKeyboardRemove,
)

from database.orders import get_user_order
from database.users import get_internal_user_id
from keyboards.order_chat import (
    EXIT_ORDER_CHAT_TEXT,
    exit_order_chat_keyboard,
)
from states.order_chat import OrderChatState
from utils.order_statuses import is_order_active
from database.master_response_times import (
    start_master_response_waiting,
)


from database.chat_archive import update_delivery
from utils.chat_content import is_relay_content
from services.chat_capture import capture_client_message, capture_error

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("write_order:"))
async def enter_order_chat(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """
    Включает режим диалога клиента с исполнителем
    по конкретному активному заказу.
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
        await state.clear()

        await callback.answer(
            "Диалог доступен только для активных заказов.",
            show_alert=True,
        )
        return

    group_chat_id = order["group_chat_id"]
    topic_id = order["topic_id"]

    if group_chat_id is None or topic_id is None:
        await callback.answer(
            "Диалог по этому заказу пока недоступен. "
            "Попробуйте немного позже.",
            show_alert=True,
        )
        return

    previous_data = await state.get_data()
    previous_order_id = previous_data.get("order_id")

    await state.set_state(
        OrderChatState.active
    )

    await state.set_data(
        {
            "order_id": order_id,
            "group_chat_id": group_chat_id,
            "topic_id": topic_id,
            "subject": str(
                order["subject"] or "Не указан"
            ),
        }
    )

    if (
        previous_order_id is not None
        and previous_order_id != order_id
    ):
        title = (
            f"🔄 <b>Вы переключились на диалог "
            f"по заказу №{order_id}</b>"
        )
    else:
        title = (
            f"💬 <b>Вы вошли в диалог "
            f"по заказу №{order_id}</b>"
        )

    subject = html.escape(
        str(order["subject"] or "Не указан")
    )

    await callback.message.answer(
        f"{title}\n\n"
        f"📘 Предмет: <b>{subject}</b>\n\n"
        "Теперь все сообщения и файлы, которые вы отправите "
        "боту, будут автоматически переданы исполнителю.\n\n"
        "Чтобы завершить общение, нажмите кнопку "
        f"«{EXIT_ORDER_CHAT_TEXT}».",
        reply_markup=exit_order_chat_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(OrderChatState.active)
async def block_callbacks_inside_order_chat(
    callback: CallbackQuery,
) -> None:
    """
    Блокирует остальные inline-кнопки,
    пока клиент находится в активном диалоге.
    """

    await callback.answer(
        "Сначала выйдите из активного диалога "
        "с помощью кнопки под полем ввода.",
        show_alert=True,
    )


# @router.message(
#     OrderChatState.active,
#     F.text == EXIT_ORDER_CHAT_TEXT,
# )
# async def exit_order_chat(
#     message: Message,
#     state: FSMContext,
# ) -> None:
#     """
#     Выход клиента из активного диалога.
#     """

#     data = await state.get_data()
#     order_id = data.get("order_id")

#     await state.clear()

#     if order_id is not None:
#         text = (
#             f"✅ Вы вышли из диалога по заказу №{order_id}.\n\n"
#             "При необходимости вы сможете снова открыть его "
#             "через раздел «Мои заказы»."
#         )
#     else:
#         text = (
#             "✅ Вы вышли из диалога.\n\n"
#             "При необходимости вы сможете снова открыть его "
#             "через раздел «Мои заказы»."
#         )

#     await message.answer(
#         text,
#         reply_markup=ReplyKeyboardRemove(),
#     )

@router.message(
    OrderChatState.active,
    F.text == EXIT_ORDER_CHAT_TEXT,
)
async def exit_order_chat(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Закрывает активный диалог клиента с исполнителем
    и удаляет Reply-клавиатуру.
    """

    data = await state.get_data()
    order_id = data.get("order_id")

    await state.clear()

    if order_id is not None:
        text = (
            f"✅ Вы вышли из диалога по заказу №{order_id}.\n\n"
            "При необходимости вы сможете снова открыть его "
            "через раздел «Мои заказы»."
        )
    else:
        text = (
            "✅ Вы вышли из диалога.\n\n"
            "При необходимости вы сможете снова открыть его "
            "через раздел «Мои заказы»."
        )

    await message.answer(
        text,
        reply_markup=ReplyKeyboardRemove(),
    )


# @router.message(
#     OrderChatState.active,
#     F.text.startswith("/"),
# )
# async def block_commands_inside_order_chat(
#     message: Message,
# ) -> None:
#     """
#     Не позволяет случайно отправить команду исполнителю.
#     """

#     await message.answer(
#         "Сейчас вы находитесь в диалоге по заказу.\n\n"
#         f"Чтобы выйти, нажмите «{EXIT_ORDER_CHAT_TEXT}».",
#         reply_markup=exit_order_chat_keyboard(),
#     )

# @router.message(
#     OrderChatState.active,
#     F.text == EXIT_ORDER_CHAT_TEXT,
# )
# async def exit_order_chat(
#     message: Message,
#     state: FSMContext,
# ) -> None:
#     await state.clear()

#     await message.answer(
#         "✅ Вы вышли из диалога по заказу.",
#         reply_markup=ReplyKeyboardRemove(),
#     )


@router.message(OrderChatState.active, is_relay_content)
async def send_client_message_to_order_topic(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> None:
    """
    Перенаправляет сообщение клиента
    в Telegram-тему соответствующего заказа.

    Перед каждой отправкой повторно проверяет,
    что заказ всё ещё активен.

    После успешной доставки сообщения в тему
    запускает замер времени ответа исполнителя.
    Если незакрытый замер уже существует,
    новый не создаётся.
    """

    data = await state.get_data()

    order_id = data.get("order_id")
    group_chat_id = data.get("group_chat_id")
    topic_id = data.get("topic_id")

    if (
        order_id is None
        or group_chat_id is None
        or topic_id is None
    ):
        await state.clear()

        await message.answer(
            "Не удалось определить активный заказ.\n\n"
            "Откройте диалог снова через раздел «Мои заказы».",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if message.from_user is None:
        await state.clear()

        await message.answer(
            "Не удалось определить отправителя сообщения.\n\n"
            "Откройте диалог снова через раздел «Мои заказы».",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    internal_user_id = await get_internal_user_id(
        message.from_user.id
    )

    if internal_user_id is None:
        await state.clear()

        await message.answer(
            "Не удалось определить ваш профиль.\n\n"
            "Попробуйте пройти регистрацию заново.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    order = await get_user_order(
        order_id=order_id,
        user_id=internal_user_id,
    )

    if order is None:
        await state.clear()

        await message.answer(
            "Заказ не найден или больше недоступен.\n\n"
            "Диалог был закрыт.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if not is_order_active(
        str(order["status"])
    ):
        await state.clear()

        await message.answer(
            f"⚠️ Заказ №{order_id} уже завершён или отменён.\n\n"
            "Отправка сообщений по этому заказу "
            "больше недоступна.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    current_group_chat_id = order[
        "group_chat_id"
    ]

    current_topic_id = order[
        "topic_id"
    ]

    assigned_master_telegram_id = order[
        "assigned_master_telegram_id"
    ]

    if (
        current_group_chat_id is None
        or current_topic_id is None
    ):
        await state.clear()

        await message.answer(
            "Диалог по этому заказу сейчас недоступен.\n\n"
            "Попробуйте открыть его позже "
            "через раздел «Мои заказы».",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    # Не прерываем переписку при проблеме архива, но отмечаем
    # такую тему небезопасной для будущего удаления.
    archive_message_id = None
    try:
        archive_message_id = await capture_client_message(
            message, bot, order_id=int(order_id),
            group_chat_id=int(current_group_chat_id),
            topic_id=int(current_topic_id),
        )
    except Exception as error:
        logger.exception('Ошибка сохранения сообщения клиента заказа %s', order_id)
        await capture_error(int(current_group_chat_id), int(current_topic_id), str(error))

    try:
        # Сначала реально доставляем сообщение клиента
        # в рабочую тему заказа.
        copied_message = await bot.copy_message(
            chat_id=int(
                current_group_chat_id
            ),
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=int(
                current_topic_id
            ),
        )
        if archive_message_id is not None:
            try:
                await update_delivery(
                    archive_message_id, int(current_group_chat_id),
                    int(copied_message.message_id),
                )
            except Exception as error:
                logger.exception('Ошибка записи ID доставленного сообщения заказа %s', order_id)
                await capture_error(int(current_group_chat_id), int(current_topic_id), str(error))

        # Запускаем замер только после успешной
        # доставки сообщения в тему.
        if assigned_master_telegram_id is not None:
            try:
                started = (
                    await start_master_response_waiting(
                        order_id=int(order_id),
                        master_telegram_id=int(
                            assigned_master_telegram_id
                        ),
                    )
                )

                if started:
                    logger.info(
                        "Запущен замер времени ответа исполнителя. "
                        "Заказ: %s, исполнитель: %s",
                        order_id,
                        assigned_master_telegram_id,
                    )

                else:
                    logger.debug(
                        "Замер времени ответа уже активен. "
                        "Новый замер не создан. "
                        "Заказ: %s, исполнитель: %s",
                        order_id,
                        assigned_master_telegram_id,
                    )

            except Exception:
                # Ошибка статистики не должна мешать
                # основной переписке клиента и исполнителя.
                logger.exception(
                    "Не удалось запустить замер "
                    "времени ответа исполнителя. "
                    "Заказ: %s, исполнитель: %s",
                    order_id,
                    assigned_master_telegram_id,
                )

    except TelegramForbiddenError:
        logger.exception(
            "У бота нет доступа к группе заказа %s",
            order_id,
        )

        await message.answer(
            "Не удалось отправить сообщение: "
            "у бота нет доступа к группе исполнителей.\n\n"
            "Попробуйте позже или обратитесь в поддержку.",
            reply_markup=exit_order_chat_keyboard(),
        )
        return

    except TelegramBadRequest as error:
        logger.exception(
            "Telegram отклонил сообщение "
            "по заказу %s: %s",
            order_id,
            error,
        )

        await message.answer(
            "Этот тип сообщения пока не удалось "
            "передать исполнителю.\n"
            "Попробуйте отправить его "
            "в другом формате.",
            reply_markup=exit_order_chat_keyboard(),
        )
        return

    except Exception:
        logger.exception(
            "Неожиданная ошибка при отправке "
            "сообщения клиента по заказу %s",
            order_id,
        )

        await message.answer(
            "Во время отправки сообщения "
            "произошла ошибка.\n\n"
            "Попробуйте ещё раз немного позже.",
            reply_markup=exit_order_chat_keyboard(),
        )
        return


@router.message(F.text == EXIT_ORDER_CHAT_TEXT)
async def remove_stale_order_chat_keyboard(
    message: Message,
    state: FSMContext,
) -> None:
    """
    Убирает оставшуюся клавиатуру диалога, если FSM-состояние
    уже было потеряно, например после перезапуска бота.
    """

    await state.clear()

    await message.answer(
        "✅ Диалог уже закрыт.\n\n"
        "Клавиатура диалога убрана.",
        reply_markup=ReplyKeyboardRemove(),
    )
