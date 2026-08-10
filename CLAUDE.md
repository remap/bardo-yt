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
| `ytmatrix/gemini.py` | Invents a search query from a theme plus an avoid-list. Knows nothing but Gemini. |
| `ytmatrix/budget.py` | Daily quota ledger in `cache/_budget.json`; resets on Pacific date change. Local estimate, not a real reading. |
| `ytmatrix/wallstate.py` | Persists the current query + history so reloads and restarts cost nothing. |
| `ytmatrix/letterbox.py` | Finds the picture inside a boxed 16:9 frame, from the video's thumbnail. |
| `ytmatrix/motion.py` | Scores real video vs. still-image-with-audio from storyboard frames; ranks the wall. |
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
   is 100 searches. Resolution is cache-first and the cache key deliberately
   excludes the API key.

   A Gemini-invented query is a cache miss by construction, so **generation
   costs 100 units every time**. It therefore never happens implicitly: only
   the New query button or `?new=true` triggers it, `?new=true` is stripped
   from the URL the moment it is used, and the current query is persisted in
   `cache/_wall.json` so reloads and restarts are free. Do not add generation
   to page load, a timer, or `resync()` — `resync()` also runs on every
   WebSocket reconnect, which is a network hiccup, not an intent.

   **The budget counter is an estimate, not a reading.** Google exposes no
   remaining-quota field on the YouTube Data API, and an API key cannot reach
   the Cloud quota APIs. `budget.py` assumes 100 units/search (documented) and
   counts only this app's own searches — it is blind to anything else sharing
   the project or key, and clearing `cache/` zeroes it. Ground truth is the
   403 `quotaExceeded`, handled separately. Do not present this number as
   authoritative. Reading real quota needs the Service Usage or Monitoring
   API with OAuth or a service account — note a service account *does* work
   there, unlike for the YouTube Data API (gotcha 1).

3. **Cost is per call, not per result** — so `maxResults` is always 50. Do not
   "optimize" it down to the grid size; that saves nothing and destroys the
   reserve pool.

4. **`videoEmbeddable=true` is not reliable.** Videos still fail at play time
   (rights blocks, region limits, post-indexing takedowns) as `onError` codes
   101/150/100/2/5. The reserve pool exists for this. Do not remove it after a
   run where nothing happened to fail.

5. **`playback.muted` is the STARTING state, not a permanent one.** The wall
   has a mute/unmute-all button and runtime mute is owned by that button, not
   by the config file — a config push must not silently undo a click. It
   defaults to `true` because browsers only autoplay muted video: start
   unmuted and an arbitrary subset of players never begins. Unmuting is only
   permitted off a user gesture, which is what the button supplies.

6. **The Play button is a mechanism, not decoration.** Browsers throttle many
   simultaneous autoplaying players; the click is the user gesture that lets
   them all start. Removing it in favor of pure autoplay will produce a grid
   where an arbitrary subset of cells silently never starts. It stays
   **disabled until the whole set has pre-rolled**, so there is no window in
   which pressing it starts only some cells.

18. **Nothing starts until every player has buffered.** `prerollCurrentSet()`
    plays each player muted, waits for all of them to report loaded content,
    then pauses and rewinds them together. Without it the wall trickles in and
    a new query leaves the first video seconds ahead of the last. It always
    pre-rolls muted regardless of the mute button, because a muted play is the
    only kind a browser starts without a gesture; the real mute state is
    restored afterwards. `generation` guards against a pre-roll for a
    discarded set starting players belonging to its replacement.

19. **Loop by restarting *before* the end, never on ENDED.** YouTube draws its
    end-screen suggestion grid over the video as it finishes, so by the time
    ENDED fires the cards are already visible. The interval in `player.js`
    seeks back `LOOP_GUARD_SECONDS` short of the end. The ENDED handler is a
    backstop for when one slips through, not the mechanism.

20. **Ads cannot be disabled or skipped — the API has no control over them.**
    `video_duration: short` avoids mid-rolls (YouTube only allows them at 8
    minutes and up) and `video_license: creativeCommon` avoids most monetised
    uploads, but neither stops a pre-roll. `videoSyndicated=true` is forced
    alongside `videoEmbeddable=true` to keep out videos that render a "Watch
    on YouTube" click-through instead of playing. Anything more requires
    leaving embeds behind entirely.

