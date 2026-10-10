"""Tests for the safety net in conftest.py: if these fail, no other test can be trusted."""

import urllib.request

import pytest

import config


def test_real_network_is_blocked():
    with pytest.raises(RuntimeError, match="real network"):
        urllib.request.urlopen("https://api.telegram.org", timeout=1)


def test_real_secrets_are_not_loaded():
    settings = config.get_settings()

    assert settings.telegram_bot_token is None
    assert settings.openrouter_api_key is None
