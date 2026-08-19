"""The in-memory derived index: one object per kind, flushed behind the response."""

import json

from ytmatrix.derived import DerivedIndex
from ytmatrix.store import FileStore


async def test_a_missing_index_loads_empty(tmp_path):
    index = DerivedIndex(FileStore(tmp_path), "motion/index.json")
    await index.load()
    assert index.get("abc") is None
    assert index.entries == {}


async def test_values_survive_a_flush(tmp_path):
    store = FileStore(tmp_path)
    index = DerivedIndex(store, "motion/index.json")
    await index.load()
    index.set("abc", 30.0)
    await index.aclose()

    assert json.loads(await store.get("motion/index.json")) == {"abc": 30.0}


async def test_storing_none_is_persisted(tmp_path):
    """None means "measured, and unmeasurable" -- an absent key means never
    measured, and the two must not collapse.

    Regression: `set` compared `entries.get(id) != value`, so storing None for a
    video not yet in the map compared equal to get()'s own None, looked like a
    no-op, and was dropped. Every unmeasurable video was then re-measured on
    every request forever, because the answer was never written down.
    """
    store = FileStore(tmp_path)
    index = DerivedIndex(store, "motion/index.json")
    await index.load()
    index.set("abc", None)
    await index.aclose()

    raw = await store.get("motion/index.json")
    assert raw is not None, "storing None must still mark the index dirty"
    assert json.loads(raw) == {"abc": None}


async def test_a_reload_sees_what_was_flushed(tmp_path):
    store = FileStore(tmp_path)
    first = DerivedIndex(store, "origin/index.json")
    await first.load()
    first.set("abc", "KR")
    first.set("def", None)
    await first.aclose()

    second = DerivedIndex(store, "origin/index.json")
    await second.load()
    assert second.get("abc") == "KR"
    # Present with a null value, which is what lets the caller skip re-resolving.
    assert "def" in second.entries
    assert second.get("def") is None


async def test_a_corrupt_index_starts_empty_rather_than_raising(tmp_path):
    """One round of recomputation, not a broken wall."""
    store = FileStore(tmp_path)
    await store.put("motion/index.json", b"{not json at all")
    index = DerivedIndex(store, "motion/index.json")
    await index.load()
    assert index.entries == {}


async def test_an_index_of_the_wrong_shape_starts_empty(tmp_path):
    store = FileStore(tmp_path)
    await store.put("motion/index.json", b'["not", "a", "map"]')
    index = DerivedIndex(store, "motion/index.json")
    await index.load()
    assert index.entries == {}


async def test_the_store_is_read_once(tmp_path):
    inner = FileStore(tmp_path)
    await inner.put("motion/index.json", json.dumps({"abc": 1.0}).encode())
    counted = _CountingStore(inner)
    index = DerivedIndex(counted, "motion/index.json")

    for _ in range(5):
        await index.load()
    assert counted.gets == 1


async def test_nothing_is_written_when_nothing_changed(tmp_path):
    counted = _CountingStore(FileStore(tmp_path))
    index = DerivedIndex(counted, "motion/index.json")
    await index.load()
    await index.aclose()
    assert counted.puts == 0

    index.set("abc", 1.0)
    await index.aclose()
    assert counted.puts == 1

    # Setting the same value again is not a change.
    index.set("abc", 1.0)
    await index.aclose()
    assert counted.puts == 1


async def test_a_failed_flush_is_retried_rather_than_lost(tmp_path):
    """A lost flush costs a recomputation, so it must not also clear the dirty
    flag and make the loss permanent for the life of the container."""
    store = _FailingStore()
    index = DerivedIndex(store, "motion/index.json")
    await index.load()
    index.set("abc", 1.0)
    await index.aclose()
    assert store.attempts == 1

    store.fail = False
    await index.aclose()
    assert store.attempts == 2
    assert json.loads(store.written) == {"abc": 1.0}


class _CountingStore:
    def __init__(self, inner):
        self._inner = inner
        self.gets = 0
        self.puts = 0

    async def get(self, key):
        self.gets += 1
        return await self._inner.get(key)

    async def put(self, key, data):
        self.puts += 1
        return await self._inner.put(key, data)

    async def get_with_version(self, key):
        return await self._inner.get_with_version(key)

    async def put_if_version(self, key, data, version):
        return await self._inner.put_if_version(key, data, version)

    async def list_keys(self, prefix):
        return await self._inner.list_keys(prefix)


class _FailingStore:
    def __init__(self):
        self.fail = True
        self.attempts = 0
        self.written = None

    async def get(self, key):
        return None

    async def put(self, key, data):
        self.attempts += 1
        if self.fail:
            raise RuntimeError("storage is down")
        self.written = data

    async def get_with_version(self, key):
        return None

    async def put_if_version(self, key, data, version):
        return True

    async def list_keys(self, prefix):
        return []
