import re
import html
import sqlite3
from datetime import datetime
from urllib.parse import quote
from aiogram import Bot, Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from config import TOPUP_ADMIN_USERNAME
from database.users import (
    create_empty_user,
    finish_user_registration,
    get_user_by_bot_nickname,
    get_user_by_telegram_id,
)
from states.registration import RegistrationState
from keyboards.profile import (
    profile_keyboard,
    balance_keyboard,
    referral_program_keyboard,
    referral_bonuses_keyboard,
    referral_info_keyboard,
)
from database.balance import get_user_balance
from database.referrals import (
    ensure_referral_code,
    get_active_bonus_grants,
    get_active_referral_discount,
    get_bonus_balance,
    get_referral_stats,
)
from utils.section_images import send_section_message, edit_message_content

router = Router()


def is_valid_nickname(nickname: str) -> bool:
    pattern = r"^[a-zA-Zа-яА-ЯёЁ0-9_]{3,20}$"
    return bool(re.match(pattern, nickname))



async def show_profile(
    message: Message,
    user,
) -> None:
    nickname = html.escape(
        str(user["bot_nickname"] or "Не указан")
    )
    balance = int(
        user["balance"] or 0
    )
    actual_bonus_balance = await get_bonus_balance(int(user["id"]))
    bonus_balance = int(actual_bonus_balance or 0)

    await send_section_message(
        message,
        "profile.png",
        f"""
👤 <b>Профиль</b>

Привет, <b>{nickname}</b>!

👤 Никнейм: <b>{nickname}</b>
💳 Баланс: <b>{balance} ₽</b>
🎁 Бонусы: <b>{bonus_balance} бонусов</b>

Здесь вы сможете отслеживать свои заказы и приглашать друзей.
""",
        reply_markup=profile_keyboard(),
        parse_mode="HTML",
        replace_photo=True,
    )


def _is_private_own_chat(message: Message, user_id: int) -> bool:
    """Чат принадлежит телеграм-актеру и является личным."""
    chat = message.chat
    return (
        chat is not None
        and chat.type == "private"
        and chat.id == user_id
    )


@router.callback_query(F.data == "profile")
async def open_profile(callback: CallbackQuery, state: FSMContext):
    telegram_user = callback.from_user

    if not _is_private_own_chat(callback.message, telegram_user.id):
        if callback.message is not None:
            await callback.message.answer(
                "Профиль доступен только в личном чате с ботом."
            )
        await callback.answer()
        return

    user = await get_user_by_telegram_id(telegram_user.id)

    if user and user["is_registered"]:
        await show_profile(callback.message, user)
        await callback.answer()
        return

    await create_empty_user(
        telegram_id=telegram_user.id,
        tg_username=telegram_user.username,
        tg_full_name=telegram_user.full_name,
    )

    await callback.message.answer(
        "Перед тем как открыть профиль, давайте зарегистрируемся.\n\n"
        "Придумайте никнейм от 3 до 20 символов.\n"
        "Можно использовать русские/английские буквы, цифры и нижнее подчёркивание."
    )

    await state.set_state(RegistrationState.waiting_for_nickname)
    await callback.answer()


