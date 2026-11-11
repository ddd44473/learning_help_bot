import csv
import html
import json
import logging
from utils.export_limits import ExportLimitError, LimitedParts, LimitedTextIO, checked_output
from services.export_jobs import bounded_export
from services.archive_worker import disk_call
from utils.moscow_time import format_db_datetime, now_moscow

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message,
)

from config import ADMIN_IDS
from utils.admin_access import require_private_admin

from database.activity_logs import (
    get_log_actor_names,
    get_user_activity_logs_for_export,
)
from database.users import (
    get_user_by_tg_username,
)
from database.masters import get_master_by_username

from keyboards.admin_logs import (
    user_logs_period_keyboard,
)


router = Router()
logger = logging.getLogger(__name__)


PERIOD_TITLES = {
    "day": "за последние 24 часа",
    "week": "за последние 7 дней",
    "all": "за всё время",
}


ACTION_TITLES = {
    "order_created": "Создал заказ",
    "master_selected": "Выбрал исполнителя",
    "price_offered": "Исполнитель предложил цену",
    "counter_price_offered": "Предложил свою цену",
    "price_accepted": "Принял цену",
    "price_rejected": "Отклонил предложение цены",
    "payment_succeeded": "Оплатил заказ",
    "payment_failed": "Неудачная попытка оплаты",
    "balance_deposited": "Пополнение баланса",
    "balance_withdrawn": "Списание с баланса",
    "balance_refunded": "Возврат средств",
    "order_started": "Заказ передан в работу",
    "order_result_sent": "Результат отправлен клиенту",
    "order_creation_failed": "Ошибка создания заказа",
    "order_cancelled": "Отменил заказ",
    "order_status_changed": "Изменил статус заказа",
    "review_created": "Оставил оценку",
    "review_comment_added": (
        "Добавил комментарий к отзыву"
    ),
    "user_registered": "Зарегистрировался",
    "button_pressed": "Нажал кнопку (входящее событие)",
    "message_received": "Отправил сообщение (входящее событие)",
    "command_received": "Вызвал команду (входящее событие)",
    "incoming_event": "Другое входящее событие",
    "admin_banned_user": "Заблокировал пользователя",
    "admin_unbanned_user": "Разблокировал пользователя",
}


ENTITY_TITLES = {
    "order": "Заказ",
    "master": "Исполнитель",
    "review": "Отзыв",
    "balance": "Баланс",
}


STATUS_TITLES = {
    "new": "Новый",
    "in_progress": "В работе",
    "waiting_client": "Ожидает клиента",
    "completed": "Завершён",
    "cancelled": "Отменён",
}


ACTOR_TITLES = {
    "client": "клиент",
    "master": "исполнитель",
    "admin": "администратор",
    "system": "система",
}


def is_admin(
    telegram_id: int,
) -> bool:
    """
    Проверяет, входит ли пользователь
    в список администраторов бота.
    """

    return telegram_id in ADMIN_IDS


def parse_username_argument(
    message: Message,
) -> str | None:
    """
    Получает Telegram username
    из команды:

    /user_logs @username
    """

    if not message.text:
        return None

    parts = message.text.split(
        maxsplit=1,
    )

    if len(parts) != 2:
        return None

    username = (
        parts[1]
        .strip()
        .lstrip("@")
    )

    if not username:
        return None

    return username


def parse_details(
    details: str | None,
) -> dict:
    """
    Безопасно преобразует JSON из details
    в словарь.
    """

    if not details:
        return {}

    try:
        parsed = json.loads(
            details
        )

    except (
        json.JSONDecodeError,
        TypeError,
    ):
        return {}

    if not isinstance(
        parsed,
        dict,
    ):
        return {}

    return parsed


def format_status(
    status: object,
) -> str:
    """
    Преобразует технический статус заказа
    в понятное название.
    """

    normalized_status = str(
        status
        or ""
    )

    return STATUS_TITLES.get(
        normalized_status,
        normalized_status,
    )


def format_actor(
    actor: object,
) -> str:
    """
    Преобразует техническое название
    участника в понятное.
    """

    normalized_actor = str(
        actor
        or ""
    )

    return ACTOR_TITLES.get(
        normalized_actor,
        normalized_actor,
    )


