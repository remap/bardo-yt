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

brew install mkcert nss     # nss is what lets mkcert reach Firefox
mkcert -install             # one-time: trust the local CA (asks for your password)

./run.sh                    # https://localhost:8444/
```

With `mkcert -install` done, `run.sh` issues a certificate your browser already
trusts and there is no warning. Skip it and you get a self-signed fallback plus
the usual click-through — the server tells you which one you got at startup.

**Open `localhost`, not `127.0.0.1`.** YouTube refuses to embed into a page
served from the IP address: every player fails with error 150. Same server,
same videos, different hostname.

Two pages:

- **`/`** — the wall. **Play all** / **Pause all** / **Unmute all** drive every
  cell at once. It starts muted because browsers only autoplay muted video;
  the button supplies the user gesture that unmuting requires.
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

## Generated queries

With `query_generation.enabled`, Gemini invents a fresh search query on every
page load, seeded by `theme` and steered away from the last `avoid_repeats`
queries so it doesn't circle back to one you already paid for. Needs
`GEMINI_API_KEY`.

**This deliberately defeats the cache.** A newly invented query has never been
searched, so every reload spends 100 units — about 50 reloads against the
default `quota.daily_limit_units: 5000`, and 100 against Google's real ceiling.
The header shows the running total (`200/5000 units today`) so it is never a
surprise.

`ytmatrix/budget.py` tracks the day's spend in `cache/_budget.json` and refuses
to search past the limit, falling back to stale cache when it can rather than
going blank. Raise `quota.daily_limit_units`, or set it to `0`, to lift the
guard. Typing a query by hand on `/config` overrides the generated one until
the next reload.

## Tests

```bash
uv run pytest tests/ -v          # default suite, no network
node --test 'static/*.test.mjs'  # pure frontend logic
uv run pytest -m browser -v      # real Chromium; spends no quota
uv run pytest -m live -v         # one real search; spends 100 quota units
```

The browser suite exists because two bugs made the wall render completely
blank with no console error, and neither the Python nor the node tests could
see them — one was script-ordering, the other a Python/JS serialization gap.
Run it after touching `player.js` or the config wire format.

A conftest guard fails any default-suite test that reaches the live API, so
the budget cannot be spent by accident.

## Known limitations

These are inherent to embedding, not bugs:

1. **Some chrome is unreachable.** Controls, annotations, captions and the
   hover title bar are all suppressed (see `CLAUDE.md` for which parameter does
   what and which are deprecated no-ops). End-screen suggestions on a finished
   video are not, and text **burned into the video's own pixels** — creator
   subtitles, tracklists — is part of the picture and cannot be touched.
2. **Ads play** and interrupt the grid.
3. **Concurrent iframes are heavy** — each is a full nested browsing context.
4. **Non-16:9 sources still box themselves.** The iframe is cropped to fill its
   cell, but YouTube pillarboxes a vertically-shot video *inside* its own
   player, and that is cross-origin.

If the wall version cannot tolerate these, the alternative is locally
downloaded files played through `<video>`, which trades ToS compliance and
immediacy for clean frames. That decision is deliberately deferred.
