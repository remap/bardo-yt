# Layout control surface — design

## What this is

`/layout` is what the NDI broadcaster captures for the physical wall — its
header, cursor and context menu must never appear in that feed. But an
operator still needs every interaction the plain grid page offers: hover to
unmute, scroll to zoom, drag to pan, double-click to lock audio, and the
right-click menu. This adds a second, ordinary browser tab — `/layout-control`
— showing the header controls plus one rectangle per video cell. Interacting
with a rectangle relays the equivalent action to the real cell on `/layout`,
which alone owns the real YouTube players. `/layout`'s own header is hidden
by CSS only; nothing about its behavior changes when no control window is
open.

## Why

The broadcaster captures whatever `/layout` renders, full-frame. Any visible
chrome — buttons, a cursor, a context menu — goes out over NDI onto the real
wall. The fix is not to remove the interactions, it is to move where the
*human* interacts from, while the actual player state stays exactly where it
already lives.

## Non-goals

- Not built for `/` (the plain grid). `wall-engine.js`'s new mechanism is
  generic enough to extend there later, but no second page is built for it
  now, and `/`'s behavior must not change at all.
- Not a network protocol. `BroadcastChannel` only connects tabs in the same
  browser, same profile, same machine — which is already true of how the NDI
  broadcaster works (it captures a local Chrome window). No server involvement,
  no new backend route for this feature.
- Not a video preview. `/layout-control`'s rectangles are plain divs with a
  title label — no second set of YouTube embeds, no synchronization problem
  between two live players for the same video.

## Where this lives

Same branch family: `feat/layout-control-surface`, branched from
`feat/cloudflare-deploy` (which now carries the merged `/layout` work).

New files:

| File | Responsibility |
|---|---|
| `static/layout-control.html` | The control window's markup: the same header controls as `layout.html`/`player.html`, plus a `#grid` of plain rectangles (no iframes). |
| `static/layout-control.js` | Connects to the `BroadcastChannel`, renders rectangles and header state purely from received snapshots (no independent fetch of config or screen geometry — see "Geometry" below), turns mouse/keyboard input into intents, and executes read-only context-menu actions locally. |
| `tests/test_layout_control_smoke.py` | Browser test opening both `/layout` and `/layout-control` in one Playwright browser context (required for `BroadcastChannel` to connect them) and asserting an interaction on one lands on the other. |

Modified:

