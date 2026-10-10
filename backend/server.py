import hmac
import json
import os
import traceback
import uuid
from datetime import UTC, datetime
from uuid import UUID

import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, Field

from config import get_settings
from context import prompt
from knowledge import add_qa, pop_pending
from ssm_secrets import get_secret
from tools import get_telegram_chat_id, handle_tool_calls, send_telegram, tools

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# OpenRouter configuration
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Tool-calling rounds per message before the model must answer without tools
MAX_TOOL_ROUNDS = 3
# Longest visitor message accepted: fits a pasted job description (~3-6k characters), blocks abuse
MAX_MESSAGE_LENGTH = 8000

# Created on first use and reused while the Lambda container stays warm
_llm_client: OpenAI | None = None

# Created on first use (only on Lambda, where conversations are stored in S3)
_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


# Request/Response models
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    # Only ever a UUID the server generated: anything else (e.g. "../x") never reaches a file path or S3 key
    session_id: UUID | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str


class Message(BaseModel):
    role: str
    content: str
    timestamp: str


# Memory management functions
def get_memory_path(session_id: str) -> str:
    return f"{session_id}.json"


def load_conversation(session_id: str) -> list[dict]:
    """Load conversation history from storage"""
    settings = get_settings()
    if settings.use_s3:
        try:
            response = _s3().get_object(Bucket=settings.s3_bucket, Key=get_memory_path(session_id))
            return json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return []
            raise
    else:
        # Local file storage
        file_path = os.path.join(settings.memory_dir, get_memory_path(session_id))
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                return json.load(f)
        return []


def save_conversation(session_id: str, messages: list[dict]):
    """Save conversation history to storage"""
    settings = get_settings()
    if settings.use_s3:
        _s3().put_object(
            Bucket=settings.s3_bucket,
            Key=get_memory_path(session_id),
            Body=json.dumps(messages, indent=2),
            ContentType="application/json",
        )
    else:
        # Local file storage
        os.makedirs(settings.memory_dir, exist_ok=True)
        file_path = os.path.join(settings.memory_dir, get_memory_path(session_id))
        with open(file_path, "w") as f:
            json.dump(messages, f, indent=2)


def get_llm_client() -> OpenAI:
    """Create the OpenRouter client on first use and reuse it afterwards"""
    global _llm_client
    if _llm_client is None:
        api_key = get_secret("openrouter_api_key")
        if not api_key:
            print("OpenRouter API key not configured: set OPENROUTER_API_KEY or OPENROUTER_API_KEY_PARAM")
            raise HTTPException(status_code=500, detail="AI service not configured")
        _llm_client = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            timeout=25,  # API Gateway gives up after ~30 seconds
            max_retries=1,
        )
    return _llm_client


def complete(messages: list[dict], tool_choice: str = "auto"):
    """One OpenRouter call, with errors mapped to safe HTTP responses"""
    settings = get_settings()
    try:
        # GPT-5 mini is a reasoning model: it doesn't accept temperature/top_p
        return get_llm_client().chat.completions.create(
            model=settings.openrouter_model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=4000,
            extra_body={"reasoning": {"effort": settings.openrouter_reasoning_effort}},
        )
    except APITimeoutError as e:
        print(f"OpenRouter timeout: {e}")
        raise HTTPException(status_code=504, detail="The AI took too long to respond, please try again") from e
    except APIConnectionError as e:
        print(f"OpenRouter connection error: {e}")
        raise HTTPException(status_code=504, detail="Could not reach the AI service, please try again") from e
    except APIStatusError as e:
        print(f"OpenRouter error {e.status_code}: {e}")
        if e.status_code == 401:
            raise HTTPException(status_code=500, detail="AI service authentication failed") from e
        if e.status_code == 402:
            raise HTTPException(status_code=503, detail="AI credits exhausted or key limit reached") from e
        if e.status_code == 429:
            raise HTTPException(status_code=429, detail="Too many requests, please try again shortly") from e
        raise HTTPException(status_code=502, detail="AI service error") from e


