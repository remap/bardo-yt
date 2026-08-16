"""Unit tests for `server.motion_score`'s cache-read path.

The route-level behaviour of the wall's caches is covered end to end in
test_server.py. This file exists for one thing that isn't: a direct call to
the real `motion_score`, unshadowed by conftest's autouse stub (which every
other test in the suite wants, so no cell's storyboard fetch touches the
network). Importing the function by name -- rather than going through
`server.motion_score` -- binds this module's reference before any per-test
monkeypatch runs, so it always exercises the real implementation.
"""

import json

import httpx

from ytmatrix import motion
from ytmatrix.server import motion_score
from ytmatrix.store import FileStore


class _NeverReachesTheNetwork:
    """A stand-in for httpx.AsyncClient. Records calls; touches no wire."""

    def __init__(self):
        self.calls = 0

    async def get(self, url):
        self.calls += 1
        raise httpx.HTTPError("this test must never reach the network")


async def test_a_non_numeric_cached_score_falls_back_to_remeasuring(tmp_path):
    """A corrupt cache entry is a miss, not a crash -- same rule as cache.py.

    Regression for a bug introduced while porting this function to Store:
    `float(cached)` lived in the `else` clause of the try/except, outside the
    try, so a cache entry like {"score": "not-a-number"} raised an unhandled
    ValueError instead of degrading to "re-measure" like every other
    corruption shape (bad JSON, missing key, wrong type). The worst case of a
    miss is spending three thumbnail fetches; the worst case of the bug was a
    single bad entry permanently taking down that video's cell.
    """
    store = FileStore(tmp_path)
    await store.put("motion/abc123.json", json.dumps({"score": "not-a-number"}).encode())

    client = _NeverReachesTheNetwork()
    score = await motion_score("abc123", store, client)

    # No frames were fetchable (every request raised), so score_frames([])
    # returns UNKNOWN_SCORE -- but reaching that path at all, rather than
    # raising ValueError out of the cache read, is the point of this test.
    assert score == motion.UNKNOWN_SCORE
    assert client.calls == len(motion.STORYBOARD_INDICES)
