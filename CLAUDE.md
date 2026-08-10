# yt — CLAUDE.md

A grid of muted YouTube embeds driven by a live search, with a live-editable
config page. Single FastAPI process, Python 3.13, `uv`-managed. Standalone
prototype — **not** integrated with `../layout-driver`.

Design rationale: `docs/superpowers/specs/2026-08-10-yt-matrix-design.md`.
Task-by-task plan: `docs/superpowers/plans/2026-08-10-yt-matrix.md`.

## Commands

```bash
uv sync
cp .env.example .env                    # YOUTUBE_API_KEY
./run.sh                                # https://localhost:8444/
uv run pytest tests/ -v                 # default suite, never hits the network
node --test 'static/*.test.mjs'         # pure frontend logic
uv run pytest -m live -v                # one real search; spends 100 quota units
uv run ruff check . && uv run ruff format .
```

Note the quotes on the node command: `node --test static/` resolves the
directory as a module on Node 23 and fails.

## File-by-file

| File | Responsibility |
|---|---|
| `ytmatrix/config.py` | pydantic models for `config.yaml`; load/save/validate. Knows nothing about YouTube or the cache. |
| `ytmatrix/youtube.py` | `build_params` + `search` against the REST API. Knows nothing about config files or disk. |
| `ytmatrix/cache.py` | Content-addressed on-disk cache. Knows nothing about YouTube. |
| `ytmatrix/server.py` | FastAPI routes, WS fan-out, and the cache-first `resolve_videos` that wires the three above. |
| `ytmatrix/settings.py` | Env/secrets. `YOUTUBE_API_KEY` lives here, never in `config.yaml`. |
| `ytmatrix/certs.py` | Self-signed cert generation (copied from layout-driver). |
| `ytmatrix/ws.py` | `ConnectionManager` (copied from layout-driver). |
| `ytmatrix/main.py` | Entry point: build Settings, ensure cert, run uvicorn over TLS. |
| `static/grid-logic.js` | Pure slot/reserve bookkeeping + config-change classification. Node-testable. |
| `static/socket.js` | WS connect with backoff; re-syncs on every (re)connect. |
| `static/player.js` | YT IFrame players, DOM wiring, error substitution. |
| `static/config.js` | The live editor and its quota-cost indicator. |
| `tests/conftest.py` | Autouse guard: fails any default-suite test that reaches the live API. |

## Critical gotchas

1. **Service accounts do not work with the YouTube Data API v3.** No
   domain-wide delegation, and a service account has no channel. Use a plain
   API key. If someone "fixes" auth by switching to a service account, it will
   fail at runtime with an unhelpful error.

2. **`search.list` costs 100 quota units per call, out of 10,000/day.** That
   is 100 searches. Never add a code path that searches on a timer, on page
   load, or per cell. Resolution is cache-first and the cache key deliberately
   excludes the API key.

3. **Cost is per call, not per result** — so `maxResults` is always 50. Do not
   "optimize" it down to the grid size; that saves nothing and destroys the
   reserve pool.

4. **`videoEmbeddable=true` is not reliable.** Videos still fail at play time
   (rights blocks, region limits, post-indexing takedowns) as `onError` codes
   101/150/100/2/5. The reserve pool exists for this. Do not remove it after a
   run where nothing happened to fail.

5. **`playback.muted` is validated as `true`.** Unmuting needs the OS loopback
   audio path (see layout-driver's audio design) and is deliberately deferred.

6. **The Play button is a mechanism, not decoration.** Browsers throttle many
   simultaneous autoplaying players; the click is the user gesture that lets
   them all start. Removing it in favor of pure autoplay will produce a grid
   where an arbitrary subset of cells silently never starts.

7. **YouTube chrome cannot be fully suppressed.** `modestbranding` is
   deprecated; `rel=0` only restricts related videos to the same channel. Do
   not spend time trying to hide the title bar with player parameters — the
   ceiling here is a property of embedding.

8. **A rejected `PUT /api/config` must leave `config.yaml` untouched.**
   Validation happens before the write, and there is a test for it.

9. **Never interpolate an httpx response body or request URL into an error
   message** — the URL carries the API key as a query parameter.

10. **`ValidationError.errors()` needs `include_context=False`.** The context
    of a custom-validator error holds the original `ValueError` object, which
    is not JSON serializable; without it FastAPI raises a 500 while trying to
    encode the 422. This bit once already.
