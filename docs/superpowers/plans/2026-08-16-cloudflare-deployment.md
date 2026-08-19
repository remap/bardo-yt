# Cloudflare Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run yt-matrix at `https://yt.bardo.jburke.io` for 5–10 authenticated users, each with their own wall, as a Cloudflare Container behind a statically-served frontend.

**Architecture:** Cloudflare Access authenticates at the edge and hands the Worker a verified email. The Worker serves `dist/` as static assets and routes `/api/*` + `/ws` to a **single shared** Container instance. Config is shared by everyone and lives in R2; each user's *own* wall — their current query and the history that steers Gemini away from repeats — lives in that browser's `localStorage` and is sent up with each request. The server therefore holds **no per-user state at all**: it is a pure function of (shared config, supplied query) over a shared cache and one global quota ledger.

**Tech Stack:** Python 3.13 / FastAPI / uvicorn (container), TypeScript Worker + `@cloudflare/containers` + `jose`, Cloudflare R2 (S3 API via boto3), Cloudflare Access (Zero Trust), Wrangler.

## Global Constraints

- **The container has no durable disk.** Cloudflare gives each instance a fresh copy of the image on every start. Nothing may rely on a file surviving a restart.
- **No per-user state on the server.** Current query and query history belong to the browser. If a task finds itself adding a dict keyed by user, it has taken a wrong turn.
- **A client-supplied query must never be able to spend quota.** The browser replays its stored query on every load and every WebSocket reconnect; a cache miss there falls back to the shared config query rather than searching. Spending remains the exclusive job of the New query button (gotcha 2).
- **The budget ledger is global.** Google's 10,000 units/day is a *Cloud project* limit against one API key. `quota.daily_limit_units` sits in the shared config and any user can edit it, so it may only ever *lower* the ceiling — the real cap is `YTMATRIX_GLOBAL_DAILY_UNITS` and no config may raise it.
- **`X-Wall-User` is set by the Worker and by nothing else.** It exists only so the query log records who ran what; nothing branches on it.
- The search cache stays **shared across all users** — the cache key already excludes the API key (gotcha 2), and sharing is what keeps 10 users from costing 10× quota.
- Preserve every gotcha in `CLAUDE.md`. Gotcha 11 (`localhost` vs `127.0.0.1`) no longer applies in production — the browser talks to a real HTTPS domain — but still applies to local dev and `tests/test_player_smoke.py`.
- Existing default test suite must keep passing with no network access (`tests/conftest.py` guards).
- `uv run ruff check . && uv run ruff format .` clean before every commit, and `node --test 'static/*.test.mjs'` for frontend work.

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `ytmatrix/store.py` | The `Store` protocol plus `FileStore` (local dev + tests) and `R2Store` (production). Knows nothing about YouTube, config, or budgets. |
| `ytmatrix/container.py` | Container entry point: build Settings, build an `R2Store`, run uvicorn over plain HTTP on `$PORT`. No TLS — Cloudflare terminates it. |
| `worker/index.ts` | Access JWT validation, forwarding to the shared container, static asset fallthrough. |
| `Dockerfile` | Python 3.13 + uv + the `ytmatrix` package. |
| `wrangler.jsonc` | Worker, container, assets, routes, vars. |
| `scripts/build-dist.sh` | Assembles `dist/` from `static/`. |
| `docs/DEPLOY.md` | The runbook (Task 10). |
| `tests/test_store.py` | `FileStore` + `R2Store` behaviour including compare-and-swap. |
| `static/wallstate.js` | The browser's own wall memory: current query + history in `localStorage`. Replaces `ytmatrix/wallstate.py`. |
| `static/wallstate.test.mjs` | Node tests for it. |

**Deleted:** `ytmatrix/wallstate.py` and `tests/test_wallstate.py` — that state moves into the browser.

**Modified:** `ytmatrix/cache.py`, `budget.py`, `config.py`, `querylog.py`, `server.py`, `settings.py`, `pyproject.toml`, `static/player.js`, `static/grid-logic.js`, and the matching tests.

**Untouched:** `youtube.py`, `gemini.py`, `motion.py`, `letterbox.py`, `origin.py`, `ws.py`, `static/config.js`, `static/socket.js`, and both HTML files.

**Retained for local dev:** `main.py` and `certs.py` keep working against a `FileStore`, so `./run.sh` still runs the wall on `https://localhost:8444/` with no Cloudflare account involved.

---

### Task 1: The Store abstraction

**Files:**
- Create: `ytmatrix/store.py`
- Create: `tests/test_store.py`
- Modify: `pyproject.toml` (add `boto3>=1.35`)

**Interfaces:**
- Consumes: nothing.
- Produces: `Store` protocol with `get(key) -> bytes | None`, `put(key, data) -> None`, `get_with_version(key) -> tuple[bytes, str] | None`, `put_if_version(key, data, version) -> bool`, `list_keys(prefix) -> list[str]` — all `async`. `FileStore(root: Path)`, `R2Store(client, bucket: str)`, `r2_client(account_id, access_key_id, secret_access_key)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
import pytest

from ytmatrix.store import FileStore


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


async def test_get_missing_key_is_none(store):
    assert await store.get("nope.json") is None


async def test_put_then_get_roundtrips(store):
    await store.put("a/b.json", b'{"x": 1}')
    assert await store.get("a/b.json") == b'{"x": 1}'


async def test_put_creates_nested_prefixes(store):
    await store.put("users/a@b.com/wall.json", b"{}")
    assert await store.get("users/a@b.com/wall.json") == b"{}"


async def test_get_with_version_returns_stable_version(store):
    await store.put("k", b"hello")
    first = await store.get_with_version("k")
    second = await store.get_with_version("k")
    assert first is not None and first[0] == b"hello"
    assert first[1] == second[1]


async def test_version_changes_when_content_changes(store):
    await store.put("k", b"one")
    before = (await store.get_with_version("k"))[1]
    await store.put("k", b"two")
    after = (await store.get_with_version("k"))[1]
    assert before != after


async def test_put_if_version_none_creates_only_when_absent(store):
    assert await store.put_if_version("k", b"first", None) is True
    assert await store.put_if_version("k", b"second", None) is False
    assert await store.get("k") == b"first"


async def test_put_if_version_matching_succeeds(store):
    await store.put("k", b"one")
    version = (await store.get_with_version("k"))[1]
    assert await store.put_if_version("k", b"two", version) is True
    assert await store.get("k") == b"two"


async def test_put_if_version_stale_is_refused(store):
    await store.put("k", b"one")
    stale = (await store.get_with_version("k"))[1]
    await store.put("k", b"two")
    assert await store.put_if_version("k", b"three", stale) is False
    assert await store.get("k") == b"two"


async def test_list_keys_filters_by_prefix(store):
    await store.put("logs/2026-08-16/a.json", b"{}")
    await store.put("logs/2026-08-16/b.json", b"{}")
    await store.put("logs/2026-08-17/c.json", b"{}")
    await store.put("users/x/config.yaml", b"{}")
    assert await store.list_keys("logs/2026-08-16/") == [
        "logs/2026-08-16/a.json",
        "logs/2026-08-16/b.json",
    ]


async def test_list_keys_is_sorted(store):
    for name in ["c", "a", "b"]:
        await store.put(f"logs/{name}.json", b"{}")
    assert await store.list_keys("logs/") == [
        "logs/a.json",
        "logs/b.json",
        "logs/c.json",
    ]


async def test_list_keys_missing_prefix_is_empty(store):
    assert await store.list_keys("nothing/") == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ytmatrix.store'`

- [ ] **Step 3: Write the implementation**

```python
# ytmatrix/store.py
"""Where persistent state lives.

Every module that used to take a `cache_dir: Path` takes a `Store` instead.
The container this app runs in has no durable disk -- Cloudflare hands each
instance a fresh copy of the image on every start -- so the search cache, the
quota ledger, each user's config and each user's wall state all have to live
somewhere the container does not own.

`FileStore` is the old on-disk behaviour, kept so local development and the
whole test suite run with no Cloudflare account. `R2Store` is production.

The interface is deliberately tiny: bytes in, bytes out, a prefix listing, and
one compare-and-swap. Only the budget ledger needs the CAS -- it is the single
piece of state with more than one writer, because every user's container
spends from the same 10,000-unit daily allowance. Everything else is either
immutable (content-addressed cache entries) or single-writer (a user's own
config and wall state, written only by that user's own container).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Protocol


class Store(Protocol):
    async def get(self, key: str) -> bytes | None: ...

    async def put(self, key: str, data: bytes) -> None: ...

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None: ...

    async def put_if_version(self, key: str, data: bytes, version: str | None) -> bool: ...

    async def list_keys(self, prefix: str) -> list[str]: ...


class FileStore:
    """A Store backed by a directory. Local development and every test.

    `put_if_version` is check-then-write rather than genuinely atomic. That is
    fine here and nowhere else: this store only ever runs under a single local
    process. Production concurrency is R2Store's problem, and it solves it
    properly with a conditional request.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return self._root / key

    async def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except OSError:
            # Missing, or a directory, or unreadable -- all of them are a miss.
            return None

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        try:
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None:
        data = await self.get(key)
        if data is None:
            return None
        return data, hashlib.sha256(data).hexdigest()

    async def put_if_version(self, key: str, data: bytes, version: str | None) -> bool:
        current = await self.get_with_version(key)
        if version is None:
            if current is not None:
                return False
        elif current is None or current[1] != version:
            return False
        await self.put(key, data)
        return True

    async def list_keys(self, prefix: str) -> list[str]:
        root = self._root
        if not root.is_dir():
            return []
        keys = [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and not path.name.endswith(".tmp")
        ]
        return sorted(key for key in keys if key.startswith(prefix))


def r2_client(account_id: str, access_key_id: str, secret_access_key: str) -> Any:
    """A boto3 S3 client pointed at R2, taught to send conditional headers.

    boto3 has no first-class parameter for the If-Match/If-None-Match headers
    that make `put_if_version` atomic, so the two event handlers below smuggle
    them through: the first lifts our `custom_headers` kwarg out before
    botocore's parameter validation rejects it, the second puts it on the wire.
    This is the pattern Cloudflare documents for R2 conditional writes.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
    )

    def lift_custom_headers(params, context, **kwargs):
        if custom_headers := params.pop("custom_headers", None):
            context["custom_headers"] = custom_headers

    def apply_custom_headers(params, context, **kwargs):
        if custom_headers := context.get("custom_headers"):
            params["headers"].update(custom_headers)

    events = client.meta.events
    events.register("before-parameter-build.s3.PutObject", lift_custom_headers)
    events.register("before-call.s3.PutObject", apply_custom_headers)
    return client


class R2Store:
    """A Store backed by one R2 bucket over the S3 API.

    boto3 is synchronous, so every call goes through `asyncio.to_thread`: these
    are network round trips, and blocking the event loop on them would stall
    every other player waiting on the same container.
    """

    #: A failed conditional write means someone else won the race; re-read and
    #: try again. Ten is far more than 5-10 users can realistically contend for.
    CAS_ATTEMPTS = 10

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def get(self, key: str) -> bytes | None:
        found = await self.get_with_version(key)
        return None if found is None else found[0]

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(
            self._client.put_object, Bucket=self._bucket, Key=key, Body=data
        )

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None:
        return await asyncio.to_thread(self._get_with_version, key)

    def _get_with_version(self, key: str) -> tuple[bytes, str] | None:
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        return response["Body"].read(), response["ETag"]

    async def put_if_version(self, key: str, data: bytes, version: str | None) -> bool:
        return await asyncio.to_thread(self._put_if_version, key, data, version)

    def _put_if_version(self, key: str, data: bytes, version: str | None) -> bool:
        from botocore.exceptions import ClientError

        # If-None-Match: * means "only if it does not exist yet".
        headers = {"If-None-Match": "*"} if version is None else {"If-Match": version}
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, custom_headers=headers
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "PreconditionFailed",
                "ConditionalRequestConflict",
                "412",
                "409",
            }:
                return False
            raise
        return True

    async def list_keys(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(self._list_keys, prefix)

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self._client.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in response.get("Contents", []))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
        return sorted(keys)
```