@router.callback_query(F.data == "referral_program")
async def open_referral_program(
    callback: CallbackQuery,
    bot: Bot,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    user = await get_user_by_telegram_id(callback.from_user.id)
    if user is None or not user["is_registered"]:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    referral_code = await ensure_referral_code(int(user["id"]))
    if referral_code is None:
        await callback.answer(
            "Не удалось создать реферальную ссылку.",
            show_alert=True,
        )
        return

    bot_info = await bot.get_me()
    if not bot_info.username:
        await callback.answer(
            "У бота не настроено имя пользователя.",
            show_alert=True,
        )
        return

    referral_link = (
        f"https://t.me/{bot_info.username}?start=ref_{referral_code}"
    )
    stats = await get_referral_stats(int(user["id"]))

    share_text = (
        "🏛 «Колизей Знаний»\n"
        "По моей ссылке ты получишь 250 ₽ на первый заказ 🎁"
    )
    share_url = (
        "https://t.me/share/url?url="
        f"{quote(referral_link, safe='')}"
        "&text="
        f"{quote(share_text, safe='')}"
    )

    invited = stats["invited_count"]
    completed = stats["completed_count"]
    waiting = stats["waiting_count"]
    bonus_balance = stats["bonus_balance"]

    lines = [
        "👥 <b>Пригласить друзей</b>",
        "",
    ]
    if invited == 0:
        lines.extend([
            "Поделись своей персональной ссылкой — выгоду получите оба.",
            "",
        ])

    lines.extend([
        "🎁 Другу — <b>250 ₽ скидки</b> на первый заказ",
        "🪙 Тебе — <b>250 бонусов</b> после его успешного выполнения",
        "",
        f"👥 Приглашено: <b>{invited}</b>",
        f"✅ Сделали первый заказ: <b>{completed}</b>",
    ])
    if invited > 0:
        lines.append(f"⏳ Ожидают первого заказа: <b>{waiting}</b>")

    lines.extend([
        f"🪙 Бонусный баланс: <b>{bonus_balance}</b>",
        "",
        "🔗 <b>Персональная ссылка:</b>",
        f"<code>{html.escape(referral_link)}</code>",
        "<i>Нажмите на ссылку, чтобы скопировать её.</i>",
    ])

    await edit_message_content(
        callback.message,
        "\n".join(lines),
        reply_markup=referral_program_keyboard(share_url),
        parse_mode="HTML",
    )
    await callback.answer()


def _format_bonus_expiry(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return value or "неизвестно"


@router.callback_query(F.data == "referral_bonuses")
async def open_referral_bonuses(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return

    user = await get_user_by_telegram_id(callback.from_user.id)
    if user is None or not user["is_registered"]:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    user_id = int(user["id"])
    grants = await get_active_bonus_grants(user_id)
    referral_discount = await get_active_referral_discount(user_id)
    balance = await get_bonus_balance(user_id)
    balance = int(balance or 0)

    lines = [
        "🎁 <b>Мои бонусы</b>",
        "",
        f"🪙 Доступно: <b>{balance} бонусов</b>",
        "",
        "1 бонус = 1 ₽",
        "",
        "Бонусами можно оплатить до <b>15% стоимости заказа</b>.",
        "",
        "Каждое начисление бонусов действует <b>60 дней</b> "
        "с момента фактического получения.",
        "",
        "После окончания срока действия неиспользованный остаток "
        "соответствующего начисления сгорает.",
    ]

    if referral_discount is not None:
        lines.extend([
            "",
            "🎟 <b>Скидка приглашённого</b>",
            f"💸 Доступно: <b>{int(referral_discount['amount'])} ₽</b>",
            "📅 Действует до: "
            f"<b>{_format_bonus_expiry(str(referral_discount['expires_at']))}</b>",
            "Скидка автоматически применится к твоему первому заказу.",
            "<i>Это отдельная скидка и она не входит в бонусный баланс.</i>",
        ])

    if grants:
        lines.extend(["", "<b>Активные начисления:</b>"])
        for grant in grants:
            lines.append(
                "📅 "
                f"{int(grant['remaining_amount'])} бонусов — до "
                f"{_format_bonus_expiry(str(grant['expires_at']))}"
            )
        lines.extend([
            "",
            "Если бонусы получены в разные моменты времени, каждое "
            "начисление имеет собственный срок сгорания.",
        ])
    else:
        lines.extend(["", "Активных бонусных начислений пока нет."])

    await edit_message_content(
        callback.message,
        "\n".join(lines),
        reply_markup=referral_bonuses_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "referral_how_it_works")
async def open_referral_how_it_works(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return

    text = (
        "ℹ️ <b>Как это работает</b>\n\n"
        "Отправь другу свою персональную ссылку.\n"
        "Он получит <b>250 ₽ на первый заказ</b>.\n"
        "После успешного выполнения его первого заказа тебе начислят "
        "<b>250 бонусов</b>.\n\n"
        "<b>1 бонус = 1 ₽.</b>\n"
        "Бонусами можно оплатить до <b>15% стоимости заказа</b>.\n"
        "Начисленные бонусы действуют <b>60 дней</b>.\n\n"
        "Приглашай друзей — и экономь на своих следующих заказах 🏛"
    )
    await edit_message_content(
        callback.message,
        text,
        reply_markup=referral_info_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "balance")
async def open_balance(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    telegram_user = callback.from_user
    if not _is_private_own_chat(callback.message, telegram_user.id):
        if callback.message is not None:
            await callback.message.answer(
                "Баланс доступен только в личном чате с ботом."
            )
        await callback.answer()
        return

    user = await get_user_by_telegram_id(
        telegram_user.id
    )

    if user is None or not user["is_registered"]:
        await callback.answer(
            "Сначала необходимо зарегистрироваться.",
            show_alert=True,
        )
        return

    balance = await get_user_balance(
        int(user["id"])
    )
    current_bonus_balance = await get_bonus_balance(int(user["id"]))
    bonus_balance = int(current_bonus_balance or 0)

    if balance is None:
        await callback.answer(
            "Не удалось получить данные баланса.",
            show_alert=True,
        )
        return

    admin_username = (
        f"@{TOPUP_ADMIN_USERNAME}"
        if TOPUP_ADMIN_USERNAME
        else None
    )

    await callback.message.answer(
        "💎 <b>Бонусы и баланс</b>\n\n"
        f"💳 Обычный баланс: <b>{balance} ₽</b>\n"
        f"🎁 Бонусный баланс: <b>{bonus_balance} бонусов</b>\n\n"
        "Обычный баланс используется для оплаты заказов. "
        "Бонусами можно оплатить только часть заказа.\n\n"
        + (
            "Для пополнения свяжитесь с администратором:\n"
            f"<b>{html.escape(admin_username)}</b>"
            if admin_username
            else "Контакт администратора для пополнения баланса не настроен."
        ),
        reply_markup=balance_keyboard(),
        parse_mode="HTML",
    )

    await callback.answer()


@router.message(RegistrationState.waiting_for_nickname)
async def process_nickname(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(
            "Никнейм нужно отправить текстовым сообщением."
        )
        return

    nickname = message.text.strip()

    if not is_valid_nickname(nickname):
        await message.answer(
            "Некорректный никнейм.\n\n"
            "Ник должен быть от 3 до 20 символов.\n"
            "Можно использовать буквы, цифры и нижнее подчёркивание."
        )
        return

    existing_user = await get_user_by_bot_nickname(nickname)

    if existing_user:
        await message.answer(
            "Такой никнейм уже занят. Пожалуйста, придумайте другой."
        )
        return

    if message.from_user is None:
        return

    try:
        await finish_user_registration(
            telegram_id=message.from_user.id,
            bot_nickname=nickname,
        )
    except sqlite3.IntegrityError as error:
        if 'users.bot_nickname_normalized' not in str(error):
            raise
        await message.answer('Такой никнейм уже занят. Пожалуйста, придумайте другой.')
        return  # FSM остаётся на выборе никнейма; ошибка не скрывает другие FK/constraints.

    await state.clear()

    user = await get_user_by_telegram_id(message.from_user.id)

    await message.answer("Регистрация завершена ✅")
    await show_profile(message, user)
