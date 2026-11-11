import asyncio
import logging

from aiogram import Bot

from database.referrals import (
    get_due_bonus_reminders,
    mark_bonus_reminder_sent,
)
from keyboards.profile import bonus_expiry_keyboard


logger = logging.getLogger(__name__)
BONUS_MAINTENANCE_INTERVAL_SECONDS = 60 * 60


async def process_bonus_expiry_reminders(bot: Bot) -> None:
    """Отправляет одно напоминание для каждого начисления за 5 дней до сгорания."""
    reminders = await get_due_bonus_reminders(limit=200)

    for reminder in reminders:
        grant_id = int(reminder["grant_id"])
        amount = int(reminder["remaining_amount"])
        telegram_id = int(reminder["telegram_id"])

        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=(
                    "⏳ <b>Скоро сгорят бонусы</b>\n\n"
                    f"Через 5 дней сгорят <b>{amount} бонусов</b>.\n"
                    "Ими можно оплатить до <b>15% стоимости</b> "
                    "следующего заказа."
                ),
                parse_mode="HTML",
                reply_markup=bonus_expiry_keyboard(),
            )
        except Exception:
            logger.exception(
                "Не удалось отправить напоминание о сгорании бонусов grant=%s user=%s",
                grant_id,
                telegram_id,
            )
            continue

        await mark_bonus_reminder_sent(grant_id)


async def referral_bonus_maintenance_loop(bot: Bot) -> None:
    """Фоновая проверка сроков бонусов и напоминаний раз в час."""
    while True:
        try:
            await process_bonus_expiry_reminders(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фонового обслуживания реферальных бонусов")

        await asyncio.sleep(BONUS_MAINTENANCE_INTERVAL_SECONDS)
