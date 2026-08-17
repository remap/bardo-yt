# yt — CLAUDE.md

A grid of muted YouTube embeds driven by a live search, with a live-editable
config page. Single FastAPI process, Python 3.13, `uv`-managed. Standalone
prototype — **not** integrated with `../layout-driver`.

It runs two ways from one codebase: locally over TLS on `localhost:8444`
against a directory, and on Cloudflare Containers behind Cloudflare Access
against an R2 bucket, with a TypeScript Worker in front.

Design rationale: `docs/superpowers/specs/2026-08-10-yt-matrix-design.md`.
Task-by-task plan: `docs/superpowers/plans/2026-08-10-yt-matrix.md`.
Deployment plan: `docs/superpowers/plans/2026-08-16-cloudflare-deployment.md`.
**Deployment runbook: `docs/DEPLOY.md`** — the account-side steps, the
verification checks, and what has never been run against Cloudflare.

## Commands

```bash
uv sync
cp .env.example .env                    # YOUTUBE_API_KEY
./run.sh                                # https://localhost:8444/
uv run pytest tests/ -v                 # default suite, never hits the network
node --test 'static/*.test.mjs'         # pure frontend logic
uv run pytest tests/test_player_smoke.py -m browser -v   # real Chromium; no quota
uv run pytest -m live -v                # one real search; spends 100 quota units
uv run ruff check . && uv run ruff format .
```

Note the quotes on the node command: `node --test static/` resolves the
directory as a module on Node 23 and fails.

The Worker and container half:

```bash
npm install && npx wrangler types       # fresh clone; see gotcha 33
npm run typecheck                       # wrangler types + tsc --noEmit
npm run build                           # assemble dist/ from static/
npx wrangler deploy --dry-run --containers-rollout=none   # config check, no Docker
npm run deploy                          # needs Docker; see docs/DEPLOY.md
```

## File-by-file

| File | Responsibility |
|---|---|
| `ytmatrix/store.py` | `Store` protocol plus `FileStore` (local + tests) and `R2Store` (production). Knows nothing about YouTube, config, or budgets. |
| `ytmatrix/config.py` | pydantic models for `config.yaml`; load/save/validate through the `Store`. Knows nothing about YouTube or the cache. |
| `ytmatrix/youtube.py` | `build_params` + `search` against the REST API. Knows nothing about config files or storage. |
| `ytmatrix/cache.py` | Content-addressed search cache under `search/`, over the `Store`. Knows nothing about YouTube. |
| `ytmatrix/gemini.py` | Invents a search query from a theme plus an avoid-list. Knows nothing but Gemini. |
| `ytmatrix/budget.py` | Daily quota ledger at `_budget.json`; resets on Pacific date change. Local estimate, not a real reading. The one key with more than one writer, so it uses the store's CAS. |
| `ytmatrix/letterbox.py` | Finds the picture inside a boxed 16:9 frame, from the video's thumbnail. |
| `ytmatrix/motion.py` | Scores real video vs. still-image-with-audio from storyboard frames; ranks the wall. |
| `ytmatrix/querylog.py` | The query log: one JSON object per query under `logs/<date>/`, keyed so listing sorts chronologically. Local-time stamped, records who asked. |
| `ytmatrix/origin.py` | Country-of-origin lookup + round-robin reorder so the wall spans places. |
| `ytmatrix/server.py` | FastAPI routes, WS fan-out, and the cache-first `resolve_videos` that wires the three above. Holds no per-user state. |
| `ytmatrix/settings.py` | Env/secrets. `YOUTUBE_API_KEY` lives here, never in `config.yaml`. |
| `ytmatrix/certs.py` | Self-signed cert generation (copied from layout-driver). Local only. |
| `ytmatrix/ws.py` | `ConnectionManager` (copied from layout-driver). |
| `ytmatrix/main.py` | Local entry point: Settings, `FileStore`, cert, uvicorn over TLS, and it serves `dist/` itself. |
| `ytmatrix/container.py` | Cloudflare entry point: `R2Store` from env, plain HTTP on `$PORT`, no certs, no static files. |
| `worker/index.ts` | Verifies the Access JWT, stamps `X-Wall-User`, forwards to the one shared container. Runs only for the three routes in `run_worker_first`; `dist/` is served by the assets layer without it (gotcha 32). `worker/env.d.ts` pins the five secrets for the typechecker. |
| `static/grid-logic.js` | Pure slot/reserve bookkeeping + config-change classification. Node-testable. |
| `static/wallstate.js` | What *this browser* is watching: current query + history in `localStorage`. Every access is defensive. |
| `static/socket.js` | WS connect with backoff; re-syncs on every (re)connect. |
| `static/player.js` | YT IFrame players, DOM wiring, error substitution. |
| `static/config.js` | The live editor and its quota-cost indicator. |
| `scripts/build-dist.sh` | Assembles `dist/`: `player.html` → `index.html`, `config.html`, `static/*.js`. Not the test `.mjs` files. |
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
   the browser's own `localStorage` (`static/wallstate.js`, gotcha 27) so
   reloads and restarts are free. Do not add generation to page load, a timer,
   or `resync()` — `resync()` also runs on every WebSocket reconnect, which is
   a network hiccup, not an intent.

   **The budget counter is an estimate, not a reading.** Google exposes no
   remaining-quota field on the YouTube Data API, and an API key cannot reach
   the Cloud quota APIs. `budget.py` assumes 100 units/search (documented) and
   counts only this app's own searches — it is blind to anything else sharing
   the project or key, and deleting `_budget.json` (from `cache/` locally, from
   the bucket in production) zeroes it without returning a unit. Ground truth is the
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

