from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ytmatrix.config import (
    MAX_SEARCH_RESULTS,
    Config,
    load_config,
    merge_config,
    save_config,
)

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


def test_the_committed_config_is_valid():
    """config.yaml is committed but also rewritten at runtime by /config.

    So this asserts it still parses and is coherent, not that it holds any
    particular grid or query -- those are the operator's, and pinning them
    here just breaks the suite every time someone resizes the wall.
    """
    config = load_config(Path("config.yaml"))
    assert config.query
    assert config.grid.cells >= 1
    assert config.grid.cells <= MAX_SEARCH_RESULTS


def test_round_trips_through_yaml(tmp_path):
    path = write_yaml(tmp_path, VALID)
    original = load_config(path)
    save_config(original, path)
    assert load_config(path) == original


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


def test_saved_yaml_contains_no_api_key_field(tmp_path):
    path = tmp_path / "config.yaml"
    save_config(Config.model_validate(VALID), path)
    assert "key" not in path.read_text().lower()


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