- [ ] **Step 4: Add boto3 to dependencies**

In `pyproject.toml`, add to `[project].dependencies`:

```toml
    "boto3>=1.35",
```

Then run: `uv sync`

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_store.py -v`
Expected: PASS (11 tests)

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format .
git add ytmatrix/store.py tests/test_store.py pyproject.toml uv.lock
git commit -m "feat: add Store abstraction over disk and R2"
```

---

### Task 2: Port the shared, immutable caches to Store

Covers `cache.py` and the three inline caches in `server.py` (motion score, country, content box). All four are shared across every user and content-addressed, so they need no per-user scoping and no CAS.

**Files:**
- Modify: `ytmatrix/cache.py`
- Modify: `ytmatrix/server.py:80-180` (motion/country), `ytmatrix/server.py:375-405` (content box), `ytmatrix/server.py:46-78` (`resolve_videos`)
- Modify: `tests/test_cache.py`, `tests/test_server.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `Store` from Task 1.
- Produces: `cache.read(store, params, ttl_hours, *, allow_stale=False)` and `cache.write(store, params, items)`, both `async`. `server.motion_score(video_id, store, client)`, `server.video_countries(video_ids, store, api_key)`, `server.select_videos(config, video_ids, store, api_key="")`, all `async`. R2 key layout: `search/<sha256>.json`, `motion/<video_id>.json`, `origin/<video_id>.json`, `contentbox/<video_id>.json`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cache.py -- replace the tmp_path-based cases with these
import pytest

from ytmatrix import cache
from ytmatrix.store import FileStore


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


async def test_read_miss_is_none(store):
    assert await cache.read(store, {"q": "a"}, 24) is None


async def test_write_then_read_roundtrips(store):
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    assert await cache.read(store, {"q": "a"}, 24) == [{"video_id": "x"}]


async def test_different_params_are_different_entries(store):
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    assert await cache.read(store, {"q": "b"}, 24) is None


async def test_expired_entry_reads_as_a_miss(store, monkeypatch):
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    monkeypatch.setattr(cache.time, "time", lambda: 1e12)
    assert await cache.read(store, {"q": "a"}, 24) is None


async def test_expired_entry_is_still_available_as_stale(store, monkeypatch):
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    monkeypatch.setattr(cache.time, "time", lambda: 1e12)
    assert await cache.read(store, {"q": "a"}, 24, allow_stale=True) == [{"video_id": "x"}]


async def test_corrupt_entry_is_a_miss_not_a_crash(store):
    await store.put(f"search/{cache.cache_key({'q': 'a'})}.json", b"not json")
    assert await cache.read(store, {"q": "a"}, 24) is None


async def test_entries_live_under_the_shared_search_prefix(store):
    """The search cache is shared by every user -- it must not be per-user."""
    await cache.write(store, {"q": "a"}, [{"video_id": "x"}])
    assert await store.list_keys("search/") == [f"search/{cache.cache_key({'q': 'a'})}.json"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cache.py -v`
Expected: FAIL — `TypeError` / `AttributeError`, `cache.read` still expects a `Path`

- [ ] **Step 3: Rewrite `cache.py`**

```python
# ytmatrix/cache.py
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
```

- [ ] **Step 4: Run the cache test to verify it passes**

Run: `uv run pytest tests/test_cache.py -v`
Expected: PASS

- [ ] **Step 5: Port the three inline caches in `server.py`**

Replace the disk reads/writes in `motion_score`, `video_countries`, and the content-box route. The shape of each is the same — swap `cache_dir: Path` for `store: Store` and the `read_text`/`write_text` pair for `store.get`/`store.put`:

```python
# ytmatrix/server.py -- motion_score
async def motion_score(video_id: str, store: Store, client: httpx.AsyncClient) -> float:
    key = f"motion/{video_id}.json"
    raw = await store.get(key)
    if raw is not None:
        # The float() conversion belongs INSIDE the try. A corrupt score value
        # -- {"score": "not-a-number"} -- must degrade to re-measuring like
        # every other corruption shape, or one bad entry takes that video down
        # permanently, because a raise means the entry never gets rewritten.
        try:
            cached = json.loads(raw)["score"]
            if cached is not None:
                return float(cached)
            return motion.UNKNOWN_SCORE
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass  # Fall through and re-measure.

    async def frame(index: int) -> bytes | None:
        ...  # unchanged

    score = await motion.score(video_id, frame)
    await store.put(
        key,
        json.dumps({"score": None if score == motion.UNKNOWN_SCORE else score}).encode("utf-8"),
    )
    return score
```

```python
# ytmatrix/server.py -- video_countries
async def video_countries(video_ids: list[str], store: Store, api_key: str) -> dict[str, str]:
    known: dict[str, str] = {}
    unknown: list[str] = []
    for video_id in video_ids:
        raw = await store.get(f"origin/{video_id}.json")
        if raw is None:
            unknown.append(video_id)
            continue
        try:
            country = json.loads(raw).get("country")
        except (json.JSONDecodeError, TypeError):
            unknown.append(video_id)
            continue
        if country:
            known[video_id] = country
    ...  # batched videos.list + channels.list lookup unchanged
    for video_id, country in fetched.items():
        await store.put(
            f"origin/{video_id}.json", json.dumps({"country": country}).encode("utf-8")
        )
    return known | fetched
```

```python
# ytmatrix/server.py -- content_box route body
    raw = await store.get(f"contentbox/{video_id}.json")
    if raw is not None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    ...  # letterbox detection unchanged
    await store.put(f"contentbox/{video_id}.json", json.dumps(box).encode("utf-8"))
    return box
```

- [ ] **Step 6: Update `resolve_videos` and `conftest.py` signatures**

`resolve_videos(config, store, api_key, query=None)` — change the `cache.read`/`cache.write`/`budget.*` calls to `await`. In `tests/conftest.py`, update the two autouse stubs to the new signatures:

```python
    async def unmeasured(video_id, store, client):
        return motion.UNKNOWN_SCORE

    monkeypatch.setattr(server, "motion_score", unmeasured)

    async def no_countries(video_ids, store, api_key):
        return {}

    monkeypatch.setattr(server, "video_countries", no_countries)
```

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: `test_cache.py` and `test_store.py` PASS. `test_server.py` still fails — it constructs `create_app` with `cache_dir`, which Task 6 changes. That is expected at this point.

- [ ] **Step 8: Lint and commit**

```bash
uv run ruff check . && uv run ruff format .
git add ytmatrix/cache.py ytmatrix/server.py tests/test_cache.py tests/conftest.py
git commit -m "feat: move the shared caches onto Store"
```

---

### Task 3: Port the budget ledger, with a global cap and compare-and-swap

**Files:**
- Modify: `ytmatrix/budget.py`
- Modify: `tests/test_budget.py`

**Interfaces:**
- Consumes: `Store` from Task 1.
- Produces: `budget.spent(store, *, today=None)`, `budget.record_search(store, *, today=None)`, `budget.would_exceed(store, limit_units, *, global_limit_units=DAILY_QUOTA_UNITS, today=None)` — all `async`. Ledger at key `_budget.json`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_budget.py -- add to the existing file, converting existing cases to await
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
    assert await budget.would_exceed(store, 0, global_limit_units=10_000, today="2026-08-16") is True


async def test_global_cap_refuses_even_when_user_limit_is_huge(store):
    await store.put(budget.LEDGER_KEY, json.dumps({"date": "2026-08-16", "units": 9950}).encode())
    assert (
        await budget.would_exceed(
            store, 1_000_000, global_limit_units=10_000, today="2026-08-16"
        )
        is True
    )


async def test_user_limit_can_still_lower_the_ceiling(store):
    await store.put(budget.LEDGER_KEY, json.dumps({"date": "2026-08-16", "units": 450}).encode())
    assert await budget.would_exceed(store, 500, global_limit_units=10_000, today="2026-08-16") is True


async def test_landing_exactly_on_the_limit_is_allowed(store):
    """`would_exceed` means "would push PAST the limit", not "would reach it".

    Landing exactly on the ceiling is permitted, and that is worth a test of
    its own: flipping this comparison to `>=` costs a whole search per day at
    any limit — 100 units of the scarcest resource the app has — while looking
    like harmless conservatism.
    """
    await store.put(budget.LEDGER_KEY, json.dumps({"date": "2026-08-16", "units": 400}).encode())
    assert await budget.would_exceed(store, 500, global_limit_units=10_000, today="2026-08-16") is False


