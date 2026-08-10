# yt — YouTube Matrix Design Spec

Status: approved for implementation planning
Date: 2026-08-10
Scope: a standalone browser prototype that plays a grid of muted YouTube
embeds drawn from a live search, with a live-editable config page. Layout
Driver integration is explicitly **not** part of this version.

## 1. Purpose

Search YouTube for a query, put the top results on screen as a matrix of
muted, simultaneously-playing embeds, and let the whole arrangement be
retuned from a second browser page while it runs.

This is the first step toward playing YouTube content on the Cafe Bardo LED
wall (`../layout-driver`), but it deliberately stops short of that. The goal
here is to learn what the YouTube embed path can and cannot do — how many
players run at once, how often embeds fail, how much YouTube chrome is
unavoidable — before committing to a wall design.

First milestone: an 8-embed matrix on the query `"golden cover"` with a
single Play button.

### Non-goals for this version

Named so they are understood as deferred, not forgotten:

- Layout Driver integration (six irregular screens, NDI capture)
- Unmuted audio, and the BlackHole loopback path that would require
- Per-cell distinct queries
- Gemini-authored queries, operator-typed queries, static query lists — all
  three are wanted eventually; v1 uses the single `query` config field

The config schema below leaves room for these. v1 does not build them.

## 2. Authentication and quota

**Service accounts do not work with the YouTube Data API v3.** YouTube
supports neither domain-wide delegation for this API nor a service account
acting on its own behalf — a service account has no channel. The correct
auth for public search is a **plain GCP API key** with the YouTube Data API
enabled. OAuth would only be needed to act *as a user* (their playlists,
uploads, subscriptions), which this project does not do.

The key lives in `.env` as `YOUTUBE_API_KEY`, which is gitignored.
`config.yaml` is committed and contains no secrets. A missing key fails at
**startup** with an explicit message, not at first search.

### Quota is the binding constraint

`search.list` costs **100 quota units** against a default of 10,000 per
day — **100 searches per day, total**. Everything in §4 follows from this.

The decisive detail: **cost is per call, not per result.** `maxResults=50`
costs exactly what `maxResults=8` costs. The design therefore always
requests 50, displays 8, and treats the other 42 as free insurance (§4.3).

## 3. Architecture

A single FastAPI process on **:8444** over HTTPS. The port avoids
`layout-driver` (8443) and `audio-snippet` (8010), so all three can run at
once. `run.sh` generates a self-signed cert on first run if none exists,
matching `layout-driver`'s pattern.

TLS is not only a stated requirement: the IFrame Player API is served over
https, and an http page embedding it is a mixed-content problem.

```
yt/
  pyproject.toml, uv.lock          # uv, matching layout-driver
  run.sh                           # cert-on-first-run, then uvicorn
  config.yaml                      # committed — no secrets
  .env / .env.example              # YOUTUBE_API_KEY, gitignored
  ytmatrix/
    config.py                      # pydantic models + YAML load/save/validate
    youtube.py                     # httpx search.list
    cache.py                       # content-addressed on-disk result cache
    server.py                      # FastAPI routes + WS hub
  static/
    player.html / player.js        # the matrix + Play button
    config.html  / config.js       # live editor
  cache/                           # gitignored
  tests/
```

### 3.1 Module boundaries

Each module has one job and no knowledge of its siblings' domains:

| Module | Knows about | Does not know about |
|---|---|---|
| `config.py` | YAML, pydantic validation | YouTube, disk cache, HTTP |
| `youtube.py` | The YouTube REST API, httpx | Config files, caching, disk |
| `cache.py` | Disk, hashing, TTL | YouTube, config semantics |
| `server.py` | FastAPI, WS fan-out; wires the other three | — |

This is what makes each unit testable alone: `youtube.py` against a mocked
transport, `cache.py` against a tmpdir, `config.py` with no I/O at all.

### 3.2 Library choices

**`httpx` directly against the REST endpoint, not
`google-api-python-client`.** We call exactly one API method. The official
client is synchronous — it would need thread offload from async FastAPI —
and pulls in a large dependency tree to build one authenticated GET.

