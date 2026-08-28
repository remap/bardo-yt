# Layout-driver wall — design

## What this is

A second, opt-in front end for yt-matrix: instead of one flat `cols × rows`
grid, videos are spread across the same six screens `../layout-driver` drives
on the physical wall (`F`, `B`, `C`, `D`, `A`, `E`), each screen auto-fitting
its own share of videos to its own pixel box. It is purely additive — the
existing `/` grid and the Cloudflare-deployed behavior are untouched. This is
a new front-end *option*, not a replacement.

## Why

The current single grid treats the wall as one uniform surface. The real
venue is six screens of different sizes and aspect ratios. This gives the
wall a mode that respects that geometry — more videos on bigger screens,
fewer on smaller ones — without touching the backend, the cache, the quota
ledger, or anything already deployed.

## Non-goals

- No dependency on `../layout-driver` at runtime. That repo is not deployed
  alongside yt-matrix (Cloudflare's container can't reach it), so this only
  borrows its *geometry concept*, vendored as a static file in this repo.
- Not becoming a `layout-driver` app (`apps/<name>/static`, driven by that
  project's own server, NDI broadcaster, etc.). yt-matrix stays a standalone
  FastAPI service; this is one more static page it serves.
- No change to `ytmatrix/*` backend modules, `/api/*` routes, `/ws`, the
  search cache, the budget ledger, or the query log. Identical backend.
- No change to `index.html` / `player.js`'s observable behavior on `/`.

## Where this lives

Branch `feat/layout-driver-wall` off `worktree-feat-cloudflare-deploy` — that
branch is what's actually deployed to Cloudflare (`main` predates the
Cloudflare work by 42 commits), so branching there is what makes "identical
backend" true by construction rather than by careful porting.

New files only:

| File | Responsibility |
|---|---|
| `static/layout.html` | The new page's markup, parallel to `player.html`. |
| `static/layout-page.js` | Builds the screen containers, computes each screen's fit, drives the shared player engine (see below). |
| `static/layout/screens.json` | Vendored snapshot of `../layout-driver/config/screens.yaml`'s `canvas` + `screens[].grid` geometry, converted to pixel rects. Hand-recopied if the venue layout changes — there is no live link to that repo. |
| `static/layout-fit.js` | Pure functions: per-screen cols×rows fit, and the total/per-screen budget allocation. Node-testable, mirrors how `grid-logic.js` is pure today. |
| `tests/test_layout_smoke.py` | Browser smoke test loading `/layout`, parallel to `tests/test_player_smoke.py`. |

Modified, additively:

| File | Change |
|---|---|
| `ytmatrix/config.py` | New optional `LayoutConfig` section on `Config` (default: absent from a config that doesn't have it, so existing `config.yaml` — including the one already saved in R2 — validates unchanged). |
| `ytmatrix/main.py` | One more explicit route, `GET /layout`, alongside the existing `/config` one (Starlette's `StaticFiles(html=True)` only resolves `/` to `index.html`, not arbitrary clean URLs — same reason `/config` needs its own route today). |
| `scripts/build-dist.sh` | Three more `cp` lines: `layout.html` → `dist/layout.html`, `layout-page.js` and `layout-fit.js` → `dist/static/`, `screens.json` → `dist/static/layout/`. |
| `static/config.html` / `static/config.js` | A new "Layout" section in the live editor for the fields below. Existing sections untouched. |
| `static/player.js` | Extract the "how cells get mounted" seam described below. Behavior on `/` must not change. |

## Config schema

```yaml
layout:
  total: 8            # overall video budget across every screen; validated <= 50
  max_per_screen: 3    # ceiling on any one screen's count, explicit or auto
  screens:
    F: auto
    B: auto
    C: auto
    D: auto
    A: auto
    E: auto
```

- Per-screen value is an integer (exact count), `"auto"` (computed from that
  screen's pixel box), or `"none"` (screen shows nothing). A screen id absent
  from `screens` defaults to `"auto"`.
- `total` bounds the sum across every screen combined and is validated
  `<= MAX_SEARCH_RESULTS` (50) — the same ceiling `Grid.cells` already
  enforces for the flat grid, applied here instead of `grid.cells`.
- `max_per_screen` bounds any single screen's count, whether that count came
  from an explicit integer or from auto-allocation. An explicit integer above
  `max_per_screen` is a validation error, not a silent clamp — the point of
  writing `C: 4` is to get 4, and silently capping it to 3 would be a
  confusing way to fail. Default `3`.
- The `layout` section is entirely optional on `Config`; its absence means
  "layout mode has never been configured," and `/layout` falls back to the
  defaults above.
- The sum of explicit per-screen integers must not exceed `total` — also a
  validation error, not a silent clamp, for the same reason as above: it
  would leave step 3 below with a negative remaining budget, which should
  never happen silently.

## Allocation algorithm

Given the resolved config (`total`, `max_per_screen`, `screens`) and the six
screens' known pixel dimensions (from `screens.json`):

1. Screens set to `"none"` get 0 and drop out.
2. Screens with an explicit integer keep that count exactly (already
   validated `<= max_per_screen`). Subtract their sum from `total` to get the
   remaining budget for the `"auto"` screens.
3. Split the remaining budget across the `"auto"` screens proportional to
   each screen's pixel area (`width × height` from `screens.json`), rounding
   down, then hand out any leftover (from rounding) one at a time to the
   largest screens first until the budget is exhausted or every auto screen
   is at `max_per_screen`.
4. Clamp every auto screen's share to `max_per_screen`.
5. Each screen with count `N > 0` picks its own `cols × rows` via
   `layout-fit.js`'s `fitGrid(screenWidth, screenHeight, N)`: the factor pair
   of `N` (allowing a partial last row when `N` isn't a perfect rectangle,
   the same way the existing CSS grid already tolerates `cellCount(grid)`
   cells that don't fill visually-square dimensions) whose resulting cell
   aspect ratio is closest to 16:9 for that screen's box.

With the defaults (`total: 8`, six same-ish-sized screens all `auto`), most
screens get 1 and a couple get 2 — spread across surfaces rather than one
flat 4×2 grid, which is the intent behind the default.

If the resolved total exceeds how many videos are actually available (pool
exhausted), extra cells render empty — the same graceful degradation
`splitSlots` already produces for the flat grid when there aren't enough
reserves. This is not new failure surface, just the same one spread across
more cells.

## Front-end architecture

`static/player.js` currently owns one `#grid` CSS-grid element: `buildCells()`
reads `config.grid.cols/rows`, sets the grid template, and creates
`cellCount(grid)` child divs inside it. Everything downstream of that —
`makePlayer`, `prerollCurrentSet`, mute targeting (`audioTarget`/
`isAudible`), error substitution (`substituteFailedSlot`), the context menu,
zoom/pan (`coverRect`/`rectFor`/`zoomAt`) — operates per-cell by index and
does not care which physical container holds a cell.

The refactor: pull `buildCells()`'s "how many cells, in what DOM container"
decision behind a seam that `layout-page.js` can supply a different
implementation for, while `index.html`'s own code path keeps building exactly
the single `#grid` it does today — same markup, same CSS, same behavior. No
observable change on `/`.

`layout-page.js`:

1. Fetches `static/layout/screens.json` and the shared `/api/config` /
   `/api/videos` (same endpoints `player.js` already uses).
2. Builds one absolute-positioned root sized to the vendored canvas
   dimensions, with one absolute-positioned, `overflow: hidden` container per
   screen at its rect — the same technique `layout-driver.js`'s `buildRoot()`
   uses, reimplemented locally (not imported — different repo, different
   deploy target) — and rescales the whole root on window resize the same
   way.
3. Resolves the allocation (above) against each screen's actual measured
   pixel size, builds each screen's own cols×rows sub-grid of mount divs, and
   concatenates them screen-by-screen (in `screens.json`'s screen order,
   row-major within each screen) into the flat mount list the shared engine
   from `player.js` expects.
4. Everything else — pre-roll, mute button, looping, context menu, zoom/pan —
   comes from the shared engine unmodified.

## Testing

- `static/layout-fit.js` gets node tests for `fitGrid` and the allocation
  function, parallel to `grid-logic.test.mjs`.
- `ytmatrix/config.py`'s new `LayoutConfig` gets pydantic validation tests
  (total > 50 rejected, explicit count > max_per_screen rejected, absent
  section still validates, a rejected PUT leaves the stored config untouched
  — gotcha 8 applies here too).
- `tests/test_layout_smoke.py` (marked `browser`, parallel to
  `test_player_smoke.py`) loads `/layout` and confirms players come up, the
  same class of check that already catches script-ordering/serialization bugs
  invisible to the Python and node suites (gotchas 12, 13, 14).
- The existing default suite, `node --test 'static/*.test.mjs'`, and
  `tests/test_player_smoke.py` must all still pass unmodified against `/` —
  proof the refactor didn't change the deployed page's behavior.