async def test_concurrent_records_do_not_lose_increments(store):
    """The ledger is the one piece of state with many writers -- every user's
    container spends from it. A lost update silently hands back quota Google
    has not refilled."""
    import asyncio

    await asyncio.gather(*(budget.record_search(store, today="2026-08-16") for _ in range(5)))
    assert await budget.spent(store, today="2026-08-16") == 500
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_budget.py -v`
Expected: FAIL — `budget.LEDGER_KEY` does not exist; `spent` is not awaitable

- [ ] **Step 3: Rewrite `budget.py`**

Keep the module docstring, then replace the body below it:

```python
# ytmatrix/budget.py
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
    """
    used = await spent(store, today=today)
    if used + SEARCH_COST_UNITS > global_limit_units:
        return True
    if limit_units <= 0:
        return False
    return used + SEARCH_COST_UNITS > limit_units
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_budget.py -v`
Expected: PASS

Note: `tests/test_server.py` is still failing at this point and that is expected. `budget.spent` is now a coroutine, and `server.videos_message` calls it synchronously at `server.py:230`. Task 6 Step 4a fixes that. Do not patch it here — the fix belongs with the rest of the server rewiring.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format .
git add ytmatrix/budget.py tests/test_budget.py
git commit -m "feat: global quota ledger with compare-and-swap"
```

---

### Task 4: Shared config in the Store, and delete server-side wall state

Config is shared by everyone: one key, seeded from the image's committed `config.yaml`. Wall state leaves the server entirely — Task 6b rebuilds it in the browser.

**Files:**
- Modify: `ytmatrix/config.py:186-194`
- Delete: `ytmatrix/wallstate.py`, `tests/test_wallstate.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `Store` from Task 1.
- Produces: `config.load_config(store, *, default_path=DEFAULT_CONFIG_PATH)` and `config.save_config(config, store)`, both `async`; `config.CONFIG_KEY = "config.yaml"`; `config.DEFAULT_CONFIG_PATH`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py -- add these; convert the existing load/save cases to await
import pytest

from ytmatrix.config import CONFIG_KEY, Config, load_config, save_config
from ytmatrix.store import FileStore


@pytest.fixture
def store(tmp_path):
    # A subdirectory, NOT tmp_path itself: default_path writes the seed template
    # into tmp_path, and a store rooted there would list that file as a key and
    # break test_a_save_is_visible_to_everyone's single-key assertion.
    return FileStore(tmp_path / "store")


@pytest.fixture
def default_path(tmp_path):
    # Must be a complete, valid config -- Config.model_validate rejects a
    # document missing required sections, so `query:` alone will not load.
    path = tmp_path / "default.yaml"
    path.write_text(yaml.safe_dump({**VALID, "query": "seeded from the image"}))
    return path


async def test_unsaved_config_falls_back_to_the_bundled_default(store, default_path):
    config = await load_config(store, default_path=default_path)
    assert config.query == "seeded from the image"


async def test_saved_config_is_read_back(store, default_path):
    config = await load_config(store, default_path=default_path)
    await save_config(config.model_copy(update={"query": "ours"}), store)
    assert (await load_config(store, default_path=default_path)).query == "ours"


async def test_a_save_is_visible_to_everyone(store, default_path):
    """Config is shared: there is exactly one key, not one per user."""
    config = await load_config(store, default_path=default_path)
    await save_config(config.model_copy(update={"query": "ours"}), store)
    assert await store.list_keys("") == [CONFIG_KEY]


async def test_saved_config_is_readable_yaml(store, default_path):
    config = await load_config(store, default_path=default_path)
    await save_config(config.model_copy(update={"query": "ours"}), store)
    assert b"query: ours" in await store.get(CONFIG_KEY)


async def test_unicode_survives_a_save(store, default_path):
    config = await load_config(store, default_path=default_path)
    await save_config(config.model_copy(update={"query": "케이팝 커버"}), store)
    assert (await load_config(store, default_path=default_path)).query == "케이팝 커버"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'CONFIG_KEY'`

- [ ] **Step 3: Rewrite the tail of `config.py`**

Replace `load_config` / `save_config` (lines 186-194). Add `from ytmatrix.store import Store` to the imports; keep `from pathlib import Path`.

```python
#: One config for the whole installation. Everybody edits the same document
#: and a save is visible to everyone -- that is the point of the config page.
#: What is *not* shared is which query each person is watching; that lives in
#: their own browser (static/wallstate.js), so pressing New query changes only
#: your wall.
CONFIG_KEY = "config.yaml"

#: The committed config.yaml ships inside the container image and is the
#: document the installation starts from before anyone has pressed Save.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


async def load_config(store: Store, *, default_path: Path = DEFAULT_CONFIG_PATH) -> Config:
    raw = await store.get(CONFIG_KEY)
    if raw is None:
        # Nothing saved yet: fall back to the image's committed template
        # rather than writing a copy now, so a fresh install costs no storage
        # and picks up changes to the shipped defaults.
        return Config.model_validate(yaml.safe_load(default_path.read_text()) or {})
    return Config.model_validate(yaml.safe_load(raw.decode("utf-8")) or {})


async def save_config(config: Config, store: Store) -> None:
    payload = config.model_dump(mode="json")
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    await store.put(CONFIG_KEY, body.encode("utf-8"))
```

- [ ] **Step 4: Delete the server-side wall state**

```bash
git rm ytmatrix/wallstate.py tests/test_wallstate.py
```

Its job — remembering the current query and the history that steers Gemini away from repeats — moves to the browser in Task 6b. Remove `wallstate` from the `from ytmatrix import (...)` block at `server.py:13-23`; Task 6 rewires the call sites.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check . && uv run ruff format .
git add ytmatrix/config.py tests/test_config.py
git commit -m "feat: shared config in the Store; drop server-side wall state"
```

---

### Task 5: Port the query log

R2 has no append, so the single `queries.jsonl` becomes one object per entry under a date prefix. The log stays global — it is the operator's record of the whole install — with the user's email on each entry.

**Files:**
- Modify: `ytmatrix/querylog.py`
- Modify: `tests/test_querylog.py`

**Interfaces:**
- Consumes: `Store` from Task 1.
- Produces: `querylog.append(store, entry, *, email=None)` and `querylog.read_all(store)`, both `async`. `build_entry` unchanged. Keys: `logs/<YYYY-MM-DD>/<iso-timestamp>-<uuid4hex8>.json`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_querylog.py -- convert the existing cases; add these
import pytest

from ytmatrix import querylog
from ytmatrix.store import FileStore


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


async def test_empty_log_reads_as_empty(store):
    assert await querylog.read_all(store) == []


async def test_entries_come_back_oldest_first(store):
    for i in range(3):
        await querylog.append(store, {"query": f"q{i}"})
    assert [e["query"] for e in await querylog.read_all(store)] == ["q0", "q1", "q2"]


async def test_entry_carries_the_user(store):
    await querylog.append(store, {"query": "q"}, email="A@B.com")
    assert (await querylog.read_all(store))[0]["user"] == "a@b.com"


async def test_entry_without_a_user_omits_the_field(store):
    await querylog.append(store, {"query": "q"})
    assert "user" not in (await querylog.read_all(store))[0]


async def test_two_entries_in_the_same_second_do_not_collide(store):
    """One object per entry means the key has to be unique even when two
    users search within the same second."""
    await querylog.append(store, {"query": "a"})
    await querylog.append(store, {"query": "b"})
    assert len(await querylog.read_all(store)) == 2


async def test_a_corrupt_object_is_skipped_not_fatal(store):
    await querylog.append(store, {"query": "good"})
    await store.put("logs/2026-08-16/broken.json", b"not json")
    assert [e["query"] for e in await querylog.read_all(store)] == ["good"]


async def test_logging_never_raises(store):
    class Broken:
        async def put(self, key, data):
            raise RuntimeError("storage is down")

    await querylog.append(Broken(), {"query": "q"})  # must not raise


async def test_unicode_survives(store):
    await querylog.append(store, {"query": "케이팝 커버"})
    assert (await querylog.read_all(store))[0]["query"] == "케이팝 커버"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_querylog.py -v`
Expected: FAIL — `append` still expects a `Path`

- [ ] **Step 3: Rewrite `append` and `read_all`**

Keep the module docstring but replace the "One JSON object per line" paragraph with a note that R2 has no append, so it is now one object per entry under a date prefix, and the ordering guarantee comes from the key. Then:

```python
import uuid

KEY_PREFIX = "logs/"


def local_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _entry_key(now: datetime) -> str:
    # The date prefix keeps a day's worth of entries listable without scanning
    # the whole log, and the uuid suffix keeps two users searching in the same
    # instant from overwriting each other -- there is no append in object
    # storage, so every entry is its own object.
    #
    # MICROSECONDS here, deliberately, even though the record's readable `at`
    # field stays at second precision. Ordering comes entirely from sorting
    # these keys, so a second-precision key would leave everything logged
    # within the same second to be ordered by the random uuid suffix -- i.e.
    # not ordered at all. Pass one `now` through both so the key and the field
    # can never disagree.
    stamp = now.isoformat(timespec="microseconds")
    return f"{KEY_PREFIX}{stamp[:10]}/{stamp}-{uuid.uuid4().hex[:8]}.json"


async def append(store: Store, entry: dict, *, email: str | None = None) -> None:
    """Append one record. Never raises -- logging must not break the wall."""
    try:
        at = local_timestamp()
        record = {"at": at, **entry}
        if email:
            record["user"] = email.strip().lower()
        body = json.dumps(record, ensure_ascii=False).encode("utf-8")
        await store.put(_entry_key(at), body)
    except Exception:  # noqa: BLE001 - a failed write must not fail the wall
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
            # UnicodeDecodeError matters as much as the JSON error and is NOT a
            # subclass of it: json.loads on *bytes* sniffs an encoding first, so
            # genuinely non-UTF-8 bytes raise from the decode rather than the
            # parse. Catching only JSONDecodeError lets one corrupt object abort
            # the read of the entire log instead of skipping that one entry.
            continue
    return entries
```

Swap `from pathlib import Path` for `from ytmatrix.store import Store`.

Note: `list_keys` returns sorted keys and the key begins with an ISO timestamp, so oldest-first ordering falls out of the sort.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_querylog.py -v`
Expected: PASS

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check . && uv run ruff format .
git add ytmatrix/querylog.py tests/test_querylog.py
git commit -m "feat: query log as one object per entry"
```

---

### Task 6: Make the server stateless

No dict keyed by user, no remembered query. The server takes a query, serves the shared config, and logs who asked.

**Files:**
- Modify: `ytmatrix/server.py:216-471`
- Modify: `ytmatrix/settings.py`
- Modify: `ytmatrix/main.py:38-44`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: `create_app(store: Store, settings: Settings, *, default_config_path: Path = DEFAULT_CONFIG_PATH) -> FastAPI`. Settings gains `global_daily_units: int = 10_000`. `videos_message(config, resolved, query, selection, units_spent_today: int) -> dict` stays synchronous. `resolve_videos(config, store, api_key, query=None, *, global_limit_units=budget.DAILY_QUOTA_UNITS)`. `GET /api/videos?query=<q>`; `POST /api/new-query {prompt?, history?}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_server.py -- add alongside the converted existing cases
import pytest
from fastapi.testclient import TestClient

from ytmatrix.server import create_app
from ytmatrix.settings import Settings
from ytmatrix.store import FileStore


def test_config_is_shared_between_callers(app_env):
    app, _, _ = app_env
    with TestClient(app) as client:
        client.put("/api/config", json={"grid": {"cols": 3, "rows": 3}})
        assert client.get("/api/config").json()["grid"]["cols"] == 3


def test_a_rejected_put_leaves_the_stored_config_untouched(app_env):
    """Gotcha 8, now against a Store key rather than a file."""
    app, _, _ = app_env
    with TestClient(app) as client:
        client.put("/api/config", json={"query": "good"})
        assert client.put("/api/config", json={"query": "   "}).status_code == 422
        assert client.get("/api/config").json()["query"] == "good"


def test_a_cached_client_query_is_honoured(app_env):
    app, _, store = app_env
    seed_cache(store, prefix="vid")
    cached = seed_query(store, "already searched")
    with TestClient(app) as client:
        assert client.get("/api/videos", params={"query": cached}).json()["query"] == cached


def test_an_unknown_client_query_never_spends_a_search(app_env):
    """The browser replays localStorage on every load and reconnect. A cache
    miss there must fall back, not search -- gotcha 2."""
    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        body = client.get("/api/videos", params={"query": "never searched before"}).json()
    assert body["query"] == "golden cover"
    assert body["units_spent_today"] == 0


def test_a_stale_client_query_is_still_honoured(app_env, monkeypatch):
    """Expiry must not silently move somebody's wall back to the shared
    query; the ids are the same ones they were already watching."""
    app, _, store = app_env
    seed_cache(store)
    cached = seed_query(store, "watched yesterday")
    monkeypatch.setattr(server.cache.time, "time", lambda: 1e12)
    with TestClient(app) as client:
        assert client.get("/api/videos", params={"query": cached}).json()["query"] == cached


def test_no_query_serves_the_shared_config_query(app_env):
    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        assert client.get("/api/videos").json()["query"] == "golden cover"


def test_new_query_does_not_broadcast_videos(app_env, monkeypatch):
    """Everyone shares config but not walls: one person pressing New query
    must not move anybody else's."""
    app, _, store = app_env
    seed_cache(store)
    sent = []
    monkeypatch.setattr(server.ConnectionManager, "broadcast", _record(sent))
    monkeypatch.setattr(server.gemini, "generate_query", _returns("invented"))
    with TestClient(app) as client:
        client.post("/api/new-query", json={})
    assert [m["type"] for m in sent] == []


