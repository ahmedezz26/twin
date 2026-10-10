"""Shared test setup. pytest loads this file before any test module.

Safety rules for the whole suite:
- the real .env is never loaded (server.py calls load_dotenv() on import)
- real secrets and AWS credentials are removed from the environment
- any attempt to open a real network connection fails the test
"""

import socket

import dotenv
import pytest

# Must happen before any test imports server.py, whose load_dotenv() would read the real .env
dotenv.load_dotenv = lambda *args, **kwargs: False

SECRET_ENV_VARS = [
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEY_PARAM",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN_PARAM",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_CHAT_ID_PARAM",
    "TELEGRAM_WEBHOOK_SECRET",
    "TELEGRAM_WEBHOOK_SECRET_PARAM",
    "AWS_PROFILE",
    "AWS_SESSION_TOKEN",
    "USE_S3",
    "S3_BUCKET",
    "KNOWLEDGE_BUCKET",
]


class NetworkBlockedError(RuntimeError):
    """Raised when a test tries to reach the real network."""


def _blocked(*args, **kwargs):
    raise NetworkBlockedError("A test tried to use the real network. Mock the boundary instead.")


@pytest.fixture(autouse=True)
def safe_environment(monkeypatch, tmp_path):
    """Runs around every test automatically (autouse): no secrets, no real AWS, no network."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    # Fake AWS credentials, so boto3 can never pick up the real ones (moto accepts anything)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")

    # Local storage goes to a fresh temporary folder for each test
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("KNOWLEDGE_DIR", str(tmp_path / "knowledge"))

    # Block real network connections and DNS lookups
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket.socket, "sendto", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
