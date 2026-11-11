import html
import hashlib
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_IDS
from utils.admin_access import require_private_admin

from database.activity_logs import (
    log_user_activity,
)
from database.balance import (
    change_user_balance_idempotent,
    get_user_balance,
)
from database.users import (
    get_user_by_tg_username,
)


router = Router()
logger = logging.getLogger(__name__)


def is_admin(
    telegram_id: int,
) -> bool:
    """
    Проверяет, входит ли пользователь
    в список администраторов бота.
    """

    return telegram_id in ADMIN_IDS


def parse_nickname_argument(
    message: Message,
) -> str | None:
    """
    Получает никнейм из команды:

    /balance nickname
    /balance @nickname
    """

    if not message.text:
        return None

    parts = message.text.split(
        maxsplit=1,
    )

    if len(parts) != 2:
        return None

    nickname = (
        parts[1]
        .strip()
        .lstrip("@")
    )

    if not nickname:
        return None

    return nickname


def parse_money_command(
    message: Message,
) -> tuple[str, int] | None:
    """
    Разбирает команды:

    /give_money @username 500
    /take_money @username 500
    """

    if not message.text:
        return None

    parts = message.text.split()

    if len(parts) != 3:
        return None

    username = (
        parts[1]
        .strip()
        .lstrip("@")
    )

    if not username:
        return None

    try:
        amount = int(
            parts[2]
        )

    except ValueError:
        return None

    if amount <= 0:
        return None

    return (
        username,
        amount,
    )


@router.message(
    Command("balance")
)
async def show_user_balance(
    message: Message,
) -> None:
    """
    Показывает баланс клиента.

    Команда доступна только администраторам:

    /balance nickname
    /balance @nickname
    """

    if not await require_private_admin(message):
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    nickname = parse_nickname_argument(
        message
    )

    if nickname is None:
        await message.answer(
            "Использование команды:\n\n"
            "<code>/balance @nickname</code>",
            parse_mode="HTML",
        )
        return

    try:
        user = await get_user_by_tg_username(
            nickname
        )

    except Exception:
        logger.exception(
            "Ошибка поиска пользователя "
            "по никнейму %s",
            nickname,
        )

        await message.answer(
            "⚠️ Не удалось выполнить "
            "поиск пользователя."
        )
        return

    if user is None:
        await message.answer(
            "⚠️ Пользователь с таким "
            "никнеймом не найден."
        )
        return

    try:
        balance = await get_user_balance(
            int(
                user["id"]
            )
        )

    except Exception:
        logger.exception(
            "Ошибка получения баланса "
            "пользователя %s",
            user["id"],
        )

        await message.answer(
            "⚠️ Не удалось получить "
            "баланс пользователя."
        )
        return

    if balance is None:
        await message.answer(
            "⚠️ Не удалось определить "
            "баланс пользователя."
        )
        return

    telegram_username = html.escape(
        str(
            user["tg_username"]
            or nickname
        )
    )

    bot_nickname = html.escape(
        str(
            user["bot_nickname"]
            or "Не указан"
        )
    )

    await message.answer(
        "💳 <b>Баланс пользователя</b>\n\n"
        f"📱 Telegram: "
        f"<b>@{telegram_username}</b>\n"
        f"👤 Никнейм в боте: "
        f"<b>{bot_nickname}</b>\n"
        f"🆔 Внутренний ID: "
        f"<code>{user['id']}</code>\n"
        f"💰 Баланс: "
        f"<b>{balance} ₽</b>",
        parse_mode="HTML",
    )