| File | Change |
|---|---|
| `static/wall-engine.js` | Every local event handler is refactored to build a small "intent" object and hand it to one `applyIntent()` function, instead of mutating state inline. `startWall()` gains an opt-in option that opens a `BroadcastChannel`: incoming messages feed `applyIntent()`, and a state snapshot is published on every meaningful change plus a slow heartbeat. When the option is absent (`/`'s `player.js`), none of this activates. |
| `static/layout-page.js` | Passes the new option to `startWall()`. |
| `static/layout.html` | CSS-only: the `<header>` is visually hidden (`display: none` or off-screen), not removed from the DOM — `wall-engine.js`'s `getElementById` lookups must keep succeeding. |
| `ytmatrix/main.py` | One more explicit route, `GET /layout-control`, identical in shape to the existing `/layout`/`/config` routes. |
| `scripts/build-dist.sh` | One more line shipping `layout-control.html`; `layout-control.js` is already picked up by the existing `static/*.js` glob. |

## The intent/snapshot protocol

Two message types travel over one `BroadcastChannel` (name:
`"yt-matrix-layout-control"`).

### Intents (control window → broadcast window)

Every one of today's local DOM-event effects becomes an intent object.
Coordinates are normalized (0..1, relative to the *sending* rectangle's own
box) so the two windows never need matching pixel sizes — the broadcast
window converts to its own real cell's pixel space using its own
`getBoundingClientRect()` before calling the existing `zoomAt`/`panBy` math
unchanged. Wheel `deltaY`'s *sign* is what `nextZoom` actually uses, so it
travels as-is with no scaling.

```
{ type: "play" }
{ type: "pause" }
{ type: "muteToggle" }
{ type: "rewind" }
{ type: "shuffle" }
{ type: "resetView" }
{ type: "newQuery", prompt: string | null }
{ type: "hoverUnmuteToggle", checked: boolean }
{ type: "followToggle", checked: boolean }
{ type: "cellHoverEnter", index: number }
{ type: "cellHoverLeave" }
{ type: "cellWheel", index: number, deltaY: number, x: number, y: number }
{ type: "cellDragStart", index: number }
{ type: "cellDragMove", index: number, dx: number, dy: number }   // fraction of the sending rect's own width/height
{ type: "cellDragEnd", index: number }
{ type: "cellDblclick", index: number }
{ type: "cellMenuAction", index: number, action: "togglePlay" | "toggleLock" | "restart" | "resetZoom" | "replaceReserve" }
```

`applyIntent()` in `wall-engine.js` is the single place that used to be ten
different event-listener bodies; each listener now just builds the matching
intent object and calls it locally, and the exact same function runs when the
same object arrives from the channel.

### Snapshots (broadcast window → control window)

Published on every change that would have been visible to a local user
(rebuild, mute/lock change, status change, pre-roll completion, etc.) plus a
1-second heartbeat so a late-joining or reconnecting control window is never
more than a second stale.

```
{
  type: "snapshot",
  global: {
    status: string,
    statusState: "" | "busy" | "error",
    audioIndicatorText: string,
    audioLocked: boolean,
    muted: boolean,
    prerolled: boolean,
    wantPlaying: boolean,
    playDisabled: boolean,
    hoverUnmuteChecked: boolean,
    followChecked: boolean,
    newQueryVisible: boolean,
    newQueryDisabled: boolean,
    reservesLeft: number,
  },
  cells: [
    {
      index: number,
      videoId: string | null,
      title: string | null,
      empty: boolean,
      zoom: number,
      locked: boolean,
      audible: boolean,
      playing: boolean,
      currentTime: number,
      rect: { left: number, top: number, width: number, height: number },  // percentages, same numbers layout-page.js's own cellRect(index) already computes
    },
    ...
  ],
}
```

`cells[i].rect` is the load-bearing reason `layout-control.js` never fetches
`static/layout/screens.json` or `/api/config` itself: the broadcast window
has already resolved exact geometry for its own DOM via `computeLayout`, and
handing those same percentages over the channel means both windows are
provably showing the same layout, by construction, rather than by two
independent computations agreeing. Before the first snapshot arrives (or if
none has arrived in over 2 heartbeats), `/layout-control` shows a plain
"waiting for /layout to connect…" status instead of any rectangles.

### Context menu

Right-clicking a rectangle in `/layout-control` opens the same ten items
`wall-engine.js`'s `menuItems()` already offers, built in
`layout-control.js` from the cached snapshot data for that cell:

- **Executed locally, no intent sent** (everything the label only needs
  `videoId`/`title`/`currentTime` for): Copy video URL at time, Copy video
  URL, Copy video ID, Copy title, Open on YouTube at time.
- **Sent as a `cellMenuAction` intent** (everything that mutates real player
  state, which only the broadcast window owns): Play/Pause this cell
  (`togglePlay`), Lock/unlock audio to this cell (`toggleLock`), Restart this
  cell (`restart`), Reset zoom (`resetZoom`), Replace with next reserve
  (`replaceReserve`).

## Testing

- The full existing browser suites (`tests/test_player_smoke.py` for `/`,
  `tests/test_layout_smoke.py` for `/layout` with no control window open)
  must keep passing **unmodified** — proof the `applyIntent` refactor changed
  nothing observable when no `BroadcastChannel` message ever arrives.
- `tests/test_layout_control_smoke.py` opens both pages via
  `context.new_page()` twice in one Playwright `BrowserContext` (same
  browser, same profile — required for the two tabs' `BroadcastChannel`s to
  be the same channel), drives at least one intent from the control page
  (e.g. a wheel event on a rectangle) and asserts the corresponding cell on
  `/layout` actually zoomed, plus one menu-relayed action (e.g. toggling
  play/pause) and confirms it landed on the real player.
