"""Offline media caching tests; only synthetic assets and Telegram results."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendPhoto
from aiogram.types import FSInputFile, Message

from utils import section_images


def photo_result(file_id="large-photo"):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat={"id": 123, "type": "private"},
        photo=[
            {"file_id": "small-photo", "file_unique_id": "small", "width": 10, "height": 10},
            {"file_id": file_id, "file_unique_id": "large", "width": 100, "height": 100},
        ],
    )


def bad_request(description):
    return TelegramBadRequest(
        method=SendPhoto(chat_id=123, photo="cached-photo"), message=description,
    )


@pytest.fixture
def asset(tmp_path, monkeypatch):
    monkeypatch.setattr(section_images, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(section_images, "_PHOTO_FILE_IDS", {})
    path = tmp_path / "menu.png"
    path.write_bytes(b"synthetic png")
    return path


@pytest.fixture
def message():
    return SimpleNamespace(
        bot=SimpleNamespace(id=123), photo=True,
        answer_photo=AsyncMock(return_value=photo_result()),
        edit_media=AsyncMock(return_value=photo_result()),
        answer=AsyncMock(), edit_caption=AsyncMock(), edit_text=AsyncMock(),
    )


def sent_photo(message, replace):
    call = (message.edit_media if replace else message.answer_photo).await_args
    return call.kwargs["media"].media if replace else call.kwargs["photo"]


@pytest.mark.parametrize("replace", [False, True])
async def test_initial_upload_and_reuse_preserve_content(asset, message, replace):
    options = {"caption": "<b>Раздел</b>", "parse_mode": "HTML", "replace_photo": replace}
    result = await section_images.send_section_message(message, asset.name, **options)
    upload = sent_photo(message, replace)
    assert isinstance(upload, FSInputFile)
    assert upload.path == asset
    assert upload.filename == asset.name
    assert result.photo[-1].file_id == "large-photo"

    await section_images.send_section_message(message, asset.name, **options)
    assert sent_photo(message, replace) == "large-photo"
    call = (message.edit_media if replace else message.answer_photo).await_args
    content = call.kwargs["media"] if replace else SimpleNamespace(**call.kwargs)
    assert content.caption == options["caption"]
    assert content.parse_mode == "HTML"
    assert call.kwargs["reply_markup"] is None
    assert (message.edit_media if replace else message.answer_photo).await_count == 2


async def test_reuse_between_send_and_replace_with_keyboard(asset, message):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Menu", callback_data="main_menu"),
    ]])
    await section_images.send_section_message(message, asset.name, "first")
    await section_images.send_section_message(
        message, asset.name, "second", replace_photo=True, reply_markup=keyboard,
    )
    assert sent_photo(message, True) == "large-photo"
    assert message.edit_media.await_args.kwargs["reply_markup"] is keyboard


async def test_replace_without_existing_photo_sends_photo(asset, message):
    message.photo = None
    await section_images.send_section_message(message, asset.name, "caption", replace_photo=True)
    assert isinstance(sent_photo(message, False), FSInputFile)
    message.edit_media.assert_not_awaited()


@pytest.mark.parametrize("change", ["rewrite", "replace"])
async def test_changed_asset_is_uploaded_again(asset, message, change):
    await section_images.send_section_message(message, asset.name, "caption")
    if change == "rewrite":
        asset.write_bytes(b"new synthetic png contents")
    else:
        replacement = asset.with_suffix(".new")
        replacement.write_bytes(asset.read_bytes())
        replacement.replace(asset)
    await section_images.send_section_message(message, asset.name, "caption")
    assert isinstance(sent_photo(message, False), FSInputFile)


@pytest.mark.parametrize("replace,has_photo", [(False, False), (True, True), (True, False)])
async def test_missing_asset_preserves_text_fallback(asset, message, replace, has_photo):
    await section_images.send_section_message(message, asset.name, "caption")
    asset.unlink()
    message.photo = has_photo
    await section_images.send_section_message(
        message, asset.name, "fallback", replace_photo=replace,
        reply_markup="buttons", parse_mode="HTML",
    )
    fallback = message.edit_caption if replace and has_photo else (
        message.edit_text if replace else message.answer
    )
    expected = {"reply_markup": "buttons", "parse_mode": "HTML"}
    if replace and has_photo:
        fallback.assert_awaited_once_with(caption="fallback", **expected)
    else:
        fallback.assert_awaited_once_with("fallback", **expected)
    assert not section_images._PHOTO_FILE_IDS
    message.answer_photo.assert_awaited_once()
    message.edit_media.assert_not_awaited()


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("description", [
    "Bad Request: wrong file identifier/HTTP URL specified",
    "Bad Request: wrong remote file identifier specified: Wrong character in the string",
    "Bad Request: FILE_REFERENCE_EXPIRED",
])
async def test_invalid_cached_id_reuploads_once(asset, message, replace, description):
    await section_images.send_section_message(message, asset.name, "caption")
    method = message.edit_media if replace else message.answer_photo
    method.reset_mock()
    method.side_effect = [bad_request(description), photo_result("replacement-id")]
    await section_images.send_section_message(message, asset.name, "caption", replace_photo=replace)
    calls = method.await_args_list
    first = calls[0].kwargs["media"].media if replace else calls[0].kwargs["photo"]
    assert first == "large-photo"
    assert isinstance(sent_photo(message, replace), FSInputFile)
    assert method.await_count == 2
    method.side_effect = None
    await section_images.send_section_message(message, asset.name, "caption", replace_photo=replace)
    assert sent_photo(message, replace) == "replacement-id"


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("description", [
    "Bad Request: message is not modified",
    "Bad Request: message can't be edited",
    "Bad Request: caption is too long",
    "Bad Request: can't parse entities",
])
async def test_unrelated_bad_requests_propagate_without_retry(asset, message, replace, description):
    await section_images.send_section_message(message, asset.name, "caption")
    method = message.edit_media if replace else message.answer_photo
    method.reset_mock()
    error = bad_request(description)
    method.side_effect = error
    with pytest.raises(TelegramBadRequest) as caught:
        await section_images.send_section_message(message, asset.name, "caption", replace_photo=replace)
    assert caught.value is error
    method.assert_awaited_once()
    assert section_images._PHOTO_FILE_IDS


@pytest.mark.parametrize("cached", [False, True])
async def test_network_error_propagates_without_retry(asset, message, cached):
    if cached:
        await section_images.send_section_message(message, asset.name, "caption")
    message.answer_photo.reset_mock()
    error = TelegramNetworkError(method=SendPhoto(chat_id=123, photo="id"), message="offline")
    message.answer_photo.side_effect = error
    with pytest.raises(TelegramNetworkError) as caught:
        await section_images.send_section_message(message, asset.name, "caption")
    assert caught.value is error
    message.answer_photo.assert_awaited_once()
    assert bool(section_images._PHOTO_FILE_IDS) is cached


async def test_invalid_id_during_initial_upload_is_not_retried(asset, message):
    message.answer_photo.side_effect = bad_request("Bad Request: wrong file identifier/HTTP URL specified")
    with pytest.raises(TelegramBadRequest):
        await section_images.send_section_message(message, asset.name, "caption")
    message.answer_photo.assert_awaited_once()
    assert not section_images._PHOTO_FILE_IDS


async def test_failed_reupload_evicts_invalid_id_and_propagates(asset, message):
    await section_images.send_section_message(message, asset.name, "caption")
    error = bad_request("Bad Request: caption is too long")
    message.answer_photo.reset_mock()
    message.answer_photo.side_effect = [
        bad_request("Bad Request: wrong file identifier/HTTP URL specified"), error,
    ]
    with pytest.raises(TelegramBadRequest) as caught:
        await section_images.send_section_message(message, asset.name, "caption")
    assert caught.value is error
    assert message.answer_photo.await_count == 2
    assert not section_images._PHOTO_FILE_IDS


@pytest.mark.parametrize("result", [True, None, SimpleNamespace(photo=[SimpleNamespace(file_id="id")]),
                                        photo_result().model_copy(update={"photo": []}), photo_result("")])
async def test_ambiguous_results_are_not_cached(asset, message, result):
    message.edit_media.return_value = result
    assert await section_images.send_section_message(
        message, asset.name, "caption", replace_photo=True,
    ) is result
    assert not section_images._PHOTO_FILE_IDS


async def test_asset_changed_during_upload_is_not_cached(asset, message):
    async def upload(**kwargs):
        asset.write_bytes(b"changed while awaiting Telegram")
        return photo_result()

    message.answer_photo.side_effect = upload
    await section_images.send_section_message(message, asset.name, "caption")
    assert not section_images._PHOTO_FILE_IDS


async def test_cache_is_scoped_to_bot(asset, message):
    await section_images.send_section_message(message, asset.name, "caption")
    message.bot = SimpleNamespace(id=456)
    await section_images.send_section_message(message, asset.name, "caption")
    assert isinstance(sent_photo(message, False), FSInputFile)