def call_llm(conversation: list[dict], user_message: str, session_id: str) -> str:
    """Call the LLM through OpenRouter with conversation history, running any tools it asks for"""

    # System prompt first
    messages = [{"role": "system", "content": prompt()}]

    # Add conversation history (limit to last 25 exchanges)
    for msg in conversation[-50:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # Add current user message
    messages.append({"role": "user", "content": user_message})

    response = complete(messages)
    rounds = 0
    while response.choices[0].finish_reason == "tool_calls" and response.choices[0].message.tool_calls:
        rounds += 1
        assistant_message = response.choices[0].message
        messages.append(assistant_message.model_dump(exclude_none=True))
        messages.extend(handle_tool_calls(assistant_message.tool_calls, session_id))
        # After MAX_TOOL_ROUNDS the model has to answer without calling more tools
        response = complete(messages, tool_choice="none" if rounds >= MAX_TOOL_ROUNDS else "auto")

    content = response.choices[0].message.content
    if not content:
        # e.g. the whole token budget was spent on reasoning
        print(f"OpenRouter returned an empty response: finish_reason={response.choices[0].finish_reason}")
        raise HTTPException(status_code=502, detail="The AI returned an empty response, please try again")
    return content


@app.get("/")
async def root():
    return {
        "message": "AI Digital Twin API (Powered by OpenRouter)",
        "memory_enabled": True,
        "storage": "S3" if get_settings().use_s3 else "local",
        "ai_model": get_settings().openrouter_model,
    }


@app.get("/health")
async def health_check():
    settings = get_settings()
    return {"status": "healthy", "use_s3": settings.use_s3, "ai_model": settings.openrouter_model}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        # Generate session ID if not provided
        session_id = str(request.session_id or uuid.uuid4())

        # Load conversation history
        conversation = load_conversation(session_id)

        # Call the LLM for response
        assistant_response = call_llm(conversation, request.message, session_id)

        # Update conversation history
        conversation.append({"role": "user", "content": request.message, "timestamp": datetime.now(UTC).isoformat()})
        conversation.append(
            {
                "role": "assistant",
                "content": assistant_response,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

        # Save conversation
        save_conversation(session_id, conversation)

        return ChatResponse(response=assistant_response, session_id=session_id)

    except HTTPException:
        raise
    except Exception as e:
        # Full details go to the logs (CloudWatch); the visitor only gets a generic message
        print("Error in chat endpoint:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Something went wrong, please try again") from e


@app.post("/telegram-webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Ahmed replies on Telegram to an unanswered question -> the twin learns the answer"""
    # Telegram sends back the secret we registered with setWebhook - reject everyone else
    expected_secret = get_secret("telegram_webhook_secret")
    if not expected_secret or not hmac.compare_digest(
        (x_telegram_bot_api_secret_token or "").encode(), expected_secret.encode()
    ):
        print("Telegram webhook rejected: missing or wrong secret token")
        raise HTTPException(status_code=403, detail="Forbidden")

    # From here on always return 200, otherwise Telegram keeps retrying the same update
    try:
        update = await request.json()
        message = update.get("message") or {}

        # Only Ahmed's own chat can teach the twin
        if str(message.get("chat", {}).get("id")) != str(get_telegram_chat_id()):
            print("Telegram webhook: message from another chat ignored")
            return {"ok": True}

        reply_to = message.get("reply_to_message") or {}
        answer = (message.get("text") or "").strip()
        if not reply_to.get("message_id") or not answer:
            print("Telegram webhook: not a text reply, ignored")
            return {"ok": True}

        pending = pop_pending(reply_to["message_id"])
        if pending is None:
            send_telegram(
                "⚠️ I couldn't match this to a pending question. Reply directly to a 🚨 message.",
                reply_to_message_id=message.get("message_id"),
            )
            return {"ok": True}

        add_qa(pending["question"], answer)
        send_telegram("✅ Saved. Your twin now knows this.", reply_to_message_id=message.get("message_id"))
        print(f"Telegram webhook: saved answer for: {pending['question'][:60]}")
    except Exception:  # noqa: BLE001 - webhook must always answer 200, or Telegram retries the update
        print("Error handling Telegram webhook:")
        traceback.print_exc()

    return {"ok": True}


@app.get("/conversation/{session_id}")
async def get_conversation(session_id: str):
    """Retrieve conversation history"""
    try:
        conversation = load_conversation(session_id)
        return {"session_id": session_id, "messages": conversation}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104 - local dev only; Lambda never runs this block
