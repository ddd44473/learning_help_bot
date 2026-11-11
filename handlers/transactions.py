import csv
import html
import logging
import math
from services.export_jobs import bounded_export
from services.archive_worker import disk_call
from utils.export_limits import ExportLimitError, LimitedTextIO, checked_output
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message,
)

from config import ADMIN_IDS
from utils.admin_access import require_private_admin
from utils.export_limits import safe_csv_cell
from database.balance import get_user_balance
from database.transactions import (
    get_all_transactions,
    get_all_transactions_count,
    get_transaction_by_id,
    get_transactions_for_export,
    get_user_by_internal_id,
    get_user_transactions,
    get_user_transactions_count,
)
from database.users import (
    get_user_by_bot_nickname,
    get_user_by_telegram_id,
    get_user_by_tg_username,
)
from utils.moscow_time import format_db_datetime, now_moscow

from keyboards.transactions import (
    admin_transactions_keyboard,
    user_transactions_keyboard,
)


router = Router()
logger = logging.getLogger(__name__)

PAGE_SIZE = 5

PERIOD_TITLES = {
    "day": "за последние 24 часа",
    "week": "за последние 7 дней",
    "all": "за всё время",
}

TRANSACTION_TITLES = {
    "admin_deposit": "Пополнение баланса",
    "admin_withdrawal": "Списание администратором",
    "payment": "Оплата заказа",
    "refund": "Возврат средств",
    "bonus": "Бонус",
}


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def _safe_page(page: int, total_count: int) -> tuple[int, int]:
    total_pages = max(1, math.ceil(total_count / PAGE_SIZE))
    safe_page = min(max(page, 0), total_pages - 1)
    return safe_page, total_pages


def _format_amount(amount: int) -> str:
    sign = "+" if amount > 0 else "−"
    formatted = f"{abs(amount):,}".replace(",", " ")
    return f"{sign}{formatted} ₽"


def _amount_icon(amount: int) -> str:
    return "🟢" if amount > 0 else "🔴"


def _format_msk_datetime(value: object) -> str:
    return format_db_datetime(
        value,
        output_format="%d.%m.%Y · %H:%M МСК",
        empty_value="Не указано",
    )


def _format_msk_datetime_csv(value: object) -> str:
    return format_db_datetime(
        value,
        output_format="%d.%m.%Y %H:%M:%S",
        empty_value="",
    )


def _transaction_title(
    transaction_type: object,
    order_id: object,
) -> str:
    transaction_type_str = str(transaction_type or "")
    base_title = TRANSACTION_TITLES.get(
        transaction_type_str,
        transaction_type_str,
    )

    if (
        order_id is not None
        and transaction_type_str in {"payment", "refund"}
    ):
        return f"{base_title} по заказу №{int(order_id)}"

    return base_title


def _display_user_name(row) -> str:
    if row["tg_username"]:
        return "@" + html.escape(str(row["tg_username"]))
    if row["bot_nickname"]:
        return html.escape(str(row["bot_nickname"]))
    if row["tg_full_name"]:
        return html.escape(str(row["tg_full_name"]))
    return f"Telegram ID {int(row['telegram_id'])}"


def _short_comment(value: object, limit: int = 1200) -> str:
    if value is None:
        return "Не указан"
    text = str(value).strip()
    if not text:
        return "Не указан"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return html.escape(text)


def _build_user_transactions_text(
    *, rows, balance: int, page: int, total_pages: int
) -> str:
    parts = [
        "💳 <b>История транзакций</b>",
        "",
        f"💰 Текущий баланс: <b>{balance} ₽</b>",
        f"📄 Страница: <b>{page + 1} из {total_pages}</b>",
    ]

    if not rows:
        parts.extend(["", "История транзакций пока пуста."])
        return "\n".join(parts)

    for row in rows:
        amount = int(row["amount"])
        parts.extend(
            [
                "",
                "────────────",
                f"{_amount_icon(amount)} <b>{_format_amount(amount)}</b>",
                "📌 " + html.escape(
                    _transaction_title(
                        row["transaction_type"], row["order_id"]
                    )
                ),
                "🕒 " + html.escape(
                    _format_msk_datetime(row["created_at"])
                ),
                (
                    "💳 "
                    f"{int(row['balance_before'])} ₽ → "
                    f"{int(row['balance_after'])} ₽"
                ),
            ]
        )

    return "\n".join(parts)


