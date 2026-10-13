"""Runtime configuration, loaded from HOOKDESK_* environment variables or .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LEN = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="HOOKDESK_", extra="ignore")

    webhook_secret: SecretStr
    admin_token: SecretStr
    db_path: Path = Path("hookdesk.db")
    forward_url: HttpUrl | None = None
    forward_timeout_s: float = Field(10.0, gt=0, le=120)
    max_attempts: int = Field(5, ge=1, le=20)
    backoff_base_s: float = Field(2.0, gt=0)
    backoff_cap_s: float = Field(300.0, gt=0)
    signature_tolerance_s: int = Field(300, ge=1)
    max_body_bytes: int = Field(1_000_000, ge=1)
    poll_interval_s: float = Field(1.0, gt=0)
    batch_size: int = Field(10, ge=1, le=100)

    @field_validator("webhook_secret", "admin_token")
    @classmethod
    def _strong_enough(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < MIN_SECRET_LEN:
            raise ValueError(f"must be at least {MIN_SECRET_LEN} characters")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
