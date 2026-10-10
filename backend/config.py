"""All backend configuration, in one typed and validated place.

Where values come from, highest priority first:
  1. arguments passed to Settings(...)   (tests use this)
  2. environment variables               (Terraform sets these on Lambda)
  3. the repo's .env file                (local development)
  4. the defaults below
Variable names are the field names in upper case, e.g. use_s3 <- USE_S3.
"""

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).parent
REPO_DIR = BACKEND_DIR.parent


def _split_commas(value: Any) -> Any:
    """CORS_ORIGINS is written as "https://a.com,https://b.com" in the environment."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(_split_commas)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env also holds deploy-only variables (AWS_ACCOUNT_ID, PROJECT_NAME...)
        validate_assignment=True,  # settings.x = value is validated too (tests do this)
    )

    # OpenRouter
    openrouter_model: str = "openai/gpt-5-mini"
    openrouter_reasoning_effort: Literal["minimal", "low", "medium", "high"] = "medium"  # same as production

    # Browser origins allowed to call the API
    cors_origins: CommaSeparated = ["http://localhost:3000"]

    # Conversation memory and notification counters: local files, or S3 on Lambda
    use_s3: bool = False
    s3_bucket: str = ""
    memory_dir: Path = REPO_DIR / "memory"

    # Learned Q&A: local files, or the knowledge bucket on Lambda
    knowledge_bucket: str = ""
    knowledge_dir: Path = REPO_DIR / "knowledge"

    # Secrets: locally the value itself (.env); on Lambda only the SSM parameter NAME (*_param)
    openrouter_api_key: SecretStr | None = None
    openrouter_api_key_param: str = ""
    telegram_bot_token: SecretStr | None = None
    telegram_bot_token_param: str = ""
    telegram_chat_id: SecretStr | None = None
    telegram_chat_id_param: str = ""
    telegram_webhook_secret: SecretStr | None = None
    telegram_webhook_secret_param: str = ""

    @model_validator(mode="after")
    def _s3_needs_a_bucket(self) -> "Settings":
        if self.use_s3 and not self.s3_bucket:
            raise ValueError("USE_S3=true requires S3_BUCKET to be set")
        return self


_settings: Settings | None = None


def get_settings() -> Settings:
    """The settings, read and validated once on first use. Tests replace config._settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
