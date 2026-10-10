"""Tests for the safety net in conftest.py: if these fail, no other test can be trusted."""

import os
import urllib.request

import pytest
from conftest import NetworkBlockedError


def test_real_network_is_blocked():
    with pytest.raises(NetworkBlockedError):
        urllib.request.urlopen("https://api.telegram.org", timeout=1)


def test_real_secrets_are_not_loaded():
    import server  # noqa: F401 - importing runs load_dotenv(), which must do nothing in tests

    assert os.getenv("TELEGRAM_BOT_TOKEN") is None
    assert os.getenv("OPENROUTER_API_KEY") is None