def format_entity(
    row,
) -> str:
    """
    Формирует человекочитаемое
    название сущности.
    """

    entity_type = row["entity_type"]

    if entity_type is None:
        return ""

    return ENTITY_TITLES.get(
        str(entity_type),
        str(entity_type),
    )


def format_action(
    row,
) -> str:
    """
    Преобразует техническое название
    действия в человекочитаемый текст.
    """

    action = str(
        row["action"]
        or ""
    )

    return ACTION_TITLES.get(
        action,
        action,
    )


def format_details(
    row,
    actor_names: dict[int, str] | None = None,
) -> str:
    """
    Преобразует JSON details
    в понятное описание действия.
    """

    action = str(
        row["action"]
        or ""
    )

    details = parse_details(
        row["details"]
    )

    if not details:
        return ""

    def actor_name(telegram_id: object) -> str:
        return display_actor_name(telegram_id, actor_names)

    # ---------------------------------------------
    # Создание заказа
    # ---------------------------------------------

    if action == "order_created":
        parts: list[str] = []

        subject = details.get(
            "subject"
        )

        work_type = details.get(
            "work_type"
        )

        direction = details.get(
            "direction"
        )

        deadline = details.get(
            "deadline"
        )

        files_count = details.get(
            "files_count"
        )

        assigned_master = details.get(
            "assigned_master_telegram_id"
        )

        if subject:
            parts.append(
                f"Предмет: {subject}"
            )

        if work_type:
            parts.append(
                f"Тип работы: {work_type}"
            )

        if direction:
            parts.append(
                f"Направление: {direction}"
            )

        if deadline:
            parts.append(
                f"Дедлайн: {deadline}"
            )

        if files_count is not None:
            parts.append(
                f"Файлов: {files_count}"
            )

        if assigned_master is not None:
            parts.append(
                f"Исполнитель: {actor_name(assigned_master)}"
            )

        return "; ".join(
            parts
        )

    # ---------------------------------------------
    # Выбор исполнителя
    # ---------------------------------------------

    if action == "master_selected":
        parts = []

        master_telegram_id = details.get(
            "master_telegram_id"
        )

        master_username = details.get(
            "master_username"
        )

        master_full_name = details.get(
            "master_full_name"
        )

        current_name = None
        if master_telegram_id is not None and actor_names:
            try:
                current_name = actor_names.get(int(master_telegram_id))
            except (TypeError, ValueError, OverflowError):
                pass
        if current_name:
            parts.append(f"Исполнитель: {current_name}")
        elif master_username:
            parts.append(f"Исполнитель: @{str(master_username).lstrip('@')}")
        elif master_full_name:
            parts.append(f"Исполнитель: {master_full_name}")
        elif master_telegram_id is not None:
            parts.append(f"Исполнитель: {actor_name(master_telegram_id)}")

        return "; ".join(parts)

    # ---------------------------------------------
    # Предложение цены
    # ---------------------------------------------

    if action in {
        "price_offered",
        "counter_price_offered",
    }:
        parts = []

        amount = details.get(
            "amount"
        )

        offer_id = details.get(
            "offer_id"
        )

        offered_by = details.get(
            "offered_by"
        )

        if amount is not None:
            parts.append(
                f"Сумма: {amount} ₽"
            )

        if offer_id is not None:
            parts.append(
                f"Предложение №{offer_id}"
            )

        if offered_by:
            parts.append(
                "Предложил: "
                f"{format_actor(offered_by)}"
            )

        return "; ".join(
            parts
        )

    # ---------------------------------------------
    # Принятие цены
    # ---------------------------------------------

    if action == "price_accepted":
        parts = []

        amount = details.get(
            "amount"
        )

        offer_id = details.get(
            "offer_id"
        )

        offer_created_by = details.get(
            "offer_created_by"
        )

        accepted_by = details.get(
            "accepted_by"
        )

        if amount is not None:
            parts.append(
                f"Сумма: {amount} ₽"
            )

        if offer_id is not None:
            parts.append(
                f"Предложение №{offer_id}"
            )

        if offer_created_by:
            parts.append(
                "Предложил: "
                f"{format_actor(offer_created_by)}"
            )

        if accepted_by:
            parts.append(
                "Принял: "
                f"{format_actor(accepted_by)}"
            )

        return "; ".join(
            parts
        )

    # ---------------------------------------------
    # Отклонение цены
    # ---------------------------------------------

    if action == "price_rejected":
        parts = []

        offer_id = details.get(
            "offer_id"
        )

        rejected_by = details.get(
            "rejected_by"
        )

        offer_created_by = details.get(
            "offer_created_by"
        )

        amount = details.get(
            "amount"
        )

        if amount is not None:
            parts.append(
                f"Сумма: {amount} ₽"
            )

        if offer_id is not None:
            parts.append(
                f"Предложение №{offer_id}"
            )

        if offer_created_by:
            parts.append(
                "Предложил: "
                f"{format_actor(offer_created_by)}"
            )

        if rejected_by:
            parts.append(
                "Отклонил: "
                f"{format_actor(rejected_by)}"
            )

        return "; ".join(
            parts
        )

    # ---------------------------------------------
    # Пополнение баланса администратором
    # ---------------------------------------------

    if action == "balance_deposited":
        parts = []

        amount = details.get(
            "amount"
        )

        balance_before = details.get(
            "balance_before"
        )

        balance_after = details.get(
            "balance_after"
        )

        admin_telegram_id = details.get(
            "admin_telegram_id"
        )

        admin_username = details.get(
            "admin_username"
        )

        admin_full_name = details.get(
            "admin_full_name"
        )

        if amount is not None:
            parts.append(
                f"Начислено: {amount} ₽"
            )

        if (
            balance_before is not None
            and balance_after is not None
        ):
            parts.append(
                f"Баланс: "
                f"{balance_before} ₽ → "
                f"{balance_after} ₽"
            )

        elif balance_after is not None:
            parts.append(
                f"Баланс после: "
                f"{balance_after} ₽"
            )

        if admin_username:
            parts.append(
                f"Администратор: "
                f"@{admin_username}"
            )

        elif admin_full_name:
            parts.append(
                f"Администратор: "
                f"{admin_full_name}"
            )

        if admin_telegram_id is not None and not (admin_username or admin_full_name):
            parts.append(f"Администратор: {actor_name(admin_telegram_id)}")

        return "; ".join(
            parts
        )

    # ---------------------------------------------
    # Списание баланса администратором
    # ---------------------------------------------

    if action == "balance_withdrawn":
        parts = []

        amount = details.get("amount")
        balance_before = details.get("balance_before")
        balance_after = details.get("balance_after")
        admin_telegram_id = details.get("admin_telegram_id")
        admin_username = details.get("admin_username")
        admin_full_name = details.get("admin_full_name")

        if amount is not None:
            parts.append(f"Списано: {amount} ₽")

        if balance_before is not None and balance_after is not None:
            parts.append(
                f"Баланс: {balance_before} ₽ → {balance_after} ₽"
            )
        elif balance_after is not None:
            parts.append(f"Баланс после: {balance_after} ₽")

        if admin_username:
            parts.append(f"Администратор: @{admin_username}")
        elif admin_full_name:
            parts.append(f"Администратор: {admin_full_name}")

        if admin_telegram_id is not None and not (admin_username or admin_full_name):
            parts.append(f"Администратор: {actor_name(admin_telegram_id)}")

        return "; ".join(parts)

    # ---------------------------------------------
    # Возврат средств
    # ---------------------------------------------

    if action == "balance_refunded":
        parts = []

        amount = details.get("amount")
        balance_before = details.get("balance_before")
        balance_after = details.get("balance_after")
        reason = details.get("reason")
        initiated_by = details.get("initiated_by")
        master_telegram_id = details.get("master_telegram_id")

        if amount is not None:
            parts.append(f"Возвращено: {amount} ₽")

        if balance_before is not None and balance_after is not None:
            parts.append(
                f"Баланс: {balance_before} ₽ → {balance_after} ₽"
            )
        elif balance_after is not None:
            parts.append(f"Баланс после: {balance_after} ₽")

        if reason == "order_cancelled":
            parts.append("Причина: отмена заказа")
        elif reason:
            parts.append(f"Причина: {reason}")

        if initiated_by:
            parts.append(
                f"Инициатор: {format_actor(initiated_by)}"
            )

        if master_telegram_id is not None:
            parts.append(f"Исполнитель: {actor_name(master_telegram_id)}")

        return "; ".join(parts)

    # ---------------------------------------------
    # Передача заказа в работу
    # ---------------------------------------------

    if action == "order_started":
        parts = []

        reason = details.get("reason")
        master_telegram_id = details.get(
            "assigned_master_telegram_id"
        )

        if reason == "payment_succeeded":
            parts.append("Начало работы после успешной оплаты")
        elif reason:
            parts.append(f"Причина: {reason}")

        if master_telegram_id is not None:
            parts.append(f"Исполнитель: {actor_name(master_telegram_id)}")

        return "; ".join(parts)

    # ---------------------------------------------
    # Отправка результата клиенту
    # ---------------------------------------------

    if action == "order_result_sent":
        parts = []

        files_count = details.get("files_count")
        sent_by = details.get("sent_by")
        master_telegram_id = details.get("master_telegram_id")

        if files_count is not None:
            parts.append(f"Файлов отправлено: {files_count}")

        if sent_by:
            parts.append(f"Отправил: {format_actor(sent_by)}")

        if master_telegram_id is not None:
            parts.append(f"Исполнитель: {actor_name(master_telegram_id)}")

        return "; ".join(parts)

    # ---------------------------------------------
    # Ошибка создания заказа
    # ---------------------------------------------

    if action == "order_creation_failed":
        stage = details.get("stage")
        reason = details.get("reason")
        parts = []

        if reason == "technical_error":
            parts.append("Причина: техническая ошибка")
        elif reason:
            parts.append(f"Причина: {reason}")

        if stage:
            parts.append(f"Этап: {stage}")

        return "; ".join(parts)

    # ---------------------------------------------
    # Неудачная попытка оплаты
    # ---------------------------------------------

    if action == "payment_failed":
        parts = []

        reason = details.get("reason")
        required_amount = details.get("required_amount")
        current_balance = details.get("current_balance")
        missing_amount = details.get("missing_amount")

        if reason == "insufficient_funds":
            parts.append("Причина: недостаточно средств")
        elif reason:
            parts.append(f"Причина: {reason}")

        if required_amount is not None:
            parts.append(f"Требовалось: {required_amount} ₽")

        if current_balance is not None:
            parts.append(f"Баланс: {current_balance} ₽")

        if missing_amount is not None:
            parts.append(f"Не хватало: {missing_amount} ₽")

        return "; ".join(parts)

    # ---------------------------------------------
    # Оплата заказа
    # ---------------------------------------------

    if action == "payment_succeeded":
        parts = []

        amount = details.get(
            "amount"
        )

        balance_before = details.get(
            "balance_before"
        )

        balance_after = details.get(
            "balance_after"
        )

        if amount is not None:
            parts.append(
                f"Списано: {amount} ₽"
            )

        if (
            balance_before is not None
            and balance_after is not None
        ):
            parts.append(
                f"Баланс: "
                f"{balance_before} ₽ → "
                f"{balance_after} ₽"
            )

        elif balance_after is not None:
            parts.append(
                f"Баланс после: "
                f"{balance_after} ₽"
            )

        return "; ".join(
            parts
        )

    # ---------------------------------------------
    # Изменение статуса заказа
    # ---------------------------------------------

    if action == "order_status_changed":
        parts = []

        old_status = details.get(
            "old_status"
        )

        new_status = details.get(
            "new_status"
        )

        refund_amount = details.get(
            "refund_amount"
        )

        balance_after_refund = details.get(
            "balance_after_refund"
        )

        if (
            old_status is not None
            and new_status is not None
        ):
            parts.append(
                f"{format_status(old_status)}"
                " → "
                f"{format_status(new_status)}"
            )

        if refund_amount is not None:
            parts.append(
                f"Возвращено: "
                f"{refund_amount} ₽"
            )

        if balance_after_refund is not None:
            parts.append(
                f"Баланс после возврата: "
                f"{balance_after_refund} ₽"
            )

        return "; ".join(
            parts
        )

    # ---------------------------------------------
    # Отмена заказа
    # ---------------------------------------------

    if action == "order_cancelled":
        parts = []

        cancelled_by = details.get(
            "cancelled_by"
        )

        reason = details.get(
            "reason"
        )

        if cancelled_by:
            parts.append(
                "Отменил: "
                f"{format_actor(cancelled_by)}"
            )

        if reason:
            reason_titles = {
                "master_declined_order": (
                    "исполнитель отказался от заказа"
                ),
                "client_rejected_price": (
                    "клиент отказался от цены"
                ),
                "client_cancelled_order": (
                    "клиент отменил заказ"
                ),
                "technical_error": (
                    "техническая ошибка"
                ),
            }

            parts.append(
                "Причина: "
                + reason_titles.get(
                    str(reason),
                    str(reason),
                )
            )

        return "; ".join(
            parts
        )

    # ---------------------------------------------
    # Отзыв
    # ---------------------------------------------

    if action == "review_created":
        parts = []

        rating = details.get(
            "rating"
        )

        review_text = details.get(
            "review_text"
        )

        if rating is not None:
            parts.append(
                f"Оценка: {rating} из 5"
            )

        if review_text:
            parts.append(
                f"Отзыв: {review_text}"
            )

        return "; ".join(
            parts
        )

    # ---------------------------------------------
    # Комментарий к отзыву
    # ---------------------------------------------

    if action == "review_comment_added":
        review_text = details.get(
            "review_text"
        )

        if review_text:
            return (
                f"Комментарий: {review_text}"
            )

        return ""

    # ---------------------------------------------
    # Для неизвестных действий
    # ---------------------------------------------
    #
    # Если позже появится новый action,
    # информация не потеряется:
    # details будет выведен в понятном виде.
    # ---------------------------------------------

    parts = []

    for key, value in details.items():
        if key.endswith("telegram_id") and value is not None:
            labels = {
                "admin_telegram_id": "Администратор",
                "master_telegram_id": "Исполнитель",
                "assigned_master_telegram_id": "Исполнитель",
                "target_telegram_id": "Пользователь",
            }
            label = labels.get(
                key,
                key.removesuffix("_telegram_id").replace("_", " ").capitalize(),
            )
            parts.append(f"{label}: {actor_name(value)}")
        else:
            parts.append(f"{key}: {value}")

    return "; ".join(
        parts
    )


