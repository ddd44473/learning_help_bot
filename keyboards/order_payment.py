from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


def order_payment_keyboard(
    order_id: int,
    *,
    can_pay_without_bonus: bool,
    bonus_to_use: int = 0,
    can_pay_with_bonus: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура оплаты с отдельным вариантом использования бонусов."""
    rows: list[list[InlineKeyboardButton]] = []

    if bonus_to_use > 0 and can_pay_with_bonus:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Использовать бонусы (−{bonus_to_use} ₽)",
                    callback_data=f"pay_order_bonus:{order_id}",
                )
            ]
        )

    if can_pay_without_bonus:
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        "➡️ Продолжить без бонусов"
                        if bonus_to_use > 0
                        else "✅ Оплатить заказ"
                    ),
                    callback_data=f"pay_order:{order_id}",
                )
            ]
        )

    # «Пополнить баланс» ведёт к существующему безопасному сценарию
    # пополнения (обработчик open_balance → экран баланса с контактом
    # администратора), а не к новой платёжной шлюзе. Кнопка всегда
    # видима в контексте оплаты и не исчезает при пересборке клавиатуры.
    rows.append(
        [
            InlineKeyboardButton(
                text="💰 Пополнить баланс",
                callback_data="balance",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Проверить баланс",
                callback_data=f"refresh_order_payment:{order_id}",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="📁 Мои заказы",
                callback_data="my_orders",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)
