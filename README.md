# yt

A grid of muted YouTube embeds driven by a live search, retunable from a
second browser page while it runs.

This is a prototype, and deliberately standalone: it is the first step toward
playing YouTube content on the Cafe Bardo LED wall (`../layout-driver`), but
it does not integrate with it. The point is to learn what the embed path can
and cannot do before committing to a wall design.

Design rationale: `docs/superpowers/specs/2026-08-10-yt-matrix-design.md`.
Task-by-task build: `docs/superpowers/plans/2026-08-10-yt-matrix.md`.

## Quick start

```bash
uv sync
cp .env.example .env        # set YOUTUBE_API_KEY
./run.sh                    # https://localhost:8444/
```

The certificate is self-signed and generated on first run, so the browser will
warn once — click through it.

Two pages:

- **`/`** — the wall. One Play button starts every cell.
- **`/config`** — live editor. Saving pushes to the wall over a WebSocket.

## Authentication

A plain GCP API key with the YouTube Data API v3 enabled. **Service accounts
do not work with this API** — there is no domain-wide delegation path and a
service account has no channel. OAuth is only needed to act as a user, which
this does not do.

## Quota is the real constraint

`search.list` costs **100 units** of a 10,000/day default: **100 searches per
day**. Results are cached on disk under `cache/`, keyed by a hash of the
search parameters, so only a change to the query or a search filter spends
quota — grid and playback edits are free. The config page shows which kind of
change you are about to make before you save it.

Because the cost is per call rather than per result, every search asks for the
full 50 results. The first `cols × rows` fill the grid; the rest are held as a
reserve pool and swapped in when an embed fails at play time.

## Tests

```bash
uv run pytest tests/ -v          # default suite, no network
node --test 'static/*.test.mjs'  # pure frontend logic
uv run pytest -m live -v         # one real search; spends 100 quota units
```

A conftest guard fails any default-suite test that reaches the live API, so
the budget cannot be spent by accident.

## Known limitations

These are inherent to embedding, not bugs:

1. **YouTube chrome cannot be fully removed.** `modestbranding` is deprecated
   and ignored; `rel=0` no longer removes related videos, it only restricts
   them to the same channel. Expect a title overlay and end-screen
   suggestions.
2. **Ads play.** Muted, but they play, and they interrupt the grid.
3. **Concurrent iframes are heavy** — each is a full nested browsing context.

If the wall version cannot tolerate these, the alternative is locally
downloaded files played through `<video>`, which trades ToS compliance and
immediacy for clean frames. That decision is deliberately deferred.