def format_created_at(
    created_at: object,
) -> str:
    """Показывает timestamp журнала в московском времени."""
    return format_db_datetime(
        created_at,
        output_format="%d.%m.%Y %H:%M:%S МСК",
        empty_value="",
    )


def _safe_csv_cell(value):
    """Prevent spreadsheet formula execution in user-controlled log fields.

    Excel and other spreadsheet applications may execute cells that begin with
    =, +, -, @, even when the CSV field is quoted.
    """
    if not isinstance(value, str):
        return value
    trimmed = value.lstrip('\ufeff\t\r\n ')
    if trimmed.startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def display_actor_name(
    telegram_id: object,
    actor_names: dict[int, str] | None = None,
) -> str:
    """Human-friendly identity; do not invent a Telegram username."""
    try:
        tid = int(telegram_id)
    except (TypeError, ValueError, OverflowError):
        return "Неизвестный пользователь"
    if actor_names and tid in actor_names:
        return actor_names[tid]
    # Historical activity sometimes belongs to deleted or not-yet-registered
    # accounts. Keep a traceable fallback rather than claiming a fake @handle.
    return f"Нет @username (ID {tid})"


def _related_telegram_ids(rows) -> set[int]:
    ids: set[int] = set()
    for row in rows:
        try:
            ids.add(int(row["telegram_id"]))
        except (ValueError, TypeError):
            pass
        for key, value in parse_details(row["details"]).items():
            if not key.endswith("telegram_id") or isinstance(value, bool):
                continue
            try:
                ids.add(int(value))
            except (ValueError, TypeError):
                pass
    return ids