def _build_admin_user_transactions_text(
    *, user, rows, balance: int, page: int, total_pages: int
) -> str:
    telegram_username = (
        f"@{html.escape(str(user['tg_username']))}"
        if user["tg_username"]
        else "Не указан"
    )
    bot_nickname = html.escape(str(user["bot_nickname"] or "Не указан"))
    full_name = html.escape(str(user["tg_full_name"] or "Не указано"))

    parts = [
        "💳 <b>История транзакций пользователя</b>",
        "",
        f"👤 Имя: <b>{full_name}</b>",
        f"🔗 Telegram: <b>{telegram_username}</b>",
        f"👤 Никнейм в боте: <b>{bot_nickname}</b>",
        f"🆔 User ID: <code>{int(user['id'])}</code>",
        f"💰 Текущий баланс: <b>{balance} ₽</b>",
        f"📄 Страница: <b>{page + 1} из {total_pages}</b>",
    ]

    if not rows:
        parts.extend(["", "Финансовых операций пока нет."])
        return "\n".join(parts)

    for row in rows:
        amount = int(row["amount"])
        transaction_id = int(row["id"])
        parts.extend(
            [
                "",
                "────────────",
                (
                    f"<code>#{transaction_id}</code> "
                    f"{_amount_icon(amount)} "
                    f"<b>{_format_amount(amount)}</b>"
                ),
                "📌 " + html.escape(
                    _transaction_title(
                        row["transaction_type"], row["order_id"]
                    )
                ),
                "🕒 " + html.escape(
                    _format_msk_datetime(row["created_at"])
                ),
                (
                    "💳 "
                    f"{int(row['balance_before'])} ₽ → "
                    f"{int(row['balance_after'])} ₽"
                ),
            ]
        )

    return "\n".join(parts)


def _build_admin_global_transactions_text(
    *, rows, page: int, total_pages: int
) -> str:
    parts = [
        "💳 <b>Финансовые операции сервиса</b>",
        "",
        f"📄 Страница: <b>{page + 1} из {total_pages}</b>",
    ]

    if not rows:
        parts.extend(["", "Финансовых операций пока нет."])
        return "\n".join(parts)

    for row in rows:
        amount = int(row["amount"])
        transaction_id = int(row["id"])
        parts.extend(
            [
                "",
                "────────────",
                (
                    f"<code>#{transaction_id}</code> "
                    f"{_amount_icon(amount)} "
                    f"<b>{_format_amount(amount)}</b>"
                ),
                (
                    f"👤 <b>{_display_user_name(row)}</b> "
                    f"· ID {int(row['user_id'])}"
                ),
                "📌 " + html.escape(
                    _transaction_title(
                        row["transaction_type"], row["order_id"]
                    )
                ),
                "🕒 " + html.escape(
                    _format_msk_datetime(row["created_at"])
                ),
            ]
        )

    return "\n".join(parts)


