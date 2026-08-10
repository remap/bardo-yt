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


class VideoLicense(StrEnum):
    ANY = "any"
    # Creative Commons uploads are overwhelmingly unmonetised, so they carry
    # far fewer ads. The pool is much smaller, though -- for most music
    # searches, restrictively so.
    CREATIVE_COMMON = "creativeCommon"


class SearchConfig(Strict):
    order: Order = Order.RELEVANCE
    # `short` (<4 min) is the ad-conscious default: YouTube only permits
    # mid-roll breaks on videos of 8 minutes or more, so short videos can be
    # interrupted at most once, at the start. It cannot prevent pre-rolls.
    video_duration: VideoDuration = VideoDuration.SHORT
    safe_search: SafeSearch = SafeSearch.MODERATE
    relevance_language: str | None = None
    video_license: VideoLicense = VideoLicense.ANY


class PlaybackConfig(Strict):
    # The state the wall STARTS in, not a permanent one: the page has a
    # mute/unmute-all button that overrides this at runtime. Defaults to muted
    # because browsers only permit autoplay when muted -- start unmuted and an
    # arbitrary subset of players silently refuses to begin.
    muted: bool = True
    # Seeds the wall's "follow play state" checkbox. Off by default: a new
    # query lands paused and pre-rolled, waiting for a human, rather than
    # bursting into eight fresh videos on its own.
    autoplay_on_change: bool = False
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
    theme: str = (
        "K-pop covers: other people performing K-pop songs -- dance covers, "
        "dance practice, busking, street and stage performances, acoustic and "
        "band reinterpretations. Always covers, never the official artist upload."
    )
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
    # Spread the wall across countries of origin where the metadata allows.
    # search.list carries no country field; recovering it costs 2 quota units
    # per new query (videos.list + channels.list, both batched 50 at a time)
    # against the 100 the search itself costs. Coverage is partial -- roughly
    # 60% of channels publish a country -- so this reorders, it never drops.
    prefer_country_diversity: bool = True
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


def merge_config(current: dict, payload: dict) -> dict:
    """Overlay an edit onto the existing config, section by section.

    Every section has model defaults, so validating a partial payload on its
    own does not fail -- it silently *resets* whatever the sender omitted. A
    config page that only knows about query/grid/search/playback therefore
    wiped query_generation, filtering and quota back to defaults on every
    save, which is how query generation ended up switched off by pressing
    Save. Merging first makes an omitted section mean "leave it alone", which
    is what every caller actually intends.
    """
    merged = dict(current)
    for key, value in payload.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            merged[key] = {**current[key], **value}
        else:
            merged[key] = value
    return merged


def load_config(path: Path) -> Config:
    return Config.model_validate(yaml.safe_load(path.read_text()) or {})


def save_config(config: Config, path: Path) -> None:
    payload = config.model_dump(mode="json")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    tmp.replace(path)