@router.message(
    Command("give_money")
)
async def give_money(
    message: Message,
) -> None:
    """
    Начисляет пользователю рубли.

    Команда:
    /give_money @username 500

    После успешного пополнения:
    - изменение сохраняется в balance_transactions;
    - событие balance_deposited записывается
      в журнал активности пользователя;
    - в журнале сохраняется информация
      об администраторе.
    """

    if not await require_private_admin(message):
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    parsed_command = parse_money_command(
        message
    )

    if parsed_command is None:
        await message.answer(
            "Использование команды:\n\n"
            "<code>/give_money @username 500</code>\n\n"
            "Сумма должна быть целым "
            "положительным числом.",
            parse_mode="HTML",
        )
        return

    username, amount = parsed_command

    # -------------------------------------------------
    # Находим пользователя.
    # -------------------------------------------------

    try:
        user = await get_user_by_tg_username(
            username
        )

    except Exception:
        logger.exception(
            "Ошибка поиска пользователя @%s",
            username,
        )

        await message.answer(
            "⚠️ Не удалось выполнить "
            "поиск пользователя."
        )
        return

    if user is None:
        await message.answer(
            "⚠️ Пользователь с таким "
            "Telegram username не найден."
        )
        return

    user_id = int(
        user["id"]
    )

    user_telegram_id = int(
        user["telegram_id"]
    )

    # -------------------------------------------------
    # Получаем баланс до операции.
    # -------------------------------------------------

    try:
        balance_before = await get_user_balance(
            user_id
        )

    except Exception:
        logger.exception(
            "Ошибка получения баланса "
            "пользователя %s перед пополнением",
            user_id,
        )

        await message.answer(
            "⚠️ Не удалось определить "
            "текущий баланс пользователя."
        )
        return

    if balance_before is None:
        await message.answer(
            "⚠️ Не удалось определить "
            "текущий баланс пользователя."
        )
        return

    # -------------------------------------------------
    # Выполняем пополнение.
    # -------------------------------------------------

    try:
        changed, new_balance, duplicate = (
            await change_user_balance_idempotent(
                op_key=f"admin_balance:{message.bot.id}:{message.chat.id}:{message.message_id}",
                content_hash=hashlib.sha256((message.text or "").encode()).hexdigest(),
                user_id=user_id,
                amount=amount,
                transaction_type=(
                    "admin_deposit"
                ),
                admin_telegram_id=(
                    message.from_user.id
                ),
                comment=(
                    "Пополнение баланса "
                    "администратором"
                ),
            )
        )

    except Exception:
        logger.exception(
            "Ошибка пополнения баланса "
            "пользователя %s",
            user_id,
        )

        await message.answer(
            "⚠️ Не удалось пополнить баланс."
        )
        return

    if (
        not changed
        or new_balance is None
    ):
        await message.answer(
            "⚠️ Баланс пользователя "
            "не был изменён."
        )
        return

    if duplicate:
        await message.answer("ℹ️ Эта операция уже обработана; повторного начисления нет.")
        return
    balance_before = new_balance - amount

    # -------------------------------------------------
    # Логируем пополнение в журнал пользователя.
    # -------------------------------------------------

    admin_username = (
        message.from_user.username
        if message.from_user.username
        else None
    )

    admin_full_name = (
        message.from_user.full_name
    )

    try:
        await log_user_activity(
            telegram_id=user_telegram_id,
            user_id=user_id,
            action="balance_deposited",
            entity_type="balance",
            entity_id=None,
            details={
                "amount": amount,
                "balance_before": int(
                    balance_before
                ),
                "balance_after": int(
                    new_balance
                ),
                "admin_telegram_id": (
                    message.from_user.id
                ),
                "admin_username": (
                    admin_username
                ),
                "admin_full_name": (
                    admin_full_name
                ),
            },
        )

    except Exception:
        # Деньги уже начислены.
        # Ошибка журнала не должна
        # влиять на выполненную операцию.
        logger.exception(
            "Не удалось записать событие "
            "balance_deposited "
            "для пользователя %s",
            user_id,
        )

    # -------------------------------------------------
    # Подтверждаем операцию администратору.
    # -------------------------------------------------

    telegram_username = html.escape(
        str(
            user["tg_username"]
            or username
        )
    )

    await message.answer(
        "✅ <b>Баланс пополнен</b>\n\n"
        f"📱 Пользователь: "
        f"<b>@{telegram_username}</b>\n"
        f"➕ Начислено: "
        f"<b>{amount} ₽</b>\n"
        f"💳 Новый баланс: "
        f"<b>{new_balance} ₽</b>",
        parse_mode="HTML",
    )

    # -------------------------------------------------
    # Уведомляем пользователя.
    # -------------------------------------------------

    try:
        await message.bot.send_message(
            chat_id=user_telegram_id,
            text=(
                "💰 <b>Ваш баланс пополнен</b>\n\n"
                f"Начислено: "
                f"<b>{amount} ₽</b>\n"
                f"Текущий баланс: "
                f"<b>{new_balance} ₽</b>"
            ),
            parse_mode="HTML",
        )

    except Exception:
        logger.warning(
            "Баланс пополнен, "
            "но пользователя %s "
            "не удалось уведомить.",
            user_telegram_id,
            exc_info=True,
        )