async def _edit_message(
    callback: CallbackQuery,
    *, text: str, reply_markup,
) -> None:
    if callback.message is None:
        return

    try:
        await callback.message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _render_user_history(
    callback: CallbackQuery,
    *, page: int, edit: bool,
) -> None:
    if (
        not isinstance(callback.message, Message)
        or callback.message.chat.type != "private"
        or callback.message.chat.id != callback.from_user.id
    ):
        await callback.answer("История доступна только в личном чате с ботом.", show_alert=True)
        return
    user = await get_user_by_telegram_id(callback.from_user.id)

    if user is None or not user["is_registered"]:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    user_id = int(user["id"])
    total_count = await get_user_transactions_count(user_id)
    safe_page, total_pages = _safe_page(page, total_count)
    rows = await get_user_transactions(
        user_id=user_id,
        limit=PAGE_SIZE,
        offset=safe_page * PAGE_SIZE,
    )
    balance = await get_user_balance(user_id)

    if balance is None:
        await callback.answer(
            "Не удалось получить баланс.",
            show_alert=True,
        )
        return

    text = _build_user_transactions_text(
        rows=rows,
        balance=balance,
        page=safe_page,
        total_pages=total_pages,
    )
    keyboard = user_transactions_keyboard(
        page=safe_page,
        total_pages=total_pages,
    )

    if callback.message is None:
        await callback.answer()
        return

    if edit:
        await _edit_message(
            callback,
            text=text,
            reply_markup=keyboard,
        )
    else:
        await callback.message.answer(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    await callback.answer()


async def _render_admin_user_history(
    *,
    message: Message | None,
    callback: CallbackQuery | None,
    user_id: int,
    page: int,
    edit: bool,
) -> None:
    user = await get_user_by_internal_id(user_id)

    if user is None:
        if callback is not None:
            await callback.answer(
                "Пользователь не найден.",
                show_alert=True,
            )
        elif message is not None:
            await message.answer("⚠️ Пользователь не найден.")
        return

    total_count = await get_user_transactions_count(user_id)
    safe_page, total_pages = _safe_page(page, total_count)
    rows = await get_user_transactions(
        user_id=user_id,
        limit=PAGE_SIZE,
        offset=safe_page * PAGE_SIZE,
    )
    balance = await get_user_balance(user_id)
    if balance is None:
        balance = int(user["balance"] or 0)

    text = _build_admin_user_transactions_text(
        user=user,
        rows=rows,
        balance=balance,
        page=safe_page,
        total_pages=total_pages,
    )
    keyboard = admin_transactions_keyboard(
        scope="u",
        target_user_id=user_id,
        page=safe_page,
        total_pages=total_pages,
    )

    if callback is not None:
        if edit:
            await _edit_message(
                callback, text=text, reply_markup=keyboard
            )
        elif callback.message is not None:
            await callback.message.answer(
                text, reply_markup=keyboard, parse_mode="HTML"
            )
        await callback.answer()
        return

    if message is not None:
        await message.answer(
            text, reply_markup=keyboard, parse_mode="HTML"
        )


async def _render_admin_global_history(
    *,
    message: Message | None,
    callback: CallbackQuery | None,
    page: int,
    edit: bool,
) -> None:
    total_count = await get_all_transactions_count()
    safe_page, total_pages = _safe_page(page, total_count)
    rows = await get_all_transactions(
        limit=PAGE_SIZE,
        offset=safe_page * PAGE_SIZE,
    )
    text = _build_admin_global_transactions_text(
        rows=rows, page=safe_page, total_pages=total_pages
    )
    keyboard = admin_transactions_keyboard(
        scope="g",
        target_user_id=0,
        page=safe_page,
        total_pages=total_pages,
    )

    if callback is not None:
        if edit:
            await _edit_message(
                callback, text=text, reply_markup=keyboard
            )
        elif callback.message is not None:
            await callback.message.answer(
                text, reply_markup=keyboard, parse_mode="HTML"
            )
        await callback.answer()
        return

    if message is not None:
        await message.answer(
            text, reply_markup=keyboard, parse_mode="HTML"
        )


@router.callback_query(F.data == "balance_transactions")
async def open_user_transactions(callback: CallbackQuery) -> None:
    try:
        await _render_user_history(callback, page=0, edit=False)
    except Exception:
        logger.exception(
            "Ошибка открытия истории транзакций пользователя %s",
            callback.from_user.id,
        )
        await callback.answer(
            "Не удалось открыть историю транзакций.",
            show_alert=True,
        )


@router.callback_query(F.data.startswith("tx_user:"))
async def paginate_user_transactions(callback: CallbackQuery) -> None:
    if callback.data is None:
        await callback.answer()
        return

    try:
        page = int(callback.data.split(":", maxsplit=1)[1])
        await _render_user_history(callback, page=page, edit=True)
    except Exception:
        logger.exception(
            "Ошибка пагинации истории транзакций пользователя %s",
            callback.from_user.id,
        )
        await callback.answer(
            "Не удалось открыть эту страницу.",
            show_alert=True,
        )


@router.message(Command("transactions"))
async def admin_transactions_command(message: Message) -> None:
    if not await require_private_admin(message):
        return
    if message.from_user is None:
        return
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) == 1:
        try:
            await _render_admin_global_history(
                message=message,
                callback=None,
                page=0,
                edit=False,
            )
        except Exception:
            logger.exception(
                "Ошибка открытия общей истории транзакций."
            )
            await message.answer(
                "⚠️ Не удалось получить историю транзакций."
            )
        return

    username = parts[1].strip().lstrip("@")

    if not username:
        await message.answer(
            "Использование:\n\n"
            "<code>/transactions</code>\n"
            "<code>/transactions @username</code>",
            parse_mode="HTML",
        )
        return

    try:
        user = await get_user_by_tg_username(username)
        if user is None:
            user = await get_user_by_bot_nickname(username)
    except Exception:
        logger.exception(
            "Ошибка поиска пользователя для /transactions: %s",
            username,
        )
        await message.answer(
            "⚠️ Не удалось выполнить поиск пользователя."
        )
        return

    if user is None:
        await message.answer(
            "⚠️ Пользователь "
            f"<b>{html.escape(username)}</b> не найден.",
            parse_mode="HTML",
        )
        return

    try:
        await _render_admin_user_history(
            message=message,
            callback=None,
            user_id=int(user["id"]),
            page=0,
            edit=False,
        )
    except Exception:
        logger.exception(
            "Ошибка открытия истории транзакций пользователя %s",
            user["id"],
        )
        await message.answer(
            "⚠️ Не удалось получить историю транзакций пользователя."
        )


