"""What is currently on the wall, persisted across restarts.

The video *results* are already durable -- they live in the search cache. What
was missing is which query is current, so a server restart or a page reload
silently fell back to config.yaml's query and, with generation enabled, bought
a new one for 100 units.

Kept out of config.yaml deliberately: that file is committed, and writing every
generated query to it would churn git on every click.
"""

from __future__ import annotations

import json
from pathlib import Path

STATE_NAME = "_wall.json"

# Bounds the history that steers Gemini away from repeats. Without a cap this
# grows forever across a long-running install.
MAX_HISTORY = 200


def _path(cache_dir: Path) -> Path:
    return cache_dir / STATE_NAME


def load(cache_dir: Path) -> dict:
    """Return {"query": str | None, "history": list[str]}."""
    path = _path(cache_dir)
    if not path.exists():
        return {"query": None, "history": []}
    try:
        payload = json.loads(path.read_text())
        return {
            "query": payload.get("query") or None,
            "history": [str(q) for q in payload.get("history", [])],
        }
    except (json.JSONDecodeError, TypeError, ValueError, OSError):
        # A corrupt state file must not stop the wall from starting.
        return {"query": None, "history": []}


def save(cache_dir: Path, state: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _path(cache_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "query": state.get("query"),
                "history": list(state.get("history", []))[-MAX_HISTORY:],
            }
        )
    )
    tmp.replace(path)
