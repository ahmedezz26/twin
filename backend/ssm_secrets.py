from typing import Literal

import boto3
from botocore.exceptions import ClientError
from pydantic import SecretStr

from config import get_settings

# Values read from SSM, reused while the Lambda container stays warm
_cache: dict[str, str] = {}

# The secrets the app knows: a typo like get_secret("telegram_bot_tokn") is a mypy error, not a runtime None
SecretName = Literal["openrouter_api_key", "telegram_bot_token", "telegram_chat_id", "telegram_webhook_secret"]


def get_secret(name: SecretName) -> str | None:
    """Get a secret by its settings name, e.g. get_secret("telegram_bot_token").

    Locally the value itself is set (TELEGRAM_BOT_TOKEN in .env). On Lambda only the
    SSM parameter NAME is set (TELEGRAM_BOT_TOKEN_PARAM), so the secret value never
    appears in Terraform state or the Lambda configuration.
    """
    settings = get_settings()
    value: SecretStr | None = getattr(settings, name)
    if value is not None and value.get_secret_value():
        return value.get_secret_value()

    if name in _cache:
        return _cache[name]

    param_name: str = getattr(settings, f"{name}_param")
    if not param_name:
        return None

    try:
        response = boto3.client("ssm").get_parameter(Name=param_name, WithDecryption=True)
    except ClientError as e:
        # Log only the error code - never the parameter value
        print(f"Could not read SSM parameter {param_name}: {e.response['Error']['Code']}")
        return None

    _cache[name] = response["Parameter"]["Value"]
    return _cache[name]