@router.message(
    Command("take_money")
)
async def take_money(
    message: Message,
) -> None:
    """
    Списывает рубли с баланса пользователя.

    Команда:
    /take_money @username 500

    После успешного списания:
    - изменение сохраняется в balance_transactions;
    - событие balance_withdrawn записывается
      в журнал активности пользователя;
    - в журнале сохраняется информация
      об администраторе.
    """

    if not await require_private_admin(message):
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    parsed_command = parse_money_command(
        message
    )

    if parsed_command is None:
        await message.answer(
            "Использование команды:\n\n"
            "<code>/take_money @username 500</code>\n\n"
            "Сумма должна быть целым "
            "положительным числом.",
            parse_mode="HTML",
        )
        return

    username, amount = parsed_command

    # -------------------------------------------------
    # Находим пользователя.
    # -------------------------------------------------

    try:
        user = await get_user_by_tg_username(
            username
        )

    except Exception:
        logger.exception(
            "Ошибка поиска пользователя @%s",
            username,
        )

        await message.answer(
            "⚠️ Не удалось выполнить "
            "поиск пользователя."
        )
        return

    if user is None:
        await message.answer(
            "⚠️ Пользователь с таким "
            "Telegram username не найден."
        )
        return

    user_id = int(
        user["id"]
    )

    user_telegram_id = int(
        user["telegram_id"]
    )

    # -------------------------------------------------
    # Получаем баланс до списания.
    # -------------------------------------------------

    try:
        balance_before = await get_user_balance(
            user_id
        )

    except Exception:
        logger.exception(
            "Ошибка получения баланса "
            "пользователя %s перед списанием",
            user_id,
        )

        await message.answer(
            "⚠️ Не удалось определить "
            "баланс пользователя."
        )
        return

    if balance_before is None:
        await message.answer(
            "⚠️ Не удалось определить "
            "баланс пользователя."
        )
        return

    # -------------------------------------------------
    # Выполняем списание.
    # -------------------------------------------------

    try:
        changed, new_balance, duplicate = (
            await change_user_balance_idempotent(
                op_key=f"admin_balance:{message.bot.id}:{message.chat.id}:{message.message_id}",
                content_hash=hashlib.sha256((message.text or "").encode()).hexdigest(),
                user_id=user_id,
                amount=-amount,
                transaction_type=(
                    "admin_withdrawal"
                ),
                admin_telegram_id=(
                    message.from_user.id
                ),
                comment=(
                    "Списание баланса "
                    "администратором"
                ),
            )
        )

    except Exception:
        logger.exception(
            "Ошибка списания баланса "
            "пользователя %s",
            user_id,
        )

        await message.answer(
            "⚠️ Не удалось списать средства."
        )
        return

    if (
        not changed
        or new_balance is None
    ):
        await message.answer(
            "⚠️ Баланс пользователя "
            "не был изменён."
        )
        return

    if duplicate:
        await message.answer("ℹ️ Эта операция уже обработана; повторного списания нет.")
        return
    balance_before = new_balance + amount

    # -------------------------------------------------
    # Логируем списание в журнал пользователя.
    # -------------------------------------------------

    admin_username = (
        message.from_user.username
        if message.from_user.username
        else None
    )

    admin_full_name = (
        message.from_user.full_name
    )

    try:
        await log_user_activity(
            telegram_id=user_telegram_id,
            user_id=user_id,
            action="balance_withdrawn",
            entity_type="balance",
            entity_id=None,
            details={
                "amount": amount,
                "balance_before": int(
                    balance_before
                ),
                "balance_after": int(
                    new_balance
                ),
                "admin_telegram_id": (
                    message.from_user.id
                ),
                "admin_username": (
                    admin_username
                ),
                "admin_full_name": (
                    admin_full_name
                ),
            },
        )

    except Exception:
        # Деньги уже списаны.
        # Ошибка журнала не должна
        # влиять на выполненную операцию.
        logger.exception(
            "Не удалось записать событие "
            "balance_withdrawn "
            "для пользователя %s",
            user_id,
        )

    # -------------------------------------------------
    # Подтверждаем операцию администратору.
    # -------------------------------------------------

    telegram_username = html.escape(
        str(
            user["tg_username"]
            or username
        )
    )

    await message.answer(
        "✅ <b>Средства списаны</b>\n\n"
        f"📱 Пользователь: "
        f"<b>@{telegram_username}</b>\n"
        f"➖ Списано: "
        f"<b>{amount} ₽</b>\n"
        f"💳 Новый баланс: "
        f"<b>{new_balance} ₽</b>",
        parse_mode="HTML",
    )

    # -------------------------------------------------
    # Уведомляем пользователя.
    # -------------------------------------------------

    try:
        await message.bot.send_message(
            chat_id=user_telegram_id,
            text=(
                "💸 <b>С вашего баланса "
                "списаны средства</b>\n\n"
                f"Списано: "
                f"<b>{amount} ₽</b>\n"
                f"Текущий баланс: "
                f"<b>{new_balance} ₽</b>"
            ),
            parse_mode="HTML",
        )

    except Exception:
        logger.warning(
            "Средства списаны, "
            "но пользователя %s "
            "не удалось уведомить.",
            user_telegram_id,
            exc_info=True,
        )
