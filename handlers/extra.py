from aiogram import Router, F
from aiogram.types import CallbackQuery

from keyboards.extra import become_executor_keyboard, extra_keyboard
from utils.section_images import send_section_message

router = Router()


EXTRA_TEXT = """
📂 <b>Дополнительные разделы</b>

Здесь собраны дополнительные возможности сервиса.

👨‍💻 Если хотите сотрудничать с нами — откройте раздел для исполнителей.
✨ Также здесь будут размещаться дополнительные услуги.
"""


BECOME_EXECUTOR_TEXT = """
👤 <b>Стать исполнителем</b>

Хотите получать заказы через «Колизей Знаний»?
Для начала необходимо пройти верификацию и подтвердить свою квалификацию.

<b>Как проходит подключение:</b>
• заполнение анкеты и указание специализации;
• портфолио или документы об образовании;
• небольшое тестовое задание;
• собеседование с координатором.

💰 <b>Условия работы</b>
• комиссия сервиса — <b>30%</b> с выполненного и принятого заказа;
• вы получаете доступ к заказам и сопровождение сделки;
• расчёты и спорные ситуации проходят через сервис;
• условия и требования фиксируются в регламенте.

📄 <b>Подробные условия — в регламенте исполнителей.</b>

Если всё подходит — оставьте заявку, и менеджер расскажет о следующих шагах.
"""


@router.callback_query(F.data == "extra_sections")
async def open_extra_sections(callback: CallbackQuery):
    if callback.message is None:
        await callback.answer()
        return

    await send_section_message(
        callback.message,
        "addition.png",
        EXTRA_TEXT,
        reply_markup=extra_keyboard(),
        parse_mode="HTML",
        replace_photo=True,
    )
    await callback.answer()


@router.callback_query(F.data == "become_executor")
async def become_executor(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await callback.message.delete()
    await callback.message.answer(
        BECOME_EXECUTOR_TEXT,
        parse_mode="HTML",
        reply_markup=become_executor_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "become_executor_back")
async def become_executor_back(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return

    await callback.message.delete()
    await send_section_message(
        callback.message,
        "addition.png",
        EXTRA_TEXT,
        reply_markup=extra_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()
