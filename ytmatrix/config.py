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
    # The state the wall STARTS in, not a permanent one: the page has a
    # mute/unmute-all button that overrides this at runtime. Defaults to muted
    # because browsers only permit autoplay when muted -- start unmuted and an
    # arbitrary subset of players silently refuses to begin.
    muted: bool = True
    autoplay_on_change: bool = True
    start_offset: int = Field(default=0, ge=0)
    loop: bool = True


class CacheConfig(Strict):
    ttl_hours: float = Field(default=24.0, gt=0)


class QueryGenerationConfig(Strict):
    """Gemini invents a fresh query on each page load when enabled.

    Every generated query is a cache miss by construction, so every reload
    costs a 100-unit search. That is the point of the feature and also its
    danger -- see QuotaConfig.
    """

    enabled: bool = False
    theme: str = "cover songs and reinterpretations"
    model: str = "gemini-3.6-flash"
    # How many previously generated queries to show Gemini so it does not
    # circle back to one already paid for.
    avoid_repeats: int = Field(default=20, ge=0)


class FilteringConfig(Strict):
    """Keep still-image uploads off the wall.

    A four-minute static album cover with a soundtrack is a legitimate search
    result and useless on a video wall. The Data API cannot filter for motion,
    so this is measured from the video's own storyboard frames instead.
    """

    skip_static: bool = True
    # Mean frame-to-frame luma difference below which a video counts as a still.
    # Measured stills cluster under 2.5; real footage starts around 5.
    static_threshold: float = Field(default=3.5, ge=0)
    # How many extra candidates to measure beyond the grid size, so there is
    # something to substitute in when the top results turn out to be stills.
    scan_depth: int = Field(default=24, ge=0)


class QuotaConfig(Strict):
    """A self-imposed ceiling below the real 10,000-unit daily allowance.

    Set to 0 to disable the guard entirely and spend freely up to whatever
    Google cuts you off at.
    """

    daily_limit_units: int = Field(default=5000, ge=0)


class Config(Strict):
    query: str
    grid: Grid
    search: SearchConfig = SearchConfig()
    playback: PlaybackConfig = PlaybackConfig()
    cache: CacheConfig = CacheConfig()
    query_generation: QueryGenerationConfig = QueryGenerationConfig()
    filtering: FilteringConfig = FilteringConfig()
    quota: QuotaConfig = QuotaConfig()

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
