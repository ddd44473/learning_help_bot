from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
from utils import section_images


@pytest.mark.parametrize("field,value", [
    ("BOT_TOKEN", None), ("BOT_TOKEN", "invalid"), ("BOT_TOKEN", "123: secret"),
    ("ADMIN_IDS", set()), ("ADMIN_IDS", {-1}), ("MASTER_GROUP_ID", 0),
])
async def test_invalid_config_fails_without_secrets(monkeypatch, field, value):
    monkeypatch.setattr(config, field, value)
    with pytest.raises(ValueError) as exc:
        config.validate_deployment_config()
    assert "secret" not in str(exc.value)


async def test_synthetic_config_valid():
    config.validate_deployment_config()


async def test_changed_handlers_and_startup_import_without_polling():
    import importlib
    for name in ("handlers.master_order", "handlers.client_price", "main"):
        assert importlib.import_module(name) is not None


async def test_image_fallback_and_available_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(section_images, "IMAGES_DIR", tmp_path)
    message = SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())
    await section_images.send_section_message(message, "optional.png", "Раздел", reply_markup="buttons")
    message.answer.assert_awaited_once_with("Раздел", reply_markup="buttons", parse_mode=None)
    message.answer_photo.assert_not_called()
    (tmp_path / "optional.png").write_bytes(b"synthetic image placeholder")
    await section_images.send_section_message(message, "optional.png", "Раздел", reply_markup="buttons")
    message.answer_photo.assert_awaited_once()
