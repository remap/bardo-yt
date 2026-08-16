"""An append-only record of every query the wall has run, and what it returned.

This is a running record of a particular installation, not source. It stays
global -- one record for the whole install, not per user -- but each entry
carries the email of whoever ran the query; that email is the one and only
thing this module does with user identity, nothing branches on it.

R2 has no append, so it is no longer one JSON object per line in a single
file: it is one object per entry, keyed under a date prefix. There is nothing
to open and append to, only new objects to write, so ordering has to come
from somewhere else -- it falls out of the key. Every key begins with an ISO
timestamp and `Store.list_keys` returns keys sorted, so reading them in key
order reads them oldest first for free.

Timestamps are local with an explicit UTC offset (`2026-08-10T18:42:03-07:00`),
so a line is readable at a glance by whoever is standing in front of the wall
while still being unambiguous months later. The quota ledger deliberately keys
off Pacific time instead, because that is when Google resets; the two are
answering different questions and should not be merged.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from ytmatrix.store import Store

KEY_PREFIX = "logs/"


def local_timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now().astimezone()).isoformat(timespec="seconds")


def _entry_key(now: datetime) -> str:
    # Sorting has to come from the key alone -- there is no append in object
    # storage, so every entry is its own object and ordering falls out of
    # `list_keys` returning sorted keys. Seconds are precise enough for the
    # human-readable "at" stored in the record, but not for the key: a
    # handful of searches easily land in the same second, and at
    # second-resolution the tiebreak would fall to the uuid suffix and scatter
    # them. Microsecond resolution is what actually orders them -- a real
    # clock never produces two calls this app makes (a handful of searches a
    # day) at the identical microsecond, so the timestamp alone sorts
    # chronologically. The uuid suffix's job is narrower: keeping two
    # genuinely concurrent writers (different containers) from colliding on
    # an identical key on the rare occasion they do land on the same
    # microsecond, not ordering them.
    stamp = now.isoformat(timespec="microseconds")
    return f"{KEY_PREFIX}{stamp[:10]}/{stamp}-{uuid.uuid4().hex[:8]}.json"


async def append(store: Store, entry: dict, *, email: str | None = None) -> None:
    """Append one record. Never raises -- logging must not break the wall."""
    try:
        now = datetime.now().astimezone()
        record = {"at": local_timestamp(now), **entry}
        if email:
            record["user"] = email.strip().lower()
        body = json.dumps(record, ensure_ascii=False).encode("utf-8")
        await store.put(_entry_key(now), body)
    except Exception:  # noqa: BLE001, S110 - a failed write must not fail the wall
        pass


async def read_all(store: Store) -> list[dict]:
    """Every record, oldest first. Malformed objects are skipped, not fatal."""
    entries = []
    for key in await store.list_keys(KEY_PREFIX):
        raw = await store.get(key)
        if raw is None:
            continue
        try:
            entries.append(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # json.loads on bytes sniffs an encoding first; bytes that decode
            # under none of UTF-8/16/32 raise UnicodeDecodeError, a sibling
            # of JSONDecodeError rather than a subclass. One corrupt object
            # must not abort reading the rest of the log.
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