21. **`pointer-events: none` on the iframe is load-bearing twice over.** It
    stops YouTube's hover chrome (gotcha 7) *and* it is the only reason the
    cell can receive a right-click, which is what the context menu is built
    on. Removing it breaks both at once.

22. **Titles arrive HTML-escaped from the Data API** — `Rumi &amp; Jinu`,
    `&#39;`. `youtube.py` runs `html.unescape` once at the boundary so nothing
    downstream has to know. Do not escape them again on the way out; the
    context menu and the log both use `textContent`/JSON, not innerHTML.

24. **Country of origin is not in `search.list`.** It takes `videos.list`
    (→ channelId) then `channels.list` (→ `snippet.country`), both batched 50
    ids per request at 1 unit each — 2 units against the search's 100, cached
    per video forever. Coverage is ~60%, so `origin.diversify` **reorders and
    never drops**: unknown origin is *one* bucket taking its turn, not one
    bucket per video, or the 40% unknown would crowd out the known.

    Order of operations matters: diversify runs **before** `motion.rank`,
    because rank preserves the order it is given among the videos it keeps.
    Reversing them would let the static filter undo the spread.

    **Those 2 units are not in the ledger.** `budget.py` counts searches and
    nothing else, so the status line's "units today" under-reports by 2 per
    newly-resolved set — one more reason the number is an estimate rather than
    a reading (gotcha 2). Not worth fixing at 2 units against 10,000; worth
    knowing before anyone tries to reconcile the figure with Google's console.

25. **Whatever the theme constrains must survive into the query.** Naming an
    artist and song without a cover word returns that act's own official
    upload — one canonical video and its neighbours, the opposite of a wall.
    The system prompt enforces this explicitly; do not soften it while
    "relaxing" the specificity rules.

23. **The prompt box is a metaprompt, not a query.** It is passed to Gemini as
    `instruction` alongside the standing theme and all the usual rules, so the
    result is still a search that returns dozens of moving videos. Do not
    "simplify" it into sending the raw text to YouTube.

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

26. **The container has no durable disk.** Every instance starts from a fresh
    copy of the image, so nothing may rely on a file surviving a restart — the
    cache, the log, the ledger and config all go through `Store` to R2
    instead. `FileStore` keeps local development and the whole test suite on
    disk; do not "simplify" a module back to a `Path`.

