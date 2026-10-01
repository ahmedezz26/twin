from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from typing import Optional, List, Dict
import json
import uuid
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError
from context import prompt

# Load environment variables
load_dotenv()

app = FastAPI()

# Configure CORS
origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# OpenRouter configuration
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-5-mini")
REASONING_EFFORT = os.getenv("OPENROUTER_REASONING_EFFORT", "low")
# On Lambda the API key is read from SSM Parameter Store (only the parameter name is in the environment).
# Locally, OPENROUTER_API_KEY from .env takes priority.
OPENROUTER_API_KEY_PARAM = os.getenv("OPENROUTER_API_KEY_PARAM", "")

# Created on first use and reused while the Lambda container stays warm
_llm_client: Optional[OpenAI] = None

# Memory storage configuration
USE_S3 = os.getenv("USE_S3", "false").lower() == "true"
S3_BUCKET = os.getenv("S3_BUCKET", "")
MEMORY_DIR = os.getenv("MEMORY_DIR", "../memory")

# Initialize S3 client if needed
if USE_S3:
    s3_client = boto3.client("s3")


# Request/Response models
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


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


def load_conversation(session_id: str) -> List[Dict]:
    """Load conversation history from storage"""
    if USE_S3:
        try:
            response = s3_client.get_object(Bucket=S3_BUCKET, Key=get_memory_path(session_id))
            return json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return []
            raise
    else:
        # Local file storage
        file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                return json.load(f)
        return []


def save_conversation(session_id: str, messages: List[Dict]):
    """Save conversation history to storage"""
    if USE_S3:
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=get_memory_path(session_id),
            Body=json.dumps(messages, indent=2),
            ContentType="application/json",
        )
    else:
        # Local file storage
        os.makedirs(MEMORY_DIR, exist_ok=True)
        file_path = os.path.join(MEMORY_DIR, get_memory_path(session_id))
        with open(file_path, "w") as f:
            json.dump(messages, f, indent=2)


def get_openrouter_api_key() -> Optional[str]:
    """Get the OpenRouter API key from the local environment, or from SSM Parameter Store on Lambda"""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if api_key:
        return api_key

    if OPENROUTER_API_KEY_PARAM:
        try:
            ssm_client = boto3.client("ssm")
            response = ssm_client.get_parameter(Name=OPENROUTER_API_KEY_PARAM, WithDecryption=True)
            return response["Parameter"]["Value"]
        except ClientError as e:
            # Log only the error code - never the parameter value
            print(f"Could not read SSM parameter {OPENROUTER_API_KEY_PARAM}: {e.response['Error']['Code']}")
            return None

    return None


def get_llm_client() -> OpenAI:
    """Create the OpenRouter client on first use and reuse it afterwards"""
    global _llm_client
    if _llm_client is None:
        api_key = get_openrouter_api_key()
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


def call_llm(conversation: List[Dict], user_message: str) -> str:
    """Call the LLM through OpenRouter with conversation history"""

    # System prompt first
    messages = [{"role": "system", "content": prompt()}]

    # Add conversation history (limit to last 25 exchanges)
    for msg in conversation[-50:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # Add current user message
    messages.append({"role": "user", "content": user_message})

    try:
        # GPT-5 mini is a reasoning model: it doesn't accept temperature/top_p
        response = get_llm_client().chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            max_tokens=4000,
            extra_body={"reasoning": {"effort": REASONING_EFFORT}},
        )
    except APITimeoutError as e:
        print(f"OpenRouter timeout: {e}")
        raise HTTPException(status_code=504, detail="The AI took too long to respond, please try again")
    except APIConnectionError as e:
        print(f"OpenRouter connection error: {e}")
        raise HTTPException(status_code=504, detail="Could not reach the AI service, please try again")
    except APIStatusError as e:
        print(f"OpenRouter error {e.status_code}: {e}")
        if e.status_code == 401:
            raise HTTPException(status_code=500, detail="AI service authentication failed")
        if e.status_code == 402:
            raise HTTPException(status_code=503, detail="AI credits exhausted or key limit reached")
        if e.status_code == 429:
            raise HTTPException(status_code=429, detail="Too many requests, please try again shortly")
        raise HTTPException(status_code=502, detail="AI service error")

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
        "storage": "S3" if USE_S3 else "local",
        "ai_model": OPENROUTER_MODEL
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "use_s3": USE_S3,
        "ai_model": OPENROUTER_MODEL
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        # Generate session ID if not provided
        session_id = request.session_id or str(uuid.uuid4())

        # Load conversation history
        conversation = load_conversation(session_id)

        # Call the LLM for response
        assistant_response = call_llm(conversation, request.message)

        # Update conversation history
        conversation.append(
            {"role": "user", "content": request.message, "timestamp": datetime.now().isoformat()}
        )
        conversation.append(
            {
                "role": "assistant",
                "content": assistant_response,
                "timestamp": datetime.now().isoformat(),
            }
        )

        # Save conversation
        save_conversation(session_id, conversation)

        return ChatResponse(response=assistant_response, session_id=session_id)

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in chat endpoint: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conversation/{session_id}")
async def get_conversation(session_id: str):
    """Retrieve conversation history"""
    try:
        conversation = load_conversation(session_id)
        return {"session_id": session_id, "messages": conversation}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