def test_new_query_returns_the_query_for_the_client_to_store(app_env, monkeypatch):
    app, _, store = app_env
    seed_cache(store)
    monkeypatch.setattr(server.gemini, "generate_query", _returns("invented"))
    with TestClient(app) as client:
        assert client.post("/api/new-query", json={}).json()["query"] == "invented"


def test_new_query_passes_the_clients_history_to_gemini(app_env, monkeypatch):
    """History steers Gemini away from repeats and now lives in the browser,
    so it has to arrive on the request."""
    seen = {}

    async def capture(theme, history, model, api_key, instruction=None):
        seen["history"] = history
        return "invented"

    app, _, store = app_env
    seed_cache(store)
    monkeypatch.setattr(server.gemini, "generate_query", capture)
    with TestClient(app) as client:
        client.post("/api/new-query", json={"history": ["a", "b"]})
    assert seen["history"] == ["a", "b"]


def test_new_query_tolerates_a_missing_history(app_env, monkeypatch):
    app, _, store = app_env
    seed_cache(store)
    monkeypatch.setattr(server.gemini, "generate_query", _returns("invented"))
    with TestClient(app) as client:
        assert client.post("/api/new-query", json={}).status_code == 200


def test_a_config_change_broadcasts_config_only(app_env, monkeypatch):
    """The client decides whether its own query needs refetching -- the server
    cannot, because it does not know what any browser is watching."""
    app, _, store = app_env
    seed_cache(store)
    sent = []
    monkeypatch.setattr(server.ConnectionManager, "broadcast", _record(sent))
    with TestClient(app) as client:
        client.put("/api/config", json={"search": {"order": "date"}})
    assert [m["type"] for m in sent] == ["config"]


def test_the_query_log_records_who_asked(app_env):
    import asyncio

    app, _, store = app_env
    seed_cache(store)
    with TestClient(app) as client:
        client.get("/api/videos", headers={"X-Wall-User": "A@B.com"})
    assert asyncio.run(querylog.read_all(store))[0]["user"] == "a@b.com"


