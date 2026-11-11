import html

from aiogram import F, Router
from aiogram.types import CallbackQuery

from catalog.directions import DIRECTION_BY_KEY
from database.orders import (
    get_all_reviews,
    get_masters_with_reviews,
    get_masters_with_reviews_count,
    get_reviews_by_master,
)
from keyboards.about import about_keyboard, guarantees_keyboard
from utils.section_images import edit_message_content, send_section_message
from keyboards.reviews import (
    master_reviews_keyboard,
    reviews_directions_keyboard,
    reviews_keyboard,
    reviews_masters_keyboard,
)


router = Router()


ABOUT_TEXT = """
ℹ️ <b>О сервисе</b>

Здесь вы можете узнать больше о том, как мы работаем, какие правила действуют внутри сервиса и какие гарантии получает пользователь.

📌 В этом разделе собрана основная информация: правила, отзывы и порядок работы.
"""


GUARANTEES_TEXT = """
🛡 <b>Гарантии и порядок работы</b>

📋 <b>Перед началом работы</b>
Вы передаёте необходимые материалы и требования к заказу. Все условия фиксируются в чате сделки и используются при оценке результата.

👤 <b>Работа исполнителя</b>
Исполнитель работает по согласованному заданию и установленному сроку.

🛡 <b>Ваши гарантии</b>
• бесплатные доработки в течение <b>14 дней</b> по первоначальному заданию;
• возможность заменить исполнителя на начальном этапе — до <b>25% объёма</b>;
• возможность обратиться к <b>Арбитру</b> при споре о соответствии результата требованиям.

⚖️ <b>Если возник спор</b>
Сначала вопрос решается с менеджером. Если этого недостаточно — подключается Арбитр. В предусмотренных регламентом случаях возможна апелляция руководству проекта.

⚠️ <b>Важно</b>
Результат заказа является информационным материалом для самостоятельной работы и не предназначен для сдачи в учебное заведение без самостоятельной доработки.

Подробные условия и исключения — в полном регламенте.
"""


def build_review_text(
    review,
    title: str = "⭐ Отзыв клиента",
    *,
    show_master: bool = False,
) -> str:
    rating = int(
        review["rating"]
    )

    direction = html.escape(
        str(
            review["direction"]
            or "Не указано"
        )
    )
    work_type = html.escape(
        str(
            review["work_type"]
            or "Не указано"
        )
    )
    review_text = html.escape(
        str(
            review["review_text"]
            or "Без комментария"
        )
    )
    client_nickname = html.escape(
        str(
            review["client_bot_nickname"]
            or "Пользователь"
        )
    )

    author_block = (
        f"👤 <b>Отзыв от:</b> {client_nickname}\n\n"
    )

    master_block = ""

    if show_master:
        master_name = html.escape(
            str(
                review["master_name"]
                or "Исполнитель"
            )
        )
        master_block = (
            f"👨‍💻 <b>Исполнитель:</b> {master_name}\n\n"
        )

    return (
        f"<b>{title}</b>\n\n"
        f"{author_block}"
        f"{master_block}"
        f"<b>Оценка:</b> {'⭐' * rating}\n\n"
        f"<b>Направление:</b> {direction}\n\n"
        f"<b>Тип работы:</b> {work_type}\n\n"
        f"<b>Комментарий:</b>\n"
        f"{review_text}"
    )