def build_activity_csv(rows, actor_names: dict[int, str] | None = None) -> bytes:
    """CSV для Excel: имена впереди, ID в отдельном техническом столбце."""
    buffer = LimitedTextIO(newline="")
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "Дата и время", "Пользователь (@username)", "Действие",
        "Сущность", "№ сущности", "Подробности", "Telegram ID (технический)",
    ])
    for row in rows:
        columns = [
            format_created_at(row["created_at"]),
            display_actor_name(row["telegram_id"], actor_names),
            format_action(row),
            format_entity(row),
            row["entity_id"] if row["entity_id"] is not None else "",
            format_details(row, actor_names),
            row["telegram_id"],
        ]
        writer.writerow([_safe_csv_cell(value) for value in columns])
    result = buffer.getvalue().encode("utf-8-sig")
    buffer.close()
    return checked_output(result)


def build_activity_html(
    rows,
    actor_names: dict[int, str] | None = None,
    *,
    subject_name: str,
    period_title: str,
) -> bytes:
    """Standalone HTML without JavaScript or external network dependencies."""
    escape = lambda text: html.escape(str(text), quote=True)
    cards = LimitedParts()
    for row in rows:
        date = escape(format_created_at(row["created_at"]))
        actor = escape(display_actor_name(row["telegram_id"], actor_names))
        action = escape(format_action(row))
        entity = format_entity(row)
        entity_id = row["entity_id"]
        entity_text = f"{entity} №{entity_id}" if entity and entity_id is not None else entity
        description = format_details(row, actor_names)
        meta = f'<div class="meta">{escape(entity_text)}</div>' if entity_text else ""
        details = (
            f'<p class="details">{escape(description)}</p>'
            if description else '<p class="details quiet">Без дополнительных сведений</p>'
        )
        cards.append(
            '<article class="record"><div class="top">'
            f'<time>{date}</time><span class="person">{actor}</span>'
            '</div>'
            f'<h2>{action}</h2>{meta}{details}</article>'
        )

    result = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Журнал активности — {escape(subject_name)}</title>