def test_the_html_routes_are_gone(app_env):
    """The frontend is served by the Worker's asset binding now, not FastAPI."""
    app, _, _ = app_env
    with TestClient(app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/config").status_code == 404
```

Add these two helpers next to `seed_cache`:

```python
def seed_query(store, query: str) -> str:
    """Put a query in the shared cache so the server will honour it."""
    params = youtube.build_params(query, "relevance", "any", "moderate", "en")
    asyncio.run(
        cache.write(
            store,
            params,
            [{"video_id": f"q{i:03d}", "title": "T", "channel": "C"} for i in range(50)],
        )
    )
    return query


def _record(sink):
    async def broadcast(self, message):
        sink.append(message)

    return broadcast


def _returns(value):
    async def generate(*args, **kwargs):
        return value

    return generate
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL — `create_app() got an unexpected keyword argument 'store'`

- [ ] **Step 3: Add the new setting**

```python
# ytmatrix/settings.py -- add to Settings
    # Google's project-wide daily allowance. Deliberately not in config.yaml:
    # every wall spends from the same API key, and config is shared and
    # editable, so this ceiling has to sit somewhere no user can raise it.
    global_daily_units: int = 10_000
```

- [ ] **Step 4: Make `videos_message` a pure formatter**

It calls `budget.spent(cache_dir)` at `server.py:230`, which became `async` in Task 3. Rather than make the whole formatter async for one number, take it as a parameter — it is otherwise pure.

```python
def videos_message(
    config: Config, resolved: dict, query: str, selection: dict, units_spent_today: int
) -> dict:
    items = resolved["items"]
    note = resolved["note"] or ("no_results" if not items else None)
    return {
        "type": "videos",
        "query": query,
        "video_ids": selection["slots"],
        "reserves": selection["reserves"],
        "titles": {item["video_id"]: item["title"] for item in items},
        "from_cache": resolved["from_cache"],
        "note": note,
        "static_relaxed": selection["relaxed"],
        "units_spent_today": units_spent_today,
        "daily_limit_units": config.quota.daily_limit_units,
    }
```

Thread the global cap into `resolve_videos`, whose budget check must honour it too:

```python
async def resolve_videos(
    config: Config,
    store: Store,
    api_key: str,
    query: str | None = None,
    *,
    global_limit_units: int = budget.DAILY_QUOTA_UNITS,
) -> dict:
    params = search_params_for(config, query)

    items = await cache.read(store, params, config.cache.ttl_hours)
    if items is not None:
        return {"items": items, "from_cache": True, "note": None}

    # Checked before the call, not after: the point is to not spend the unit.
    if await budget.would_exceed(
        store, config.quota.daily_limit_units, global_limit_units=global_limit_units
    ):
        stale = await cache.read(store, params, config.cache.ttl_hours, allow_stale=True)
        if stale is not None:
            return {"items": stale, "from_cache": True, "note": "budget_exceeded_stale"}
        ...  # the raise below is unchanged
```

- [ ] **Step 5: Rewrite `create_app`**

Imports first — `server.py` needs `Request` from FastAPI, `Store`, and `DEFAULT_CONFIG_PATH`; `wallstate` comes out of the `from ytmatrix import (...)` block:

```python
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from ytmatrix.config import DEFAULT_CONFIG_PATH, Config, load_config, merge_config, save_config
from ytmatrix.store import Store
```

```python
def create_app(
    store: Store,
    settings: Settings,
    *,
    default_config_path: Path = DEFAULT_CONFIG_PATH,
) -> FastAPI:
    app = FastAPI(title="yt matrix")
    manager = ConnectionManager()

    def user_of(scope) -> str:
        """Who is asking, for the query log and nothing else.

        Set by the Worker after it validated the Access JWT. Nothing branches
        on it: config is shared and the current query lives in the caller's
        own browser, so the server has no per-user state to look up.
        """
        return (scope.headers.get("x-wall-user") or "").strip().lower()

    async def shared_config() -> Config:
        return await load_config(store, default_path=default_config_path)

    async def usable_query(config: Config, query: str | None) -> str:
        """Which query to actually put on the wall.

        A query supplied by the browser is honoured only if the shared cache
        already has it -- stale included. The browser replays whatever is in
        its localStorage on every load and every WebSocket reconnect, and a
        cache miss there must never become a 100-unit search: spending is the
        New query button's job and nothing else's (gotcha 2). An unknown query
        silently falls back to the shared config query.

        Stale counts as known on purpose. Expiry must not quietly move
        somebody's wall back to the shared query -- the ids are the same ones
        they were already watching.
        """
        if not query:
            return config.query
        params = search_params_for(config, query)
        cached = await cache.read(store, params, config.cache.ttl_hours, allow_stale=True)
        return query if cached is not None else config.query

    async def videos_for(
        config: Config,
        query: str,
        *,
        source: str,
        email: str,
        prompt: str | None = None,
    ) -> dict:
        try:
            resolved = await resolve_videos(
                config,
                store,
                settings.youtube_api_key,
                query,
                global_limit_units=settings.global_daily_units,
            )
        except BudgetExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except youtube.QuotaExceededError as exc:
            raise HTTPException(
                status_code=503,
                detail="YouTube API daily quota exceeded and no cached results are available.",
            ) from exc
        except youtube.SearchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        video_ids = [item["video_id"] for item in resolved["items"]]
        selection = await select_videos(config, video_ids, store, settings.youtube_api_key)
        message = videos_message(config, resolved, query, selection, await budget.spent(store))

        # Every resolution is logged, including plain reloads: the log is a
        # record of what was on the wall and when, not just of what was newly
        # searched. `from_cache` distinguishes the two.
        await querylog.append(
            store,
            querylog.build_entry(
                query=query,
                source=source,
                video_ids=message["video_ids"],
                titles=message["titles"],
                from_cache=message["from_cache"],
                units_spent_today=message["units_spent_today"],
                reserves=len(message["reserves"]),
                static_relaxed=message["static_relaxed"],
                prompt=prompt,
                note=message["note"],
            ),
            email=email,
        )
        return message
```

Routes:

```python
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/api/config")
    async def get_config() -> dict:
        return (await shared_config()).model_dump(mode="json")

    @app.put("/api/config")
    async def put_config(payload: dict) -> dict:
        previous = await shared_config()
        try:
            # Merged, not replaced: an omitted section means "leave it alone".
            new_config = Config.model_validate(
                merge_config(previous.model_dump(mode="json"), payload)
            )
        except ValidationError as exc:
            # include_context=False matters -- see gotcha 10.
            raise HTTPException(
                status_code=422,
                detail=exc.errors(include_url=False, include_context=False),
            ) from exc

        await save_config(new_config, store)

        # Config only. The server cannot broadcast a video set any more: it
        # does not know what query any given browser is watching. Each client
        # decides for itself whether the change means it has to refetch --
        # see needsRefetch() in grid-logic.js.
        await manager.broadcast(
            {"type": "config", "config": new_config.model_dump(mode="json")}
        )
        return {"status": "ok"}

    @app.get("/api/videos")
    async def get_videos(request: Request, query: str | None = None) -> dict:
        config = await shared_config()
        chosen = await usable_query(config, query)
        return await videos_for(
            config,
            chosen,
            source="client" if query and chosen == query else "config",
            email=user_of(request),
        )

    @app.post("/api/new-query")
    async def new_query(request: Request, payload: dict | None = None) -> dict:
        """Invent a fresh query with Gemini and return it to the caller.

        Never called implicitly: only the New query button, ?new=true, or the
        prompt box. A generated query is a cache miss by definition and costs
        100 units.

        The result is returned, not broadcast. Config is shared but walls are
        not -- the caller stores this query in its own localStorage and
        nobody else's wall moves.
        """
        payload = payload or {}
        raw_prompt = payload.get("prompt")
        # Whitespace-only is no prompt at all -- normalise to None so "manual"
        # never gets recorded for an empty box.
        prompt = raw_prompt.strip() or None if isinstance(raw_prompt, str) else None

        config = await shared_config()
        if not config.query_generation.enabled:
            raise HTTPException(status_code=409, detail="query_generation.enabled is false")
        if not settings.gemini_api_key:
            raise HTTPException(
                status_code=503, detail="GEMINI_API_KEY is not set; cannot generate a query"
            )
        # Refuse before calling Gemini, not after: a generated query is a cache
        # miss by definition, so generating one we cannot then search for wastes
        # a Gemini call and leaves the wall unchanged anyway.
        if await budget.would_exceed(
            store,
            config.quota.daily_limit_units,
            global_limit_units=settings.global_daily_units,
        ):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"daily search budget of {config.quota.daily_limit_units} units is spent "
                    f"({await budget.spent(store)} used); keeping the current query."
                ),
            )

        # The avoid-list lives in the caller's browser now, so it arrives on
        # the request rather than being read from disk.
        raw_history = payload.get("history")
        history = [str(q) for q in raw_history] if isinstance(raw_history, list) else []

        try:
            query = await gemini.generate_query(
                config.query_generation.theme,
                history[-config.query_generation.avoid_repeats :],
                config.query_generation.model,
                settings.gemini_api_key,
                instruction=prompt,
            )
        except gemini.QueryGenerationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return await videos_for(
            config,
            query,
            source="manual" if prompt else "generated",
            email=user_of(request),
            prompt=prompt,
        )
```

The `/api/cache-status` and `/api/content-box/{video_id}` routes keep their shape; swap `cache_dir` for `store` and `await` the cache calls. The WebSocket endpoint is unchanged.

- [ ] **Step 6: Delete the HTML and static routes**

Remove `player_page`, `config_page`, the `STATIC_DIR` constant, the `app.mount("/static", ...)` block, and the now-unused `FileResponse` / `StaticFiles` imports.

- [ ] **Step 7: Update `main.py` for local development**

```python
    app = create_app(
        store=FileStore(Path(os.environ.get("YTMATRIX_CACHE_DIR", REPO_ROOT / "cache"))),
        settings=settings,
        default_config_path=Path(
            os.environ.get("YTMATRIX_CONFIG_PATH", REPO_ROOT / "config.yaml")
        ),
    )

    # Local development only. In production the Worker's asset binding serves
    # these and the container never sees the request.
    dist = REPO_ROOT / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dist")
```

Drop the `log_dir` argument and the `YTMATRIX_LOG_DIR` lookup — the log lives in the store now.

- [ ] **Step 8: Convert the existing `test_server.py` suite**

~1000 lines, but nearly all of it goes through one fixture and one helper.

```python
# tests/test_server.py
import asyncio

from ytmatrix.store import FileStore


def seed_cache(store, count: int = 50, prefix: str = "vid") -> list[str]:
    """Still synchronous: the tests around it drive a sync TestClient, and
    there is no running loop to conflict with."""
    params = youtube.build_params("golden cover", "relevance", "any", "moderate", "en")
    ids = [f"{prefix}{i:03d}" for i in range(count)]
    asyncio.run(
        cache.write(
            store,
            params,
            [{"video_id": v, "title": f"T{v}", "channel": "C"} for v in ids],
        )
    )
    return ids


@pytest.fixture
def app_env(tmp_path):
    default_path = tmp_path / "config.yaml"
    default_path.write_text(yaml.safe_dump(VALID))
    store = FileStore(tmp_path / "store")
    settings = Settings(youtube_api_key="TEST_KEY")
    app = create_app(store=store, settings=settings, default_config_path=default_path)
    return app, default_path, store
```

The fixture keeps its three-tuple shape, so `app, _, _ = app_env` unpacking is untouched. Then sweep the file:

1. `cache_dir` → `store` at every call site (the third element is now a `FileStore`, not a `Path`).
2. Any direct `budget.*`, `cache.*`, `querylog.*` call inside a sync test gets wrapped in `asyncio.run(...)`.
3. `querylog.read_all(log_dir)` → `asyncio.run(querylog.read_all(store))`; the separate `log_dir` is gone.
4. Assertions on `config_path.read_text()` become `client.get("/api/config")` or `asyncio.run(load_config(store, default_path=default_path))`.
5. Delete any test asserting that a config change broadcasts videos — replaced by `test_a_config_change_broadcasts_config_only` in Step 1. Delete tests asserting a persisted server-side query survives a restart; that behaviour moves to Task 6b's node tests.

Run after each group rather than at the end: `uv run pytest tests/test_server.py -x -q`

- [ ] **Step 9: Run the full suite**

Run: `uv run pytest tests/ -v`
Expected: PASS

- [ ] **Step 10: Lint and commit**

```bash
uv run ruff check . && uv run ruff format .
git add ytmatrix/server.py ytmatrix/settings.py ytmatrix/main.py tests/test_server.py
git commit -m "feat: stateless server; client supplies the query"
```

---

### Task 6b: The browser remembers the wall

`localStorage` takes over what `wallstate.py` used to do. Same two facts — current query, and the history that steers Gemini away from repeats — now per browser rather than per installation.

**Files:**
- Create: `static/wallstate.js`, `static/wallstate.test.mjs`
- Modify: `static/grid-logic.js` (add `needsRefetch`), `static/grid-logic.test.mjs`
- Modify: `static/player.js`

**Interfaces:**
- Consumes: `GET /api/videos?query=`, `POST /api/new-query {prompt?, history?}` from Task 6.
- Produces: `loadQuery(storage?)`, `saveQuery(query, storage?)`, `clearQuery(storage?)`, `loadHistory(storage?)`, `pushHistory(query, storage?)`, `MAX_HISTORY`. `needsRefetch(previous, next)` from `grid-logic.js`.

- [ ] **Step 1: Write the failing test**

```js
// static/wallstate.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_HISTORY,
  clearQuery,
  loadHistory,
  loadQuery,
  pushHistory,
  saveQuery,
} from "./wallstate.js";

// node has no localStorage, and injecting one keeps these tests honest about
// the fact that every read can fail -- Safari in private mode throws on write.
function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
  };
}

test("a fresh browser has no query", () => {
  assert.equal(loadQuery(fakeStorage()), null);
});

test("a saved query is read back", () => {
  const storage = fakeStorage();
  saveQuery("케이팝 커버", storage);
  assert.equal(loadQuery(storage), "케이팝 커버");
});

test("clearing removes the query", () => {
  const storage = fakeStorage();
  saveQuery("q", storage);
  clearQuery(storage);
  assert.equal(loadQuery(storage), null);
});

test("a blank query saves as nothing", () => {
  const storage = fakeStorage();
  saveQuery("   ", storage);
  assert.equal(loadQuery(storage), null);
});

test("a fresh browser has an empty history", () => {
  assert.deepEqual(loadHistory(fakeStorage()), []);
});

test("history accumulates oldest first", () => {
  const storage = fakeStorage();
  pushHistory("a", storage);
  pushHistory("b", storage);
  assert.deepEqual(loadHistory(storage), ["a", "b"]);
});

test("history is capped so it cannot grow forever", () => {
  const storage = fakeStorage();
  for (let i = 0; i < MAX_HISTORY + 50; i += 1) pushHistory(`q${i}`, storage);
  const history = loadHistory(storage);
  assert.equal(history.length, MAX_HISTORY);
  assert.equal(history.at(-1), `q${MAX_HISTORY + 49}`);
});

test("corrupt stored history reads as empty rather than throwing", () => {
  assert.deepEqual(loadHistory(fakeStorage({ "ytmatrix.history": "not json" })), []);
});

test("stored history of the wrong shape reads as empty", () => {
  assert.deepEqual(loadHistory(fakeStorage({ "ytmatrix.history": '{"a":1}' })), []);
});

test("a storage that throws on write does not break the wall", () => {
  const hostile = {
    getItem: () => null,
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
    removeItem: () => {},
  };
  assert.doesNotThrow(() => saveQuery("q", hostile));
  assert.doesNotThrow(() => pushHistory("q", hostile));
});

test("a missing storage does not break the wall", () => {
  assert.equal(loadQuery(null), null);
  assert.deepEqual(loadHistory(null), []);
  assert.doesNotThrow(() => saveQuery("q", null));
});
```

Add to `static/grid-logic.test.mjs`:

```js
test("no change needs no refetch", () => {
  const config = { query: "a", search: { order: "relevance" }, grid: { cols: 4, rows: 2 } };
  assert.equal(needsRefetch(config, config), false);
});

test("a changed search parameter needs a refetch", () => {
  const before = { query: "a", search: { order: "relevance" }, grid: { cols: 4, rows: 2 } };
  const after = { query: "a", search: { order: "date" }, grid: { cols: 4, rows: 2 } };
  assert.equal(needsRefetch(before, after), true);
});

test("a changed cell count needs a refetch", () => {
  const before = { query: "a", search: {}, grid: { cols: 4, rows: 2 } };
  const after = { query: "a", search: {}, grid: { cols: 5, rows: 2 } };
  assert.equal(needsRefetch(before, after), true);
});

test("a cosmetic change needs no refetch", () => {
  const before = { query: "a", search: {}, grid: { cols: 4, rows: 2 }, playback: { loop: true } };
  const after = { query: "a", search: {}, grid: { cols: 4, rows: 2 }, playback: { loop: false } };
  assert.equal(needsRefetch(before, after), false);
});

test("an edited config query overrides whatever the browser was watching", () => {
  const before = { query: "a", search: {}, grid: { cols: 4, rows: 2 } };
  const after = { query: "b", search: {}, grid: { cols: 4, rows: 2 } };
  assert.equal(overridesStoredQuery(before, after), true);
});

test("an unchanged config query leaves the browser's own query alone", () => {
  const config = { query: "a", search: { order: "date" }, grid: { cols: 4, rows: 2 } };
  assert.equal(overridesStoredQuery(config, { ...config, search: { order: "relevance" } }), false);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node --test 'static/*.test.mjs'`
Expected: FAIL — `Cannot find module './wallstate.js'`

(Note the quotes: `node --test static/` resolves the directory as a module on Node 23 and fails.)

- [ ] **Step 3: Write `static/wallstate.js`**

```js
// What this browser is watching, and what it has already seen.
//
// This used to be ytmatrix/wallstate.py, one file for the whole installation.
// It lives here now because config is shared but walls are not: everyone edits
// the same config document, and everyone still gets their own query. Moving it
// into localStorage is what makes that true without the server holding a
// single scrap of per-user state.
//
// Every access is defensive. Storage is missing in some embedding contexts,
// throws on write in Safari's private mode, and can contain anything a
// previous version or a curious user left behind -- and none of that is
// allowed to stop the wall from starting.

const QUERY_KEY = "ytmatrix.query";
const HISTORY_KEY = "ytmatrix.history";

// Bounds the history that steers Gemini away from repeats. Without a cap this
// grows forever in a browser that is never cleared.
export const MAX_HISTORY = 200;

function defaultStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    // Accessing localStorage itself throws when storage is blocked.
    return null;
  }
}

function read(storage, key) {
  const target = storage === undefined ? defaultStorage() : storage;
  try {
    return target?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function write(storage, key, value) {
  const target = storage === undefined ? defaultStorage() : storage;
  try {
    if (value === null) target?.removeItem(key);
    else target?.setItem(key, value);
  } catch {
    // A full or blocked quota costs us the memory, not the wall.
  }
}

export function loadQuery(storage) {
  const value = read(storage, QUERY_KEY);
  return value && value.trim() ? value : null;
}

export function saveQuery(query, storage) {
  const trimmed = typeof query === "string" ? query.trim() : "";
  write(storage, QUERY_KEY, trimmed || null);
}

export function clearQuery(storage) {
  write(storage, QUERY_KEY, null);
}

export function loadHistory(storage) {
  const raw = read(storage, HISTORY_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

export function pushHistory(query, storage) {
  const trimmed = typeof query === "string" ? query.trim() : "";
  if (!trimmed) return;
  const history = [...loadHistory(storage), trimmed].slice(-MAX_HISTORY);
  write(storage, HISTORY_KEY, JSON.stringify(history));
}
```

- [ ] **Step 4: Add the config classifiers to `grid-logic.js`**

Append below `classifyConfigChange`:

```js
// Mirrors what the server used to decide for everyone. It cannot any more --
// it does not know what query any given browser is watching -- so each client
// works it out from the config broadcast itself.
const SEARCH_KEYS = ["query", "search"];

export function needsRefetch(previous, next) {
  if (!previous) return true;
  if (SEARCH_KEYS.some((key) => differs(previous, next, key))) return true;
  return cellCount(previous.grid) !== cellCount(next.grid);
}

// Typing a query by hand on the config page is an override: it must beat
// whatever Gemini last handed this browser, or the config page would appear
// to do nothing for anyone who had ever pressed New query.
export function overridesStoredQuery(previous, next) {
  return Boolean(previous) && differs(previous, next, "query");
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `node --test 'static/*.test.mjs'`
Expected: PASS

- [ ] **Step 6: Wire it into `player.js`**

Add the import beside the existing ones:

```js
import { clearQuery, loadHistory, loadQuery, pushHistory, saveQuery } from "./wallstate.js";
import { needsRefetch, overridesStoredQuery } from "./grid-logic.js";
```

`requestNewQuery` sends the history up and stores what comes back:

```js
    const response = await fetch("/api/new-query", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        ...(prompt ? { prompt } : {}),
        // The avoid-list lives here now, so it has to travel with the request.
        history: loadHistory(),
      }),
    });
    if (response.ok) {
      const message = await response.json();
      // Remember it before applying: a reload must land on the same wall, and
      // replaying a stored query costs nothing (the server serves it from
      // cache or falls back -- it never re-searches).
      saveQuery(message.query);
      pushHistory(message.query);
      applyVideos(message);
      return true;
    }
