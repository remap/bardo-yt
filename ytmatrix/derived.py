"""Per-video derived values, held in memory and persisted as one object.

Motion scores and countries of origin are derived, immutable per video, and
cheap to recompute. They were stored one small object per video, which is free
against a FileStore and expensive against R2: a fresh query resolves 50 new ids
and paid 50 gets plus 50 puts for the countries and 32 of each for the motion
scores, every one of them an HTTPS round trip on the critical path of a request
somebody was waiting for. Measured on Cloudflare: 2.2s for origin and 3.3s for
motion, against 0.19s and 0.22s for the identical code on a laptop.

So the shape changes rather than the data:

- **One object per kind**, a `{video_id: value}` map, not one per video. A cold
  container pays one get instead of eighty-two.
- **Memory is the working copy.** Lookups never touch the store after the load.
- **Writes go behind the response.** `flush()` is fired as a background task, so
  a request never waits for a put. Losing a flush costs a recomputation later,
  which is the same price the first request paid and no worse than a container
  that slept before it wrote.

Concurrency, since the point of one shared container is many simultaneous
users:

- **One process, one event loop.** `container.py` runs uvicorn with no worker
  count, so every request shares this map. Two users resolving at once is the
  normal case and they cooperate: whoever measures a video first, the other
  reads it. That sharing is the design, not a hazard.
- **The lock covers load and snapshot**, so a cold container fetches the map
  once however many requests arrive together, and two flushes cannot interleave
  and write a torn map.
- **A flush re-arms itself.** Values measured while a put is in flight are not
  in that put's snapshot, and with several users at once that window is wide.
  `_flush` re-schedules rather than leaving them for a later request to notice.
- **Shutdown flushes.** Cloudflare SIGTERMs the container when it sleeps, which
  would otherwise cancel a pending background task and drop the last window.

What this design genuinely depends on is *one writer process*. Adding uvicorn
workers, or two container instances serving simultaneously, would give each its
own map and make the last flush win, silently discarding the other's
measurements. `max_instances` is 3 in wrangler.jsonc for rolling deploys, so a
brief overlap during a deploy is possible; the cost is recomputation, because
these values are derived and each instance re-measures whatever it does not
find.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ytmatrix.store import Store

logger = logging.getLogger(__name__)


class DerivedIndex:
    """One `{video_id: value}` map, loaded once and flushed in the background."""

    def __init__(self, store: Store, key: str) -> None:
        self._store = store
        self._key = key
        self._entries: dict[str, Any] = {}
        self._loaded = False
        self._dirty = False
        # Serialises the load so two concurrent requests on a cold container do
        # not both fetch it, and serialises flushes so two cannot interleave and
        # write a torn map.
        self._lock = asyncio.Lock()
        # A background task with no strong reference can be garbage collected
        # mid-flight, which would drop the write silently.
        self._flush_task: asyncio.Task[None] | None = None

    async def load(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            raw = await self._store.get(self._key)
            if raw is not None:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        self._entries = parsed
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # A corrupt index costs a round of recomputation, not a
                    # broken wall. Starting empty is the same state a fresh
                    # deployment is in.
                    logger.warning("derived index %s is unreadable; starting empty", self._key)
            self._loaded = True

    @property
    def entries(self) -> dict[str, Any]:
        """The live map. Read-only by convention -- callers need `in` to tell a
        stored null (measured, and the answer was 'unknown') apart from an
        absent key (never measured), which `get` alone cannot express."""
        return self._entries

    def get(self, video_id: str) -> Any | None:
        return self._entries.get(video_id)

    def set(self, video_id: str, value: Any) -> None:
        # Membership first, not `get() != value`. Storing None for a video that
        # is not in the map yet compares equal to `get()`'s own None, so the
        # write looked like a no-op and was dropped -- meaning every
        # unmeasurable video (a 404 storyboard, a channel with no country) was
        # re-measured on every request, forever, because the result was never
        # written down. Both kinds legitimately store None, so this cannot rely
        # on the value alone.
        if video_id not in self._entries or self._entries[video_id] != value:
            self._entries[video_id] = value
            self._dirty = True

    def schedule_flush(self) -> None:
        """Persist the map without making anyone wait for it."""
        if not self._dirty:
            return
        if self._flush_task is not None and not self._flush_task.done():
            # A flush is already in flight and will pick up what is in memory
            # when it runs; queueing another would write the same thing twice.
            return
        self._flush_task = asyncio.create_task(self._flush())

    async def _flush(self) -> None:
        async with self._lock:
            if not self._dirty:
                return
            payload = json.dumps(self._entries).encode("utf-8")
            self._dirty = False
        try:
            await self._store.put(self._key, payload)
        except Exception:
            # Marked dirty again so the next request retries. Derived data, so
            # the cost of never succeeding is recomputation, not correctness.
            self._dirty = True
            logger.warning("could not flush derived index %s", self._key, exc_info=True)

        # Anything measured WHILE that put was in flight is in memory but not on
        # the wire: the snapshot was taken before it arrived, and schedule_flush
        # declined to queue a second task because this one was still running.
        # With several users resolving at once that window is wide, so close it
        # here rather than waiting for a later request to notice.
        if self._dirty:
            self.schedule_flush()

    async def aclose(self) -> None:
        """Flush and wait -- for tests and for a clean shutdown."""
        self.schedule_flush()
        if self._flush_task is not None:
            await self._flush_task
