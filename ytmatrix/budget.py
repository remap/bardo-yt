"""Track how much YouTube quota has been spent today.

The daily allowance is 10,000 units and a search costs 100, so the real
currency is "100 searches a day". Generating a fresh query on every page
reload spends one every time, which makes an accidental afternoon of reloading
enough to exhaust it. This ledger makes the ceiling enforceable.

Google resets the allowance at midnight **Pacific**, not local midnight, so
that is the clock this ledger keeps. Using the local date would roll the
counter over at the wrong moment and hand back a budget that Google has not
actually refilled yet.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# The timezone Google resets YouTube Data API quota in.
QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")

LEDGER_NAME = "_budget.json"

SEARCH_COST_UNITS = 100
DAILY_QUOTA_UNITS = 10_000


def _ledger_path(cache_dir: Path) -> Path:
    return cache_dir / LEDGER_NAME


def _today() -> str:
    return datetime.now(QUOTA_RESET_TZ).date().isoformat()


def spent(cache_dir: Path, *, today: str | None = None) -> int:
    """Units spent on the given day. A different day reads as zero."""
    today = today or _today()
    path = _ledger_path(cache_dir)
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text())
        if payload["date"] != today:
            return 0
        return int(payload["units"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        return 0


def record_search(cache_dir: Path, *, today: str | None = None) -> None:
    today = today or _today()
    cache_dir.mkdir(parents=True, exist_ok=True)
    units = spent(cache_dir, today=today) + SEARCH_COST_UNITS
    path = _ledger_path(cache_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"date": today, "units": units}))
    tmp.replace(path)


def would_exceed(cache_dir: Path, limit_units: int, *, today: str | None = None) -> bool:
    """True when one more search would push past the limit. 0 disables."""
    if limit_units <= 0:
        return False
    return spent(cache_dir, today=today) + SEARCH_COST_UNITS > limit_units