async def show_masters_reviews_list(
    callback: CallbackQuery,
    *,
    direction: str | None,
    filter_key: str,
    page: int,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    page_size = 10

    total_count = await get_masters_with_reviews_count(
        direction=direction,
    )

    if total_count == 0:
        if direction is None:
            message = (
                "Пока нет исполнителей с отзывами."
            )
        else:
            message = (
                "В этом направлении пока нет "
                "исполнителей с отзывами."
            )

        await callback.answer(
            message,
            show_alert=True,
        )
        return

    total_pages = max(
        (total_count + page_size - 1)
        // page_size,
        1,
    )

    if page < 0:
        page = 0

    if page >= total_pages:
        page = total_pages - 1

    masters = await get_masters_with_reviews(
        limit=page_size,
        offset=page * page_size,
        direction=direction,
    )

    if direction is None:
        heading = (
            "👨‍💻 <b>Все исполнители с отзывами</b>"
        )
        description = (
            "Выберите исполнителя, чтобы посмотреть "
            "все отзывы о его работе."
        )
    else:
        safe_direction = html.escape(
            direction
        )
        heading = (
            f"🧭 <b>{safe_direction}</b>"
        )
        description = (
            "Показаны исполнители этого направления, "
            "у которых уже есть отзывы. После выбора "
            "исполнителя будут показаны все его отзывы."
        )

    await edit_message_content(callback.message, 
        f"{heading}\n\n"
        f"{description}",
        parse_mode="HTML",
        reply_markup=reviews_masters_keyboard(
            masters=masters,
            current_page=page,
            total_pages=total_pages,
            filter_key=filter_key,
        ),
    )

    await callback.answer()


@router.callback_query(F.data == "about_service")
async def open_about_service(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await send_section_message(
        callback.message,
        "about_service.png",
        ABOUT_TEXT,
        reply_markup=about_keyboard(),
        parse_mode="HTML",
        replace_photo=True,
    )
    await callback.answer()


@router.callback_query(F.data == "guarantees")
async def show_guarantees(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await callback.message.delete()
    await callback.message.answer(
        GUARANTEES_TEXT,
        parse_mode="HTML",
        reply_markup=guarantees_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "guarantees_back")
async def guarantees_back(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await callback.message.delete()
    await send_section_message(
        callback.message,
        "about_service.png",
        ABOUT_TEXT,
        reply_markup=about_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "reviews")
async def show_reviews(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer(
            "Сообщение недоступно.",
            show_alert=True,
        )
        return

    await send_section_message(
        callback.message,
        "reviews.png",
        "<b>⭐ Отзывы клиентов</b>\n\n"
        "Выберите направление, чтобы посмотреть "
        "исполнителей этой области.\n\n"
        "Также можно открыть всех исполнителей "
        "или сразу перейти ко всем отзывам.",
        parse_mode="HTML",
        reply_markup=reviews_directions_keyboard(),
        replace_photo=True,
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith("reviews_direction:")
)
async def show_reviews_by_direction(
    callback: CallbackQuery,
) -> None:
    if callback.data is None:
        await callback.answer(
            "Некорректное направление.",
            show_alert=True,
        )
        return

    try:
        _, direction_key = callback.data.split(
            ":",
            maxsplit=1,
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректное направление.",
            show_alert=True,
        )
        return

    direction = DIRECTION_BY_KEY.get(
        direction_key
    )

    if direction is None:
        await callback.answer(
            "Неизвестное направление.",
            show_alert=True,
        )
        return

    await show_masters_reviews_list(
        callback,
        direction=direction,
        filter_key=direction_key,
        page=0,
    )


@router.callback_query(
    F.data == "reviews_all_masters"
)
async def show_all_reviewed_masters(
    callback: CallbackQuery,
) -> None:
    await show_masters_reviews_list(
        callback,
        direction=None,
        filter_key="all",
        page=0,
    )


@router.callback_query(
    F.data.startswith("reviews_masters_page:")
)
async def change_reviews_masters_page(
    callback: CallbackQuery,
) -> None:
    if callback.data is None:
        await callback.answer(
            "Некорректные данные страницы.",
            show_alert=True,
        )
        return

    try:
        _, filter_key, page_raw = (
            callback.data.split(
                ":",
                maxsplit=2,
            )
        )
        page = int(page_raw)
    except (ValueError, IndexError):
        await callback.answer(
            "Некорректный номер страницы.",
            show_alert=True,
        )
        return

    if filter_key == "all":
        direction = None
    else:
        direction = DIRECTION_BY_KEY.get(
            filter_key
        )

        if direction is None:
            await callback.answer(
                "Неизвестное направление.",
                show_alert=True,
            )
            return

    await show_masters_reviews_list(
        callback,
        direction=direction,
        filter_key=filter_key,
        page=page,
    )


@router.callback_query(
    F.data == "reviews_masters_page_info"
)
async def reviews_masters_page_info(
    callback: CallbackQuery,
) -> None:
    await callback.answer()


@router.callback_query(F.data == "reviews_all")
async def show_all_reviews(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer(
            "Сообщение недоступно.",
            show_alert=True,
        )
        return

    reviews = await get_all_reviews(
        limit=100,
        offset=0,
    )

    if not reviews:
        await callback.answer(
            "Пока отзывов нет.",
            show_alert=True,
        )
        return

    await edit_message_content(callback.message, 
        build_review_text(
            reviews[0],
            title="⭐ Отзывы клиентов",
            show_master=True,
        ),
        parse_mode="HTML",
        reply_markup=reviews_keyboard(
            current_index=0,
            total_reviews=len(reviews),
        ),
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith("reviews_master:")
)
async def show_master_reviews(
    callback: CallbackQuery,
) -> None:
    """
    Показывает все отзывы выбранного исполнителя.

    Направление, через которое клиент пришёл к списку,
    фильтрует только список исполнителей. После выбора
    человека показываются все его отзывы.
    """

    if callback.message is None:
        await callback.answer(
            "Сообщение недоступно.",
            show_alert=True,
        )
        return

    if callback.data is None:
        await callback.answer(
            "Некорректные данные кнопки.",
            show_alert=True,
        )
        return

    try:
        _, master_id_raw = callback.data.split(
            ":",
            maxsplit=1,
        )
        master_telegram_id = int(
            master_id_raw
        )
    except (ValueError, IndexError):
        await callback.answer(
            "Не удалось определить исполнителя.",
            show_alert=True,
        )
        return

    reviews = await get_reviews_by_master(
        master_telegram_id=master_telegram_id,
        limit=100,
        offset=0,
    )

    if not reviews:
        await callback.answer(
            "У этого исполнителя пока нет отзывов.",
            show_alert=True,
        )
        return

    await edit_message_content(callback.message, 
        build_review_text(
            reviews[0]
        ),
        parse_mode="HTML",
        reply_markup=master_reviews_keyboard(
            master_telegram_id=master_telegram_id,
            current_index=0,
            total_reviews=len(reviews),
            source="about",
        ),
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith("review_master_page:")
)
async def change_master_review_page(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer(
            "Сообщение недоступно.",
            show_alert=True,
        )
        return

    if callback.data is None:
        await callback.answer(
            "Некорректные данные.",
            show_alert=True,
        )
        return

    try:
        parts = callback.data.split(
            ":"
        )
        master_telegram_id = int(
            parts[1]
        )
        current_index = int(
            parts[2]
        )
        source = (
            parts[3]
            if len(parts) >= 4
            else "about"
        )
    except (
        AttributeError,
        IndexError,
        ValueError,
    ):
        await callback.answer(
            "Не удалось открыть отзыв.",
            show_alert=True,
        )
        return

    if source not in {
        "about",
        "order",
    }:
        source = "about"

    reviews = await get_reviews_by_master(
        master_telegram_id=master_telegram_id,
        limit=100,
        offset=0,
    )

    if not reviews:
        await callback.answer(
            "У этого исполнителя пока нет отзывов.",
            show_alert=True,
        )
        return

    if (
        current_index < 0
        or current_index >= len(reviews)
    ):
        await callback.answer(
            "Этот отзыв больше недоступен.",
            show_alert=True,
        )
        return

    await edit_message_content(callback.message, 
        build_review_text(
            reviews[current_index]
        ),
        parse_mode="HTML",
        reply_markup=master_reviews_keyboard(
            master_telegram_id=master_telegram_id,
            current_index=current_index,
            total_reviews=len(reviews),
            source=source,
        ),
    )

    await callback.answer()


@router.callback_query(
    F.data.startswith("review_page:")
)
async def change_review_page(
    callback: CallbackQuery,
) -> None:
    if callback.message is None:
        await callback.answer()
        return

    reviews = await get_all_reviews(
        limit=100
    )

    if not reviews:
        await callback.answer(
            "Отзывы не найдены.",
            show_alert=True,
        )
        return

    if callback.data is None:
        await callback.answer(
            "Некорректные данные.",
            show_alert=True,
        )
        return

    try:
        current_index = int(
            callback.data.split(
                ":",
                maxsplit=1,
            )[1]
        )
    except (IndexError, ValueError):
        await callback.answer(
            "Не удалось открыть отзыв.",
            show_alert=True,
        )
        return

    if (
        current_index < 0
        or current_index >= len(reviews)
    ):
        await callback.answer(
            "Такого отзыва нет.",
            show_alert=True,
        )
        return

    await edit_message_content(callback.message, 
        build_review_text(
            reviews[current_index],
            title="⭐ Отзывы клиентов",
            show_master=True,
        ),
        reply_markup=reviews_keyboard(
            current_index=current_index,
            total_reviews=len(reviews),
        ),
        parse_mode="HTML",
    )

    await callback.answer()


@router.callback_query(
    F.data == "review_page_info"
)
async def review_page_info(
    callback: CallbackQuery,
) -> None:
    await callback.answer()
