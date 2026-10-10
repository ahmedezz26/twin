import boto3
from botocore.exceptions import ClientError

from config import get_settings

# Values read from SSM, reused while the Lambda container stays warm
_cache: dict[str, str] = {}


def get_secret(name: str) -> str | None:
    """Get a secret by its settings name, e.g. get_secret("telegram_bot_token").

    Locally the value itself is set (TELEGRAM_BOT_TOKEN in .env). On Lambda only the
    SSM parameter NAME is set (TELEGRAM_BOT_TOKEN_PARAM), so the secret value never
    appears in Terraform state or the Lambda configuration.
    """
    settings = get_settings()
    value = getattr(settings, name)
    if value is not None and value.get_secret_value():
        return value.get_secret_value()

    if name in _cache:
        return _cache[name]

    param_name = getattr(settings, f"{name}_param")
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
