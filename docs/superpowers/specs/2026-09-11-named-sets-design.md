# Named zoom/pan sets and named video sets — design

## What this is

Two independent features, both driven from `/layout-control`:

1. **Named zoom/pan sets** — an operator captures the current zoom/pan of
   every cell on `/layout` as one named "look," and restores it later.
2. **Named video sets** — an operator captures the current video ids showing
   on `/layout` (visible + reserves) as one named set, and restores it later
   — entirely independent of the live search/query mechanism.

Both are shared, server-side, named, listable, deletable. Both save/restore
the *whole wall* under one name (not a per-cell library of views, not a
per-video save) — one name in, one full snapshot out.

## Why

`/layout`'s zoom/pan (`views` Map in `wall-engine.js`) and its live video set
(`slotState`) are both intentionally ephemeral today: zoom/pan resets on
every rebuild, and the video set is whatever the last search/resync produced.
That's correct for the default flow, but an operator running a show wants to
recall a specific composition — "the wide shot," "the finale videos" — on
demand, without re-doing the zoom/pan by hand or gambling on search turning
up the same clips again.

## Non-goals

- Not a per-cell library of named views (rejected during design: a whole-wall
  snapshot per name is simpler and matches how an operator thinks about a
  "look").
- Not built for `/` (the plain grid) or the config page. Both features live
  entirely on `/layout` + `/layout-control`, following the same division of
  responsibility the control surface already established: `/layout` owns all
  state and fetches; `/layout-control` renders snapshots and sends intents,
  nothing else (gotcha 40).
- Not wired into `POST /api/intent`'s `WALL_WIDE_INTENT_TYPES` allowlist
  (gotcha 42) — no OSC/show-control triggering in v1. The six new intent
  types are `BroadcastChannel`-only.
- Restoring a video set does not re-run motion vetting (`ytmatrix/motion.py`).
  A saved set is trusted as-is; a since-broken video is handled by the
  existing `onError`→reserve-substitution mechanism, the same safety net
  live search already relies on.
- Not solving cell-index stability across a layout-topology change (see
  Known limitations).

## Where this lives

New files:

| File | Responsibility |
|---|---|
| `ytmatrix/sets.py` | Pydantic models for a saved zoom set and a saved video set; load/save/delete/list through the `Store`, mirroring `config.py`'s `load_config`/`save_config` pattern but keyed by name instead of one fixed key. |
| `tests/test_sets.py` | Round-trip, 404-on-missing-name, overwrite-on-save, and validation tests for `ytmatrix/sets.py` and its endpoints. |

Modified:

| File | Change |
|---|---|
| `ytmatrix/server.py` | Six new routes: `GET/PUT/DELETE /api/zoom-sets[/{name}]`, same shape for `video-sets`. Unauthenticated, matching `/api/config`'s local trust model. |
| `static/wall-engine.js` | Six new `applyIntent` cases (`saveZoomSet`, `restoreZoomSet`, `deleteZoomSet`, `saveVideoSet`, `restoreVideoSet`, `deleteVideoSet`); fetches both set lists at startup and after every save/delete; includes both lists in `buildSnapshot()`; extends the persisted wall blob (`wallstate.js`) with a `source` tag so a restored video set survives a WS reconnect (see below). |
| `static/wallstate.js` | `saveWall`/`loadWall` gain a `source: {type: "query"\|"video-set", name?}` field alongside the existing blob. |
| `static/grid-logic.js` | Pure helper(s) to convert `views` Map ↔ zoom-set JSON shape, and to build a video-set payload from `slotState` — kept here so they're node-testable like everything else in this file. |
| `static/layout-control.html` / `.js` | Two new UI blocks (name field + Save/Restore/Delete, a `<select>` of existing names) for zoom sets and video sets, rendered purely from the snapshot's new fields — no independent fetch. |
| `tests/test_layout_control_smoke.py` | New case: save a zoom set and a video set from the control page, confirm both round-trip through restore, in the same two-page `BroadcastChannel` context the existing suite already uses. |

## Data model & storage

Reuses the existing `Store` protocol (`ytmatrix/store.py`) exactly as
`config.yaml` does, under two new key prefixes:

```
zoom-sets/<encoded-name>.json
video-sets/<encoded-name>.json
```

`<encoded-name>` is the operator's free-text name, URL-safe-encoded (spaces
and slashes escaped) so it can appear directly in a key and a route path.
`Store.list_keys(prefix)` (already used by `querylog.py` for `logs/<date>/`)
enumerates saved names for the list endpoints.

Saved shapes:

```json
// zoom-sets/<name>.json
{
  "name": "wide shot",
  "saved_at": "2026-09-11T14:20:00Z",
  "views": { "0": {"zoom": 1.4, "offsetX": 12, "offsetY": -8}, "3": {...} }
}
```

```json
// video-sets/<name>.json
{
  "name": "finale",
  "saved_at": "2026-09-11T14:20:00Z",
  "video_ids": ["abc123", "def456", ...],
  "reserves": ["ghi789", ...]
}
```

Saving under an existing name overwrites it — that plus explicit delete is
the whole lifecycle; no versioning.

