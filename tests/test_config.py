from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ytmatrix.config import Config, load_config, save_config

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


def test_loads_the_committed_default_config():
    config = load_config(Path("config.yaml"))
    assert config.query == "golden cover"
    assert config.grid.cells == 8


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


def test_rejects_unmuted_playback_in_this_version():
    data = {**VALID, "playback": {**VALID["playback"], "muted": False}}
    with pytest.raises(ValidationError, match="must be true"):
        Config.model_validate(data)


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