@router.callback_query(F.data.startswith("tx_admin:"))
async def paginate_admin_transactions(callback: CallbackQuery) -> None:
    if not await require_private_admin(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback.data is None:
        await callback.answer()
        return

    try:
        _, scope, target_raw, page_raw = callback.data.split(":")
        target_user_id = int(target_raw)
        page = int(page_raw)
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные страницы.",
            show_alert=True,
        )
        return

    try:
        if scope == "u":
            await _render_admin_user_history(
                message=None,
                callback=callback,
                user_id=target_user_id,
                page=page,
                edit=True,
            )
            return

        if scope == "g":
            await _render_admin_global_history(
                message=None,
                callback=callback,
                page=page,
                edit=True,
            )
            return

        await callback.answer(
            "Неизвестный режим истории.",
            show_alert=True,
        )
    except Exception:
        logger.exception(
            "Ошибка пагинации административной истории транзакций."
        )
        await callback.answer(
            "Не удалось открыть эту страницу.",
            show_alert=True,
        )


@router.message(Command("transaction"))
async def admin_transaction_details(message: Message) -> None:
    if not await require_private_admin(message):
        return
    if message.from_user is None:
        return
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer(
            "Использование:\n\n<code>/transaction 148</code>",
            parse_mode="HTML",
        )
        return

    try:
        transaction_id = int(parts[1])
        if transaction_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "ID транзакции должен быть положительным числом."
        )
        return

    try:
        row = await get_transaction_by_id(transaction_id)
    except Exception:
        logger.exception(
            "Ошибка получения транзакции %s", transaction_id
        )
        await message.answer("⚠️ Не удалось получить транзакцию.")
        return

    if row is None:
        await message.answer("⚠️ Транзакция не найдена.")
        return

    amount = int(row["amount"])
    telegram_username = (
        f"@{html.escape(str(row['tg_username']))}"
        if row["tg_username"]
        else "Не указан"
    )
    bot_nickname = html.escape(
        str(row["bot_nickname"] or "Не указан")
    )
    order_text = (
        f"№{int(row['order_id'])}"
        if row["order_id"] is not None
        else "Не связан с заказом"
    )
    admin_text = (
        str(int(row["admin_telegram_id"]))
        if row["admin_telegram_id"] is not None
        else "Нет"
    )

    text = (
        f"💳 <b>Транзакция №{transaction_id}</b>\n\n"
        f"📌 Тип: <b>{html.escape(_transaction_title(row['transaction_type'], row['order_id']))}</b>\n"
        f"💰 Сумма: <b>{_format_amount(amount)}</b>\n"
        f"🕒 Дата: <b>{html.escape(_format_msk_datetime(row['created_at']))}</b>\n\n"
        f"👤 Пользователь: <b>{telegram_username}</b>\n"
        f"👤 Никнейм в боте: <b>{bot_nickname}</b>\n"
        f"🆔 User ID: <code>{int(row['user_id'])}</code>\n"
        f"🆔 Telegram ID: <code>{int(row['telegram_id'])}</code>\n\n"
        f"💳 Баланс: <b>{int(row['balance_before'])} ₽ → {int(row['balance_after'])} ₽</b>\n"
        f"📦 Заказ: <b>{order_text}</b>\n"
        f"🛡 Администратор Telegram ID: <code>{html.escape(admin_text)}</code>\n\n"
        f"📝 Комментарий:\n{_short_comment(row['comment'])}"
    )

    await message.answer(text, parse_mode="HTML")


