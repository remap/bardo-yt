import json
import re
from datetime import datetime

from ytmatrix import querylog

ENTRY = {
    "query": "kpop street dance cover",
    "source": "generated",
    "video_ids": ["aaa", "bbb"],
    "titles": {"aaa": "One", "bbb": "Two"},
    "from_cache": False,
    "units_spent_today": 300,
}


def test_nothing_logged_yet_reads_as_empty(tmp_path):
    assert querylog.read_all(tmp_path) == []


def test_an_entry_round_trips(tmp_path):
    querylog.append(tmp_path, querylog.build_entry(**ENTRY))
    entries = querylog.read_all(tmp_path)
    assert len(entries) == 1
    assert entries[0]["query"] == "kpop street dance cover"
    assert entries[0]["count"] == 2


def test_entries_accumulate_oldest_first(tmp_path):
    for i in range(3):
        querylog.append(tmp_path, querylog.build_entry(**{**ENTRY, "query": f"q{i}"}))
    assert [e["query"] for e in querylog.read_all(tmp_path)] == ["q0", "q1", "q2"]


def test_every_entry_carries_a_local_timestamp_with_an_offset(tmp_path):
    querylog.append(tmp_path, querylog.build_entry(**ENTRY))
    at = querylog.read_all(tmp_path)[0]["at"]
    # Local time with an explicit offset, e.g. 2026-08-10T18:42:03-07:00.
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", at), at
    assert datetime.fromisoformat(at).tzinfo is not None


def test_titles_are_stored_so_the_log_stays_readable_later(tmp_path):
    querylog.append(tmp_path, querylog.build_entry(**ENTRY))
    results = querylog.read_all(tmp_path)[0]["results"]
    assert results == [
        {"video_id": "aaa", "title": "One"},
        {"video_id": "bbb", "title": "Two"},
    ]


def test_the_source_of_the_query_is_recorded(tmp_path):
    for source in ("generated", "manual", "config"):
        querylog.append(tmp_path, querylog.build_entry(**{**ENTRY, "source": source}))
    assert [e["source"] for e in querylog.read_all(tmp_path)] == [
        "generated",
        "manual",
        "config",
    ]


def test_a_manual_prompt_is_recorded_alongside_the_query_it_produced(tmp_path):
    entry = querylog.build_entry(**ENTRY, prompt="something with more guitars")
    querylog.append(tmp_path, entry)
    assert querylog.read_all(tmp_path)[0]["prompt"] == "something with more guitars"


def test_no_prompt_key_when_there_was_no_prompt(tmp_path):
    querylog.append(tmp_path, querylog.build_entry(**ENTRY))
    assert "prompt" not in querylog.read_all(tmp_path)[0]


def test_it_is_one_json_object_per_line(tmp_path):
    for i in range(3):
        querylog.append(tmp_path, querylog.build_entry(**{**ENTRY, "query": f"q{i}"}))
    lines = (tmp_path / querylog.LOG_NAME).read_text().strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # each line must parse on its own


def test_a_malformed_line_is_skipped_rather_than_fatal(tmp_path):
    querylog.append(tmp_path, querylog.build_entry(**ENTRY))
    with (tmp_path / querylog.LOG_NAME).open("a") as handle:
        handle.write("{ this is not json\n")
    querylog.append(tmp_path, querylog.build_entry(**{**ENTRY, "query": "after"}))

    entries = querylog.read_all(tmp_path)
    assert [e["query"] for e in entries] == ["kpop street dance cover", "after"]


def test_logging_creates_the_directory(tmp_path):
    target = tmp_path / "nested" / "logs"
    querylog.append(target, querylog.build_entry(**ENTRY))
    assert len(querylog.read_all(target)) == 1


def test_an_unwritable_directory_does_not_raise(tmp_path):
    # Logging must never take the wall down.
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory")
    querylog.append(blocker, querylog.build_entry(**ENTRY))  # must not raise


def test_non_ascii_queries_survive(tmp_path):
    querylog.append(tmp_path, querylog.build_entry(**{**ENTRY, "query": "케이팝 커버"}))
    assert querylog.read_all(tmp_path)[0]["query"] == "케이팝 커버"


def test_cache_hits_are_distinguishable_from_fresh_searches(tmp_path):
    querylog.append(tmp_path, querylog.build_entry(**{**ENTRY, "from_cache": True}))
    querylog.append(tmp_path, querylog.build_entry(**{**ENTRY, "from_cache": False}))
    assert [e["from_cache"] for e in querylog.read_all(tmp_path)] == [True, False]
