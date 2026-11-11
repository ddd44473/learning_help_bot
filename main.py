import asyncio
import contextlib
import logging

from bot import bot, dp
from config import validate_deployment_config

from database.db import init_db
from database.access_control import init_access_control
from middlewares.security import SecurityMiddleware
from middlewares.activity_audit import ActivityAuditMiddleware
from handlers.admin_bans import router as admin_bans_router
from database.chat_archive import init_chat_archive
from database.migrations import run_migrations
from services.archive_worker import archive_worker
from utils.moscow_time import configure_moscow_logging
from middlewares.telegram_identity import TelegramIdentitySyncMiddleware
from middlewares.chat_archive import ChatArchiveMiddleware
from handlers.admin_archive import router as admin_archive_router
from services.referrals import referral_bonus_maintenance_loop

from handlers.start import router as start_router
from handlers.menu import router as menu_router
from handlers.profile import router as profile_router
from handlers.about import router as about_router
from handlers.help import router as help_router
from handlers.extra import router as extra_router
from handlers.order import router as order_router
from handlers.order_form import router as order_form_router
from handlers.master_chat import router as master_chat_router
from handlers.my_orders import router as my_orders_router
from handlers.client_order_chat import router as client_order_chat_router
from handlers.master_order import router as master_order_router
from handlers.placeholders import router as placeholders_router
from handlers.admin_balance import router as admin_balance_router
from handlers.client_price import (
    router as client_price_router,
)
from handlers.admin_master_profile import (
    router as admin_master_profile_router,
)
from handlers.admin_logs import (
    router as admin_logs_router,
)
from handlers.transactions import (
    router as transactions_router,
)


async def main() -> None:
    configure_moscow_logging(level=logging.INFO)
    validate_deployment_config()

    # Сначала подготавливаем базу данных.
    await init_db()
    await run_migrations()
    await init_chat_archive()
    await init_access_control()

    # REL-01: после перезапуска возобновляем незавершённые публикации
    # рабочих тем заказов (контрольные точки order_topic_checkpoints).
    try:
        from utils.order_card import (
            recover_unfinished_topic_publications,
        )
        recovered = await recover_unfinished_topic_publications(bot)
        if recovered:
            logging.getLogger(__name__).info(
                "Возобновлены публикации тем заказов: %s",
                recovered,
            )
    except Exception:
        logging.getLogger(__name__).exception(
            "Ошибка восстановления незавершённых публикаций тем заказов"
        )

    # Автоматически синхронизируем изменяемый Telegram username
    # по стабильному Telegram ID при любой активности пользователя.
    # Global safeguards fire before ALL FSM, group and callback handlers.
    dp.update.outer_middleware(SecurityMiddleware())
    dp.update.outer_middleware(ActivityAuditMiddleware())
    identity_sync = TelegramIdentitySyncMiddleware()
    dp.message.outer_middleware(identity_sync)
    dp.message.outer_middleware(ChatArchiveMiddleware())
    dp.edited_message.outer_middleware(identity_sync)
    dp.callback_query.outer_middleware(identity_sync)

    # C1: обработчикам доступна изоляция событий FSM того Dispatcher,
    # который реально принял update (а не глобальный бот-синглтон).
    # Финализатор альбома в order_form работает вне обработки update
    # и обязан блокироваться по этой же изоляции.
    dp["fsm_events_isolation"] = dp.fsm.events_isolation

    # Подключаем обработчики.
    dp.include_router(admin_bans_router)
    dp.include_router(admin_archive_router)
    dp.include_router(start_router)
    dp.include_router(menu_router)
    dp.include_router(profile_router)
    dp.include_router(my_orders_router)
    dp.include_router(about_router)
    dp.include_router(help_router)
    dp.include_router(extra_router)

    dp.include_router(client_order_chat_router)

    dp.include_router(order_router)
    dp.include_router(order_form_router)

    dp.include_router(master_order_router)
    dp.include_router(master_chat_router)
    dp.include_router(admin_balance_router)
    dp.include_router(client_price_router)
    dp.include_router(admin_master_profile_router)
    dp.include_router(admin_logs_router)
    dp.include_router(transactions_router)

    # Заглушки должны находиться последними,
    # чтобы не перехватывать известные кнопки.
    dp.include_router(placeholders_router)

    print("Бот запущен!")

    await bot.delete_webhook(
        drop_pending_updates=True,
    )

    bonus_maintenance_task = asyncio.create_task(
        referral_bonus_maintenance_loop(bot)
    )

    try:
        await archive_worker.start(bot)
        await dp.start_polling(bot)
    finally:
        await archive_worker.stop()
        bonus_maintenance_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await bonus_maintenance_task


if __name__ == "__main__":
    asyncio.run(main())