```

`resync` asks for this browser's own query:

```js
  const stored = loadQuery();
  const videosResponse = await fetch(
    stored ? `/api/videos?query=${encodeURIComponent(stored)}` : "/api/videos",
  );
  if (!videosResponse.ok) {
    const body = await videosResponse.json().catch(() => ({}));
    setStatus(body.detail ?? `error ${videosResponse.status}`, "error");
    return;
  }
  const message = await videosResponse.json();
  // Store ONLY what New query hands us -- never what /api/videos served.
  //
  // A browser that has never pressed New query must keep sending no query at
  // all, so the server uses the shared config query on its normal path where
  // cache.ttl_hours still applies. Storing whatever came back would put the
  // config query onto the client path too, which is served cache-only and
  // never re-searches -- the wall would then never refresh on its own, and
  // `source: "client"` in the query log would stop distinguishing anything.
  //
  // The one thing worth writing here is a deletion: if we sent a stored query
  // and the server answered with a different one, our query has aged out of
  // the shared cache and is gone. Clearing lets this browser fall back to the
  // shared query cleanly. Storing the fallback instead would pin it as a
  // client query forever, which is the same bug wearing a different hat.
  if (stored && message.query !== stored) clearQuery();
  applyVideos(message);
```

The config handler classifies for itself instead of waiting for a videos broadcast:

```js
connectSocket({
  onReconnect: resync,
  onMessage: (message) => {
    if (message.type === "config") {
      const previous = config;
      const change = classifyConfigChange(previous, message.config);
      config = message.config;
      if (change === "rebuild") rebuild();
      else if (change === "in-place") applyInPlace();
      // Someone typed a query on the config page: that is an explicit
      // override and it beats whatever this browser had stored.
      if (overridesStoredQuery(previous, message.config)) clearQuery();
      if (needsRefetch(previous, message.config)) resync();
    } else if (message.type === "videos") {
      applyVideos(message);
    }
  },
});
```

- [ ] **Step 7: Verify in a browser**

Run `./run.sh`, open `https://localhost:8444/`. Check:
- Press New query; reload. The same query comes back and the budget counter does **not** move.
- Open a second browser profile. It shows the config query, not the first profile's.
- Press New query in one profile. The other profile's wall does not change.
- Edit `grid.cols` on the config page. Both profiles re-lay-out, each keeping its own query.
- Type a query into the config page's query field. Both profiles switch to it.

- [ ] **Step 8: Run the browser smoke test**

Run: `uv run pytest tests/test_player_smoke.py -m browser -v`
Expected: PASS — the only thing that catches gotchas 12 and 13. Update any assertion that depended on the server remembering a query.

- [ ] **Step 9: Commit**

```bash
git add static/wallstate.js static/wallstate.test.mjs static/grid-logic.js \
        static/grid-logic.test.mjs static/player.js tests/test_player_smoke.py
git commit -m "feat: the browser remembers its own wall"
```

---

### Task 7: Build the static frontend bundle

The HTML references `/static/player.js` and `/static/config.js`, so keeping that path in `dist/` means **no HTML edits at all** — the frontend ships byte-identical.