def _build_transactions_csv(rows) -> bytes:
    buffer = LimitedTextIO(newline="")
    writer = csv.writer(
        buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL
    )
    writer.writerow(
        [
            "Дата и время (МСК)",
            "ID транзакции",
            "Тип",
            "Сумма",
            "Баланс до",
            "Баланс после",
            "User ID",
            "Telegram ID",
            "Telegram username",
            "Никнейм в боте",
            "ID заказа",
            "Telegram ID администратора",
            "Комментарий",
        ]
    )

    for row in rows:
        transaction_type = str(row["transaction_type"] or "")
        writer.writerow(
            [
                _format_msk_datetime_csv(row["created_at"]),
                int(row["id"]),
                TRANSACTION_TITLES.get(
                    transaction_type, transaction_type
                ),
                int(row["amount"]),
                int(row["balance_before"]),
                int(row["balance_after"]),
                int(row["user_id"]),
                int(row["telegram_id"]),
                safe_csv_cell(row["tg_username"] or ""),
                safe_csv_cell(row["bot_nickname"] or ""),
                row["order_id"] if row["order_id"] is not None else "",
                (
                    row["admin_telegram_id"]
                    if row["admin_telegram_id"] is not None
                    else ""
                ),
                safe_csv_cell(row["comment"] or ""),
            ]
        )

    data = buffer.getvalue().encode("utf-8-sig")
    buffer.close()
    return checked_output(data)


@router.callback_query(F.data.startswith("tx_csv:"))
@bounded_export
async def export_transactions_csv(callback: CallbackQuery) -> None:
    if not await require_private_admin(callback):
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    if callback.data is None:
        await callback.answer()
        return

    try:
        _, scope, target_raw, period = callback.data.split(":")
        target_user_id = int(target_raw)
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректные данные выгрузки.",
            show_alert=True,
        )
        return

    if period not in PERIOD_TITLES:
        await callback.answer("Неизвестный период.", show_alert=True)
        return
    if scope not in {"u", "g"}:
        await callback.answer(
            "Неизвестный режим выгрузки.",
            show_alert=True,
        )
        return

    try:
        rows = await get_transactions_for_export(
            period=period,
            user_id=target_user_id if scope == "u" else None,
        )
    except ExportLimitError:
        raise
    except Exception:
        logger.exception("Ошибка получения транзакций для CSV.")
        await callback.answer(
            "Не удалось получить данные для CSV.",
            show_alert=True,
        )
        return

    if not rows:
        await callback.answer(
            "За выбранный период транзакций нет.",
            show_alert=True,
        )
        return

    try:
        csv_data = await disk_call(_build_transactions_csv, rows)
    except ExportLimitError:
        raise
    except Exception:
        logger.exception(
            "Ошибка формирования CSV истории транзакций."
        )
        await callback.answer(
            "Не удалось сформировать CSV.",
            show_alert=True,
        )
        return

    current_date = now_moscow().strftime("%Y-%m-%d")
    scope_name = (
        f"user_{target_user_id}" if scope == "u" else "all_users"
    )
    filename = (
        f"transactions_{scope_name}_{period}_{current_date}.csv"
    )
    document = BufferedInputFile(csv_data, filename=filename)

    if callback.message is None:
        await callback.answer(
            "Сообщение недоступно.",
            show_alert=True,
        )
        return

    await callback.message.answer_document(
        document=document,
        caption=(
            "💳 <b>История транзакций</b>\n\n"
            f"📅 Период: <b>{PERIOD_TITLES[period]}</b>\n"
            f"📊 Операций: <b>{len(rows)}</b>"
        ),
        parse_mode="HTML",
    )
    await callback.answer("CSV сформирован.")


@router.callback_query(F.data == "tx_noop")
async def transactions_noop(callback: CallbackQuery) -> None:
    await callback.answer()
