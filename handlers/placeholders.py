from aiogram import F, Router
from aiogram.types import CallbackQuery


router = Router()


IN_DEVELOPMENT_CALLBACKS = {
    # Главное меню
    "choose_executor",

    # Направления заказа
    "direction_humanities",
    "direction_natural_sciences",
    "direction_economics",
    "direction_law",
    "direction_languages",
    "direction_other",

    # Мои заказы
    "completed_orders",

    # Профиль
    "profile_bonuses",
    "invite_friends",
    "account_levels",
    "profile_settings",

    # О сервисе
    "reviews",
    "team",

    # Помощь и настройки
    "contact_manager",
    "support",

    # Дополнительно
    "free_materials",
    "useful_sections",
    "additional_services",
}


@router.callback_query(
    F.data.in_(IN_DEVELOPMENT_CALLBACKS)
)
async def handle_in_development_callback(
    callback: CallbackQuery,
) -> None:
    """
    Обрабатывает кнопки, функциональность которых
    ещё не реализована.
    """

    await callback.answer(
        "🚧 Этот раздел пока находится в разработке.",
        show_alert=True,
    )