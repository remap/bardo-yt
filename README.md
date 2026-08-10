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
  **hover to unmute** makes exactly one cell audible — whichever the cursor is
  over — and outlines it. Leaving the grid restores the global state. Same
  trick as the context menu: `pointer-events: none` on the iframe means the
  cell, not YouTube, receives the pointer.
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

With `query_generation.enabled`, Gemini invents a search query seeded by
`theme` and steered away from the last `avoid_repeats` queries so it doesn't
circle back to one you already paid for. Needs `GEMINI_API_KEY`.

**Generating never happens implicitly**, because each new query is a cache miss
by construction and costs 100 units. It takes an explicit act:

- the **New query** button, or
- loading `/?new=true` (the parameter is stripped from the URL immediately, so
  a stray refresh or a restored tab can't spend another 100 units)

Everything else is free. The current query is persisted to `cache/_wall.json`,
so a plain reload — or a server restart — restores the same wall at zero cost.

### Steering it

The box in the header is a **metaprompt**, not a raw search. What you type goes
to Gemini alongside the app's standing guidance (return things that move, avoid
static-upload words, must return dozens of results), so `sadder, more piano`
comes back as a usable query rather than being pasted straight into YouTube.
Press Enter. It costs 100 units, same as any other generation.

## Query log

Every resolution is appended to `logs/queries.jsonl` — one JSON object per
line, gitignored, never truncated. Each record carries a local timestamp with
an explicit UTC offset, the query, where it came from (`generated`, `manual`
with the prompt that produced it, or `config`), whether it came from cache, and
the full result list with titles:

```json
{"at":"2026-08-10T15:53:22-07:00","query":"kpop solo ballad live performance",
 "source":"manual","prompt":"slower and more melancholy, solo performers",
 "from_cache":false,"count":8,"units_spent_today":1700,"results":[…]}
```

Plain reloads are logged too — the file is a record of what was on the wall and
when, not only of what was newly searched. `from_cache` tells the two apart.

## Right-click a cell

The iframe has `pointer-events: none` (which is what stops YouTube's hover
chrome appearing), and that is exactly what leaves the cell free to take a
right-click. The menu offers:

- **Copy video URL at time** — `https://youtu.be/<id>?t=96`, at the position
  that cell is actually at
- Copy video URL / video ID / title
- Open on YouTube at time
- Play, pause, mute, unmute or restart **that one cell**
- Replace it with the next video from the reserve pool

`ytmatrix/budget.py` tracks the day's spend in `cache/_budget.json` and refuses
to search past the limit, falling back to stale cache when it can rather than
going blank. Raise `quota.daily_limit_units`, or set it to `0`, to lift the
guard. Typing a query by hand on `/config` overrides the generated one.

### The counter is an estimate, not a reading

**Nothing here retrieves your real quota.** Google exposes no
remaining-quota field on the YouTube Data API, and an API key cannot read the
Cloud quota APIs. So the header's `900/5000` is this app's own tally: it
assumes the documented 100 units per `search.list` and counts the searches it
performed. It does **not** see usage from anything else sharing the project or
key, and clearing `cache/` resets it to zero.

The ground truth is Google's own 403 `quotaExceeded`, which is handled
separately and independently of this counter. For an authoritative number, read
the Cloud console (**APIs & Services → Quotas**), or wire up the Service Usage
API — that one *does* accept a service account, unlike the YouTube Data API.

## Keeping still images off the wall

A large share of music results on YouTube are one static album cover with
audio over it — legitimate results, useless on a video wall. **The Data API
cannot filter for this**: `search.list` offers `videoDefinition`,
`videoDuration`, `videoDimension`, `videoType`, `videoCaption` and friends,
and none of them say whether the picture moves.

So it's measured instead. YouTube samples three frames across every video at
`/1.jpg`, `/2.jpg`, `/3.jpg`; if they're near-identical, nothing is happening.
Against a real result set the separation is wide:

| score | what it is |
|---|---|
| under 2.5 | still image with a soundtrack |
| 5–15 | slow or locked-off footage |
| 25–41 | genuinely edited video |

`filtering.static_threshold` (default 3.5) is the cutoff. Stills are held back
in the reserve pool rather than shown, exactly like a failed embed — but if a
query returns *mostly* stills, the wall relaxes and uses the liveliest of them
rather than leaving cells empty. A still beats a black hole. The count of
relaxed cells is reported as `static_relaxed`, so it is never silent.

