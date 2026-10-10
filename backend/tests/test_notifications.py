"""Tests for the two tools that notify Ahmed on Telegram, and for handle_tool_calls.

Telegram is replaced by a fake that records what *would* have been sent.
"""

import json
from types import SimpleNamespace

import pytest

import knowledge
import tools


@pytest.fixture
def telegram(monkeypatch):
    """Fake send_telegram: records every message and returns a new message id, like Telegram does."""
    sent = []

    def fake_send(text, reply_to_message_id=None):
        sent.append(text)
        return 1000 + len(sent)  # Telegram's message id

    monkeypatch.setattr(tools, "send_telegram", fake_send)
    return sent


@pytest.fixture
def telegram_down(monkeypatch):
    """Fake send_telegram that always fails, like Telegram being unreachable."""
    monkeypatch.setattr(tools, "send_telegram", lambda text, reply_to_message_id=None: None)


@pytest.fixture
def any_domain_accepts_mail(monkeypatch):
    """Skip the DNS check (it has its own tests in test_tools.py)."""
    monkeypatch.setattr(tools, "email_domain_accepts_mail", lambda domain: True)


# --- record_unknown_question ------------------------------------------------------


def test_unknown_question_notifies_and_saves_pending(telegram):
    result = tools.record_unknown_question("session-1", "What was your first car?")

    assert result == {"status": "success"}
    assert len(telegram) == 1
    assert "What was your first car?" in telegram[0]
    # The reply to Telegram message 1001 must be matchable to this question later
    pending = knowledge.pop_pending(1001)
    assert pending["question"] == "What was your first car?"
    assert pending["session_id"] == "session-1"


def test_unknown_question_is_truncated_before_sending(telegram):
    tools.record_unknown_question("session-1", "x" * 2000)

    assert "x" * tools.MAX_FIELD_LENGTH + "…" in telegram[0]
    assert "x" * (tools.MAX_FIELD_LENGTH + 1) not in telegram[0]


def test_empty_question_sends_nothing(telegram):
    result = tools.record_unknown_question("session-1", "   ")

    assert result["status"] == "error"
    assert telegram == []


def test_notification_limit_per_session(telegram):
    for i in range(tools.MAX_NOTIFICATIONS_PER_SESSION):
        assert tools.record_unknown_question("session-1", f"question {i}")["status"] == "success"

    result = tools.record_unknown_question("session-1", "one too many")

    assert result["status"] == "error"
    assert len(telegram) == tools.MAX_NOTIFICATIONS_PER_SESSION


def test_notification_limit_is_per_session_not_global(telegram):
    for i in range(tools.MAX_NOTIFICATIONS_PER_SESSION):
        tools.record_unknown_question("session-1", f"question {i}")

    result = tools.record_unknown_question("session-2", "a new visitor")

    assert result["status"] == "success"


def test_failed_telegram_send_does_not_use_up_the_limit(telegram_down):
    result = tools.record_unknown_question("session-1", "What was your first car?")

    assert result["status"] == "error"
    assert tools._get_notification_count("session-1") == 0


# --- record_user_details ----------------------------------------------------------


def test_contact_details_are_sent(telegram, any_domain_accepts_mail):
    result = tools.record_user_details("session-1", email="recruiter@company.com", name="Sam", notes="ADAS role")

    assert result == {"status": "success"}
    assert "recruiter@company.com" in telegram[0]
    assert "Sam" in telegram[0]
    assert "ADAS role" in telegram[0]


def test_invalid_email_sends_nothing(telegram, any_domain_accepts_mail):
    result = tools.record_user_details("session-1", email="not-an-email")

    assert result["status"] == "error"
    assert telegram == []


def test_email_domain_without_mail_sends_nothing(telegram, monkeypatch):
    monkeypatch.setattr(tools, "email_domain_accepts_mail", lambda domain: False)

    result = tools.record_user_details("session-1", email="someone@invented-domain.com")

    assert result["status"] == "error"
    assert telegram == []


def test_both_tools_share_the_same_limit(telegram, any_domain_accepts_mail):
    for i in range(tools.MAX_NOTIFICATIONS_PER_SESSION):
        tools.record_unknown_question("session-1", f"question {i}")

    result = tools.record_user_details("session-1", email="recruiter@company.com")

    assert result["status"] == "error"


# --- handle_tool_calls ------------------------------------------------------------


def fake_tool_call(name, arguments, call_id="call-1"):
    """Looks like one tool call from the OpenAI SDK: only .id and .function.name/.arguments are used."""
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def test_tool_call_runs_the_tool_and_returns_a_tool_message(telegram):
    call = fake_tool_call("record_unknown_question", json.dumps({"question": "Favourite food?"}))

    [message] = tools.handle_tool_calls([call], "session-1")

    assert message["role"] == "tool"
    assert message["tool_call_id"] == "call-1"
    assert json.loads(message["content"]) == {"status": "success"}


def test_unknown_tool_returns_an_error_instead_of_crashing():
    [message] = tools.handle_tool_calls([fake_tool_call("delete_everything", "{}")], "session-1")

    assert json.loads(message["content"])["status"] == "error"


@pytest.mark.parametrize(
    "arguments",
    ["{not json", json.dumps({"unexpected_argument": 1})],
    ids=["invalid-json", "wrong-arguments"],
)
def test_bad_tool_arguments_return_an_error_instead_of_crashing(arguments, telegram):
    [message] = tools.handle_tool_calls([fake_tool_call("record_unknown_question", arguments)], "session-1")

    assert json.loads(message["content"])["status"] == "error"
    assert telegram == []


# --- Notification counter in S3 mode (what production uses), with moto ------------------


def test_notification_limit_is_stored_in_s3_in_production(telegram, settings):
    import boto3
    from moto import mock_aws

    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-north-1")
        s3.create_bucket(Bucket="twin-memory-test", CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        settings.s3_bucket = "twin-memory-test"
        settings.use_s3 = True

        for i in range(tools.MAX_NOTIFICATIONS_PER_SESSION):
            assert tools.record_unknown_question("session-1", f"question {i}")["status"] == "success"
        result = tools.record_unknown_question("session-1", "one too many")

        assert result["status"] == "error"
        stored = json.loads(s3.get_object(Bucket="twin-memory-test", Key="limits/session-1.json")["Body"].read())
        assert stored == {"count": tools.MAX_NOTIFICATIONS_PER_SESSION}
