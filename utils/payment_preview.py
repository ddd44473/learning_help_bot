"""Отображение клиентского экрана оплаты заказа.

Единственный канонический рендер расчёта оплаты: текст с бонусами,
реферальной скидкой и балансом плюс клавиатура оплаты. Раньше точно
одинаковые копии жили в handlers/master_order.py и handlers/client_price.py;
экспорт отсюда гарантирует одинаковую строку и раскладку кнопок для обоих
сценариев (приём предложения исполнителя и согласование цены клиентом).
Только чистые функции: без Bot, без FSM, без обращений к БД.
"""
from keyboards.order_payment import order_payment_keyboard


def build_payment_preview_text(preview: dict[str, int | bool]) -> str:
    price = int(preview["price"])
    balance = int(preview["balance"])
    bonus_balance = int(preview["bonus_balance"])
    referral_discount = int(preview["referral_discount"])
    bonus_available = int(preview["max_bonus_available"])
    cash_without_bonus = int(preview["cash_without_bonus"])
    cash_with_bonus = int(preview["cash_with_bonus"])

    if bonus_available > 0:
        lines = [
            "🎁 <b>Использовать бонусы?</b>",
            f"Стоимость заказа: <b>{price} ₽</b>",
            f"🪙 Доступно: <b>{bonus_balance} бонусов</b>",
            f"Можно использовать в этом заказе: <b>до {bonus_available} бонусов</b>",
        ]
        if referral_discount > 0:
            lines.append(
                f"🎉 Скидка по приглашению: <b>−{referral_discount} ₽</b>"
            )
        lines.extend([
            f"💳 Ваш обычный баланс: <b>{balance} ₽</b>",
            f"💰 К оплате с бонусами: <b>{cash_with_bonus} ₽</b>",
            f"💰 Продолжить без бонусов: <b>{cash_without_bonus} ₽</b>",
        ])
    else:
        lines = [
            f"💳 Ваш баланс: <b>{balance} ₽</b>",
        ]
        if referral_discount > 0:
            lines.append(
                f"🎉 Скидка по приглашению: <b>−{referral_discount} ₽</b>"
            )
        lines.append(
            f"💰 К оплате с баланса: <b>{cash_without_bonus} ₽</b>"
        )

    can_pay_any = bool(preview["can_pay_without_bonus"]) or bool(
        preview["can_pay_with_bonus"]
    )
    if can_pay_any:
        lines.append("\n✅ Средств достаточно. Выберите вариант оплаты ниже.")
    else:
        best_price = cash_with_bonus if bonus_available > 0 else cash_without_bonus
        missing = max(best_price - balance, 0)
        lines.append(
            f"\n⚠️ Не хватает: <b>{missing} ₽</b>\n"
            "Пополните баланс, затем нажмите «🔄 Проверить баланс»."
        )

    return "\n".join(lines)


def payment_keyboard_from_preview(
    order_id: int,
    preview: dict[str, int | bool],
):
    return order_payment_keyboard(
        order_id=order_id,
        can_pay_without_bonus=bool(
            preview["can_pay_without_bonus"]
        ),
        bonus_to_use=int(
            preview["max_bonus_available"]
        ),
        can_pay_with_bonus=bool(
            preview["can_pay_with_bonus"]
        ),
    )
