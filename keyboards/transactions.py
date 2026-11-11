from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


def _page_row(
    *,
    prefix: str,
    page: int,
    total_pages: int,
) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []

    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"{prefix}:{page - 1}",
            )
        )

    buttons.append(
        InlineKeyboardButton(
            text=f"{page + 1} / {total_pages}",
            callback_data="tx_noop",
        )
    )

    if page + 1 < total_pages:
        buttons.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"{prefix}:{page + 1}",
            )
        )

    return buttons


def user_transactions_keyboard(
    *,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if total_pages > 1:
        rows.append(
            _page_row(
                prefix="tx_user",
                page=page,
                total_pages=total_pages,
            )
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="↩️ К балансу",
                callback_data="balance",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_transactions_keyboard(
    *,
    scope: str,
    target_user_id: int,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if total_pages > 1:
        rows.append(
            _page_row(
                prefix=f"tx_admin:{scope}:{target_user_id}",
                page=page,
                total_pages=total_pages,
            )
        )

    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="📅 CSV за 24 часа",
                    callback_data=(
                        f"tx_csv:{scope}:{target_user_id}:day"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗓 CSV за 7 дней",
                    callback_data=(
                        f"tx_csv:{scope}:{target_user_id}:week"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗃 CSV за всё время",
                    callback_data=(
                        f"tx_csv:{scope}:{target_user_id}:all"
                    ),
                )
            ],
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)
