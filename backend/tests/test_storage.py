"""Tests for knowledge.py (learned Q&A) and ssm_secrets.py.

Local mode uses the temporary folder from conftest.py. S3 and SSM use moto, a fake AWS that runs in memory.
"""

from types import SimpleNamespace
from typing import Final

import boto3
import pytest
from moto import mock_aws

import knowledge
import ssm_secrets

REGION: Final = "eu-north-1"


@pytest.fixture
def fake_aws():
    """Everything boto3 does inside this fixture goes to moto's in-memory AWS, never the real one."""
    with mock_aws():
        yield


@pytest.fixture
def knowledge_bucket(fake_aws, settings):
    """A fake S3 knowledge bucket, and knowledge.py switched to S3 mode."""
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket="twin-knowledge-test", CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    settings.knowledge_bucket = "twin-knowledge-test"


@pytest.fixture
def clock(monkeypatch):
    """Controls the time knowledge.py sees, so the 60-second cache is tested without sleeping."""
    now = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(knowledge, "time", SimpleNamespace(monotonic=lambda: now.value))
    return now


def questions(entries):
    return [entry["question"] for entry in entries]


# --- knowledge.py, local files -----------------------------------------------------


def test_pending_question_can_be_popped_once():
    knowledge.save_pending(42, "What was your first car?", "session-1")

    first = knowledge.pop_pending(42)
    second = knowledge.pop_pending(42)

    assert first is not None
    assert first["question"] == "What was your first car?"
    assert second is None  # popping removes it, so a second reply can't save the answer twice


def test_unknown_pending_question_returns_none():
    assert knowledge.pop_pending(999) is None


def test_added_qa_is_listed_with_its_answer():
    knowledge.add_qa("What car do you drive?", "A VW Passat")

    [entry] = knowledge.list_qa()

    assert entry["question"] == "What car do you drive?"
    assert entry["answer"] == "A VW Passat"


def test_list_qa_is_empty_when_nothing_was_learned():
    assert knowledge.list_qa() == []


# --- knowledge.py, the 60-second cache ---------------------------------------------


def test_list_qa_is_cached_for_60_seconds(clock):
    assert knowledge.list_qa() == []

    # Written behind the cache's back (like another Lambda container saving an answer)
    knowledge._put("qa/20261011T120000-abcd1234.json", {"question": "Q1", "answer": "A1"})

    clock.value += 59
    assert knowledge.list_qa() == []  # still the cached (old) list

    clock.value += 2
    assert questions(knowledge.list_qa()) == ["Q1"]  # cache expired, read again


def test_add_qa_makes_the_new_answer_visible_immediately(clock):
    assert knowledge.list_qa() == []

    knowledge.add_qa("Q1", "A1")

    assert questions(knowledge.list_qa()) == ["Q1"]  # no waiting for the cache to expire


# --- knowledge.py, S3 mode (moto) --------------------------------------------------


def test_s3_pending_question_round_trip(knowledge_bucket):
    knowledge.save_pending(42, "What was your first car?", "session-1")

    pending = knowledge.pop_pending(42)
    assert pending is not None
    assert pending["question"] == "What was your first car?"
    assert knowledge.pop_pending(42) is None


def test_s3_qa_entries_are_listed_oldest_first(knowledge_bucket):
    knowledge._put("qa/20261011T120000-bbbb.json", {"question": "newer", "answer": "A"})
    knowledge._put("qa/20261010T120000-aaaa.json", {"question": "older", "answer": "A"})

    assert questions(knowledge.list_qa()) == ["older", "newer"]


def test_s3_error_returns_empty_list_instead_of_failing_the_chat(fake_aws, settings):
    settings.knowledge_bucket = "bucket-that-does-not-exist"

    assert knowledge.list_qa() == []


# --- ssm_secrets.py ----------------------------------------------------------------


def test_secret_from_local_environment_wins(settings):
    settings.telegram_bot_token = "local-token"
    settings.telegram_bot_token_param = "/twin/test/telegram_bot_token"  # ignored: the local value wins

    assert ssm_secrets.get_secret("telegram_bot_token") == "local-token"


def test_secret_is_none_when_not_configured():
    assert ssm_secrets.get_secret("telegram_bot_token") is None


def test_secret_is_read_from_ssm_and_cached(fake_aws, settings):
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name="/twin/test/telegram_bot_token", Value="ssm-token", Type="SecureString")
    settings.telegram_bot_token_param = "/twin/test/telegram_bot_token"

    assert ssm_secrets.get_secret("telegram_bot_token") == "ssm-token"

    # Second call must not need SSM: delete the parameter and it still works from the cache
    ssm.delete_parameter(Name="/twin/test/telegram_bot_token")
    assert ssm_secrets.get_secret("telegram_bot_token") == "ssm-token"


def test_missing_ssm_parameter_returns_none(fake_aws, settings):
    settings.telegram_bot_token_param = "/twin/test/does-not-exist"

    assert ssm_secrets.get_secret("telegram_bot_token") is None
