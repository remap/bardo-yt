from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. Secrets live here; never in config.yaml."""

    model_config = SettingsConfigDict(
        env_file=".env",
        # Without a prefix, `host`/`port` would be read from the ambient HOST
        # and PORT, which plenty of shells already set.
        env_prefix="YTMATRIX_",
        # The alias below bypasses env_prefix, so field-name construction in
        # tests needs this explicitly.
        populate_by_name=True,
        extra="ignore",
    )

    # No default: a missing key must fail at startup, not at first search.
    # Aliased so the env var stays the conventional YOUTUBE_API_KEY rather
    # than YTMATRIX_YOUTUBE_API_KEY.
    youtube_api_key: str = Field(validation_alias="YOUTUBE_API_KEY")

    # Optional: only needed when query_generation.enabled is true. Absent is a
    # legitimate configuration, so this must not fail startup the way a missing
    # YouTube key does.
    gemini_api_key: str | None = Field(default=None, validation_alias="GEMINI_API_KEY")

    host: str = "0.0.0.0"
    port: int = 8444
