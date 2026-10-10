"""API tests: real HTTP requests to the FastAPI app (in memory, no server), with a fake LLM.

The fake LLM replays scripted OpenRouter answers, so the whole chat flow is tested
without paying for a model call and with the same result every run.
"""

import json
import uuid

import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError
from openai.types.chat import ChatCompletion

import knowledge
import server
import tools

WEBHOOK_SECRET = "test-webhook-secret"
AHMED_CHAT_ID = "12345"


# --- Fake LLM ---------------------------------------------------------------------


def text_answer(content):
    """An OpenRouter answer that is plain text (real OpenAI SDK type, built from a dict)."""
    return _completion({"role": "assistant", "content": content}, finish_reason="stop")


def tool_call_answer(tool_name, arguments, call_id="call-1"):
    """An OpenRouter answer where the model asks the server to run a tool."""
    tool_call = {"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(arguments)}}
    return _completion({"role": "assistant", "content": None, "tool_calls": [tool_call]}, finish_reason="tool_calls")


def _completion(message, finish_reason):
    return ChatCompletion.model_validate(
        {
            "id": "test",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        }
    )


class FakeLLM:
    """Stands in for the OpenAI client. Returns the scripted answers in order and records every call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.chat = self  # so server code can call fake.chat.completions.create(...)
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def llm(monkeypatch):
    """Install a FakeLLM; each test sets llm.answers to script what the model says."""
    fake = FakeLLM()
    monkeypatch.setattr(server, "get_llm_client", lambda: fake)
    return fake


@pytest.fixture
def client(monkeypatch, tmp_path):
    # server.py reads these once at import time, so patch the module values directly
    monkeypatch.setattr(server, "USE_S3", False)
    monkeypatch.setattr(server, "MEMORY_DIR", str(tmp_path / "memory"))
    return TestClient(server.app)


@pytest.fixture
def telegram(monkeypatch):
    """Fake send_telegram in both modules that use it; records texts and returns message ids."""
    sent = []

    def fake_send(text, reply_to_message_id=None):
        sent.append(text)
        return 1000 + len(sent)

    monkeypatch.setattr(tools, "send_telegram", fake_send)
    monkeypatch.setattr(server, "send_telegram", fake_send)
    return sent


# --- Simple endpoints ------------------------------------------------------------


def test_health(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_chat_without_message_is_rejected(client):
    assert client.post("/chat", json={}).status_code == 422


# --- /chat ----------------------------------------------------------------------


def test_chat_returns_the_answer_and_a_new_session_id(client, llm):
    llm.answers = [text_answer("Hi, I'm Ezz.")]

    response = client.post("/chat", json={"message": "Who are you?"})

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "Hi, I'm Ezz."
    uuid.UUID(body["session_id"])  # raises if it isn't a valid UUID


def test_chat_sends_previous_messages_on_the_second_question(client, llm):
    llm.answers = [text_answer("I work at Valeo."), text_answer("On ACC.")]

    first = client.post("/chat", json={"message": "Where do you work?"}).json()
    client.post("/chat", json={"message": "On what?", "session_id": first["session_id"]})

    second_call_messages = [m["content"] for m in llm.calls[1]["messages"]]
    assert "Where do you work?" in second_call_messages
    assert "I work at Valeo." in second_call_messages


def test_chat_runs_a_tool_then_answers(client, llm, telegram):
    llm.answers = [
        tool_call_answer("record_unknown_question", {"question": "What was your first car?"}),
        text_answer("I don't know yet, I've asked Ahmed."),
    ]

    response = client.post("/chat", json={"message": "What was your first car?"})

    assert response.status_code == 200
    assert response.json()["response"] == "I don't know yet, I've asked Ahmed."
    assert len(llm.calls) == 2
    assert len(telegram) == 1
    # The second model call must include the tool's result
    tool_messages = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"]) == {"status": "success"}


def test_tool_loop_is_bounded(client, llm, telegram):
    # A model that keeps asking for tools: after MAX_TOOL_ROUNDS it must be forced to answer
    llm.answers = [tool_call_answer("record_unknown_question", {"question": f"q{i}"}) for i in range(3)]
    llm.answers.append(text_answer("Final answer."))

    response = client.post("/chat", json={"message": "Tell me everything"})

    assert response.status_code == 200
    assert len(llm.calls) == server.MAX_TOOL_ROUNDS + 1
    assert [call["tool_choice"] for call in llm.calls] == ["auto", "auto", "auto", "none"]


def test_empty_model_answer_gives_502(client, llm):
    llm.answers = [text_answer("")]

    assert client.post("/chat", json={"message": "Hello"}).status_code == 502


def test_openrouter_out_of_credits_gives_503(client, llm):
    response_402 = httpx2.Response(402, request=httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    llm.answers = [APIStatusError("Payment required", response=response_402, body=None)]

    assert client.post("/chat", json={"message": "Hello"}).status_code == 503


def test_openrouter_unreachable_gives_504(client, llm):
    request = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    llm.answers = [APIConnectionError(request=request)]

    assert client.post("/chat", json={"message": "Hello"}).status_code == 504


# --- /telegram-webhook ------------------------------------------------------------


@pytest.fixture
def webhook_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", AHMED_CHAT_ID)


def telegram_reply(text, reply_to_message_id, chat_id=AHMED_CHAT_ID):
    """The JSON Telegram sends when Ahmed replies to one of the twin's messages."""
    return {
        "message": {
            "message_id": 555,
            "chat": {"id": int(chat_id)},
            "text": text,
            "reply_to_message": {"message_id": reply_to_message_id},
        }
    }


def post_webhook(client, update, secret=WEBHOOK_SECRET):
    return client.post("/telegram-webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def test_webhook_rejects_a_wrong_secret(client, webhook_configured):
    assert post_webhook(client, telegram_reply("hi", 1), secret="wrong").status_code == 403


def test_webhook_rejects_everything_when_no_secret_is_configured(client):
    assert post_webhook(client, telegram_reply("hi", 1)).status_code == 403


def test_webhook_saves_a_reply_to_a_pending_question(client, webhook_configured, telegram):
    knowledge.save_pending(1001, "What was your first car?", "session-1")

    response = post_webhook(client, telegram_reply("A red Golf", reply_to_message_id=1001))

    assert response.status_code == 200
    [entry] = knowledge.list_qa()
    assert entry["question"] == "What was your first car?"
    assert entry["answer"] == "A red Golf"
    assert "Saved" in telegram[0]


def test_webhook_warns_when_the_reply_matches_no_question(client, webhook_configured, telegram):
    response = post_webhook(client, telegram_reply("A red Golf", reply_to_message_id=9999))

    assert response.status_code == 200
    assert knowledge.list_qa() == []
    assert "couldn't match" in telegram[0]


def test_webhook_ignores_other_chats(client, webhook_configured, telegram):
    knowledge.save_pending(1001, "What was your first car?", "session-1")

    response = post_webhook(client, telegram_reply("I'm not Ahmed", 1001, chat_id="99999"))

    assert response.status_code == 200
    assert knowledge.list_qa() == []
    assert telegram == []
