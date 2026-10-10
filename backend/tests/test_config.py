"""Tests for config.py: bad configuration must stop the app at startup, with a clear message."""

import pytest
from pydantic import ValidationError

from config import Settings


def make(**values):
    """Settings from these values only (never the real .env)."""
    return Settings(_env_file=None, **values)


def test_defaults_match_production():
    settings = make()

    assert settings.openrouter_model == "openai/gpt-5-mini"
    assert settings.openrouter_reasoning_effort == "medium"  # terraform.tfvars uses medium too
    assert settings.use_s3 is False


@pytest.mark.parametrize("value", ["true", "True", "1", "yes"])
def test_use_s3_accepts_real_booleans(value, monkeypatch):
    monkeypatch.setenv("USE_S3", value)
    monkeypatch.setenv("S3_BUCKET", "bucket")

    assert make().use_s3 is True


def test_use_s3_rejects_nonsense(monkeypatch):
    monkeypatch.setenv("USE_S3", "maybe")

    with pytest.raises(ValidationError, match="use_s3"):
        make()


def test_s3_without_a_bucket_is_rejected():
    with pytest.raises(ValidationError, match="S3_BUCKET"):
        make(use_s3=True)


def test_unknown_reasoning_effort_is_rejected():
    with pytest.raises(ValidationError, match="openrouter_reasoning_effort"):
        make(openrouter_reasoning_effort="meduim")


def test_cors_origins_are_read_as_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example, https://b.example")

    assert make().cors_origins == ["https://a.example", "https://b.example"]


def test_secrets_are_hidden_when_printed():
    settings = make(telegram_bot_token="123456:real-looking-token")

    assert "real-looking-token" not in str(settings)
    assert "real-looking-token" not in repr(settings)
    assert settings.telegram_bot_token.get_secret_value() == "123456:real-looking-token"
