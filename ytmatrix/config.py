from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# One search.list call returns at most 50 items, and costs the same 100 quota
# units whether you ask for 1 or 50. See spec section 4.
MAX_SEARCH_RESULTS = 50


class Order(StrEnum):
    RELEVANCE = "relevance"
    DATE = "date"
    RATING = "rating"
    VIEW_COUNT = "viewCount"
    TITLE = "title"


class VideoDuration(StrEnum):
    ANY = "any"
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class SafeSearch(StrEnum):
    NONE = "none"
    MODERATE = "moderate"
    STRICT = "strict"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Grid(Strict):
    cols: int = Field(ge=1)
    rows: int = Field(ge=1)

    @property
    def cells(self) -> int:
        return self.cols * self.rows


class SearchConfig(Strict):
    order: Order = Order.RELEVANCE
    video_duration: VideoDuration = VideoDuration.ANY
    safe_search: SafeSearch = SafeSearch.MODERATE
    relevance_language: str | None = None


class PlaybackConfig(Strict):
    muted: bool = True
    autoplay_on_change: bool = True
    start_offset: int = Field(default=0, ge=0)
    loop: bool = True

    @field_validator("muted")
    @classmethod
    def _forced_muted(cls, value: bool) -> bool:
        # Unmuting is deliberately deferred: it needs the OS loopback audio path
        # (spec section 1, non-goals). The field exists so it can be relaxed later.
        if not value:
            raise ValueError("playback.muted must be true in this version")
        return value


class CacheConfig(Strict):
    ttl_hours: float = Field(default=24.0, gt=0)


class Config(Strict):
    query: str
    grid: Grid
    search: SearchConfig = SearchConfig()
    playback: PlaybackConfig = PlaybackConfig()
    cache: CacheConfig = CacheConfig()

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be empty")
        return stripped

    @model_validator(mode="after")
    def _grid_fits_one_search(self) -> Config:
        if self.grid.cells > MAX_SEARCH_RESULTS:
            raise ValueError(
                f"grid has {self.grid.cells} cells but one search returns "
                f"at most {MAX_SEARCH_RESULTS}"
            )
        return self


def load_config(path: Path) -> Config:
    return Config.model_validate(yaml.safe_load(path.read_text()) or {})


def save_config(config: Config, path: Path) -> None:
    payload = config.model_dump(mode="json")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    tmp.replace(path)
