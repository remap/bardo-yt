from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ytmatrix.config import (
    CONFIG_KEY,
    MAX_SEARCH_RESULTS,
    Config,
    load_config,
    merge_config,
    save_config,
)
from ytmatrix.store import FileStore

VALID = {
    "query": "golden cover",
    "grid": {"cols": 4, "rows": 2},
    "search": {
        "order": "relevance",
        "video_duration": "any",
        "safe_search": "moderate",
        "relevance_language": "en",
    },
    "playback": {
        "muted": True,
        "autoplay_on_change": True,
        "start_offset": 0,
        "loop": True,
    },
    "cache": {"ttl_hours": 24},
}


def write_yaml(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def store(tmp_path):
    # A subdirectory of its own, not tmp_path itself: default_path's template
    # file also lives under tmp_path, and list_keys("") must see only what was
    # actually written through the store, not an unrelated sibling file.
    return FileStore(tmp_path / "store")


@pytest.fixture
def default_path(tmp_path):
    # grid is a required field with no default (Config demands one search's
    # worth of cells or fewer) -- a query-only template is not a valid config,
    # so a minimal grid rides along purely to make this template loadable.
    path = tmp_path / "default.yaml"
    path.write_text("query: seeded from the image\ngrid:\n  cols: 1\n  rows: 1\n")
    return path


async def test_the_committed_config_is_valid(store):
    """config.yaml is committed but also rewritten at runtime by /config.

    So this asserts it still parses and is coherent, not that it holds any
    particular grid or query -- those are the operator's, and pinning them
    here just breaks the suite every time someone resizes the wall. The store
    is empty, so this falls back to the real, repo-root DEFAULT_CONFIG_PATH.
    """
    config = await load_config(store)
    assert config.query
    assert config.grid.cells >= 1
    assert config.grid.cells <= MAX_SEARCH_RESULTS


async def test_round_trips_through_yaml(store, tmp_path):
    # Named differently from CONFIG_KEY ("config.yaml") on purpose: this file
    # shares tmp_path with `store`, and a same-named template would collide
    # with the key the store itself writes.
    default_path = tmp_path / "default.yaml"
    default_path.write_text(yaml.safe_dump(VALID))
    original = await load_config(store, default_path=default_path)
    await save_config(original, store)
    assert await load_config(store, default_path=default_path) == original


async def test_unsaved_config_falls_back_to_the_bundled_default(store, default_path):
    config = await load_config(store, default_path=default_path)
    assert config.query == "seeded from the image"


async def test_saved_config_is_read_back(store, default_path):
    config = await load_config(store, default_path=default_path)
    await save_config(config.model_copy(update={"query": "ours"}), store)
    assert (await load_config(store, default_path=default_path)).query == "ours"


async def test_a_save_is_visible_to_everyone(store, default_path):
    """Config is shared: there is exactly one key, not one per user."""
    config = await load_config(store, default_path=default_path)
    await save_config(config.model_copy(update={"query": "ours"}), store)
    assert await store.list_keys("") == [CONFIG_KEY]


async def test_saved_config_is_readable_yaml(store, default_path):
    config = await load_config(store, default_path=default_path)
    await save_config(config.model_copy(update={"query": "ours"}), store)
    assert b"query: ours" in await store.get(CONFIG_KEY)


async def test_unicode_survives_a_save(store, default_path):
    config = await load_config(store, default_path=default_path)
    await save_config(config.model_copy(update={"query": "케이팝 커버"}), store)
    assert (await load_config(store, default_path=default_path)).query == "케이팝 커버"


async def test_invalid_yaml_in_the_store_falls_back_to_the_default(store, default_path):
    """A corrupt stored config must not 500 every route for every user.

    Unlike the old per-operator config.yaml, this key backs the whole
    installation -- a crash here takes everyone's wall down at once. The
    fallback is also how the app recovers on its own: the next Save simply
    overwrites the bad value.
    """
    await store.put(CONFIG_KEY, b": not: valid: yaml: [")
    config = await load_config(store, default_path=default_path)
    assert config.query == "seeded from the image"


async def test_a_stored_config_missing_a_required_field_falls_back_to_the_default(
    store, default_path
):
    """Covers schema evolution: a config saved by an older version can fail
    validation against a newer, stricter Config without taking the wall down.
    """
    await store.put(CONFIG_KEY, b"query: no grid here\n")
    config = await load_config(store, default_path=default_path)
    assert config.query == "seeded from the image"


def test_grid_cells_is_the_product():
    assert Config.model_validate(VALID).grid.cells == 8


def test_rejects_a_grid_larger_than_one_search_can_fill():
    data = {**VALID, "grid": {"cols": 10, "rows": 10}}
    with pytest.raises(ValidationError, match="at most 50"):
        Config.model_validate(data)


def test_rejects_a_zero_dimension_grid():
    with pytest.raises(ValidationError):
        Config.model_validate({**VALID, "grid": {"cols": 0, "rows": 2}})


def test_rejects_an_empty_query():
    with pytest.raises(ValidationError, match="must not be empty"):
        Config.model_validate({**VALID, "query": "   "})


def test_strips_surrounding_whitespace_from_the_query():
    assert Config.model_validate({**VALID, "query": "  hello  "}).query == "hello"


def test_starting_unmuted_is_now_a_valid_configuration():
    # muted is the STARTING state, not a permanent one -- the wall has a
    # mute/unmute-all button. It defaults to true only because browsers refuse
    # to autoplay audible video.
    data = {**VALID, "playback": {**VALID["playback"], "muted": False}}
    assert Config.model_validate(data).playback.muted is False


def test_playback_starts_muted_by_default():
    playback = {k: v for k, v in VALID["playback"].items() if k != "muted"}
    assert Config.model_validate({**VALID, "playback": playback}).playback.muted is True


def test_rejects_a_nonpositive_cache_ttl():
    with pytest.raises(ValidationError):
        Config.model_validate({**VALID, "cache": {"ttl_hours": 0}})


def test_rejects_an_unknown_enum_value():
    data = {**VALID, "search": {**VALID["search"], "order": "popularity"}}
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_rejects_unknown_keys_rather_than_silently_ignoring_them():
    with pytest.raises(ValidationError):
        Config.model_validate({**VALID, "colour": "blue"})


def test_relevance_language_is_optional():
    data = {
        **VALID,
        "search": {k: v for k, v in VALID["search"].items() if k != "relevance_language"},
    }
    assert Config.model_validate(data).search.relevance_language is None


def test_a_rejected_edit_leaves_the_file_on_disk_untouched(tmp_path):
    path = write_yaml(tmp_path, VALID)
    before = path.read_text()
    with pytest.raises(ValidationError):
        Config.model_validate({**VALID, "query": ""})
    assert path.read_text() == before


async def test_saved_yaml_contains_no_api_key_field(store):
    await save_config(Config.model_validate(VALID), store)
    assert "key" not in (await store.get(CONFIG_KEY)).decode("utf-8").lower()


# --- merging edits ---------------------------------------------------------
#
# Every section has defaults, so validating a partial payload does not fail --
# it RESETS whatever was omitted. That is how pressing Save on the config page
# silently switched query generation off.


def test_merge_keeps_sections_the_edit_never_mentions():
    current = {**VALID, "query_generation": {"enabled": True, "theme": "kpop"}}
    merged = merge_config(current, {"query": "new"})
    assert merged["query_generation"] == {"enabled": True, "theme": "kpop"}
    assert merged["query"] == "new"


def test_merge_keeps_untouched_keys_within_a_section():
    current = {**VALID, "search": {"order": "date", "safe_search": "strict"}}
    merged = merge_config(current, {"search": {"order": "relevance"}})
    assert merged["search"] == {"order": "relevance", "safe_search": "strict"}


def test_merge_lets_an_edit_win():
    merged = merge_config({**VALID, "query": "old"}, {"query": "new"})
    assert merged["query"] == "new"


def test_merge_replaces_a_scalar_that_was_a_dict_before():
    assert merge_config({"a": {"b": 1}}, {"a": 5}) == {"a": 5}


def test_merge_does_not_mutate_its_inputs():
    current = {"search": {"order": "date"}}
    merge_config(current, {"search": {"order": "relevance"}})
    assert current == {"search": {"order": "date"}}


def test_an_empty_edit_changes_nothing():
    assert merge_config(VALID, {}) == VALID


def test_the_config_page_payload_no_longer_disables_generation():
    """The exact shape the config page used to send, replayed."""
    current = {
        **VALID,
        "query_generation": {"enabled": True, "theme": "kpop", "avoid_repeats": 20},
        "filtering": {"skip_static": True, "static_threshold": 3.5},
        "quota": {"daily_limit_units": 5000},
    }
    page_payload = {
        "query": "golden cover",
        "grid": {"cols": 4, "rows": 4},
        "search": VALID["search"],
        "playback": VALID["playback"],
        "cache": {"ttl_hours": 24},
    }
    merged = Config.model_validate(merge_config(current, page_payload))
    assert merged.query_generation.enabled is True, "Save must not switch generation off"
    assert merged.filtering.static_threshold == 3.5
    assert merged.quota.daily_limit_units == 5000
    assert merged.grid.cells == 16, "and the edit itself must still apply"
