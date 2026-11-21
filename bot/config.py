from __future__ import annotations

import json
import re
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_TELEGRAM_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    BOT_TOKEN: str = Field(..., min_length=10, description="Telegram Bot API token")
    DATABASE_URL: str = Field(
        ...,
        min_length=10,
        description="Cloud PostgreSQL connection URL (postgresql+asyncpg://...)",
    )
    ADMIN_IDS: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        description="List of platform super-admin Telegram user IDs",
    )
    WEBHOOK_URL: str = Field(
        ...,
        min_length=8,
        description="Public HTTPS base URL of the Vercel deployment",
    )
    WEBHOOK_SECRET: str = Field(
        ...,
        min_length=8,
        max_length=256,
        description="Secret token for Telegram X-Telegram-Bot-Api-Secret-Token verification",
    )
    CRON_SECRET: str = Field(
        ...,
        min_length=8,
        description="Bearer token sent by Vercel Cron in Authorization header",
    )

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: Any) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, int):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [int(item) for item in value]
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError("ADMIN_IDS JSON value must be a list of integers")
                return [int(item) for item in parsed]
            return [int(part.strip()) for part in raw.split(",") if part.strip()]
        raise ValueError(f"Unsupported ADMIN_IDS format: {type(value)!r}")

    @field_validator("WEBHOOK_URL")
    @classmethod
    def validate_webhook_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith("https://"):
            raise ValueError("WEBHOOK_URL must start with https:// (required by Telegram Bot API)")
        return cleaned

    @field_validator("WEBHOOK_SECRET")
    @classmethod
    def validate_webhook_secret(cls, value: str) -> str:
        cleaned = value.strip()
        if not _TELEGRAM_SECRET_RE.fullmatch(cleaned):
            raise ValueError(
                "WEBHOOK_SECRET must contain only A-Z, a-z, 0-9, '_' and '-' (1-256 chars)"
            )
        return cleaned

    @property
    def webhook_endpoint(self) -> str:
        """Full Telegram webhook URL pointing to /api/webhook."""
        if self.WEBHOOK_URL.endswith("/api/webhook"):
            return self.WEBHOOK_URL
        return f"{self.WEBHOOK_URL}/api/webhook"


def load_settings() -> Settings:
    """Create and validate Settings instance."""
    return Settings()