7. **Know which chrome suppressions actually work.** Do not spend time on the
   dead ones or re-add them:

   | Parameter | Status |
   |---|---|
   | `modestbranding` | deprecated, silently ignored |
   | `showinfo` | removed in 2018, ignored |
   | `rel=0` | works, but only restricts related videos to the same channel |
   | `controls=0`, `disablekb=1`, `fs=0` | work |
   | `iv_load_policy=3` | works — annotations and cards off |
   | `cc_load_policy=0` | works, but only means "do not turn captions on for me" |

   Two things no parameter can do, handled in `player.js` instead:

   - **The title bar and channel avatar appear on hover.** `pointer-events:
     none` on the iframe is what prevents them — the cursor can never reach the
     player. Everything is driven through the JS API, so nothing needs to click
     it. Removing that CSS brings the overlay back.
   - **Captions.** A video whose own default is captions-on ignores
     `cc_load_policy`, and the captions module does not exist until playback
     starts — so `suppressCaptions()` runs on **PLAYING as well as ready**.
     Calling it only in `onReady` is too early and does nothing.

   Text burned into the video's own pixels (creator-added subtitles, tracklists)
   is part of the picture. Nothing reaches it. Do not try.

8. **A rejected `PUT /api/config` must leave `config.yaml` untouched.**
   Validation happens before the write, and there is a test for it.

9. **Never interpolate an httpx response body or request URL into an error
   message** — the URL carries the API key as a query parameter.

10. **`ValidationError.errors()` needs `include_context=False`.** The context
    of a custom-validator error holds the original `ValueError` object, which
    is not JSON serializable; without it FastAPI raises a 500 while trying to
    encode the 422. This bit once already.

11. **Use `localhost`, never `127.0.0.1`.** YouTube refuses to embed into a
    page served from the IP — every player fails with `onError` 150
    ("embedding disallowed") — while accepting the identical page at
    `localhost`. This costs hours if you don't know it: the failure looks
    exactly like genuinely non-embeddable videos, and the reserve pool
    dutifully burns through all 42 spares trying to recover. The browser smoke
    test's fixture URL and `CERT_HOSTS` both depend on this.

12. **`onYouTubeIframeAPIReady` is a race the module usually loses.**
    `player.js` is an ES module, so it is deferred until after the document
    parses, while `iframe_api` is injected during parsing and often finishes
    first — especially warm. When it wins, it finds no callback registered and
    never calls one; `apiReady` stays false and the wall renders blank with no
    console error whatsoever. `whenYouTubeApiReady()` checks for an
    already-loaded `YT.Player` before registering. Do not "simplify" it back
    to a bare assignment.

13. **`Grid.cells` is a Python `@property`, so it is NOT in the JSON.**
    `model_dump()` emits only `cols` and `rows`. Reading `config.grid.cells`
    in JS yields `undefined`, `splitSlots` builds zero slots, and the wall goes
    blank — again with no error. JS derives the count via `cellCount(grid)` in
    `grid-logic.js`. Same class of bug as #12 and it hid behind it.

16. **The Data API has no "is it actually moving" filter, and never will.**
    Do not go looking for one — `videoDefinition`, `videoDuration`,
    `videoDimension`, `videoType`, `videoCaption` and `videoCategoryId` are the
    whole menu. Still-image-with-audio uploads are detected instead, by
    comparing the three storyboard frames YouTube samples at `/1.jpg`,
    `/2.jpg`, `/3.jpg`. Stills score under ~2.5, real footage 25+.

    Two deliberate choices in `motion.py`: an unmeasurable video (thumbnail
    404s) counts as **moving**, because dropping a legitimate result is worse
    than showing one still; and when a query returns mostly stills the wall
    **relaxes and uses them** rather than leaving cells empty, reporting the
    count as `static_relaxed`. Do not "tighten" either into a hard reject.

    `select_videos` measures only `grid + scan_depth` videos, not all 50 —
    scoring everything would be 150 image fetches to fill eight cells.

17. **Tests must not touch i.ytimg.com either.** It costs no quota, but it is
    still the network: `conftest.py` stubs `server.motion_score` for every
    non-browser test. Without it the suite went from 0.8s to 9.4s and started
    depending on ids that only exist in a fixture.

15. **Thumbnail choice decides whether letterbox detection works at all.**
    `mqdefault.jpg` (320×180) and `maxresdefault.jpg` (1280×720) are true 16:9
    — the same frame the player renders. **`hqdefault.jpg` is 4:3 with its own
    padding baked in** and will report letterboxing on every video ever made.
    Detection also refuses to crop below `MIN_CONTENT_FRACTION` (30%), so a
    dark or fading shot degrades to "no crop" instead of a 10× zoom into one
    lit corner. Thumbnails come from `i.ytimg.com` and cost no quota.

14. **Browser bugs need a browser test.** #12 and #13 were both invisible to
    the Python suite and the node tests — one is script-ordering, the other a
    serialization gap between two languages. `tests/test_player_smoke.py`
    (marked `browser`) is the only thing that catches either. Run it after any
    change to `player.js` or the config wire format.