## Zoom/pan sets

- **Save** (`saveZoomSet {name}`): a new `grid-logic.js` helper
  (`viewsToZoomSet(views)`) converts the live `views` Map (`wall-engine.js:339`)
  to `{cellIndex: {zoom, offsetX, offsetY}}` → `PUT /api/zoom-sets/<name>`.
- **Restore** (`restoreZoomSet {name}`): `GET /api/zoom-sets/<name>` → for
  every cell index present in the response, `views.set(index, {...})` and
  re-apply that cell's CSS transform through the same internal step
  `applyCellWheel`/`applyCellPan` already use. Cells *not* present in the
  saved set are left untouched — restoring is robust to the live cell count
  being different from when the set was saved (more cells, fewer cells).
- **Delete** (`deleteZoomSet {name}`): `DELETE /api/zoom-sets/<name>`.

## Video sets

- **Save** (`saveVideoSet {name}`): a new `grid-logic.js` helper
  (`slotStateToVideoSet(slotState)`) captures
  `slotState.slots.concat(slotState.reserves)` as `{video_ids, reserves}` →
  `PUT /api/video-sets/<name>`.
- **Restore** (`restoreVideoSet {name}`): `GET /api/video-sets/<name>` →
  feed `{video_ids, reserves}` straight into the same code path
  `applyVideos()` already uses for a live search result, bypassing
  `/api/videos` and search entirely. No re-vetting (see Non-goals).
- **Delete** (`deleteVideoSet {name}`): `DELETE /api/video-sets/<name>`.

### Reconnect stickiness

Approved: a restored video set must survive a WebSocket reconnect, not
silently revert to the live query the way gotcha 28 already treats a stored
query as reconnect-safe.

`wallstate.js`'s persisted wall blob gains a `source` tag:

```
{ type: "query" }                     // today's default — the live query drives resync
{ type: "video-set", name: "finale" } // set by restoreVideoSet
```

`resync()` checks this tag before doing its normal search-driven refetch: if
`source.type === "video-set"`, it re-applies the *persisted* video/reserve
ids directly (no `/api/videos` call, no search) instead of re-deriving from
the stored query. The tag is cleared back to `{type: "query"}` by anything
that already clears/overrides the stored query today — a new search
(`newQuery` intent), or restoring a *different* named set overwrites it in
place. This is the one behavioral change to the existing reconnect path;
everything else in `resync()` is unchanged for the default (query-driven)
case.

## New intents

All six follow the existing intent shape (a plain object over the
`yt-matrix-layout-control` `BroadcastChannel`), and — like every existing
intent — trigger a snapshot publish afterward:

```
{ type: "saveZoomSet",    name: "wide shot" }
{ type: "restoreZoomSet", name: "wide shot" }
{ type: "deleteZoomSet",  name: "wide shot" }
{ type: "saveVideoSet",    name: "finale" }
{ type: "restoreVideoSet", name: "finale" }
{ type: "deleteVideoSet",  name: "finale" }
```

`buildSnapshot()` gains two fields so `/layout-control` can render a list
with no fetch of its own:

```
{ ..., zoomSets: ["wide shot", "closeup"], videoSets: ["finale", "intro"] }
```

## API endpoints

```
GET    /api/zoom-sets            -> ["wide shot", "closeup"]
PUT    /api/zoom-sets/{name}     -> {"status": "ok"}
DELETE /api/zoom-sets/{name}     -> {"status": "ok"}   (404 if missing)

GET    /api/video-sets           -> ["finale", "intro"]
PUT    /api/video-sets/{name}    -> {"status": "ok"}
DELETE /api/video-sets/{name}    -> {"status": "ok"}   (404 if missing)
```

Unauthenticated, same trust model as `/api/config` — this is a local/one-operator
tool, not a multi-tenant surface.

## Known limitations

- **Zoom/pan sets are keyed by flat cell index**, which depends on the
  current layout topology (`layout.total` + per-screen distribution,
  `layout-fit.js`). Restoring a set saved under a different topology can
  apply a saved view to the wrong physical cell. Not fixed here — keying by
  `screenId` + local index would fix it but is real added complexity, out of
  scope unless asked for.
- **Video sets do not re-vet embeddability on restore.** A video that was
  fine when saved can go private/region-locked/deleted later; restore trusts
  the saved ids and relies on the ordinary `onError`→reserve path to recover,
  exactly like a live search already does for its own reserves.

## Testing

- `tests/test_sets.py`: `ytmatrix/sets.py` round-trip (save/get/list/delete),
  404 on missing name, overwrite-on-save, and a rejected/malformed payload
  leaving existing storage untouched (mirroring gotcha 8's config-write
  guarantee).
- `static/grid-logic.js`'s new pure helpers: node tests alongside the
  existing `grid-logic.test.mjs`-style suite.
- `tests/test_layout_control_smoke.py`: one new case driving both features
  end-to-end across the two-page `BroadcastChannel` context — save a zoom
  set and a video set from `/layout-control`, restore both, assert the
  results land on `/layout`. A second case simulates a WS reconnect after a
  video-set restore and asserts the set survives it (the reconnect-stickiness
  behavior).
