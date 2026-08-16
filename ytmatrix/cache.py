"""Content-addressed cache for search results, shared by every user.

Knows nothing about YouTube -- callers pass the exact request parameters and
get back whatever was stored under their hash, or None on a miss or a corrupt
entry. Backed by a `Store`, not a directory: the container has no durable
disk, so this must go through the same abstraction as everything else that
used to live under `cache_dir`.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from ytmatrix.store import Store

#: Shared by every user. The key is a hash of the search parameters and
#: deliberately excludes the API key, so one user's search warms the cache for
#: everyone -- which is the whole reason ten users do not cost ten times the
#: quota.
KEY_PREFIX = "search/"


def cache_key(params: dict[str, Any]) -> str:
    """Hash the request parameters that determine the result set.

    Callers must not include the API key: it does not affect results, and
    hashing it would put a secret into a storage key.
    """
    normalized = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _entry_key(params: dict[str, Any]) -> str:
    return f"{KEY_PREFIX}{cache_key(params)}.json"


async def read(
    store: Store,
    params: dict[str, Any],
    ttl_hours: float,
    *,
    allow_stale: bool = False,
) -> list[dict] | None:
    raw = await store.get(_entry_key(params))
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        fetched_at = float(payload["fetched_at"])
        items = payload["items"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # A corrupt entry is a miss, not a crash -- worst case we spend a call.
        return None
    if not allow_stale and (time.time() - fetched_at) >= ttl_hours * 3600:
        return None
    return items


async def write(store: Store, params: dict[str, Any], items: list[dict]) -> None:
    payload = json.dumps({"fetched_at": time.time(), "params": params, "items": items})
    await store.put(_entry_key(params), payload.encode("utf-8"))