**`uv` + `pyproject.toml`**, following `layout-driver` rather than
`audio-snippet`'s `.venv` + `install.sh`. It is the newer sibling
convention and where this project eventually integrates.

### 3.3 Endpoints

| Route | Purpose |
|---|---|
| `GET /` | player page |
| `GET /config` | live config editor |
| `GET /api/config` | current config as JSON |
| `PUT /api/config` | validate → write `config.yaml` → broadcast on WS |
| `GET /api/videos` | resolved video set for current config (cache-first) |
| `WS /ws` | server→client push: `config`, `videos` |
| `GET /healthz` | liveness |

## 4. Search, cache, and the reserve pool

### 4.1 The call

One GET to `https://www.googleapis.com/youtube/v3/search` with `key`, `q`,
`part=snippet`, `maxResults=50`, plus the config's `order`,
`video_duration`, `safe_search`, and `relevance_language`.

Two parameters are **forced and not configurable**, because they are
correctness rather than taste:

- `type=video` — without it, results include channels and playlists, which
  have no video ID to embed
- `videoEmbeddable=true` — filters out the obviously unplayable up front

### 4.2 The cache

Cache key: SHA-256 over the normalized parameter set — every parameter that
affects results, sorted for order-independence, **excluding the API key**.
Value: `cache/<hash>.json` holding `{fetched_at, params, items[]}`.

On request, hash the params and read the file; if
`now - fetched_at < ttl_hours`, return it without touching the network.
Writes are atomic (temp file + rename), as `audio-snippet` does for its run
JSON.

This is what makes a live config page viable on a 100-search budget.
Changing `muted` or `grid` does not alter the search parameters, so it hits
cache and costs nothing; only `query` or a search filter spends quota.

**The config page surfaces this.** A cache-hit indicator sits next to the
search fields, showing whether saving the current settings would cost an API
call. The budget is small enough that guessing is not acceptable.

### 4.3 The reserve pool

`videoEmbeddable=true` is **not reliable**. Videos still fail at embed time:
rights-holder blocks, region restrictions, and takedowns that land after
indexing. The IFrame API reports these via `onError`:

| Code | Meaning |
|---|---|
| 2 | invalid parameter |
| 5 | HTML5 player failure |
| 100 | video removed or private |
| 101, 150 | embedding disallowed by the owner |

The 50 results become an ordered list. The first `cols × rows` fill the
grid; the rest sit in reserve. On `onError`, the client discards that video,
takes the next unused reserve, and rebuilds **that one cell** — the other
players keep going untouched. No server round trip, no quota cost, no
visible interruption beyond one cell reloading.

With 42 spares behind 8 slots, exhaustion is unlikely; if it happens, the
cell renders a plain "no playable result" state rather than failing silently.

## 5. The two pages

### 5.1 Player page (`/`)

A CSS-grid container sized to the viewport, `cols × rows` from config, one
cell per slot. Each cell holds a `<div>` that the IFrame Player API replaces
with a `YT.Player`, created with:

```js
playerVars: { mute: 1, controls: 0, rel: 0, playsinline: 1 }
```

**A limit to state plainly: YouTube chrome cannot be fully removed.**
`modestbranding` is deprecated and ignored. `rel=0` no longer removes
related videos — it only restricts them to the same channel. `controls: 0`
gets most of the way visually, but expect a title overlay and end-screen
suggestions. This is the ceiling of the embed approach, and it is the
specific thing that would push a future wall version toward downloaded
files instead.

**The Play button is a mechanism, not just UX.** Muted autoplay is permitted
by browser policy, but eight players starting simultaneously is exactly the
case browsers throttle. The button supplies the user gesture that makes all
eight start reliably. Once pressed, the gesture is granted for the page, so
subsequent config-driven reloads may autoplay (`autoplay_on_change`).

### 5.2 Config page (`/config`)

A typed form, **not** a raw YAML textarea — fields mirror the schema, with
the cache-hit indicator from §4.2 beside the search fields. Save issues
`PUT /api/config`.

