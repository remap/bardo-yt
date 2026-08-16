import pytest

from ytmatrix import cache
from ytmatrix.store import FileStore


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


async def test_read_miss_is_none(store):
    assert await cache.read(store, {"q": "a"}, 24) is None


async def test_write_then_read_roundtrips(store):
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    assert await cache.read(store, {"q": "a"}, 24) == [{"video_id": "x"}]


async def test_different_params_are_different_entries(store):
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    assert await cache.read(store, {"q": "b"}, 24) is None


async def test_expired_entry_reads_as_a_miss(store, monkeypatch):
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    monkeypatch.setattr(cache.time, "time", lambda: 1e12)
    assert await cache.read(store, {"q": "a"}, 24) is None


async def test_expired_entry_is_still_available_as_stale(store, monkeypatch):
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    monkeypatch.setattr(cache.time, "time", lambda: 1e12)
    assert await cache.read(store, {"q": "a"}, 24, allow_stale=True) == [{"video_id": "x"}]


async def test_corrupt_entry_is_a_miss_not_a_crash(store):
    await store.put(f"search/{cache.cache_key({'q': 'a'})}.json", b"not json")
    assert await cache.read(store, {"q": "a"}, 24) is None


async def test_entries_live_under_the_shared_search_prefix(store):
    """The search cache is shared by every user -- it must not be per-user."""
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    assert await store.list_keys("search/") == [f"search/{cache.cache_key({'q': 'a'})}.json"]