**Files:**
- Create: `scripts/build-dist.sh`
- Modify: `.gitignore` (add `dist/`)
- Modify: `run.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: `dist/index.html` (from `static/player.html`), `dist/config.html`, `dist/static/*.js`.

- [ ] **Step 1: Write the build script**

```bash
#!/usr/bin/env bash
# Assemble the static bundle the Worker serves.
#
# The HTML asks for /static/player.js, so the JS keeps that path and the HTML
# needs no rewriting. player.html becomes index.html because Workers static
# assets serve index.html at "/", and config.html is served at "/config" by
# the default .html-stripping behaviour -- which is exactly what the existing
# <a href="/config"> and <a href="/"> links already expect.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
dist="$root/dist"

rm -rf "$dist"
mkdir -p "$dist/static"

cp "$root/static/player.html" "$dist/index.html"
cp "$root/static/config.html" "$dist/config.html"
# *.js only: grid-logic.test.mjs is a node test and must not ship.
cp "$root"/static/*.js "$dist/static/"

echo "built $dist"
```

- [ ] **Step 2: Make it executable and run it**

```bash
chmod +x scripts/build-dist.sh
./scripts/build-dist.sh
```

Expected: `built /Users/jburke/Dropbox/eutamias-dev/bardo/yt/dist`

- [ ] **Step 3: Verify the bundle contents**

Run: `find dist -type f | sort`
Expected exactly:
```
dist/config.html
dist/index.html
dist/static/backoff.js
dist/static/config.js
dist/static/grid-logic.js
dist/static/player.js
dist/static/socket.js
dist/static/wallstate.js
```
Confirm `grid-logic.test.mjs` and `wallstate.test.mjs` are both absent — they are `.mjs`, so the `*.js` glob already excludes them, but check rather than assume.

- [ ] **Step 4: Gitignore the bundle and build it in run.sh**

Add `dist/` to `.gitignore`. In `run.sh`, add before the uvicorn/`uv run` line:

```bash
"$(dirname "$0")/scripts/build-dist.sh"
```

- [ ] **Step 5: Verify the local wall still works**

Run: `./run.sh`, open `https://localhost:8444/`, confirm the grid renders and `/config` loads.
Expected: identical behaviour to before this task.

- [ ] **Step 6: Run the browser smoke test**

Run: `uv run pytest tests/test_player_smoke.py -m browser -v`
Expected: PASS — this is the only thing that catches gotchas 12 and 13.

- [ ] **Step 7: Commit**

```bash
git add scripts/build-dist.sh .gitignore run.sh
git commit -m "feat: build the static frontend bundle"
```

---

### Task 8: Containerize

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `ytmatrix/container.py`

**Interfaces:**
- Consumes: `create_app` (Task 6), `R2Store` / `r2_client` (Task 1).
- Produces: an image listening on `$PORT` (default 8080), plain HTTP.

- [ ] **Step 1: Write the container entry point**

```python
# ytmatrix/container.py
"""Entry point inside the Cloudflare container.

Nothing here generates or presents a certificate: Cloudflare terminates TLS at
the edge and the Worker reaches this process over plain HTTP on a private
port. `main.py` remains the local-development entry point and keeps its
self-signed cert -- gotcha 11 still applies there, and only there.

Persistence is R2 rather than disk. The container's filesystem is a fresh copy
of the image on every start, so anything written to it is gone by the next
request that wakes the instance.
"""

from __future__ import annotations

import os

import uvicorn

from ytmatrix.server import create_app
from ytmatrix.settings import Settings
from ytmatrix.store import R2Store, r2_client


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        # Fail at startup, not at the first search -- the same rule the
        # YouTube key follows in settings.py.
        raise RuntimeError(f"{name} is not set")
    return value


def main() -> None:
    settings = Settings()
    store = R2Store(
        r2_client(
            account_id=_required("R2_ACCOUNT_ID"),
            access_key_id=_required("R2_ACCESS_KEY_ID"),
            secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
        ),
        bucket=_required("R2_BUCKET"),
    )
    uvicorn.run(
        create_app(store=store, settings=settings),
        host="0.0.0.0",  # noqa: S104 - the container's port is not public
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write the Dockerfile**

```dockerfile
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so an edit to ytmatrix/ does not re-resolve the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY ytmatrix/ ./ytmatrix/
# The committed config is the template every new user's wall starts from.
COPY config.yaml ./config.yaml
RUN uv sync --frozen --no-dev

ENV PORT=8080
# Required for `wrangler dev` to reach the container locally.
EXPOSE 8080

CMD ["uv", "run", "--no-dev", "python", "-m", "ytmatrix.container"]
```

- [ ] **Step 3: Write .dockerignore**

```
.git
.venv
dist
cache
logs
runtime
docs
tests
static
node_modules
__pycache__
*.pyc
.env
```

- [ ] **Step 4: Build the image**

Run: `docker build -t yt-matrix-test .`
Expected: builds clean. If Pillow fails to find a wheel, add `libjpeg62-turbo zlib1g` via `apt-get` — but the manylinux wheels should need nothing.

- [ ] **Step 5: Verify it starts and refuses to run without R2 config**

```bash
docker run --rm -e YOUTUBE_API_KEY=x yt-matrix-test
```
Expected: exits with `RuntimeError: R2_ACCOUNT_ID is not set` — proving the fail-at-startup rule holds.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile .dockerignore ytmatrix/container.py
git commit -m "feat: containerize for Cloudflare"
```

---

### Task 9: The Worker

**Files:**
- Create: `worker/index.ts`
- Create: `wrangler.jsonc`
- Create: `package.json`

**Interfaces:**
- Consumes: `dist/` (Task 7), the container image (Task 8).
- Produces: a deployed Worker binding `WALL` to the `Wall` container class.

- [ ] **Step 1: Create package.json and install dependencies**

```json
{
  "name": "yt-matrix",
  "private": true,
  "type": "module",
  "scripts": {
    "build": "./scripts/build-dist.sh",
    "dev": "npm run build && wrangler dev",
    "deploy": "npm run build && wrangler deploy"
  },
  "devDependencies": {
    "wrangler": "^4",
    "typescript": "^5"
  },
  "dependencies": {
    "@cloudflare/containers": "^0.0.40",
    "jose": "^5"
  }
}
```

Run: `npm install`

- [ ] **Step 2: Write the Worker**

```ts
// worker/index.ts
import { Container } from "@cloudflare/containers";
import { env } from "cloudflare:workers";
import { createRemoteJWKSet, jwtVerify } from "jose";

/**
 * One shared container for the whole installation.
 *
 * It can be shared because the server holds no per-user state: config is
 * global and each browser remembers its own query in localStorage. That makes
 * this a single mostly-idle process rather than one per user -- playback never
 * touches it, so ten walls cost about what one does.
 *
 * If per-user server state ever comes back, this is the line that changes:
 * `getByName(email)` instead of a constant gives every user their own
 * instance. That is a product decision, not a refactor.
 */
export class Wall extends Container {
  defaultPort = 8080;
  // Long enough that a wall left playing does not cold-start every time the
  // operator reaches for the config page. Playback itself never touches this
  // process -- video streams from YouTube straight to the browser -- so a
  // sleeping instance does not interrupt a running wall.
  sleepAfter = "20m";
  envVars = {
    YOUTUBE_API_KEY: env.YOUTUBE_API_KEY,
    GEMINI_API_KEY: env.GEMINI_API_KEY,
    R2_ACCOUNT_ID: env.R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID: env.R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY: env.R2_SECRET_ACCESS_KEY,
    R2_BUCKET: env.R2_BUCKET,
    YTMATRIX_GLOBAL_DAILY_UNITS: env.YTMATRIX_GLOBAL_DAILY_UNITS,
  };
}

// createRemoteJWKSet caches and refreshes the key set itself; building a new
// one per request would refetch Cloudflare's certs on every call.
let jwks: ReturnType<typeof createRemoteJWKSet> | undefined;
function keySet(teamDomain: string) {
  jwks ??= createRemoteJWKSet(new URL(`${teamDomain}/cdn-cgi/access/certs`));
  return jwks;
}

async function verifiedEmail(request: Request, env: Env): Promise<string | null> {
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, keySet(env.ACCESS_TEAM_DOMAIN), {
      issuer: env.ACCESS_TEAM_DOMAIN,
      audience: env.ACCESS_POLICY_AUD,
    });
    const email = String(payload.email ?? "").trim().toLowerCase();
    return email || null;
  } catch {
    return null;
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // Unauthenticated on purpose: this is the Worker's own liveness, and it
    // must answer before Access is configured or the first deploy cannot be
    // checked at all.
    if (url.pathname === "/healthz") {
      return Response.json({ status: "ok" });
    }

    const email = await verifiedEmail(request, env);
    if (!email) {
      return new Response("Unauthorized", { status: 401 });
    }

    // Overwrite rather than merge. The container writes this straight into the
    // query log, so it must come from the verified token on this line and
    // never from anything the client sent.
    const headers = new Headers(request.headers);
    headers.delete("x-wall-user");
    headers.set("X-Wall-User", email);

    // A constant name, so every user lands on the same instance. WebSocket
    // upgrades forward through this same call -- the Container class proxies
    // them to the container's port without special handling.
    return env.WALL.getByName("wall").fetch(new Request(request, { headers }));
  },
} satisfies ExportedHandler<Env>;

