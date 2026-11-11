from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from database.users import (
    accept_user_rules,
    create_empty_user,
    has_accepted_rules,
)
from database.referrals import attach_referral_by_code
from keyboards.inline import rules_keyboard, go_to_main_menu_keyboard
from utils.section_images import send_section_message

router = Router()


RULES_TEXT = """
👋 Добро пожаловать!

Перед тем как оформить заказ, пожалуйста, ознакомьтесь с несколькими простыми правилами.

1. 📚 Общение осуществляется только по вопросам оформления и выполнения заказов.

2. 🔐 Не передавайте личные данные, пароли и другую конфиденциальную информацию.

3. 🤝 Будьте вежливы и уважительно общайтесь с администрацией и исполнителями.

4. 📋 Чем подробнее вы опишете задание, тем быстрее мы сможем оценить стоимость и приступить к работе.

⚠️ В целях безопасности переписка может проверяться администрацией сервиса. Нарушение правил может привести к ограничению доступа.

Если вы ознакомились с правилами, нажмите кнопку «Принимаю» ниже.
"""


def extract_referral_code(message: Message) -> str | None:
    if not message.text:
        return None

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2:
        return None

    payload = parts[1].strip()
    if not payload.startswith("ref_"):
        return None

    code = payload[4:].strip()
    if not code or len(code) > 32:
        return None

    return code


@router.message(CommandStart())
async def cmd_start(
    message: Message,
) -> None:
    if message.from_user is None:
        return

    telegram_user = message.from_user

    await create_empty_user(
        telegram_id=telegram_user.id,
        tg_username=telegram_user.username,
        tg_full_name=telegram_user.full_name,
    )

    referral_attached = False
    referral_code = extract_referral_code(message)
    if referral_code is not None:
        referral_attached = await attach_referral_by_code(
            referred_telegram_id=telegram_user.id,
            referral_code=referral_code,
        )

    if referral_attached:
        await message.answer(
            "🎁 <b>Для тебя есть подарок</b>\n\n"
            "Тебя пригласили в «Колизей Знаний».\n"
            "За тобой закреплена <b>скидка 250 ₽ на первый заказ</b>.\n"
            "Скидка будет автоматически учтена при оформлении первого "
            "заказа и действует <b>60 дней</b> с момента перехода по ссылке.",
            parse_mode="HTML",
        )

    rules_accepted = await has_accepted_rules(telegram_user.id)

    if rules_accepted:
        await send_section_message(
            message,
            "welcome.png",
            "👋 Добро пожаловать!",
            reply_markup=go_to_main_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    await send_section_message(
        message,
        "welcome.png",
        RULES_TEXT,
        reply_markup=rules_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(
    F.data == "accept_rules"
)
async def accept_rules(
    callback: CallbackQuery,
) -> None:
    if callback.from_user is None:
        return

    accepted = await accept_user_rules(
        callback.from_user.id
    )

    if not accepted:
        await callback.answer(
            "Не удалось сохранить согласие.",
            show_alert=True,
        )
        return

    if callback.message is not None:
        await callback.message.answer(
            "Вы приняли правила ✅",
            reply_markup=go_to_main_menu_keyboard(),
        )

    await callback.answer()