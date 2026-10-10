"""Q&A knowledge the twin learns from Ahmed's Telegram replies.

Stored in a dedicated S3 bucket that is NOT managed by Terraform, so destroying an
environment never wipes what the twin has learned. Locally (no KNOWLEDGE_BUCKET set)
everything is stored as files in the repo's knowledge/ folder instead.

Layout:
  pending/<telegram_message_id>.json   questions waiting for Ahmed's reply
  qa/<timestamp>-<uuid>.json           answered questions (one object per entry, so saves never overwrite each other)
"""

import json
import os
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict, cast

import boto3
from botocore.exceptions import ClientError

from config import get_settings

if TYPE_CHECKING:  # type stubs are a dev dependency: never imported at runtime (not on Lambda)
    from types_boto3_s3 import S3Client

# How long the Q&A list is reused before reading it again from storage
CACHE_SECONDS = 60


class PendingQuestion(TypedDict):
    """A question sent to Ahmed on Telegram, waiting for his reply."""

    question: str
    session_id: str
    asked_at: str


class QAEntry(TypedDict):
    """A question Ahmed answered: the twin uses it in its prompt."""

    question: str
    answer: str
    answered_at: str


_qa_cache: list[QAEntry] | None = None
_qa_cache_time = 0.0
_s3_client: "S3Client | None" = None


def _bucket() -> str:
    return get_settings().knowledge_bucket


def _local_dir() -> str:
    return str(get_settings().knowledge_dir)


def _s3() -> "S3Client":
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _put(key: str, data: Mapping[str, Any]) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2)
    if _bucket():
        _s3().put_object(Bucket=_bucket(), Key=key, Body=body, ContentType="application/json")
    else:
        path = os.path.join(_local_dir(), key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)


def _get(key: str) -> dict[str, Any] | None:
    if _bucket():
        try:
            response = _s3().get_object(Bucket=_bucket(), Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        return cast(dict[str, Any], json.loads(response["Body"].read().decode("utf-8")))

    path = os.path.join(_local_dir(), key)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return cast(dict[str, Any], json.load(f))


def _delete(key: str) -> None:
    if _bucket():
        _s3().delete_object(Bucket=_bucket(), Key=key)
    else:
        path = os.path.join(_local_dir(), key)
        if os.path.exists(path):
            os.remove(path)


def _list_keys(prefix: str) -> list[str]:
    if _bucket():
        keys: list[str] = []
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return sorted(keys)

    folder = os.path.join(_local_dir(), prefix)
    if not os.path.isdir(folder):
        return []
    return sorted(prefix + name for name in os.listdir(folder) if name.endswith(".json"))


def save_pending(telegram_message_id: int, question: str, session_id: str) -> None:
    """Remember which question a Telegram notification was about, so a reply to it can be matched exactly"""
    pending: PendingQuestion = {
        "question": question,
        "session_id": session_id,
        "asked_at": datetime.now(UTC).isoformat(),
    }
    _put(f"pending/{telegram_message_id}.json", pending)


def pop_pending(telegram_message_id: int) -> PendingQuestion | None:
    """Return the pending question for this Telegram message and remove it, or None if unknown"""
    key = f"pending/{telegram_message_id}.json"
    pending = _get(key)
    if pending is not None:
        _delete(key)
    return cast(PendingQuestion | None, pending)


def add_qa(question: str, answer: str) -> None:
    """Save an answered question as its own object"""
    global _qa_cache
    now = datetime.now(UTC)
    entry: QAEntry = {"question": question, "answer": answer, "answered_at": now.isoformat()}
    _put(f"qa/{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json", entry)
    # Make the new answer visible immediately in this container
    _qa_cache = None


def list_qa() -> list[QAEntry]:
    """All answered questions, oldest first (cached for CACHE_SECONDS)"""
    global _qa_cache, _qa_cache_time
    if _qa_cache is not None and time.monotonic() - _qa_cache_time < CACHE_SECONDS:
        return _qa_cache

    try:
        entries = [_get(key) for key in _list_keys("qa/")]
        _qa_cache = [cast(QAEntry, entry) for entry in entries if entry]
    except ClientError as e:
        # Answer without learned Q&A rather than failing the whole chat
        print(f"Could not load Q&A knowledge: {e.response['Error']['Code']}")
        return _qa_cache or []

    _qa_cache_time = time.monotonic()
    return _qa_cache
