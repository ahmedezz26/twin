"""Shared test setup. pytest loads this file before any test module.

Safety rules for the whole suite:
- the real .env is never read, and real secrets / AWS credentials are never used
- every test gets its own Settings object (request the `settings` fixture to change it)
- any attempt to open a real network connection fails the test
"""

import socket

import pytest

import config

# Must happen before any test imports server.py (it reads the settings at import, for CORS):
# settings built here never read the real .env, and secrets are explicitly empty
SAFE_DEFAULTS = {
    "_env_file": None,
    "use_s3": False,
    "s3_bucket": "",
    "knowledge_bucket": "",
    "openrouter_api_key": None,
    "openrouter_api_key_param": "",
    "telegram_bot_token": None,
    "telegram_bot_token_param": "",
    "telegram_chat_id": None,
    "telegram_chat_id_param": "",
    "telegram_webhook_secret": None,
    "telegram_webhook_secret_param": "",
}
config._settings = config.Settings(**SAFE_DEFAULTS)

# Imported only now, on purpose: server.py reads the settings while it is being imported
import knowledge
import server
import ssm_secrets
import tools


class NetworkBlockedError(RuntimeError):
    """Raised when a test tries to reach the real network."""


def _blocked(*args, **kwargs):
    raise NetworkBlockedError("A test tried to use the real network. Mock the boundary instead.")


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    """Runs around every test (autouse). Returns that test's Settings, so a test can change it:

    def test_something(settings):
        settings.telegram_bot_token = "fake-token"
    """
    test_settings = config.Settings(
        **SAFE_DEFAULTS,
        memory_dir=tmp_path / "memory",  # a fresh temporary folder per test
        knowledge_dir=tmp_path / "knowledge",
    )
    monkeypatch.setattr(config, "_settings", test_settings)

    # Fake AWS credentials, so boto3 can never pick up the real ones (moto accepts anything)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    # Module-level caches would leak state from one test into the next: start every test empty
    monkeypatch.setattr(ssm_secrets, "_cache", {})
    monkeypatch.setattr(knowledge, "_qa_cache", None)
    monkeypatch.setattr(knowledge, "_qa_cache_time", 0.0)
    monkeypatch.setattr(knowledge, "_s3_client", None)
    monkeypatch.setattr(tools, "_s3_client", None)
    monkeypatch.setattr(server, "_s3_client", None)
    monkeypatch.setattr(server, "_llm_client", None)

    # Block real network connections and DNS lookups
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket.socket, "sendto", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    return test_settings
