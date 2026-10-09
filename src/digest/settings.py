"""Process settings from the environment and `.env` (`DIGEST_` prefix, see `.env.example`)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: a blank `KEY=` line in .env reads as None, not "".
    model_config = SettingsConfigDict(
        env_prefix="DIGEST_", env_file=".env", extra="ignore", env_ignore_empty=True
    )

    mode: Literal["replay", "record", "live"] = "replay"
    backend: Literal["anthropic_api", "openrouter"] = "openrouter"
    world: str = "kestrel"
    db: Path | None = None
    cassettes: Path = Path("data/cassettes")
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openrouter_api_key: str | None = Field(default=None, validation_alias="OPENROUTER_API_KEY")
