"""Tests for send_telegram, the only code that talks to the real Telegram API.

The HTTP call (urllib.request.urlopen) is faked, so every Telegram answer and failure can be simulated.
"""

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from types import SimpleNamespace

import pytest

import tools

TOKEN = "123456:secret-bot-token"


class FakeResponse:
    """Looks like what urlopen returns: usable in a `with` block, with .read()."""

    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def configured(settings):
    settings.telegram_bot_token = TOKEN
    settings.telegram_chat_id = "12345"


@pytest.fixture
def telegram_api(monkeypatch):
    """Fake urlopen. Set .reply to a dict (Telegram's JSON) or to an exception to raise. Records requests."""

    api = SimpleNamespace(reply={"ok": True, "result": {"message_id": 777}}, requests=[])

    def fake_urlopen(request, timeout=None):
        api.requests.append(request)
        if isinstance(api.reply, Exception):
            raise api.reply
        return FakeResponse(api.reply)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return api


def http_error(code, body):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    return urllib.error.HTTPError(url, code, "error", Message(), io.BytesIO(body))


def test_not_configured_sends_nothing(telegram_api):
    assert tools.send_telegram("hello") is None
    assert telegram_api.requests == []


def test_success_returns_the_message_id(configured, telegram_api):
    assert tools.send_telegram("hello") == 777

    [request] = telegram_api.requests
    payload = json.loads(request.data)
    assert payload == {"chat_id": "12345", "text": "hello"}  # no parse_mode: visitor text can't inject formatting


def test_reply_includes_reply_parameters(configured, telegram_api):
    tools.send_telegram("saved", reply_to_message_id=555)

    payload = json.loads(telegram_api.requests[0].data)
    assert payload["reply_parameters"] == {"message_id": 555, "allow_sending_without_reply": True}


def test_telegram_saying_not_ok_returns_none(configured, telegram_api):
    telegram_api.reply = {"ok": False, "description": "Bad Request: chat not found"}

    assert tools.send_telegram("hello") is None


@pytest.mark.parametrize(
    "error",
    [
        http_error(400, b'{"ok": false, "description": "Bad Request: chat not found"}'),
        http_error(502, b"<html>Bad Gateway</html>"),
        urllib.error.URLError("no route to host"),
        TimeoutError(),
    ],
    ids=["http-error-with-json", "http-error-with-html", "unreachable", "timeout"],
)
def test_failures_return_none_instead_of_crashing(configured, telegram_api, error):
    telegram_api.reply = error

    assert tools.send_telegram("hello") is None


def test_the_bot_token_is_never_printed(configured, telegram_api, capsys):
    # The URL contains the token; an error must not leak it into the logs (CloudWatch)
    telegram_api.reply = http_error(401, b"not json")

    tools.send_telegram("hello")

    assert TOKEN not in capsys.readouterr().out