27. **Config is shared; walls are not.** Everyone edits one `config.yaml` in
    R2 and a save reaches everyone. What each person is *watching* — their
    current query and the history that steers Gemini away from repeats — lives
    in their own browser (`static/wallstate.js`) and travels on the request.
    The server holds no per-user state at all. A dict keyed by user is a sign
    something has gone wrong.

    The cost of this is real and accepted: `localStorage` is per browser, not
    per account, so one person's laptop and TV are two separate walls. Shared
    config still reaches both.

28. **A client-supplied query must never spend quota.** The browser replays its
    stored query on every load *and every WebSocket reconnect* — which is a
    network hiccup, not an intent. `usable_query()` therefore honours it only
    if the shared cache already has it, stale included, and otherwise falls
    back to the config query. Making `/api/videos?query=` search on a miss
    would turn a flaky connection into a quota fire. Spending stays the New
    query button's job (gotcha 2).

    Stale counts as known deliberately: expiry must not silently move
    somebody's wall back to the shared query.

29. **The server can no longer broadcast a video set on a config change.** It
    does not know what query any browser is watching. `put_config` broadcasts
    config only; each client decides via `needsRefetch()` in `grid-logic.js`
    whether to refetch, and `overridesStoredQuery()` handles the one case where
    a config edit must beat the browser's own query — someone typing in the
    config page's query field.

    Two consequences of moving that decision to the client, both live:

    **Every open wall refetches at once, and `resolve_videos`'s single-flight
    is the only thing keeping that at one search.** `needsRefetch` is true for
    any change under `search.*`, not just the query. Ten walls each miss the
    same new cache key in the same instant, and each miss is 100 units — 1000
    from one toggle of `search.order` without the guard. The guard is an
    in-process dict of locks scoped to `create_app`, which works *because there
    is one shared container*; waiters re-read the cache instead of searching.
    Per-user containers would silently void it (gotcha 30).

    **Any search-affecting edit also erases everyone's personal query and
    unifies the walls** — not just an edit to the query field. A stored query
    is honoured only when the shared cache holds it *under the current search
    params* (gotcha 28), so changing `search.order` makes every stored query
    unservable; each browser gets the config query back, and
    `if (stored && message.query !== stored) clearQuery()` in `player.js`
    drops it. That is correct — their query genuinely cannot be served without
    spending 100 units per wall — but it is much broader than
    `overridesStoredQuery()`, which covers only the query field, and it
    surprises people.

30. **The container is shared and `getByName("wall")` is why.** It can be
    shared because there is no per-user server state, and that is what keeps
    ten walls costing about what one does. Passing the email there instead
    would give every user their own instance and multiply the bill; the email
    is carried only so the query log records who asked.

31. **`ACCESS_TEAM_DOMAIN` needs the scheme and no trailing slash.** The Worker
    uses it twice: concatenated into the JWKS URL
    (`${teamDomain}/cdn-cgi/access/certs`) and compared against the token's
    `iss` claim. A trailing slash leaves the key fetch working and breaks the
    issuer check, so every request 401s *after* a successful Access login while
    everything upstream looks healthy. Dropping the scheme breaks both.

32. **`run_worker_first` lives inside `assets`, not at the top level.** Move it
    out and wrangler emits a warning — `Unexpected fields found in top-level
    field: "run_worker_first"` — and deploys anyway, with `/api/*`, `/ws` and
    `/healthz` silently served from static assets. A warning during a
    successful deploy is easy to miss and the failure looks like a broken
    backend. (Confirmed by dry-run, not by argument.)

    The list has a second consequence worth holding onto: **everything not on
    it never reaches the Worker at all**, so `/` and `/static/*` are protected
    by Cloudflare Access alone. The Worker's own 401 is not a backstop for
    them. Between the first deploy and the Access application existing, the
    bundle is public.

33. **The typecheck gate is `npm run typecheck`, never a bare `npx tsc
    --noEmit`.** `tsconfig.json` references `worker-configuration.d.ts`, which
    `wrangler types` generates and `.gitignore` excludes — it mirrors
    `wrangler.jsonc` plus whatever local `.env` exists, so it is per-machine
    and never the source of truth. The bare command fails with `TS2688` on a
    fresh clone; the npm script generates the file first. A fresh clone needs
    `npm install` and `npx wrangler types` before anything typechecks at all.

