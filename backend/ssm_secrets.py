import os
from typing import Dict, Optional

import boto3
from botocore.exceptions import ClientError

# Values read from SSM, reused while the Lambda container stays warm
_cache: Dict[str, str] = {}


def get_secret(local_env_name: str, param_env_name: str) -> Optional[str]:
    """Get a secret from the local environment (.env), or from SSM Parameter Store on Lambda.

    On Lambda only the parameter NAME is in the environment (param_env_name),
    so the secret value never appears in Terraform state or the Lambda configuration.
    """
    value = os.getenv(local_env_name)
    if value:
        return value

    if local_env_name in _cache:
        return _cache[local_env_name]

    param_name = os.getenv(param_env_name, "")
    if not param_name:
        return None

    try:
        response = boto3.client("ssm").get_parameter(Name=param_name, WithDecryption=True)
    except ClientError as e:
        # Log only the error code - never the parameter value
        print(f"Could not read SSM parameter {param_name}: {e.response['Error']['Code']}")
        return None

    _cache[local_env_name] = response["Parameter"]["Value"]
    return _cache[local_env_name]