interface Env {
  WALL: DurableObjectNamespace<Wall>;
  ACCESS_TEAM_DOMAIN: string;
  ACCESS_POLICY_AUD: string;
  YOUTUBE_API_KEY: string;
  GEMINI_API_KEY: string;
  R2_ACCOUNT_ID: string;
  R2_ACCESS_KEY_ID: string;
  R2_SECRET_ACCESS_KEY: string;
  R2_BUCKET: string;
  YTMATRIX_GLOBAL_DAILY_UNITS: string;
}
```

- [ ] **Step 3: Write wrangler.jsonc**

```jsonc
{
  "$schema": "node_modules/wrangler/config-schema.json",
  "name": "yt-matrix",
  "main": "worker/index.ts",
  "compatibility_date": "2026-08-01",
  "compatibility_flags": ["nodejs_compat"],

  "assets": {
    "directory": "./dist",
    "binding": "ASSETS",
    // Everything else is served straight from dist/ without invoking the
    // Worker. Access still protects those files -- it runs at the edge,
    // before any of this.
    "run_worker_first": ["/api/*", "/ws", "/healthz"]
  },

  "containers": [
    {
      "class_name": "Wall",
      "image": "./Dockerfile",
      // Routing pins everyone to one instance; the headroom is for rolling
      // deploys, when the old and new instances overlap briefly.
      "max_instances": 3,
      "instance_type": "basic"
    }
  ],

  "durable_objects": {
    "bindings": [{ "name": "WALL", "class_name": "Wall" }]
  },

  "migrations": [{ "tag": "v1", "new_sqlite_classes": ["Wall"] }],

  "vars": {
    "ACCESS_TEAM_DOMAIN": "https://REPLACE-ME.cloudflareaccess.com",
    "ACCESS_POLICY_AUD": "REPLACE-ME",
    "R2_BUCKET": "yt-matrix",
    "YTMATRIX_GLOBAL_DAILY_UNITS": "10000"
  },

  "routes": [{ "pattern": "yt.bardo.jburke.io", "custom_domain": true }]
}
```

- [ ] **Step 4: Typecheck**

Run: `npx wrangler types && npx tsc --noEmit`
Expected: no errors. If `run_worker_first` is rejected by the schema, move it to the top level of the config — the option has lived in both places across wrangler versions; the generated schema is authoritative.

- [ ] **Step 5: Add node artifacts to .gitignore**

```
node_modules/
.wrangler/
worker-configuration.d.ts
```

- [ ] **Step 6: Commit**

```bash
git add worker/ wrangler.jsonc package.json package-lock.json .gitignore
git commit -m "feat: Worker with Access auth and container routing"
```

---

### Task 10: Deploy to yt.bardo.jburke.io

**Files:**
- Create: `docs/DEPLOY.md`
- Modify: `CLAUDE.md` (new gotchas + file table rows)

- [ ] **Step 1: Write `docs/DEPLOY.md`**

````markdown
# Deploying to yt.bardo.jburke.io

## Prerequisites

- The `jburke.io` zone on Cloudflare (nameservers pointed at Cloudflare).
- A **Workers Paid** plan — Containers are not on the free tier.
- Docker running locally (`wrangler` builds the image and pushes it).
- Node 20+ and `npm install` already run.

## 1. Create the R2 bucket

```bash
npx wrangler r2 bucket create yt-matrix
```

Then create an R2 API token: **Cloudflare dashboard → R2 → API → Manage API
Tokens → Create API Token**, permission **Object Read & Write**, scoped to the
`yt-matrix` bucket. Save the Access Key ID and Secret Access Key — the secret
is shown once.

Your account ID is in the dashboard URL and on the R2 overview page.

## 2. Set the secrets

```bash
npx wrangler secret put YOUTUBE_API_KEY
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put R2_ACCOUNT_ID
npx wrangler secret put R2_ACCESS_KEY_ID
npx wrangler secret put R2_SECRET_ACCESS_KEY
```

These are Worker secrets; the `Wall` class forwards them into the container as
environment variables. They never appear in `wrangler.jsonc`.

## 3. First deploy

```bash
npm run deploy
```

This builds `dist/`, builds and pushes the container image, and — because of
the `custom_domain` route — creates the `yt.bardo.jburke.io` DNS record and
its certificate automatically. Certificate issuance takes a minute or two.

Verify the Worker is alive (this route is deliberately unauthenticated):

```bash
curl https://yt.bardo.jburke.io/healthz
# {"status":"ok"}
```

At this point every other path returns 401 — that is correct, Access is not
configured yet.

## 4. Put Cloudflare Access in front

**Zero Trust dashboard → Access → Applications → Add an application →
Self-hosted.**

- **Application name:** yt matrix
- **Session duration:** 24 hours (or whatever suits — this is how often people
  re-authenticate)
- **Public hostname:** `yt.bardo.jburke.io`, path empty (the whole site)

Add a policy:

- **Policy name:** wall users
- **Action:** Allow
- **Include → Emails** — list the 5–10 addresses. (**Emails ending in
  `@your-domain`** works too if everyone shares a domain.)

Choose login methods under **Settings → Authentication** — One-time PIN needs
no setup and emails a code; Google or GitHub is smoother if everyone already
has one.

Once saved, open the application and copy its **Application Audience (AUD)
Tag**.

Your team domain is under **Settings → Custom Pages / General** and looks like
`https://yourteam.cloudflareaccess.com`.

## 5. Wire the Access values in and redeploy

Edit `wrangler.jsonc`:

```jsonc
  "vars": {
    "ACCESS_TEAM_DOMAIN": "https://yourteam.cloudflareaccess.com",
    "ACCESS_POLICY_AUD": "the-aud-tag-you-copied",
    ...
  }
```

```bash
npm run deploy
```

## 6. Verify

Open `https://yt.bardo.jburke.io/` in a fresh browser profile. You should get
the Access login page, then the wall.

Then check the three things the design actually rests on. Use two different
browser profiles signed in as two different users:

1. **Walls are separate.** Press **New query** in one. The other must not
   change.
2. **Config is shared.** Change `grid.cols` on the config page in one. Both
   re-lay-out — each keeping its own query.
3. **A reload is free.** Note `units_spent_today`, reload both walls, and
   confirm it has not moved. Each browser replays its stored query and the
   server serves it from the shared cache.

Quota is shared: `_budget.json` in the bucket climbs by 100 per search
regardless of who ran it.

## Adding and removing users

Zero Trust → Access → Applications → yt matrix → Policies → edit the email
list. No deploy needed, and nothing to clean up in R2 — a user's wall lived
only in their own browser. Their entries stay in the query log under `logs/`,
which is the point of the log.

## Costs and what drives them

Containers bill for the time an instance is **awake**, and `sleepAfter` is
20 minutes. There is exactly one instance for everybody, so the cost does not
scale with the number of users — it scales with how long *somebody* has a tab
open.

Playback never touches the server, so what keeps the instance alive is the
open WebSocket, not request volume. If that cost matters, shorten `sleepAfter`
or have the client drop the socket when the tab is hidden; a sleeping
container does not interrupt a playing wall, and the first request after it
wakes pays a few seconds of cold start.

## Local development is unchanged

`./run.sh` still runs the whole thing against a `FileStore` on
`https://localhost:8444/` with no Cloudflare account, no Access, and no R2.
Use `localhost`, never `127.0.0.1` (gotcha 11).

To exercise the Worker and container together locally:

```bash
npm run dev
```

Access is not in front of `wrangler dev`, so requests arrive with no
identity header and fall through to `settings.default_user`.
````

- [ ] **Step 2: Update CLAUDE.md**

Add to the file-by-file table:

```markdown
| `ytmatrix/store.py` | `Store` protocol plus `FileStore` (local/tests) and `R2Store` (production). Knows nothing about YouTube, config, or budgets. |
| `ytmatrix/container.py` | Entry point inside the Cloudflare container: R2-backed store, plain HTTP, no certs. |
| `worker/index.ts` | Validates the Access JWT, forwards to the shared container, serves `dist/`. |
| `static/wallstate.js` | What *this browser* is watching: current query + history in `localStorage`. |
```

Replace the `ytmatrix/wallstate.py` row — that module is gone. Then add these
gotchas:

```markdown
26. **The container has no durable disk.** Every instance starts from a fresh
    copy of the image, so nothing may rely on a file surviving a restart —
    the cache, the log, the ledger, and config all go through `Store` to R2
    instead. `FileStore` keeps local dev and the test suite on disk; do not
    "simplify" a module back to a `Path`.

27. **Config is shared; walls are not.** Everyone edits one `config.yaml` in
    R2 and a save reaches everyone. What each person is *watching* — their
    current query and the history that steers Gemini away from repeats — lives
    in their own browser (`static/wallstate.js`) and travels on the request.
    The server holds no per-user state at all. A dict keyed by user is a sign
    something has gone wrong.

28. **A client-supplied query must never spend quota.** The browser replays
    its stored query on every load *and every WebSocket reconnect* — which is
    a network hiccup, not an intent. `usable_query()` therefore honours it
    only if the shared cache already has it, stale included, and otherwise
    falls back to the config query. Making `/api/videos?query=` search on a
    miss would turn a flaky connection into a quota fire. Spending stays the
    New query button's job (gotcha 2).

    Stale counts as known deliberately: expiry must not silently move
    somebody's wall back to the shared query.

29. **The server can no longer broadcast a video set on a config change.** It
    does not know what query any browser is watching. `put_config` broadcasts
    config only; each client decides via `needsRefetch()` in `grid-logic.js`
    whether to refetch, and `overridesStoredQuery()` handles the one case
    where a config edit must beat the browser's own query — someone typing in
    the config page's query field.

30. **The container is shared and `getByName("wall")` is why.** It can be
    shared because there is no per-user server state. Passing the email there
    instead would give every user their own instance and multiply the cost;
    the email is carried only so the query log records who asked.
```

- [ ] **Step 3: Run everything one last time**

```bash
uv run pytest tests/ -v
node --test 'static/*.test.mjs'
uv run pytest tests/test_player_smoke.py -m browser -v
uv run ruff check . && uv run ruff format .
npm run typecheck
```
Expected: all pass.

`npm run typecheck` rather than a bare `npx tsc --noEmit`: `tsconfig.json` references
`worker-configuration.d.ts`, which `wrangler types` generates and `.gitignore` excludes, so the
bare command fails with `TS2688` on any fresh clone. The script generates the file first.

- [ ] **Step 4: Commit**

```bash
git add docs/DEPLOY.md CLAUDE.md
git commit -m "docs: Cloudflare deployment runbook"
```

---

## Deferred, with reasons

- **One person's own devices are now separate walls.** `localStorage` is per
  browser, not per account, so the laptop and the TV each keep their own
  query. Shared config still propagates to both over the WebSocket — remote
  control survives for config edits — but pressing **New query** on the laptop
  no longer moves the TV. That is a direct consequence of holding no per-user
  server state. Restoring it without reintroducing that state would mean
  telling each client its own email (a `/api/whoami`, or a field on
  `/api/config`) and broadcasting the videos message tagged with the
  originator, so clients could apply only their own. Not built.
- **Container→R2 latency.** Every cache read is now a network round trip
  instead of a disk read. For a grid of 8 with a scan depth of ~12 that is
  tens of sequential gets on a cold query. If the wall feels slow to change,
  batch the motion/origin lookups concurrently with `asyncio.gather` before
  reaching for anything structural.
- **A `GET /api/log` route.** `read_all` is ported and works, but nothing
  exposes it over HTTP; today it is read by tests and by whoever opens the
  bucket.
- **Dropping the WebSocket for polling.** Would let containers sleep through
  playback and cut the dominant cost, but it removes live remote control,
  which is the feature the socket exists for. Listed in `docs/DEPLOY.md` as a
  lever, not taken here.