34. **`wrangler deploy --dry-run` needs Docker.** It builds the container image
    even in dry-run, and fails outright with the daemon down. For a
    configuration-only check use `--containers-rollout=none`, which skips the
    image and prints the bindings — preflight only, never on a real deploy, or
    the Worker ships pointing at a stale image. A real `wrangler deploy`
    requires Docker running, full stop.

35. **An upgrade is forwarded unmodified; only HTTP gets the identity header.**
    `worker/index.ts` branches on **`/ws` *and* `upgrade: websocket`** — after
    verifying the JWT, before rebuilding the headers — and hands the original
    Request straight to the stub. The socket has no use for identity —
    `websocket_endpoint` never reads `X-Wall-User`, and the only consumer of
    that header is the query log, written on HTTP requests — so the one path
    whose request reconstruction nothing has ever executed is also the one path
    with nothing to gain from it. Do not "unify" the two branches.

    **The path half of that test is not redundant.** The header rebuild is the
    only thing that strips a client-supplied `x-wall-user`, so whatever skips
    it can name itself in the query log. Keyed on the header alone, a plain
    `GET /api/videos` carrying `Upgrade: websocket` and no `Connection:
    upgrade` opts out of sanitisation and is then handled as ordinary HTTP by
    uvicorn (`_get_upgrade` returns None without the Connection token). Only
    `/ws` may skip the rebuild.

    On both branches the stub's response is returned **unwrapped**, because
    `new Response(res.body, res)` silently drops the `webSocket` a 101 carries.
    The HTTP branch still needs `new Request(request, { headers })`: inbound
    headers are immutable in Workers.

    Docker was down for the whole of this work, so no socket has ever gone
    through the Worker at all. If a deployed wall renders but never reacts to a
    config change, start here (`docs/DEPLOY.md`, verification check 4).

36. **The `# noqa: BLE001` / `# noqa: S110` directives are load-bearing.** This
    project runs ruff 0.16.2, whose default rule set is far wider than older
    versions'. BLE001 fires on all four deliberate catch-and-carry-on blocks —
    `querylog.py`, `ws.py`, `letterbox.py`, `motion.py` — and S110 fires on the
    one that is a bare `except: pass`, in `querylog.py`. Deleting either
    directive as "dead" breaks `ruff check`. Each is followed by a reason; keep
    the reason with it.

37. **The Dockerfile pins `--platform=linux/amd64`, and removing it produces a
    crash that points nowhere.** Cloudflare Containers run amd64 only, so the
    pin is not portability boilerplate — it is the target. On an ARM host
    without it, the arm64 wheel for `google-genai` (imported by `gemini.py`)
    dies on `from google import genai` with **SIGILL**: exit 132, no traceback,
    no message, the process simply gone during startup. Everything else in the
    image imports fine, so it reads as a bug in this app rather than a wheel.

    BuildKit warns that a constant `--platform` hurts portability; the
    `# check=skip=FromPlatformFlagConstDisallowed` directive on line 1 silences
    exactly that check. It must stay the **first** line — BuildKit reads parser
    directives only before any other content, so moving it below a comment
    silently stops it working.

    Verified on Apple Silicon: pinned build → `amd64`, Pillow's JPEG codec
    present, `/healthz` answers `{"status":"ok"}`, and a missing R2 credential
    still aborts startup rather than failing later.

38. **Unreachable R2 is a 500, and that is deliberate.** `load_config` falls
    back to the shipped template on a *corrupt* document (`yaml.YAMLError`,
    `ValidationError`, `UnicodeDecodeError`) but not on a transport failure —
    botocore raises `SSLError`/`EndpointConnectionError`, which propagate.
    Bad R2 credentials therefore surface as a 500 naming the exact URL, which
    is what an operator needs. Widening that catch would mask a storage outage
    behind shipped defaults and quietly serve everyone the wrong config.