Invalid input returns 422 with per-field messages and **leaves the file on
disk untouched**. A bad edit can never render `config.yaml` unparseable.

### 5.3 WebSocket protocol

Server→client only. Two message types:

```json
{"type": "config", "config": {...}}
{"type": "videos", "video_ids": [...], "reserves": [...], "from_cache": true}
```

On a successful `PUT`, the server writes the file, then decides scope: if
`query` or any search parameter changed, it re-resolves (cache-first) and
broadcasts both messages; otherwise it broadcasts `config` alone.

The player reacts by scope:

| Changed | Response |
|---|---|
| `grid`, or the video set | tear down and rebuild the players |
| `muted`, `start_offset`, `loop` | apply in place via `mute()` / `seekTo()` |

Everything updates live, but cosmetic edits do not restart eight videos.

Both pages connect with reconnect-and-backoff (the pattern `layout-driver`
already implements), and **on reconnect re-fetch `/api/config` and
`/api/videos`** to resync anything missed while disconnected.

## 6. Config schema

```yaml
query: "golden cover"

grid:
  cols: 4
  rows: 2

search:
  order: relevance          # relevance | date | rating | viewCount | title
  video_duration: any       # any | short | medium | long
  safe_search: moderate     # none | moderate | strict
  relevance_language: en    # optional; omit for none

playback:
  muted: true               # forced true in v1; present so it can be relaxed
  autoplay_on_change: true  # applies after the first Play press
  start_offset: 0           # seconds into each video
  loop: true                # restart a video on ended

cache:
  ttl_hours: 24
```

Pydantic models with real bounds:

- `cols`, `rows` ≥ 1, and `cols * rows ≤ 50` — you cannot fill more cells
  than one search returns
- enums for every fixed-vocabulary field
- `ttl_hours` > 0
- `query` non-empty after stripping

**The models are the single source of truth.** The YAML loader, the `PUT`
validator, and the config page's field list all derive from them, so the
three cannot drift.

## 7. Error handling

| Condition | Response |
|---|---|
| Missing `YOUTUBE_API_KEY` | fail at startup with an explicit message |
| 403 `quotaExceeded` | report as such to both pages; fall back to stale cache if present — expired-but-present beats blank |
| Transport error, 5xx | bounded retry with backoff |
| Zero results | not an error; show "no results for that query" and keep the previous set on screen |
| Embed failure | substitute from the reserve pool (§4.3) |

## 8. Testing

Mock at the network boundary, keep everything else real — `audio-snippet`'s
convention.

| Target | Coverage |
|---|---|
| `config.py` | YAML round-trip, every validation bound, rejection leaves the file untouched |
| `cache.py` | hit, miss, expiry, hash independence of parameter order, atomic write under simulated crash |
| `youtube.py` | `httpx.MockTransport`: normal response, quota 403, 5xx retry, empty results |
| `server.py` | `TestClient`: every route, `PUT` validation failures, WS broadcast on change |
| `static/*.js` | pure logic as `.test.mjs` node tests — reserve-pool substitution, and change-scope classification (rebuild vs. in-place) — mirroring `layout-driver`'s `sketch-loader.test.mjs` |

**One live end-to-end test, run manually, excluded from the default suite.**
`audio-snippet`'s CLAUDE.md records this lesson directly: every unit test
mocked the SDK boundary, and the boundary was where the bug was. One live
search costs 100 of 10,000 units — cheap enough to run deliberately, too
expensive to run per commit.

## 9. Known limitations

Recorded so the wall-version decision is informed:

1. **YouTube chrome cannot be fully suppressed** (§5.1) — title overlays and
   end-screen suggestions will appear.
2. **Ads play.** Muted, but they play, and they interrupt the grid.
3. **Eight concurrent iframes are heavy** — each is a full nested browsing
   context. Whether this holds up in headless capture is one of the open
   questions this prototype exists to answer.
4. **100 searches per day** without a quota increase.

Limitations 1–3 are inherent to the embed approach. If the wall version
cannot tolerate them, the alternative is locally downloaded files played
through `<video>` — which trades ToS compliance and immediacy for clean
frames.
