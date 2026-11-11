"""Точная обёртка расчёта оплаты: один канонический рендер (utils.payment_preview).

Phase 3 C4: раньше байт-в-байт одинаковые копии _build_client_payment_preview_text
(handlers/master_order.py) и _build_payment_details_text (handlers/client_price.py)
дублировались. Здесь фиксируется точный вывод (текст + раскладка клавиатуры),
чтобы консолидация не изменила ни один символ пользовательского сообщения.
"""
import pytest

from keyboards.order_payment import order_payment_keyboard
from utils.payment_preview import (
    build_payment_preview_text,
    payment_keyboard_from_preview,
)


def preview(bonus_available, discount=0, *, balance=5000,
            bonus_balance=300, can_without=True, can_with=True,
            cash_without=900, cash_with=750, price=1000):
    return {
        "price": price,
        "balance": balance,
        "bonus_balance": bonus_balance,
        "referral_discount": discount,
        "max_bonus_available": bonus_available,
        "cash_without_bonus": cash_without,
        "cash_with_bonus": cash_with,
        "can_pay_without_bonus": can_without,
        "can_pay_with_bonus": can_with,
    }


def test_bonus_with_discount_sufficient_exact_output():
    assert build_payment_preview_text(preview(250, 100)) == (
        "🎁 <b>Использовать бонусы?</b>\n"
        "Стоимость заказа: <b>1000 ₽</b>\n"
        "🪙 Доступно: <b>300 бонусов</b>\n"
        "Можно использовать в этом заказе: <b>до 250 бонусов</b>\n"
        "🎉 Скидка по приглашению: <b>−100 ₽</b>\n"
        "💳 Ваш обычный баланс: <b>5000 ₽</b>\n"
        "💰 К оплате с бонусами: <b>750 ₽</b>\n"
        "💰 Продолжить без бонусов: <b>900 ₽</b>\n"
        "\n✅ Средств достаточно. Выберите вариант оплаты ниже."
    )


def test_bonus_without_discount_sufficient_exact_output():
    result = build_payment_preview_text(preview(250, 0))
    assert "🎉 Скидка по приглашению" not in result
    assert result == (
        "🎁 <b>Использовать бонусы?</b>\n"
        "Стоимость заказа: <b>1000 ₽</b>\n"
        "🪙 Доступно: <b>300 бонусов</b>\n"
        "Можно использовать в этом заказе: <b>до 250 бонусов</b>\n"
        "💳 Ваш обычный баланс: <b>5000 ₽</b>\n"
        "💰 К оплате с бонусами: <b>750 ₽</b>\n"
        "💰 Продолжить без бонусов: <b>900 ₽</b>\n"
        "\n✅ Средств достаточно. Выберите вариант оплаты ниже."
    )


def test_no_bonus_with_discount_insufficient_exact_output():
    # Бонусов нет, скидка есть, не хватает до cash_without_bonus.
    assert build_payment_preview_text(
        preview(0, 100, balance=500, cash_without=900, cash_with=1000,
                can_without=False, can_with=False)
    ) == (
        "💳 Ваш баланс: <b>500 ₽</b>\n"
        "🎉 Скидка по приглашению: <b>−100 ₽</b>\n"
        "💰 К оплате с баланса: <b>900 ₽</b>\n"
        "\n⚠️ Не хватает: <b>400 ₽</b>\n"
        "Пополните баланс, затем нажмите «🔄 Проверить баланс»."
    )


def test_no_bonus_no_discount_insufficient_missing_never_negative():
    # Недостача считается по лучшей цене (cash_without_bonus) и не уходит в минус.
    assert build_payment_preview_text(
        preview(0, 0, balance=700, cash_without=700, cash_with=1200,
                can_without=True, can_with=False)
    ) == (
        "💳 Ваш баланс: <b>700 ₽</b>\n"
        "💰 К оплате с баланса: <b>700 ₽</b>\n"
        "\n✅ Средств достаточно. Выберите вариант оплаты ниже."
    )
    above = build_payment_preview_text(
        preview(0, 0, balance=500, cash_without=700, cash_with=1200,
                can_without=False, can_with=False)
    )
    assert "\n⚠️ Не хватает: <b>200 ₽</b>" in above


def test_keyboard_with_bonus_option_exact_buttons():
    keyboard = payment_keyboard_from_preview(88, preview(250, 100))
    rows = [[(button.text, button.callback_data) for button in row] for row in
            keyboard.inline_keyboard]
    assert rows == [
        [("✅ Использовать бонусы (−250 ₽)", "pay_order_bonus:88")],
        [("➡️ Продолжить без бонусов", "pay_order:88")],
        [("💰 Пополнить баланс", "balance")],
        [("🔄 Проверить баланс", "refresh_order_payment:88")],
        [("📁 Мои заказы", "my_orders")],
    ]


def test_keyboard_without_bonus_option_exact_buttons():
    keyboard = payment_keyboard_from_preview(
        91, preview(0, 0))
    rows = [[(button.text, button.callback_data) for button in row] for row in
            keyboard.inline_keyboard]
    assert rows == [
        [("✅ Оплатить заказ", "pay_order:91")],
        [("💰 Пополнить баланс", "balance")],
        [("🔄 Проверить баланс", "refresh_order_payment:91")],
        [("📁 Мои заказы", "my_orders")],
    ]


def test_bundle_matches_direct_keyboard_builder():
    # Рендер клавиатуры опирается на тот же order_payment_keyboard, а не на копию.
    direct = order_payment_keyboard(
        order_id=5,
        can_pay_without_bonus=True,
        bonus_to_use=250,
        can_pay_with_bonus=True,
    )
    assert payment_keyboard_from_preview(5, preview(250)).model_dump() == (
        direct.model_dump()
    )


def test_preview_rejects_missing_keys():
    with pytest.raises(KeyError):
        build_payment_preview_text({"price": 1})
