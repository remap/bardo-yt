# tests/test_store.py
import pytest

from ytmatrix.store import FileStore


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


async def test_get_missing_key_is_none(store):
    assert await store.get("nope.json") is None


async def test_put_then_get_roundtrips(store):
    await store.put("a/b.json", b'{"x": 1}')
    assert await store.get("a/b.json") == b'{"x": 1}'


async def test_put_creates_nested_prefixes(store):
    await store.put("users/a@b.com/wall.json", b"{}")
    assert await store.get("users/a@b.com/wall.json") == b"{}"


async def test_get_with_version_returns_stable_version(store):
    await store.put("k", b"hello")
    first = await store.get_with_version("k")
    second = await store.get_with_version("k")
    assert first is not None and first[0] == b"hello"
    assert first[1] == second[1]


async def test_version_changes_when_content_changes(store):
    await store.put("k", b"one")
    before = (await store.get_with_version("k"))[1]
    await store.put("k", b"two")
    after = (await store.get_with_version("k"))[1]
    assert before != after


async def test_put_if_version_none_creates_only_when_absent(store):
    assert await store.put_if_version("k", b"first", None) is True
    assert await store.put_if_version("k", b"second", None) is False
    assert await store.get("k") == b"first"


async def test_put_if_version_matching_succeeds(store):
    await store.put("k", b"one")
    version = (await store.get_with_version("k"))[1]
    assert await store.put_if_version("k", b"two", version) is True
    assert await store.get("k") == b"two"


async def test_put_if_version_stale_is_refused(store):
    await store.put("k", b"one")
    stale = (await store.get_with_version("k"))[1]
    await store.put("k", b"two")
    assert await store.put_if_version("k", b"three", stale) is False
    assert await store.get("k") == b"two"


async def test_list_keys_filters_by_prefix(store):
    await store.put("logs/2026-08-16/a.json", b"{}")
    await store.put("logs/2026-08-16/b.json", b"{}")
    await store.put("logs/2026-08-17/c.json", b"{}")
    await store.put("users/x/config.yaml", b"{}")
    assert await store.list_keys("logs/2026-08-16/") == [
        "logs/2026-08-16/a.json",
        "logs/2026-08-16/b.json",
    ]


async def test_list_keys_is_sorted(store):
    for name in ["c", "a", "b"]:
        await store.put(f"logs/{name}.json", b"{}")
    assert await store.list_keys("logs/") == [
        "logs/a.json",
        "logs/b.json",
        "logs/c.json",
    ]


async def test_list_keys_missing_prefix_is_empty(store):
    assert await store.list_keys("nothing/") == []
