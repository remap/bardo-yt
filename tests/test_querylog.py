import re
from datetime import datetime

import pytest

from ytmatrix import querylog
from ytmatrix.store import FileStore

ENTRY = {
    "query": "kpop street dance cover",
    "source": "generated",
    "video_ids": ["aaa", "bbb"],
    "titles": {"aaa": "One", "bbb": "Two"},
    "from_cache": False,
    "units_spent_today": 300,
}


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


async def test_empty_log_reads_as_empty(store):
    assert await querylog.read_all(store) == []


async def test_an_entry_round_trips(store):
    await querylog.append(store, querylog.build_entry(**ENTRY))
    entries = await querylog.read_all(store)
    assert len(entries) == 1
    assert entries[0]["query"] == "kpop street dance cover"
    assert entries[0]["count"] == 2


async def test_entries_come_back_oldest_first(store):
    for i in range(3):
        await querylog.append(store, {"query": f"q{i}"})
    assert [e["query"] for e in await querylog.read_all(store)] == ["q0", "q1", "q2"]


async def test_entry_carries_the_user(store):
    await querylog.append(store, {"query": "q"}, email="A@B.com")
    assert (await querylog.read_all(store))[0]["user"] == "a@b.com"


async def test_entry_without_a_user_omits_the_field(store):
    await querylog.append(store, {"query": "q"})
    assert "user" not in (await querylog.read_all(store))[0]


async def test_two_entries_in_the_same_second_do_not_collide(store):
    """One object per entry means the key has to be unique even when two
    users search within the same second."""
    await querylog.append(store, {"query": "a"})
    await querylog.append(store, {"query": "b"})
    assert len(await querylog.read_all(store)) == 2


async def test_a_corrupt_object_is_skipped_not_fatal(store):
    await querylog.append(store, {"query": "good"})
    await store.put("logs/2026-08-16/broken.json", b"not json")
    assert [e["query"] for e in await querylog.read_all(store)] == ["good"]


async def test_logging_never_raises(store):
    class Broken:
        async def put(self, key, data):
            raise RuntimeError("storage is down")

    await querylog.append(Broken(), {"query": "q"})  # must not raise


async def test_unicode_survives(store):
    await querylog.append(store, {"query": "케이팝 커버"})
    assert (await querylog.read_all(store))[0]["query"] == "케이팝 커버"


async def test_every_entry_carries_a_local_timestamp_with_an_offset(store):
    await querylog.append(store, querylog.build_entry(**ENTRY))
    at = (await querylog.read_all(store))[0]["at"]
    # Local time with an explicit offset, e.g. 2026-08-10T18:42:03-07:00.
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", at), at
    assert datetime.fromisoformat(at).tzinfo is not None


async def test_titles_are_stored_so_the_log_stays_readable_later(store):
    await querylog.append(store, querylog.build_entry(**ENTRY))
    results = (await querylog.read_all(store))[0]["results"]
    assert results == [
        {"video_id": "aaa", "title": "One"},
        {"video_id": "bbb", "title": "Two"},
    ]


async def test_the_source_of_the_query_is_recorded(store):
    for source in ("generated", "manual", "config"):
        await querylog.append(store, querylog.build_entry(**{**ENTRY, "source": source}))
    assert [e["source"] for e in await querylog.read_all(store)] == [
        "generated",
        "manual",
        "config",
    ]


async def test_a_manual_prompt_is_recorded_alongside_the_query_it_produced(store):
    entry = querylog.build_entry(**ENTRY, prompt="something with more guitars")
    await querylog.append(store, entry)
    assert (await querylog.read_all(store))[0]["prompt"] == "something with more guitars"


async def test_no_prompt_key_when_there_was_no_prompt(store):
    await querylog.append(store, querylog.build_entry(**ENTRY))
    assert "prompt" not in (await querylog.read_all(store))[0]


async def test_an_unwritable_location_does_not_raise(tmp_path):
    # Logging must never take the wall down, even against a real FileStore.
    blocker = tmp_path / "logs"
    blocker.write_text("I am a file, not a directory")
    store = FileStore(tmp_path)
    await querylog.append(store, querylog.build_entry(**ENTRY))  # must not raise


async def test_cache_hits_are_distinguishable_from_fresh_searches(store):
    await querylog.append(store, querylog.build_entry(**{**ENTRY, "from_cache": True}))
    await querylog.append(store, querylog.build_entry(**{**ENTRY, "from_cache": False}))
    assert [e["from_cache"] for e in await querylog.read_all(store)] == [True, False]


async def test_non_utf8_bytes_are_skipped_not_fatal(store):
    """json.loads on bytes sniffs an encoding first; bytes that decode under
    none of UTF-8/16/32 raise UnicodeDecodeError, a sibling of
    JSONDecodeError rather than a subclass. One corrupt object must not
    abort reading the rest of the log."""
    await querylog.append(store, {"query": "good"})
    await store.put("logs/2026-08-16/corrupt.json", b"\xff\xfe\x00\x01garbage-not-valid")
    assert [e["query"] for e in await querylog.read_all(store)] == ["good"]


async def test_logging_never_raises_on_an_unserialisable_entry(store):
    # A set is not JSON-serialisable. json.dumps blowing up on a bad entry
    # must be swallowed exactly like a storage failure -- that is the
    # guarantee that actually protects the wall, not just the store.put path.
    await querylog.append(store, {"query": "q", "bad": {1, 2, 3}})  # must not raise


async def test_microsecond_keys_sort_chronologically_even_within_one_second(store, monkeypatch):
    """Regression guard for the original bug: keying purely off a
    second-precision timestamp let three rapid appends collide and fall back
    to a random uuid tiebreak, scattering their order. Proven deterministically
    here -- rather than relying on a tight loop happening to land inside the
    same second on whatever machine runs it -- by mocking datetime.now() to
    return three instants one microsecond apart, all inside the same second.
    That is the actual condition a real clock produces for this app (a
    handful of searches a day are never microseconds apart in practice, but
    they can easily share a second); microsecond-precision keys sort that
    correctly on their own, with no other machinery required."""
    base = datetime(2026, 8, 16, 12, 0, 0, 0).astimezone()
    instants = [base.replace(microsecond=i) for i in range(3)]

    class SteppingDatetime(datetime):
        _remaining = iter(instants)

        @classmethod
        def now(cls, tz=None):
            return next(cls._remaining)

    monkeypatch.setattr(querylog, "datetime", SteppingDatetime)
    for i in range(3):
        await querylog.append(store, {"query": f"q{i}"})
    assert [e["query"] for e in await querylog.read_all(store)] == ["q0", "q1", "q2"]