This costs no quota and only measures as deep as it needs to
(`grid + scan_depth` videos, not all 50). Scores are cached per video forever.

The other half is the query itself: Gemini is told to favour words implying a
filmed event (live, session, busking, rehearsal, street) and to avoid the ones
that attract static uploads (album, playlist, lyrics, audio, mix, 1 hour).

## Country diversity

On by default (`filtering.prefer_country_diversity`). Eight cells of the same
song from eight different countries is a far better wall than eight from one.

`search.list` returns no country field, so it takes two more calls —
`videos.list` for the channel id, `channels.list` for `snippet.country` —
both batched 50 ids at a time at **1 unit each**. That is 2 units against the
100 the search already cost, and results are cached per video forever.

Coverage is partial: on a real result set 29 of 50 videos had a published
country, spanning 12 of them. So this **reorders and never drops** — videos of
unknown origin form one bucket and take their turn like any other country,
rather than being penalised or crowding out the ones we do know.

The reordering is a round-robin across countries in order of first appearance,
so the top search result stays first and relevance is preserved within each
country. A real run of `LE SSERAFIM Antifragile dance cover street`:

```
1. unknown   [KPOP IN PUBLIC | ONE TAKE] …      5. AT   [KPOP IN PUBLIC VIENNA] …
2. KR        [🦋ARTBEAT] 커버댄스 Dance Cover      6. HK   [LE SSERAFIM] KPOP IN PUBLIC …
3. ES        [KPOP IN PUBLIC] Dance Cover        7. TW   [KPOP IN PUBLIC CHALLENGE] …
4. US        [KPOP IN PUBLIC - NYC] …            8. RU   [K-POP IN PUBLIC | ONE TAKE] …
```

## Letterbox detection

YouTube renders every video into a 16:9 player, so vertically-shot and
ultrawide sources arrive with black bars baked into the frame; cropping the
iframe doesn't help, because the bars scale with it. The iframe is
cross-origin, so its pixels can't be read — but the video's `mqdefault`
thumbnail is the same 16:9 frame, so bars there mean bars on screen.

`ytmatrix/letterbox.py` finds the picture's bounds in that thumbnail and
`coverRect()` oversizes the iframe until the *content* covers the cell, pushing
the bars outside the crop. Detections are cached per video and cost no quota:
`i.ytimg.com` is not the Data API.

It refuses to crop when the result would be implausible (below 30% of the
frame), so a night scene or a fade-to-black shows uncropped rather than zooming
10× into one lit window. Use `mqdefault` or `maxresdefault`, never `hqdefault`
— that one is 4:3 with padding of its own and reports letterboxing on
everything.

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
   what and which are deprecated no-ops). Text **burned into the video's own
   pixels** — creator subtitles, tracklists — is part of the picture and cannot
   be touched.
2. **Ads cannot be disabled or skipped.** The IFrame Player API offers no
   control over them at all. What the config *can* do is make them rarer:
   - `video_duration: short` (the default) — YouTube only permits mid-roll
     breaks on videos of 8 minutes or more, so short videos can be interrupted
     at most once, at the start. It cannot stop pre-rolls.
   - `video_license: creativeCommon` — CC uploads are overwhelmingly
     unmonetised and carry far fewer ads. The trade-off is a much smaller
     result pool; for most music searches, restrictively so. Off by default.

   Genuinely ad-free means not using embeds — downloaded files played through
   `<video>`, the approach deliberately deferred in the design spec.
3. **Click-throughs are avoided where they can be.** `videoSyndicated=true` is
   forced alongside `videoEmbeddable=true`, which keeps out videos that render
   a "Watch on YouTube" panel instead of playing. Age-gated videos that demand
   sign-in still exist; they surface as an `onError` and get substituted from
   the reserve pool.
4. **Concurrent iframes are heavy** — each is a full nested browsing context.
4. **Non-16:9 sources still box themselves.** The iframe is cropped to fill its
   cell, but YouTube pillarboxes a vertically-shot video *inside* its own
   player, and that is cross-origin.

If the wall version cannot tolerate these, the alternative is locally
downloaded files played through `<video>`, which trades ToS compliance and
immediacy for clean frames. That decision is deliberately deferred.
