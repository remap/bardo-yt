import json
import time
from pathlib import Path

import pytest

from ytmatrix import cache

PARAMS = {"q": "golden cover", "order": "relevance", "maxResults": "50"}
ITEMS = [{"video_id": "abc123", "title": "A", "channel": "C"}]


def test_key_is_stable_across_parameter_ordering():
    reordered = {k: PARAMS[k] for k in reversed(list(PARAMS))}
    assert cache.cache_key(PARAMS) == cache.cache_key(reordered)


def test_key_changes_when_a_value_changes():
    assert cache.cache_key(PARAMS) != cache.cache_key({**PARAMS, "q": "silver cover"})


def test_read_returns_none_on_a_miss(tmp_path):
    assert cache.read(tmp_path, PARAMS, ttl_hours=24) is None


def test_write_then_read_round_trips(tmp_path):
    cache.write(tmp_path, PARAMS, ITEMS)
    assert cache.read(tmp_path, PARAMS, ttl_hours=24) == ITEMS


def test_read_returns_none_once_the_entry_has_expired(tmp_path):
    cache.write(tmp_path, PARAMS, ITEMS)
    path = tmp_path / f"{cache.cache_key(PARAMS)}.json"
    payload = json.loads(path.read_text())
    payload["fetched_at"] = time.time() - 7200  # two hours ago
    path.write_text(json.dumps(payload))
    assert cache.read(tmp_path, PARAMS, ttl_hours=1) is None


def test_allow_stale_returns_an_expired_entry(tmp_path):
    cache.write(tmp_path, PARAMS, ITEMS)
    path = tmp_path / f"{cache.cache_key(PARAMS)}.json"
    payload = json.loads(path.read_text())
    payload["fetched_at"] = time.time() - 7200
    path.write_text(json.dumps(payload))
    assert cache.read(tmp_path, PARAMS, ttl_hours=1, allow_stale=True) == ITEMS


def test_read_returns_none_on_a_corrupt_file_rather_than_raising(tmp_path):
    cache.write(tmp_path, PARAMS, ITEMS)
    (tmp_path / f"{cache.cache_key(PARAMS)}.json").write_text("{not json")
    assert cache.read(tmp_path, PARAMS, ttl_hours=24) is None


def test_write_creates_the_cache_directory(tmp_path):
    target = tmp_path / "nested" / "cache"
    cache.write(target, PARAMS, ITEMS)
    assert cache.read(target, PARAMS, ttl_hours=24) == ITEMS


def test_write_leaves_no_temp_file_behind(tmp_path):
    cache.write(tmp_path, PARAMS, ITEMS)
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_crash_during_write_leaves_the_previous_entry_intact(tmp_path, monkeypatch):
    cache.write(tmp_path, PARAMS, ITEMS)

    def explode(self, target):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(Path, "replace", explode)
    with pytest.raises(OSError):
        cache.write(tmp_path, PARAMS, [{"video_id": "new", "title": "N", "channel": "C"}])

    assert cache.read(tmp_path, PARAMS, ttl_hours=24) == ITEMS


def test_the_stored_payload_records_the_params_it_was_fetched_for(tmp_path):
    cache.write(tmp_path, PARAMS, ITEMS)
    payload = json.loads((tmp_path / f"{cache.cache_key(PARAMS)}.json").read_text())
    assert payload["params"] == PARAMS
    assert "fetched_at" in payload
