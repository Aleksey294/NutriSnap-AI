from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )
    bot_token: SecretStr
    openrouter_api_key: SecretStr
    openrouter_model: str = "openrouter/free"
    allow_paid_models: bool = False
    database_path: Path = Path("data/nutrisnap.db")
    ai_timeout_seconds: float = Field(25, ge=1, le=90)
    ai_daily_request_limit: int = Field(45, ge=1)
    ai_min_interval_seconds: float = Field(4, ge=0)
    max_concurrent_requests: int = Field(2, ge=1, le=4)
    fair_use_daily_limit: int = Field(15, ge=1)
    default_target_kcal: float = Field(2100, gt=0)
    default_target_p: float = Field(140, gt=0)
    default_target_f: float = Field(70, gt=0)
    default_target_c: float = Field(220, gt=0)
    meals_retention_days: int = Field(90, ge=1, le=365)
    allowed_user_ids: str = ""
    off_user_agent: str = "NutriSnap/0.1 (personal Telegram nutrition diary)"

    @model_validator(mode="after")
    def validate_config(self):
        if not self.bot_token.get_secret_value() or not self.openrouter_api_key.get_secret_value():
            raise ValueError("Заполните BOT_TOKEN и OPENROUTER_API_KEY в .env")
        if not self.allow_paid_models and not (
            self.openrouter_model == "openrouter/free" or self.openrouter_model.endswith(":free")
        ):
            raise ValueError("Для платной модели необходимо ALLOW_PAID_MODELS=true")
        _ = self.allowed_ids  # Validate the allowlist at startup.
        return self

    @property
    def allowed_ids(self) -> set[int]:
        return {int(x.strip()) for x in self.allowed_user_ids.split(",") if x.strip()}
