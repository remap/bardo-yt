import pytest

from ytmatrix import cache
from ytmatrix.store import FileStore


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


def test_key_is_stable_across_parameter_ordering():
    params = {"q": "golden cover", "order": "relevance", "maxResults": "50"}
    reordered = {k: params[k] for k in reversed(list(params))}
    assert cache.cache_key(params) == cache.cache_key(reordered)


def test_key_changes_when_a_value_changes():
    params = {"q": "golden cover"}
    assert cache.cache_key(params) != cache.cache_key({**params, "q": "silver cover"})


def test_key_has_no_special_case_for_an_api_key_shaped_field():
    """Guards the global constraint that the search cache key must exclude
    the API key -- it does not affect results, and hashing it would put a
    secret into a storage key.

    `cache_key` cannot enforce that itself: it has no way to know which field
    (if any) holds a secret, so exclusion is entirely the caller's job.
    `search_params_for`/`youtube.build_params` are the enforcement point and
    never include one (see
    test_youtube.py::test_build_params_never_includes_the_api_key). What this
    test documents instead is the property `cache_key` actually guarantees:
    it hashes exactly what it is given, with no stripping or special-casing
    of any field name, so if a caller ever slipped a key in by mistake, two
    different key values would land in two different cache entries rather
    than being silently conflated into one.
    """
    without_key = {"q": "a"}
    with_key = {"q": "a", "key": "AIzaSecretValue"}
    assert cache.cache_key(without_key) != cache.cache_key(with_key)


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
