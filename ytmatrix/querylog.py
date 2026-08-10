"""An append-only record of every query the wall has run, and what it returned.

One JSON object per line, newest last. Gitignored -- this is a running record
of a particular installation, not source.

Timestamps are local with an explicit UTC offset (`2026-08-10T18:42:03-07:00`),
so a line is readable at a glance by whoever is standing in front of the wall
while still being unambiguous months later. The quota ledger deliberately keys
off Pacific time instead, because that is when Google resets; the two are
answering different questions and should not be merged.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

LOG_NAME = "queries.jsonl"


def local_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def append(log_dir: Path, entry: dict) -> None:
    """Append one record. Never raises -- logging must not break the wall."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": local_timestamp(), **entry}, ensure_ascii=False)
        with (log_dir / LOG_NAME).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def read_all(log_dir: Path) -> list[dict]:
    """Every record, oldest first. Malformed lines are skipped, not fatal."""
    path = log_dir / LOG_NAME
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def build_entry(
    *,
    query: str,
    source: str,
    video_ids: list[str],
    titles: dict[str, str],
    from_cache: bool,
    units_spent_today: int,
    reserves: int = 0,
    static_relaxed: int = 0,
    prompt: str | None = None,
    note: str | None = None,
) -> dict:
    """One record. Titles are stored alongside ids so the log stays readable
    without having to re-query YouTube months later."""
    entry = {
        "query": query,
        # "generated" | "manual" | "config" -- where the query came from.
        "source": source,
        "from_cache": from_cache,
        "count": len(video_ids),
        "reserves": reserves,
        "static_relaxed": static_relaxed,
        "units_spent_today": units_spent_today,
        "results": [{"video_id": v, "title": titles.get(v, "")} for v in video_ids],
    }
    if prompt:
        entry["prompt"] = prompt
    if note:
        entry["note"] = note
    return entry
