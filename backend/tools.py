"""Tools the twin can call: notify Ahmed on Telegram about new contacts and unanswered questions."""
import json
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from knowledge import save_pending
from ssm_secrets import get_secret

# Protects Ahmed's Telegram from being spammed through the twin
MAX_NOTIFICATIONS_PER_SESSION = 3
# Visitor-supplied text is cut to this length before it is sent
MAX_FIELD_LENGTH = 500

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _truncate(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_FIELD_LENGTH else text[:MAX_FIELD_LENGTH] + "…"


def get_telegram_chat_id() -> Optional[str]:
    return get_secret("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID_PARAM")


def send_telegram(text: str, reply_to_message_id: Optional[int] = None) -> Optional[int]:
    """Send a plain-text Telegram message to Ahmed. Returns the message id, or None on failure"""
    token = get_secret("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN_PARAM")
    chat_id = get_telegram_chat_id()
    if not token or not chat_id:
        print("Telegram not configured: missing bot token or chat id")
        return None

    payload = {"chat_id": chat_id, "text": text}  # no parse_mode, so visitor text can't inject formatting
    if reply_to_message_id:
        # Still send if the original message was deleted
        payload["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        # Telegram explains errors in the response body (never log the URL - it contains the token)
        try:
            data = json.loads(e.read())
        except ValueError:
            print(f"Telegram request failed: HTTP {e.code}")
            return None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"Telegram request failed: {type(e).__name__}")
        return None

    if not data.get("ok"):
        print(f"Telegram error: {data.get('description')}")
        return None
    return data["result"]["message_id"]


# Per-session notification counter, stored next to the conversation memory
def _limit_key(session_id: str) -> str:
    return f"limits/{session_id}.json"


def _get_notification_count(session_id: str) -> int:
    if os.getenv("USE_S3", "false").lower() == "true":
        try:
            response = _s3().get_object(Bucket=os.getenv("S3_BUCKET", ""), Key=_limit_key(session_id))
            return json.loads(response["Body"].read())["count"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return 0
            raise

    path = os.path.join(os.getenv("MEMORY_DIR", "../memory"), _limit_key(session_id))
    if not os.path.exists(path):
        return 0
    with open(path, "r") as f:
        return json.load(f)["count"]


def _save_notification_count(session_id: str, count: int):
    body = json.dumps({"count": count})
    if os.getenv("USE_S3", "false").lower() == "true":
        _s3().put_object(
            Bucket=os.getenv("S3_BUCKET", ""), Key=_limit_key(session_id), Body=body, ContentType="application/json"
        )
    else:
        path = os.path.join(os.getenv("MEMORY_DIR", "../memory"), _limit_key(session_id))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(body)


def _notify(session_id: str, text: str) -> Optional[int]:
    """Send a notification unless this conversation already used its quota"""
    count = _get_notification_count(session_id)
    if count >= MAX_NOTIFICATIONS_PER_SESSION:
        print(f"Notification limit reached for session {session_id}")
        return None
    message_id = send_telegram(text)
    if message_id is not None:
        _save_notification_count(session_id, count + 1)
    return message_id


def record_user_details(session_id: str, email: str, name: str = "Name not provided", notes: str = "not provided"):
    email = (email or "").strip()
    if not EMAIL_PATTERN.match(email) or len(email) > 254:
        return {"status": "error", "message": "That email address doesn't look valid. Ask the user to check it."}

    if _get_notification_count(session_id) >= MAX_NOTIFICATIONS_PER_SESSION:
        return {"status": "error", "message": "Limit reached. Tell the user their details are noted and Ahmed will be in touch."}

    text = (
        "✍️ New contact from your digital twin\n\n"
        f"Name: {_truncate(name)}\n"
        f"Email: {email}\n"
        f"Notes: {_truncate(notes)}"
    )
    if _notify(session_id, text) is None:
        return {"status": "error", "message": "Could not record the details right now."}
    return {"status": "success"}


def record_unknown_question(session_id: str, question: str):
    question = _truncate(question)
    if not question:
        return {"status": "error", "message": "No question provided."}

    if _get_notification_count(session_id) >= MAX_NOTIFICATIONS_PER_SESSION:
        return {"status": "error", "message": "Limit reached. Do not record more questions in this conversation."}

    text = (
        "🚨 Your twin couldn't answer this question:\n\n"
        f"\"{question}\"\n\n"
        "Reply to this message with the answer and your twin will learn it."
    )
    message_id = _notify(session_id, text)
    if message_id is None:
        return {"status": "error", "message": "Could not record the question right now."}

    save_pending(message_id, question, session_id)
    return {"status": "success"}


record_user_details_json = {
    "name": "record_user_details",
    "description": "Use this tool to record that a user is interested in being in touch and provided an email address",
    "parameters": {
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "The email address of this user"},
            "name": {"type": "string", "description": "The user's name, if they provided it"},
            "notes": {
                "type": "string",
                "description": "Any additional info about the conversation that's worth recording to give context",
            },
        },
        "required": ["email"],
        "additionalProperties": False,
    },
}

record_unknown_question_json = {
    "name": "record_unknown_question",
    "description": "Always use this tool to record ONLY professional life/career questions about the person that you couldn't answer because you didn't know the answer",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question that couldn't be answered"},
        },
        "required": ["question"],
        "additionalProperties": False,
    },
}

tools = [
    {"type": "function", "function": record_user_details_json},
    {"type": "function", "function": record_unknown_question_json},
]

tool_map = {
    "record_user_details": record_user_details,
    "record_unknown_question": record_unknown_question,
}


def handle_tool_calls(tool_calls, session_id: str) -> List[Dict]:
    """Run the tools the model asked for and return their results as tool messages"""
    results = []
    for tool_call in tool_calls:
        tool_name = tool_call.function.name
        print(f"Tool called: {tool_name}")
        tool = tool_map.get(tool_name)
        try:
            arguments = json.loads(tool_call.function.arguments or "{}")
            result = tool(session_id=session_id, **arguments) if tool else {"status": "error", "message": f"Unknown tool: {tool_name}"}
        except (TypeError, ValueError) as e:
            result = {"status": "error", "message": f"Invalid arguments: {type(e).__name__}"}
        results.append({"role": "tool", "content": json.dumps(result), "tool_call_id": tool_call.id})
    return results
