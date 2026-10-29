import pytest

from app.config import Settings


def test_admin_ids_are_parsed() -> None:
    settings = Settings(bot_token="test-token", admin_user_ids="123, 456,")
    assert settings.admins == {123, 456}


def test_empty_admin_list() -> None:
    settings = Settings(bot_token="test-token", admin_user_ids="")
    assert settings.admins == set()


def test_invalid_admin_id_is_rejected() -> None:
    settings = Settings(bot_token="test-token", admin_user_ids="abc")
    with pytest.raises(ValueError):
        _ = settings.admins
