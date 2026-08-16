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
from zoneinfo import ZoneInfo

from ytmatrix.store import Store

# The timezone Google resets YouTube Data API quota in.
QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")

LEDGER_KEY = "_budget.json"

SEARCH_COST_UNITS = 100
DAILY_QUOTA_UNITS = 10_000

#: A failed conditional write means another user's container won the race.
#: Re-read and retry. With 5-10 users this effectively never fires twice.
CAS_ATTEMPTS = 10


def _today() -> str:
    return datetime.now(QUOTA_RESET_TZ).date().isoformat()


def _units_for(raw: bytes | None, today: str) -> int:
    if raw is None:
        return 0
    try:
        payload = json.loads(raw)
        if payload["date"] != today:
            return 0
        return int(payload["units"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return 0


async def spent(store: Store, *, today: str | None = None) -> int:
    """Units spent on the given day. A different day reads as zero."""
    today = today or _today()
    return _units_for(await store.get(LEDGER_KEY), today)


async def record_search(store: Store, *, today: str | None = None) -> None:
    """Add one search to the ledger, without losing a concurrent increment.

    This is the only multi-writer state in the app: every user's container
    spends from the same project allowance, so a plain read-modify-write would
    drop increments and quietly hand back quota Google has not refilled. R2's
    conditional PUT gives us compare-and-swap; on the vanishingly unlikely
    event of losing CAS_ATTEMPTS races in a row we write unconditionally
    rather than raise, because the search has already been spent by this point
    and failing the request would be a worse lie than undercounting by one.
    """
    today = today or _today()
    for _ in range(CAS_ATTEMPTS):
        current = await store.get_with_version(LEDGER_KEY)
        version = None if current is None else current[1]
        units = _units_for(None if current is None else current[0], today)
        payload = json.dumps({"date": today, "units": units + SEARCH_COST_UNITS})
        if await store.put_if_version(LEDGER_KEY, payload.encode("utf-8"), version):
            return
    units = await spent(store, today=today)
    payload = json.dumps({"date": today, "units": units + SEARCH_COST_UNITS})
    await store.put(LEDGER_KEY, payload.encode("utf-8"))


async def would_exceed(
    store: Store,
    limit_units: int,
    *,
    global_limit_units: int = DAILY_QUOTA_UNITS,
    today: str | None = None,
) -> bool:
    """True when one more search would cross either ceiling.

    Two ceilings, not one. `limit_units` comes from the caller's own config,
    which they can edit, and 0 disables it. `global_limit_units` is Google's
    project-wide allowance and is NOT user-editable: every wall shares one API
    key and one 10,000-unit bucket, so a per-user ledger would let ten users
    each believe they had the whole thing and turn this graceful refusal into
    a hard 403 quotaExceeded.

    The comparison is `>=`, not `>`: a search that would land exactly on the
    ceiling still refuses, because "at the ceiling" already means the next
    search after this one has nothing left -- it should not take one more
    just because it lands precisely on the line.
    """
    used = await spent(store, today=today)
    if used + SEARCH_COST_UNITS >= global_limit_units:
        return True
    if limit_units <= 0:
        return False
    return used + SEARCH_COST_UNITS >= limit_units
