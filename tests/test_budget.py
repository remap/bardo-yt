"""The daily search budget.

Generating a query on every reload means every reload can cost 100 of the
10,000 daily units -- roughly 100 reloads before the wall is dead until
midnight Pacific. This module makes that ceiling explicit and enforceable
rather than something you discover by hitting it.
"""

import json

import pytest

from ytmatrix import budget
from ytmatrix.store import FileStore


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


async def test_spent_starts_at_zero(store):
    assert await budget.spent(store) == 0


async def test_record_search_adds_one_search_cost(store):
    await budget.record_search(store, today="2026-08-16")
    assert await budget.spent(store, today="2026-08-16") == budget.SEARCH_COST_UNITS


async def test_records_accumulate(store):
    for _ in range(3):
        await budget.record_search(store, today="2026-08-16")
    assert await budget.spent(store, today="2026-08-16") == 300


async def test_a_new_pacific_day_reads_as_zero(store):
    await budget.record_search(store, today="2026-08-16")
    assert await budget.spent(store, today="2026-08-17") == 0


async def test_corrupt_ledger_reads_as_zero(store):
    await store.put(budget.LEDGER_KEY, b"not json")
    assert await budget.spent(store) == 0


async def test_zero_user_limit_disables_the_user_ceiling(store):
    await store.put(budget.LEDGER_KEY, json.dumps({"date": "2026-08-16", "units": 9000}).encode())
    assert await budget.would_exceed(store, 0, today="2026-08-16") is False


async def test_user_limit_refuses_when_crossed(store):
    await store.put(budget.LEDGER_KEY, json.dumps({"date": "2026-08-16", "units": 500}).encode())
    assert await budget.would_exceed(store, 500, today="2026-08-16") is True


async def test_global_cap_refuses_even_when_user_limit_is_disabled(store):
    """A user editing their own config must never be able to raise the real
    ceiling: every wall spends from one 10,000-unit project allowance."""
    await store.put(budget.LEDGER_KEY, json.dumps({"date": "2026-08-16", "units": 9950}).encode())
    assert (
        await budget.would_exceed(store, 0, global_limit_units=10_000, today="2026-08-16") is True
    )


async def test_global_cap_refuses_even_when_user_limit_is_huge(store):
    await store.put(budget.LEDGER_KEY, json.dumps({"date": "2026-08-16", "units": 9950}).encode())
    assert (
        await budget.would_exceed(store, 1_000_000, global_limit_units=10_000, today="2026-08-16")
        is True
    )


async def test_user_limit_can_still_lower_the_ceiling(store):
    await store.put(budget.LEDGER_KEY, json.dumps({"date": "2026-08-16", "units": 400}).encode())
    assert (
        await budget.would_exceed(store, 500, global_limit_units=10_000, today="2026-08-16") is True
    )


async def test_concurrent_records_do_not_lose_increments(store):
    """The ledger is the one piece of state with many writers -- every user's
    container spends from it. A lost update silently hands back quota Google
    has not refilled."""
    import asyncio

    await asyncio.gather(*(budget.record_search(store, today="2026-08-16") for _ in range(5)))
    assert await budget.spent(store, today="2026-08-16") == 500


class _LosesFirstRace:
    """Wraps a Store and makes its first `put_if_version` call lose a race.

    `FileStore.get`/`put` never actually suspend -- there is no real disk
    I/O yield point in this test process -- so `asyncio.gather` over several
    `record_search` calls runs them one at a time with no interleaving.
    `test_concurrent_records_do_not_lose_increments` above therefore passes
    even against a plain read-modify-write with no CAS at all: nothing ever
    races. This wrapper manufactures the race `record_search` is written to
    survive, by applying a rival write between the read and the write of the
    first attempt and reporting that attempt as lost, exactly what a genuine
    concurrent writer winning `put_if_version` looks like from the caller's
    side.
    """

    def __init__(self, inner):
        self._inner = inner
        self.put_if_version_calls = 0

    async def get(self, key):
        return await self._inner.get(key)

    async def put(self, key, data):
        return await self._inner.put(key, data)

    async def get_with_version(self, key):
        return await self._inner.get_with_version(key)

    async def list_keys(self, prefix):
        return await self._inner.list_keys(prefix)

    async def put_if_version(self, key, data, version):
        self.put_if_version_calls += 1
        if self.put_if_version_calls == 1:
            # A rival writer's search lands in between this attempt's read
            # and its write.
            await self._inner.put(key, json.dumps({"date": "2026-08-16", "units": 999}).encode())
            return False
        return await self._inner.put_if_version(key, data, version)


async def test_a_lost_race_is_retried_against_the_fresh_value(tmp_path):
    """Proves the CAS retry itself, not just the end count.

    A rival write of 999 lands between this call's read and its write. A
    plain read-modify-write would never notice -- it does not call
    `put_if_version` at all -- and would simply clobber the rival's 999 with
    its own 100. `record_search` must instead see the failed conditional
    write, retry, read the rival's 999, and add its own 100 on top.
    """
    racing_store = _LosesFirstRace(FileStore(tmp_path))
    await budget.record_search(racing_store, today="2026-08-16")
    assert racing_store.put_if_version_calls == 2, "must retry after the first attempt loses"
    assert await budget.spent(racing_store, today="2026-08-16") == 1099, (
        "must add to the rival's write, not overwrite it"
    )