<style>
:root{{color-scheme:light dark;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}
body{{margin:0;background:#f5f6f8;color:#18212f}}
main{{max-width:850px;margin:0 auto;padding:18px 12px 48px}}
header{{background:#fff;border-radius:16px;padding:18px;margin-bottom:14px;box-shadow:0 1px 4px #0001}}
h1{{font-size:1.35rem;margin:0 0 9px;overflow-wrap:anywhere}}
.sub{{font-size:.88rem;color:#586477;margin:4px 0}}
.record{{background:#fff;padding:15px 17px;margin:10px 0;border-radius:14px;box-shadow:0 1px 4px #0001;overflow-wrap:anywhere}}
.top{{display:flex;justify-content:space-between;gap:7px;flex-wrap:wrap;font-size:.80rem;color:#586477}}
.person{{font-weight:650;color:#2261b5}}
h2{{font-size:1rem;margin:10px 0 6px}}
.meta{{font-size:.84rem;color:#586477}}
.details{{white-space:pre-wrap;font-size:.93rem;line-height:1.5;margin:9px 0 0}}
.quiet{{color:#818a99}}
@media(prefers-color-scheme:dark){{body{{background:#10151d;color:#f1f3f6}}header,.record{{background:#1b2531;box-shadow:none}}.sub,.top,.meta{{color:#b5c1cd}}.person{{color:#79b3ff}}.quiet{{color:#b5c1cd}}}}
</style></head><body><main>
<header><h1>📋 Журнал активности: {escape(subject_name)}</h1>
<p class="sub">Период: {escape(period_title)}</p>
<p class="sub">Записей: {len(rows)} · Время: МСК · Юзернеймы актуальны на момент выгрузки</p></header>
{''.join(cards)}
</main></body></html>"""
    return checked_output(result.encode("utf-8"))


@router.message(
    Command("user_logs")
)
async def request_user_logs(
    message: Message,
) -> None:
    """
    Находит пользователя по Telegram username
    и предлагает администратору выбрать
    период выгрузки журнала.

    Использование:

    /user_logs @username
    """

    if not await require_private_admin(message):
        return

    if not is_admin(
        message.from_user.id
    ):
        return

    username = parse_username_argument(
        message
    )

    if username is None:
        await message.answer(
            "Использование команды:\n\n"
            "<code>/user_logs @username</code>",
            parse_mode="HTML",
        )
        return

    try:
        user = await get_user_by_tg_username(
            username
        )

    except Exception:
        logger.exception(
            "Ошибка поиска пользователя "
            "для выгрузки логов. "
            "Username: %s",
            username,
        )

        await message.answer(
            "⚠️ Не удалось выполнить поиск "
            "пользователя."
        )
        return

    master = None
    if user is None:
        try:
            master = await get_master_by_username(username)
        except Exception:
            logger.exception("Ошибка поиска исполнителя для логов: %s", username)
            await message.answer("⚠️ Не удалось найти исполнителя.")
            return
    if user is None and master is None:
        await message.answer(
            "⚠️ Пользователь "
            f"<b>@{html.escape(username)}</b> "
            "не найден.",
            parse_mode="HTML",
        )
        return

    telegram_id = int(user["telegram_id"] if user is not None else master["telegram_id"])
    stored_username = str(
        (user["tg_username"] if user is not None else master["username"]) or username
    )
    full_name = str(
        (user["tg_full_name"] if user is not None else master["full_name"])
        or "Не указано"
    )

    await message.answer(
        "📋 <b>Журнал активности пользователя</b>\n\n"
        f"👤 Имя: "
        f"<b>{html.escape(full_name)}</b>\n"
        f"🔗 Telegram: "
        f"<b>@{html.escape(stored_username)}</b>\n"
        "\nВыберите период и формат выгрузки:\n"
        "📱 HTML — удобно читать с телефона и ПК\n"
        "📊 CSV — для Excel и таблиц",
        parse_mode="HTML",
        reply_markup=user_logs_period_keyboard(
            telegram_id=telegram_id,
        ),
    )


@router.callback_query(F.data.startswith("user_logs:"))
@bounded_export
async def export_user_logs(callback: CallbackQuery) -> None:
    if not await require_private_admin(callback):
        return
    """Экспорт выбранного периода в HTML или CSV.

    Существующие старые кнопки ``user_logs:day:123`` всё ещё отдают CSV.
    """
    if callback.message is None:
        await callback.answer("Сообщение недоступно.", show_alert=True)
        return
    if not is_admin(callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) == 3:
        _, period, telegram_id_raw = parts
        file_format = "csv"
    elif len(parts) == 4:
        _, period, file_format, telegram_id_raw = parts
    else:
        await callback.answer("Некорректные данные выгрузки.", show_alert=True)
        return

    try:
        telegram_id = int(telegram_id_raw)
    except ValueError:
        await callback.answer("Некорректный пользователь.", show_alert=True)
        return
    if telegram_id <= 0 or period not in PERIOD_TITLES or file_format not in {"csv", "html"}:
        await callback.answer("Некорректный формат или период.", show_alert=True)
        return

    try:
        rows = await get_user_activity_logs_for_export(telegram_id, period)
        if not rows:
            await callback.answer("За выбранный период действий не найдено.", show_alert=True)
            return

        actor_names = await get_log_actor_names(_related_telegram_ids(rows) | {telegram_id})
        subject_name = display_actor_name(telegram_id, actor_names)
        period_title = PERIOD_TITLES[period]
        if file_format == "html":
            data = await disk_call(build_activity_html,
                rows, actor_names,
                subject_name=subject_name,
                period_title=period_title,
            )
        else:
            data = await disk_call(build_activity_csv, rows, actor_names)
    except ExportLimitError:
        raise
    except Exception:
        logger.exception("Ошибка формирования журнала %s за %s", telegram_id, period)
        await callback.answer("Не удалось сформировать журнал.", show_alert=True)
        return

    # Filename may contain only safe ASCII username characters.
    raw_name = subject_name.lstrip("@") if subject_name.startswith("@") else ""
    safe_name = "".join(c for c in raw_name if c.isascii() and (c.isalnum() or c in "_-"))[:32]
    if not safe_name:
        safe_name = str(telegram_id)
    filename = f"user_logs_{safe_name}_{period}_{now_moscow():%Y-%m-%d}.{file_format}"
    if len(data) > 45 * 1024 * 1024:
        await callback.answer(
            "Журнал слишком большой для Telegram. Выбери меньший период.",
            show_alert=True,
        )
        return
    document = BufferedInputFile(data, filename=filename)
    try:
        await callback.message.answer_document(
            document=document,
            caption=(
                "📋 <b>Журнал активности</b>\n\n"
                f"👤 Пользователь: <b>{html.escape(subject_name)}</b>\n"
                f"📅 Период: <b>{period_title}</b>\n"
                f"📊 Записей: <b>{len(rows)}</b>\n"
                f"📄 Формат: <b>{file_format.upper()}</b>"
            ),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Не удалось отправить %s журнал пользователя %s", file_format, telegram_id)
        await callback.answer("Файл сформирован, но отправить его не удалось.", show_alert=True)
        return
    await callback.answer(f"{file_format.upper()} сформирован.")
