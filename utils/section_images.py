from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, Message


IMAGES_DIR = Path(__file__).resolve().parent.parent / "imgs"

# One entry per bot and asset; file metadata invalidates replaced/edited assets.
_PHOTO_FILE_IDS: dict[tuple[int | None, Path], tuple[tuple[int, ...], str]] = {}


def _image_version(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _invalid_file_id(exc: TelegramBadRequest) -> bool:
    description = exc.message.lower().removeprefix("bad request: ")
    return description.startswith((
        "wrong file identifier/http url specified",
        "wrong remote file identifier specified",
    )) or description in {
        "invalid file_id", "invalid file id", "file_id_invalid",
        "file_reference_expired", "file reference expired",
    }


async def send_section_message(
    message: Message,
    filename: str,
    caption: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
    replace_photo: bool = False,
):
    """Показывает раздел одним сообщением: фото + текст + inline-кнопки."""

    image_path = IMAGES_DIR / filename
    cache_key = (getattr(getattr(message, "bot", None), "id", None), image_path.resolve())

    if not image_path.is_file():
        _PHOTO_FILE_IDS.pop(cache_key, None)
        if replace_photo:
            return await edit_message_content(
                message, caption, reply_markup=reply_markup, parse_mode=parse_mode,
            )
        return await message.answer(
            caption, reply_markup=reply_markup, parse_mode=parse_mode,
        )

    version = _image_version(image_path)
    cached = _PHOTO_FILE_IDS.get(cache_key)
    if cached is not None and cached[0] != version:
        _PHOTO_FILE_IDS.pop(cache_key, None)
        cached = None

    async def deliver(photo: str | FSInputFile):
        if replace_photo and message.photo:
            return await message.edit_media(
                media=InputMediaPhoto(
                    media=photo,
                    caption=caption,
                    parse_mode=parse_mode,
                ),
                reply_markup=reply_markup,
            )
        return await message.answer_photo(
            photo=photo,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    try:
        result = await deliver(cached[1] if cached else FSInputFile(image_path))
    except TelegramBadRequest as exc:
        if cached is None or not _invalid_file_id(exc):
            raise
        _PHOTO_FILE_IDS.pop(cache_key, None)
        result = await deliver(FSInputFile(image_path))

    # Inline edits can return True; mocks/other ambiguous results are not IDs.
    if isinstance(result, Message) and result.photo:
        file_id = result.photo[-1].file_id
        if file_id and not file_id.isspace():
            try:
                unchanged = image_path.is_file() and _image_version(image_path) == version
            except FileNotFoundError:
                unchanged = False
            if unchanged:
                _PHOTO_FILE_IDS[cache_key] = (version, file_id)
    return result


async def edit_message_content(
    message: Message,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
):
    """Редактирует текстовое сообщение или подпись у сообщения с фото."""

    if message.photo:
        try:
            return await message.edit_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except TelegramBadRequest as exc:
            # У фото caption ограничен 1024 символами. Для длинного текста
            # убираем старый экран и продолжаем обычным текстовым сообщением.
            if "caption is too long" not in str(exc).lower():
                raise

            await message.delete()
            return await message.answer(
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )

    return await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
