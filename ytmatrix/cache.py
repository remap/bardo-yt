from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


def cache_key(params: dict[str, Any]) -> str:
    """Hash the request parameters that determine the result set.

    Callers must not include the API key: it does not affect results, and
    hashing it would put a secret into a filename.
    """
    normalized = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _entry_path(cache_dir: Path, params: dict[str, Any]) -> Path:
    return cache_dir / f"{cache_key(params)}.json"


def read(
    cache_dir: Path,
    params: dict[str, Any],
    ttl_hours: float,
    *,
    allow_stale: bool = False,
) -> list[dict] | None:
    path = _entry_path(cache_dir, params)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        fetched_at = float(payload["fetched_at"])
        items = payload["items"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        # A corrupt entry is a miss, not a crash -- worst case we spend a call.
        return None
    if not allow_stale and (time.time() - fetched_at) >= ttl_hours * 3600:
        return None
    return items


def write(cache_dir: Path, params: dict[str, Any], items: list[dict]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _entry_path(cache_dir, params)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time(), "params": params, "items": items}))
    try:
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
